"""
blockfeed: sync with and process new blocks from counterpartyd
"""
import re
import os
import sys
import json
import copy
import logging
import datetime
import decimal
import ConfigParser
import time
import itertools
import pymongo
import gevent

from counterblock.lib import config, util, blockchain, cache, database
from counterblock.lib.processor import MessageProcessor, MempoolMessageProcessor, BlockProcessor, CaughtUpProcessor

D = decimal.Decimal 
logger = logging.getLogger(__name__)

def fuzzy_is_caught_up():
    """We don't want to give users 525 errors or login errors if counterblockd/counterpartyd is in the process of
    getting caught up, but we DO if counterblockd is either clearly out of date with the blockchain, or reinitializing its database"""
    return     config.state['caught_up'] \
           or (    config.state['cp_backend_block_index']
               and config.state['my_latest_block']['block_index'] >= config.state['cp_backend_block_index'] - 1
              )
        
def process_cp_blockfeed():
    config.LATEST_BLOCK_INIT = {'block_index': config.BLOCK_FIRST, 'block_time': None, 'block_hash': None}

    #initialize state
    config.state['cur_block'] = {'block_index': 0, } #block being currently processed
    config.state['my_latest_block'] = {'block_index': 0 } #last block that was successfully processed by counterblockd
    config.state['last_message_index'] = -1 #initialize (last processed message index)
    config.state['cp_latest_block_index'] = 0 #last block that was successfully processed by counterparty
    config.state['cp_backend_block_index'] = 0 #the latest block height as reported by the cpd blockchain backend
    config.state['caught_up_started_events'] = False
    #^ set after we are caught up and start up the recurring events that depend on us being caught up with the blockchain 
    
    #enabled processor functions
    logger.debug("Enabled Message Processor Functions {0}".format(MessageProcessor.active_functions()))
    logger.debug("Enabled Block Processor Functions {0}".format(BlockProcessor.active_functions()))
    
    def publish_mempool_tx():
        """fetch new tx from mempool"""
        tx_hashes = []
        mempool_txs = config.mongo_db.mempool.find(fields={'tx_hash': True})
        for mempool_tx in mempool_txs:
            tx_hashes.append(str(mempool_tx['tx_hash']))
    
        params = None
        if len(tx_hashes) > 0:
            params = {
                'filters': [
                    {'field':'tx_hash', 'op': 'NOT IN', 'value': tx_hashes},
                    {'field':'category', 'op': 'IN', 'value': ['sends', 'btcpays', 'issuances', 'dividends']}
                ],
                'filterop': 'AND'
            }
        new_txs = util.jsonrpc_api("get_mempool", params, abort_on_error=True)
    
        for new_tx in new_txs['result']:
            tx = {
                'tx_hash': new_tx['tx_hash'],
                'command': new_tx['command'],
                'category': new_tx['category'],
                'bindings': new_tx['bindings'],
                'timestamp': new_tx['timestamp'],
                'viewed_in_block': config.state['my_latest_block']['block_index']
            }
            
            config.mongo_db.mempool.insert(tx)
            del(tx['_id'])
            tx['_category'] = tx['category']
            tx['_message_index'] = 'mempool'
            logger.debug("Spotted mempool tx: %s" % tx)
            
            for function in MempoolMessageProcessor.active_functions():
                logger.debug('starting {} (mempool)'.format(function['function']))
                # TODO: Better handling of double parsing
                try:
                    cmd = function['function'](tx, json.loads(tx['bindings'])) or None
                except pymongo.errors.DuplicateKeyError, e:
                    logging.exception(e)
                if cmd == 'continue': break
            
    def clean_mempool_tx():
        """clean mempool transactions older than MAX_REORG_NUM_BLOCKS blocks"""
        config.mongo_db.mempool.remove(
            {"viewed_in_block": {"$lt": config.state['my_latest_block']['block_index'] - config.MAX_REORG_NUM_BLOCKS}})

    def parse_message(msg): 
        msg_data = json.loads(msg['bindings'])
        logger.debug("Received message %s: %s ..." % (msg['message_index'], msg))
        
        #out of order messages should not happen (anymore), but just to be sure
        assert msg['message_index'] == config.state['last_message_index'] + 1 or config.state['last_message_index'] == -1
        
        for function in MessageProcessor.active_functions():
            logger.debug('starting {}'.format(function['function']))
            # TODO: Better handling of double parsing
            try:
                cmd = function['function'](msg, msg_data) or None
            except pymongo.errors.DuplicateKeyError, e:
                logging.exception(e)
            #break or *return* (?) depends on whether we want config.last_message_index to be updated
            if cmd == 'continue': break
            elif cmd == 'break': return 'break' 
            
        config.state['last_message_index'] = msg['message_index']

    def parse_block(block_data):
        config.state['cur_block'] = block_data
        config.state['cur_block']['block_time_obj'] \
            = datetime.datetime.utcfromtimestamp(config.state['cur_block']['block_time'])
        config.state['cur_block']['block_time_str'] = config.state['cur_block']['block_time_obj'].isoformat()
        cmd = None
        
        for msg in config.state['cur_block']['_messages']: 
            cmd = parse_message(msg)
            if cmd == 'break': break
        #logger.debug("*config.state* {}".format(config.state))
        
        #Run Block Processor Functions
        BlockProcessor.run_active_functions()
        #block successfully processed, track this in our DB
        new_block = {
            'block_index': config.state['cur_block']['block_index'],
            'block_time': config.state['cur_block']['block_time_obj'],
            'block_hash': config.state['cur_block']['block_hash'],
        }
        config.mongo_db.processed_blocks.insert(new_block)
        
        config.state['my_latest_block'] = new_block 

        logger.info("Block: %i of %i [message height=%s]" % (
            config.state['my_latest_block']['block_index'],
            config.state['cp_backend_block_index'] \
                if config.state['cp_backend_block_index'] else '???',
            config.state['last_message_index'] if config.state['last_message_index'] != -1 else '???'))

        if config.state['cp_latest_block_index'] - cur_block_index < config.MAX_REORG_NUM_BLOCKS: #only when we are near the tip
            clean_mempool_tx()
        
    #grab our stored preferences, and rebuild the database if necessary
    app_config = config.mongo_db.app_config.find()
    assert app_config.count() in [0, 1]
    if (   app_config.count() == 0
        or config.REPARSE_FORCED
        or app_config[0]['db_version'] != config.DB_VERSION
        or app_config[0]['running_testnet'] != config.TESTNET):
        if app_config.count():
            logger.warn("counterblockd database version UPDATED (from %i to %i) or testnet setting changed (from %s to %s), or REINIT forced (%s). REBUILDING FROM SCRATCH ..." % (
                app_config[0]['db_version'], config.DB_VERSION, app_config[0]['running_testnet'],
                config.TESTNET, config.REPARSE_FORCED))
        else:
            logger.warn("counterblockd database app_config collection doesn't exist. BUILDING FROM SCRATCH...")
        app_config = database.reset_db_state()
        config.state['my_latest_block'] = config.LATEST_BLOCK_INIT
    else:
        app_config = app_config[0]
        #get the last processed block out of mongo
        my_latest_block = config.mongo_db.processed_blocks.find_one(sort=[("block_index", pymongo.DESCENDING)]) or config.LATEST_BLOCK_INIT
        #remove any data we have for blocks higher than this (would happen if counterblockd or mongo died
        # or errored out while processing a block)
        config.state['my_latest_block'] = database.rollback(my_latest_block['block_index'])
    
    #avoid contacting counterpartyd (on reparse, to speed up)
    autopilot = False
    autopilot_runner = 0

    #start polling counterpartyd for new blocks
    while True:
        if not autopilot or autopilot_runner == 0:
            try:
                cp_running_info = util.jsonrpc_api("get_running_info", abort_on_error=True)['result']
            except Exception, e:
                logger.warn("Cannot contact counterpartyd get_running_info: %s" % e)
                time.sleep(3)
                continue
                
        #wipe our state data if necessary, if counterpartyd has moved on to a new DB version
        wipeState = False
        updatePrefs = False
        
        #Checking appconfig against old running info (when batch-fetching) is redundant 
        if    app_config['counterpartyd_db_version_major'] is None \
           or app_config['counterpartyd_db_version_minor'] is None \
           or app_config['counterpartyd_running_testnet'] is None:
            updatePrefs = True
        elif cp_running_info['version_major'] != app_config['counterpartyd_db_version_major']:
            logger.warn(
                "counterpartyd MAJOR DB version change (we built from %s, counterpartyd is at %s). Wiping our state data." % (
                    app_config['counterpartyd_db_version_major'], cp_running_info['version_major']))
            wipeState = True
            updatePrefs = True
        elif cp_running_info['version_minor'] != app_config['counterpartyd_db_version_minor']:
            logger.warn(
                "counterpartyd MINOR DB version change (we built from %s.%s, counterpartyd is at %s.%s). Wiping our state data." % (
                app_config['counterpartyd_db_version_major'], app_config['counterpartyd_db_version_minor'],
                cp_running_info['version_major'], cp_running_info['version_minor']))
            wipeState = True
            updatePrefs = True
        elif cp_running_info.get('running_testnet', False) != app_config['counterpartyd_running_testnet']:
            logger.warn("counterpartyd testnet setting change (from %s to %s). Wiping our state data." % (
                app_config['counterpartyd_running_testnet'], cp_running_info['running_testnet']))
            wipeState = True
            updatePrefs = True
        if wipeState:
            app_config = database.reset_db_state()
        if updatePrefs:
            app_config['counterpartyd_db_version_major'] = cp_running_info['version_major'] 
            app_config['counterpartyd_db_version_minor'] = cp_running_info['version_minor']
            app_config['counterpartyd_running_testnet'] = cp_running_info['running_testnet']
            config.mongo_db.app_config.update({}, app_config)
            #reset my latest block record
            config.state['my_latest_block'] = config.LATEST_BLOCK_INIT
            config.state['caught_up'] = False #You've Come a Long Way, Baby
            
        #work up to what block counterpartyd is at
        config.state['cp_latest_block_index'] = cp_running_info['last_block']['block_index'] \
            if isinstance(cp_running_info['last_block'], dict) else cp_running_info['last_block']
        config.state['cp_backend_block_index'] = cp_running_info['bitcoin_block_count']
        if not config.state['cp_latest_block_index']:
            logger.warn("counterpartyd has no last processed block (probably is reparsing or was just restarted)."
                + " Waiting 3 seconds before trying again...")
            time.sleep(3)
            continue
        assert config.state['cp_latest_block_index']
        if config.state['my_latest_block']['block_index'] < config.state['cp_latest_block_index']:
            #need to catch up
            config.state['caught_up'] = False
            
            #Autopilot and autopilot runner are redundant
            if config.state['cp_latest_block_index'] - config.state['my_latest_block']['block_index'] > 500: #we are safely far from the tip, switch to bulk-everything
                autopilot = True
                if autopilot_runner == 0:
                    autopilot_runner = 500
                autopilot_runner -= 1
            else:
                autopilot = False
                
            cur_block_index = config.state['my_latest_block']['block_index'] + 1
            try:
                block_data = cache.get_block_info(cur_block_index,
                    min(100, (config.state['cp_latest_block_index'] - config.state['my_latest_block']['block_index'])))
            except Exception, e:
                logger.warn(str(e) + " Waiting 3 seconds before trying again...")
                time.sleep(3)
                continue
            
            # clean api cache
            if config.state['cp_latest_block_index'] - cur_block_index <= config.MAX_REORG_NUM_BLOCKS: #only when we are near the tip
                cache.clean_block_cache(cur_block_index)

            parse_block(block_data)

        elif config.state['my_latest_block']['block_index'] > config.state['cp_latest_block_index']:
            # should get a reorg message. Just to be on the safe side, prune back MAX_REORG_NUM_BLOCKS blocks
            # before what counterpartyd is saying if we see this
            logger.error("Very odd: Ahead of counterpartyd with block indexes! Pruning back %s blocks to be safe."
                % config.MAX_REORG_NUM_BLOCKS)
            config.state['my_latest_block'] = database.rollback(
                config.state['cp_latest_block_index'] - config.MAX_REORG_NUM_BLOCKS)
        else:
            #...we may be caught up (to counterpartyd), but counterpartyd may not be (to the blockchain). And if it isn't, we aren't
            config.state['caught_up'] = cp_running_info['db_caught_up']
            
            #this logic here will cover a case where we shut down counterblockd, then start it up again quickly...
            # in that case, there are no new blocks for it to parse, so config.state['last_message_index'] would otherwise remain 0.
            # With this logic, we will correctly initialize config.state['last_message_index'] to the last message ID of the last processed block
            if config.state['last_message_index'] == -1 or config.state['my_latest_block']['block_index'] == 0:
                if config.state['last_message_index'] == -1:
                    config.state['last_message_index'] = cp_running_info['last_message_index']
                if config.state['my_latest_block']['block_index'] == 0:
                    config.state['my_latest_block']['block_index'] = cp_running_info['last_block']['block_index']
                logger.info("Detected blocks caught up on startup. Setting last message idx to %s, current block index to %s ..." % (
                    config.state['last_message_index'], config.state['my_latest_block']['block_index']))
            
            if config.state['caught_up'] and not config.state['caught_up_started_events']:
                #start up recurring events that depend on us being fully caught up with the blockchain to run
                CaughtUpProcessor.run_active_functions()
                
                config.state['caught_up_started_events'] = True

            blockchain.update_unconfirmed_addrindex()
            publish_mempool_tx()
            time.sleep(2) #counterblockd itself is at least caught up, wait a bit to query again for the latest block from cpd
