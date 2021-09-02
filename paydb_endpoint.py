# pylint: disable=unbalanced-tuple-unpacking

import logging
import time
import datetime
import json
import decimal

from flask import Blueprint, request, jsonify, flash, redirect, render_template
import flask_security
from flask_security.utils import encrypt_password, verify_password
from flask_security.recoverable import send_reset_password_instructions
from flask_socketio import Namespace, emit, join_room, leave_room

import web_utils
from web_utils import bad_request, get_json_params, check_auth, auth_request, auth_request_get_single_param, auth_request_get_params
import utils
from app_core import db, socketio, limiter
from models import user_datastore, User, UserCreateRequest, UserUpdateEmailRequest, Permission, ApiKey, ApiKeyRequest, BrokerOrder, KycRequest
import payments_core
import dasset

logger = logging.getLogger(__name__)
paydb = Blueprint('paydb', __name__, template_folder='templates')
limiter.limit('100/minute')(paydb)
ws_sids = {}

#
# Websocket events
#

NS = '/paydb'

def tx_event(txn):
    txt = json.dumps(txn.to_json())
    socketio.emit("tx", txt, json=True, room=txn.sender.email, namespace=NS)
    if txn.recipient and txn.recipient != txn.sender:
        socketio.emit("tx", txt, json=True, room=txn.recipient.email, namespace=NS)

class PayDbNamespace(Namespace):

    def on_error(self, err):
        logger.error(err)

    def on_connect(self):
        logger.info("connect sid: %s", request.sid)

    def on_auth(self, auth):
        if not isinstance(auth, dict):
            try:
                auth = json.loads(auth)
            except: # pylint: disable=bare-except
                emit("info", "invalid json", namespace=NS)
                return
        if "api_key" not in auth:
            emit("info", "'api_key' param missing", namespace=NS)
            return
        if "nonce" not in auth:
            emit("info", "'nonce' param missing", namespace=NS)
            return
        if "signature" not in auth:
            emit("info", "'signature' param missing", namespace=NS)
            return
        # check auth
        res, reason, api_key = check_auth(db.session, auth["api_key"], auth["nonce"], auth["signature"], str(auth["nonce"]))
        if res:
            emit("info", "authenticated!", namespace=NS)
            # join room and store user
            logger.info("join room for email: %s", api_key.user.email)
            join_room(api_key.user.email)
            # store sid -> email map
            ws_sids[request.sid] = api_key.user.email
        else:
            api_key = auth["api_key"]
            emit("info", f"failed authentication ({api_key}): {reason}", namespace=NS)
            logger.info("failed authentication (%s): %s", api_key, reason)

    def on_disconnect(self):
        logger.info("disconnect sid: %s", request.sid)
        if request.sid in ws_sids:
            # remove sid -> email map
            email = ws_sids[request.sid]
            logger.info("leave room for email: %s", email)
            leave_room(email)
            del ws_sids[request.sid]

socketio.on_namespace(PayDbNamespace(NS))

#
# Private (paydb) API
#

@paydb.route('/user_register', methods=['POST'])
@limiter.limit('10/hour')
def user_register():
    content = request.get_json(force=True)
    if content is None:
        return bad_request(web_utils.INVALID_JSON)
    params, err_response = get_json_params(content, ["email", "password", "first_name", "last_name", "mobile_number", "address", "photo", "photo_type"])
    if err_response:
        return err_response
    email, password, first_name, last_name, mobile_number, address, photo, photo_type = params
    if not utils.is_email(email):
        return bad_request(web_utils.INVALID_EMAIL)
    email = email.lower()
    if not password:
        return bad_request(web_utils.EMPTY_PASSWORD)
    if photo and len(photo) > 50000:
        return bad_request(web_utils.PHOTO_DATA_LARGE)
    req = UserCreateRequest(first_name, last_name, email, mobile_number, address, photo, photo_type, encrypt_password(password))
    user = User.from_email(db.session, email)
    if user:
        time.sleep(5)
        return bad_request(web_utils.USER_EXISTS)
    utils.email_user_create_request(logger, req, req.MINUTES_EXPIRY)
    db.session.add(req)
    db.session.commit()
    return 'ok'

@paydb.route('/user_registration_confirm/<token>', methods=['GET'])
@limiter.limit('20/minute')
def user_registration_confirm(token=None):
    req = UserCreateRequest.from_token(db.session, token)
    if not req:
        flash('User registration request not found.', 'danger')
        return redirect('/')
    user = User.from_email(db.session, req.email)
    if user:
        flash('User already exists.', 'danger')
        return redirect('/')
    now = datetime.datetime.now()
    if now > req.expiry:
        flash('User registration expired.', 'danger')
        return redirect('/')
    user = user_datastore.create_user(email=req.email, password=req.password, first_name=req.first_name, last_name=req.last_name)
    user.mobile_number = req.mobile_number
    user.address = req.address
    user.photo = req.photo
    user.photo_type = req.photo_type
    user.confirmed_at = now
    db.session.delete(req)
    db.session.commit()
    flash('User registered.', 'success')
    return redirect('/')

@paydb.route('/api_key_create', methods=['POST'])
@limiter.limit('10/hour')
def api_key_create():
    content = request.get_json(force=True)
    if content is None:
        return bad_request(web_utils.INVALID_JSON)
    params, err_response = get_json_params(content, ["email", "password", "device_name"])
    if err_response:
        return err_response
    email, password, device_name = params
    if not email:
        return bad_request(web_utils.INVALID_EMAIL)
    email = email.lower()
    user = User.from_email(db.session, email)
    if not user:
        time.sleep(5)
        return bad_request(web_utils.AUTH_FAILED)
    if not flask_security.verify_password(password, user.password):
        time.sleep(5)
        return bad_request(web_utils.AUTH_FAILED)
    api_key = ApiKey(user, device_name)
    for name in Permission.PERMS_ALL:
        perm = Permission.from_name(db.session, name)
        api_key.permissions.append(perm)
    db.session.add(api_key)
    db.session.commit()
    return jsonify(dict(token=api_key.token, secret=api_key.secret, device_name=api_key.device_name, expiry=api_key.expiry))

@paydb.route('/api_key_request', methods=['POST'])
@limiter.limit('10/hour')
def api_key_request():
    content = request.get_json(force=True)
    if content is None:
        return bad_request(web_utils.INVALID_JSON)
    params, err_response = get_json_params(content, ["email", "device_name"])
    if err_response:
        return err_response
    email, device_name = params
    if not email:
        return bad_request(web_utils.INVALID_EMAIL)
    email = email.lower()
    user = User.from_email(db.session, email)
    if not user:
        req = ApiKeyRequest(user, device_name)
        return jsonify(dict(token=req.token))
    req = ApiKeyRequest(user, device_name)
    utils.email_api_key_request(logger, req, req.MINUTES_EXPIRY)
    db.session.add(req)
    db.session.commit()
    return jsonify(dict(token=req.token))

@paydb.route('/api_key_claim', methods=['POST'])
@limiter.limit('20/minute')
def api_key_claim():
    content = request.get_json(force=True)
    if content is None:
        return bad_request(web_utils.INVALID_JSON)
    params, err_response = get_json_params(content, ["token"])
    if err_response:
        return err_response
    token, = params
    req = ApiKeyRequest.from_token(db.session, token)
    if not token:
        time.sleep(5)
        return bad_request(web_utils.NOT_FOUND)
    req = ApiKeyRequest.from_token(db.session, token)
    if not req.created_api_key:
        time.sleep(5)
        return bad_request(web_utils.NOT_CREATED)
    api_key = req.created_api_key
    db.session.delete(req)
    db.session.commit()
    return jsonify(dict(token=api_key.token, secret=api_key.secret, device_name=api_key.device_name, expiry=api_key.expiry))

@paydb.route('/api_key_confirm/<token>/<secret>', methods=['GET', 'POST'])
@limiter.limit('20/minute')
def api_key_confirm(token=None, secret=None):
    req = ApiKeyRequest.from_token(db.session, token)
    if not req:
        time.sleep(5)
        flash('Email login request not found.', 'danger')
        return redirect('/')
    if req.secret != secret:
        flash('Email login code invalid.', 'danger')
        return redirect('/')
    now = datetime.datetime.now()
    if now > req.expiry:
        time.sleep(5)
        flash('Email login request expired.', 'danger')
        return redirect('/')
    if request.method == 'POST':
        confirm = request.form.get('confirm') == 'true'
        if not confirm:
            db.session.delete(req)
            db.session.commit()
            flash('Email login cancelled.', 'success')
            return redirect('/')
        perms = request.form.getlist('perms')
        api_key = ApiKey(req.user, req.device_name)
        for name in perms:
            perm = Permission.from_name(db.session, name)
            api_key.permissions.append(perm)
        req.created_api_key = api_key
        db.session.add(req)
        db.session.add(api_key)
        db.session.commit()
        flash('Email login confirmed.', 'success')
        return redirect('/')
    return render_template('paydb/api_key_confirm.html', req=req, perms=Permission.PERMS_ALL)

@paydb.route('/user_info', methods=['POST'])
def user_info():
    email, api_key, err_response = auth_request_get_single_param(db, "email")
    if err_response:
        return err_response
    if not email:
        email = api_key.user.email
    else:
        email = email.lower()
    user = User.from_email(db.session, email)
    if not user:
        time.sleep(5)
        return bad_request(web_utils.AUTH_FAILED)
    if user == api_key.user:
        roles = [role.name for role in api_key.user.roles]
        perms = [perm.name for perm in api_key.permissions]
        kyc_validated = api_key.user.kyc_validated()
        kyc_url = api_key.user.kyc_url()
        return jsonify(dict(email=user.email, photo=user.photo, photo_type=user.photo_type, roles=roles, permissions=perms, kyc_validated=kyc_validated, kyc_url=kyc_url))
    return jsonify(dict(email=user.email, photo=user.photo, photo_type=user.photo_type, roles=[], permissions=[], kyc_validated=None, kyc_url=None))

@paydb.route('/user_reset_password', methods=['POST'])
@limiter.limit('10/hour')
def user_reset_password():
    api_key, err_response = auth_request(db)
    if err_response:
        return err_response
    user = api_key.user
    send_reset_password_instructions(user)
    return 'ok'

@paydb.route('/user_update_email', methods=['POST'])
@limiter.limit('10/hour')
def user_update_email():
    email, api_key, err_response = auth_request_get_single_param(db, "email")
    if err_response:
        return err_response
    if not email:
        return bad_request(web_utils.INVALID_EMAIL)
    email = email.lower()
    user = User.from_email(db.session, email)
    if user:
        time.sleep(5)
        return bad_request(web_utils.USER_EXISTS)
    req = UserUpdateEmailRequest(api_key.user, email)
    utils.email_user_update_email_request(logger, req, req.MINUTES_EXPIRY)
    db.session.add(req)
    db.session.commit()
    return 'ok'

@paydb.route('/user_update_email_confirm/<token>', methods=['GET'])
@limiter.limit('10/hour')
def user_update_email_confirm(token=None):
    req = UserUpdateEmailRequest.from_token(db.session, token)
    if not req:
        flash('User update email request not found.', 'danger')
        return redirect('/')
    now = datetime.datetime.now()
    if now > req.expiry:
        flash('User update email request expired.', 'danger')
        return redirect('/')
    user = User.from_email(db.session, req.email)
    if user:
        time.sleep(5)
        return bad_request(web_utils.USER_EXISTS)
    user = req.user
    user.email = req.email
    db.session.add(user)
    db.session.delete(req)
    db.session.commit()
    flash('User email updated.', 'success')
    return redirect('/')

@paydb.route('/user_update_password', methods=['POST'])
@limiter.limit('10/hour')
def user_update_password():
    params, api_key, err_response = auth_request_get_params(db, ["current_password", "new_password"])
    if err_response:
        return err_response
    current_password, new_password = params
    user = api_key.user
    verified_password = verify_password(current_password, user.password)
    if not verified_password:
        return bad_request(web_utils.INCORRECT_PASSWORD)
    ### set the new_password:
    user.password = encrypt_password(new_password)
    db.session.add(user)
    db.session.commit()
    return 'password changed.'

@paydb.route('/user_kyc_request_create', methods=['POST'])
@limiter.limit('10/hour')
def user_kyc_request_create():
    api_key, err_response = auth_request(db)
    if err_response:
        return err_response
    if list(api_key.user.kyc_requests):
        return bad_request(web_utils.KYC_REQUEST_EXISTS)
    user = api_key.user
    req = KycRequest(user)
    db.session.add(req)
    db.session.commit()
    return jsonify(dict(kyc_url=req.url()))

@paydb.route('/user_update_photo', methods=['POST'])
@limiter.limit('10/hour')
def user_update_photo():
    params, api_key, err_response = auth_request_get_params(db, ["photo", "photo_type"])
    if err_response:
        return err_response
    photo, photo_type = params
    user = api_key.user
    user.photo = photo
    user.photo_type = photo_type
    db.session.add(user)
    db.session.commit()
    return jsonify(dict(photo=user.photo, photo_type=user.photo_type))

@paydb.route('/assets', methods=['POST'])
def assets_req():
    _, err_response = auth_request(db)
    if err_response:
        return err_response
    return jsonify(assets=dasset.assets_req())

@paydb.route('/markets', methods=['POST'])
def markets_req():
    _, err_response = auth_request(db)
    if err_response:
        return err_response
    return jsonify(markets=dasset.markets_req())

@paydb.route('/order_book', methods=['POST'])
def order_book_req():
    symbol, _, err_response = auth_request_get_single_param(db, 'symbol')
    if err_response:
        return err_response
    if symbol not in dasset.MARKETS:
        return bad_request(web_utils.INVALID_MARKET)
    order_book, min_order, broker_fee = dasset.order_book_req(symbol)
    return jsonify(order_book=order_book, min_order=min_order, broker_fee=broker_fee)

@paydb.route('/broker_order_create', methods=['POST'])
def broker_order_create():
    params, api_key, err_response = auth_request_get_params(db, ["market", "side", "amount_dec", "recipient"])
    if err_response:
        return err_response
    market, side, amount_dec, recipient = params
    if not api_key.user.kyc_validated():
        return bad_request(web_utils.KYC_NOT_VALIDATED)
    if market not in dasset.MARKETS:
        return bad_request(web_utils.INVALID_MARKET)
    if side != dasset.MarketSide.BID.value:
        return bad_request(web_utils.INVALID_SIDE)
    side = dasset.MarketSide.BID
    amount_dec = decimal.Decimal(amount_dec)
    quote_amount_dec, err = dasset.bid_quote_amount(market, amount_dec)
    if err == dasset.QuoteResult.INSUFFICIENT_LIQUIDITY:
        return bad_request(web_utils.INSUFFICIENT_LIQUIDITY)
    if err == dasset.QuoteResult.AMOUNT_TOO_LOW:
        return bad_request(web_utils.AMOUNT_TOO_LOW)
    if not dasset.address_validate(market, side, recipient):
        return bad_request(web_utils.INVALID_RECIPIENT)
    base_asset, quote_asset = dasset.assets_from_market(market)
    amount = dasset.asset_dec_to_int(base_asset, amount_dec)
    quote_amount = dasset.asset_dec_to_int(quote_asset, quote_amount_dec)
    broker_order = BrokerOrder(api_key.user, market, amount, quote_amount, recipient)
    db.session.add(broker_order)
    db.session.commit()
    return jsonify(broker_order=broker_order.to_json())

@paydb.route('/broker_order_status', methods=['POST'])
def broker_order_status():
    token, api_key, err_response = auth_request_get_single_param(db, 'token')
    if err_response:
        return err_response
    broker_order = BrokerOrder.from_token(db.session, token)
    if not broker_order or broker_order.user != api_key.user:
        return bad_request(web_utils.NOT_FOUND)
    return jsonify(broker_order=broker_order.to_json())

@paydb.route('/broker_order_accept', methods=['POST'])
def broker_order_accept():
    token, api_key, err_response = auth_request_get_single_param(db, 'token')
    if err_response:
        return err_response
    broker_order = BrokerOrder.from_token(db.session, token)
    if not broker_order or broker_order.user != api_key.user:
        return bad_request(web_utils.NOT_FOUND)
    now = datetime.datetime.now()
    if now > broker_order.expiry:
        return bad_request(web_utils.EXPIRED)
    if broker_order.status != broker_order.STATUS_CREATED:
        return bad_request(web_utils.INVALID_STATUS)
    req = payments_core.payment_create(broker_order.quote_amount, broker_order.expiry)
    if not req:
        return bad_request(web_utils.FAILED_PAYMENT_CREATE)
    broker_order.windcave_payment_request = req
    broker_order.status = broker_order.STATUS_READY
    db.session.add(req)
    db.session.add(broker_order)
    db.session.commit()
    return jsonify(broker_order=broker_order.to_json())

@paydb.route('/broker_orders', methods=['POST'])
def broker_orders():
    params, api_key, err_response = auth_request_get_params(db, ["offset", "limit"])
    if err_response:
        return err_response
    offset, limit = params
    if limit > 1000:
        return bad_request(web_utils.LIMIT_TOO_LARGE)
    orders = BrokerOrder.from_user(db.session, api_key.user, offset, limit)
    orders = [order.to_json() for order in orders]
    return jsonify(dict(broker_orders=orders))
