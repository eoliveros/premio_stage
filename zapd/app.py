#!/usr/bin/python3

import logging
import signal

import gevent
import gevent.pool
import base58
import pywaves

import config
import rpc
import utx
import utils

cfg = config.read_cfg()
logger = logging.getLogger(__name__)

# set pywaves to offline mode
pywaves.setOffline()
if cfg.testnet:
    pywaves.setChain("testnet")

def setup_logging(level):
    # setup logging
    logger.setLevel(level)
    rpc.logger.setLevel(level)
    utx.logger.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter('[%(name)s %(levelname)s] %(message)s'))
    logger.addHandler(ch)
    rpc.logger.addHandler(ch)
    utx.logger.addHandler(ch)
    # clear loggers set by any imported modules
    logging.getLogger().handlers.clear()


def on_transfer_utx(wutx, txid, sig, pubkey, asset_id, timestamp, amount, fee, recipient, attachment):
    recipient = base58.b58encode(recipient)
    logger.info(f"!transfer!: txid {txid}, recipient {recipient}, amount {amount}, attachment {attachment}")
    if recipient == cfg.address:
        # create message
        from_ = utils.address_from_public_key(pubkey)
        invoice_id = utils.extract_invoice_id(logger, attachment)
        msg, sig = utils.create_signed_payment_notification(txid, timestamp, recipient, from_, amount, invoice_id)
        utils.call_webhook(logger, msg, sig)

def sigint_handler(signum, frame):
    global keep_running
    logger.warning("SIGINT caught, attempting to exit gracefully")
    keep_running = False

keep_running = True
if __name__ == "__main__":
    setup_logging(logging.DEBUG)
    signal.signal(signal.SIGINT, sigint_handler)

    logger.info("starting greenlets")
    group = gevent.pool.Group()
    zaprpc = rpc.ZapRPC()
    zaprpc.start(group)
    wutx = utx.WavesUTX(None, on_transfer_utx)
    wutx.start(group)
    logger.info("main loop")
    sent_start_email = False
    while keep_running:
        gevent.sleep(1)
        # check if any essential greenlets are dead
        if len(group) < 3:
            msg = "one of our greenlets is dead X("
            logger.error(msg)
            utils.email_death(logger, msg)
            break
        # send start email when all essential greenlets are started
        if not sent_start_email:
            send_start_email = True
            for g in group:
                if not g.started:
                    send_start_email = False
            if send_start_email:
                sent_start_email = True
                msg = "our greenlets have started :)"
                logger.info(msg)
                utils.email_alive(logger, msg)
    logger.info("stopping greenlets")
    wutx.stop()
    zaprpc.stop()
