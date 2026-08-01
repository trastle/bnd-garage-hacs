"""
Standalone client for the Smart Door Devices (SDD) WAN cloud API.

Covers the full client lifecycle:
* pairing a new client with a join code (remote_register()).
* upgrading that pairing into a full session credential set (v3migrate_prepare()/v3migrate_attempt()).
* authenticating (authenticate()).
* day-to-day operation (get_devices(), send_device_command(), get_device_logs()).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import string
import time
import uuid
from pathlib import Path

import requests
from Crypto.Cipher import AES
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Util.asn1 import DerSequence
from Crypto.Util.Padding import pad, unpad
from cryptography.hazmat.primitives.asymmetric import ec as ec_crypto

BASE_URL = "https://version3.smartdoordevices.com/"
SDK_VERSION = "2.21.1"
USER_AGENT = f"sddAndroid-{SDK_VERSION}-python-client(35)"

# --------------------------------------------------------------------------
# Endpoints this client uses:
#
#   app/remoteregister - pairs a new client using a join code from the app.
#   app/v3migrate       - one-time upgrade of that pairing into a full v3
#                         session credential set (see v3migrate_prepare()/
#                         v3migrate_attempt()).
#   appv3/message       - the one RPC endpoint: auth, getDevices,
#                         sendDeviceCommand, everything, all via an
#                         encrypted path/data envelope (see
#                         _signed_message_body()).
#   appv3/poll          - long-poll channel real RPC responses and
#                         unsolicited device events are delivered through
#                         (see poll()).
#
# app/connect is never used by this client - it's a session-establishment
# endpoint that only applies to accounts still on the legacy credential
# format; a client that's been through app/v3migrate never needs it.
# --------------------------------------------------------------------------
APPV3_MESSAGE_PATH = "appv3/message"

REQUEST_TIMEOUT_SECONDS = 15

# The server presents a certificate chain signed by its own private CA, so
# requests need a trusted copy of that CA to validate it - the public CA
# trust store alone rejects it with "self-signed certificate in certificate
# chain".
#
# We trust the SDD ROOT CA here, deliberately: the server's own TLS
# handshake sends its full chain (leaf -> intermediate -> root) on every
# connection, so trusting the root lets a future intermediate rotation (the
# current one is already named "V2", implying a "V1" existed before it)
# validate automatically with no update needed here, rather than breaking
# the way this integration's setup flow broke before this file existed at
# all. Trusting the intermediate directly, like the app does, would not
# survive that.
#
# Provenance - IMPORTANT, read before replacing this file: this root
# certificate was obtained via a direct live TLS connection to
# version3.smartdoordevices.com from the public internet (`openssl s_client
# -connect version3.smartdoordevices.com:443 -showcerts`), reading whatever
# chain the server happened to present at that moment.
#
# Its self-signature was checked (`openssl verify -CAfile <this file> <this file>` -> OK)
# and its issuer name matches what the app-extracted intermediate independently reported as
# ITS issuer, which is decent corroboration.
#
# Kept alongside this file (not a sibling "reference/" dir like the source
# copy) because HACS only ever pulls custom_components/bnd_smart_hub/ itself -
# anything this integration needs at runtime has to live inside that directory.
CA_BUNDLE_PATH = Path(__file__).parent / "sdd-root-ca-public.pem"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
    "version": SDK_VERSION,
    "app-version": SDK_VERSION,  # a second, separate header the server also expects
}


class SddError(Exception):
    """Raised for any non-2xx response from the SDD cloud API."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _post(path: str, body: dict) -> dict:
    """POST body as JSON to path, raising SddError on any request failure or
    non-2xx response. Returns the parsed JSON response body (or {} if empty).
    """
    url = BASE_URL + path
    verify = str(CA_BUNDLE_PATH) if CA_BUNDLE_PATH.exists() else True
    try:
        resp = requests.post(
            url, json=body, headers=_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS, verify=verify,
        )
    except requests.RequestException as e:
        raise SddError(f"Request to {path} failed: {e}") from e
    if not resp.ok:
        raise SddError(f"HTTP {resp.status_code} from {path}", status=resp.status_code, body=resp.text)
    return resp.json() if resp.text else {}


# --------------------------------------------------------------------------
# Pairing / session establishment
# --------------------------------------------------------------------------

def remote_register(
    registration_code: str,
    user_password: str,
    phone_name: str = "HomeAssistant",
    phone_model: str = "HomeAssistant",
) -> dict:
    """Pair a new client using a join/registration code shown in the app.

    One-time step per client. Returns a dict with (at least) bsid, phoneId,
    phoneSecret, phonePassword, userId, userName, isAdmin - save these, the
    phoneSecret/phonePassword are shown only once, same as the LAN API's
    clientKey.
    """
    body = {
        "remoteRegistrationCode": registration_code,
        "userPassword": user_password,
        "phoneName": phone_name,
        "phoneModel": phone_model,
    }
    return _post("app/remoteregister", body)


# --------------------------------------------------------------------------
# app/v3migrate - the one-time bootstrap that turns the legacy (no phoneKey)
# credential from remote_register() into a full v3 credential set (phoneKey,
# hubKey, phoneSecret, phonePassword). Call v3migrate_prepare() once per
# migration, then retry v3migrate_attempt() on that same session until it
# returns a completed result instead of a pending ack - see
# v3migrate_prepare()'s docstring for why reusing the same session across
# retries matters.
# --------------------------------------------------------------------------

_RANDOM_PASSWORD_CHARSET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _random_password(length: int) -> str:
    """Generate a random alphanumeric password (no symbols) of the given length."""
    return "".join(secrets.choice(_RANDOM_PASSWORD_CHARSET) for _ in range(length))


def _legacy_aes_key_iv(key_str: str, iv_str: str) -> tuple[bytes, bytes]:
    """Derive an AES-128 key/IV pair via MD5. Used only for app/v3migrate's
    `data` field, which uses this older, weaker scheme - everywhere else
    uses the SHA-256/AES-256 scheme in aes_encrypt()/aes_decrypt() below.
    """
    key = hashlib.md5(key_str.encode("utf-8")).digest()  # 16 bytes -> AES-128
    iv = hashlib.md5(iv_str.encode("utf-8")).digest()  # 16 bytes
    return key, iv


def _legacy_aes_encrypt(key_str: str, iv_str: str, plaintext: str) -> str:
    key, iv = _legacy_aes_key_iv(key_str, iv_str)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    return base64.b64encode(ct).decode("ascii")


def _legacy_aes_decrypt(key_str: str, iv_str: str, b64_ciphertext: str) -> str:
    key, iv = _legacy_aes_key_iv(key_str, iv_str)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pt = unpad(cipher.decrypt(base64.b64decode(b64_ciphertext)), 16)
    return pt.decode("utf-8")


def _rsa_public_key_pkcs1_b64(rsa_key) -> str:
    """Encode an RSA public key the way app/v3migrate's outer `phoneKey`
    field (and the public key embedded in its encrypted `data` blob) expect:
    bare PKCS#1 RSAPublicKey DER (SEQUENCE{modulus, publicExponent}), not the
    standard 294-byte X.509 SubjectPublicKeyInfo wrapper.
    """
    der = DerSequence([rsa_key.n, rsa_key.e]).encode()
    return base64.b64encode(der).decode("ascii")


def _ec_public_key_uncompressed_b64(public_key) -> str:
    """Encode an EC public key as a raw uncompressed point
    (0x04 || X(32) || Y(32), 65 bytes total) - not the X.509 SPKI wrapper
    v3migrate's EC half otherwise expects.
    """
    numbers = public_key.public_numbers()
    raw = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return base64.b64encode(raw).decode("ascii")


def _ec_public_key_from_uncompressed_b64(b64_point: str):
    raw = base64.b64decode(b64_point)
    return ec_crypto.EllipticCurvePublicKey.from_encoded_point(ec_crypto.SECP256R1(), raw)


def v3migrate_prepare(
    bsid: str,
    phone_id: str,
    legacy_phone_secret: str,
    legacy_phone_password: str,
    user_password: str,
) -> dict:
    """Generate the one-time keypairs/values for a v3migrate bootstrap.

    Do this ONCE per migration and reuse the returned session dict across
    every retry via v3migrate_attempt(). The server tracks a migration
    attempt by the phoneKey/EC-half/newPhonePassword it was first given, so
    generating fresh keys on every retry makes it treat each retry as an
    unrelated attempt that never gets to complete - it just stays "pending"
    forever. Resending the same session's keys on every retry (only the
    time/encrypted-data/signature change between attempts) is what lets a
    migration actually finish.
    """
    return {
        "bsid": bsid,
        "phone_id": phone_id,
        "legacy_phone_secret": legacy_phone_secret,
        "legacy_phone_password": legacy_phone_password,
        "user_password": user_password,
        "rsa_key": RSA.generate(2048),
        "ec_private_key": ec_crypto.generate_private_key(ec_crypto.SECP256R1()),
        "new_phone_password": _random_password(43),
    }


def v3migrate_attempt(session: dict) -> dict:
    """Send ONE app/v3migrate attempt using a session from v3migrate_prepare(),
    reusing its already-generated keys. Meant to be called repeatedly on a
    short interval (e.g. once a second) against the same session until it
    stops returning a pending result.

    Returns a dict with the new phoneKey (RSA private key, PKCS#8 DER
    base64), hubKey (as given by the server), phoneSecret (new, ECDH-derived,
    base64), and phonePassword (new, randomly generated) - the full v3
    credential set, ready to use with rpc_call()/authenticate()/etc. If the
    response doesn't carry a finished migrationData blob yet, returns
    {"pending": True, "ack": <raw response body>} instead - this can mean the
    server just hasn't finished processing (call again after a short wait,
    reusing the SAME session), but it can also mean a real error (e.g. wrong
    account password) that never produces a "migrationData" key either -
    check the "ack" body rather than retrying blindly forever.
    """
    bsid = session["bsid"]
    phone_id = session["phone_id"]
    legacy_phone_secret = session["legacy_phone_secret"]
    rsa_key = session["rsa_key"]
    ec_private_key = session["ec_private_key"]
    new_phone_password = session["new_phone_password"]

    rsa_pub_b64 = _rsa_public_key_pkcs1_b64(rsa_key.publickey())
    ec_pub_b64 = _ec_public_key_uncompressed_b64(ec_private_key.public_key())

    # the encrypted inner blob v3migrate expects for its "data" field
    inner = json.dumps({
        "phoneKey": rsa_pub_b64,
        "newPhoneSecretPhoneHalf": ec_pub_b64,
        "newPhonePassword": new_phone_password,
    })
    # Any value works here as long as it's consistent with what's reported in
    # the "time" field below, since it's purely a local IV-derivation seed,
    # never validated by the server against wall-clock time. Recomputed
    # fresh on every attempt, unlike the keys above.
    time_val = secrets.randbelow(100_000) + 1
    encrypted_data = _legacy_aes_encrypt(legacy_phone_secret, str(time_val), inner)

    # signature is over the ENCRYPTED data string, using the freshly
    # generated RSA private key - not the usual hubId:phoneId:... signing
    # string rpc_call() uses, since this predates having a phoneKey at all.
    rsa_private_pkcs8_b64 = base64.b64encode(rsa_key.export_key(format="DER", pkcs=8)).decode("ascii")
    signature = rsa_sign(rsa_private_pkcs8_b64, encrypted_data)

    body = {
        "bsid": bsid,
        "phoneId": phone_id,
        "phoneKey": rsa_pub_b64,
        "phonePassword": session["legacy_phone_password"],
        "userPassword": session["user_password"],
        "data": encrypted_data,
        "time": time_val,
        "signature": signature,
    }
    response = _post("app/v3migrate", body)
    if "migrationData" not in response:
        # NOT necessarily a real "still processing" ack - the server can also
        # respond this way for a genuine error (e.g. wrong account password).
        # Surface the raw body so a caller retrying on "pending" can tell the
        # difference instead of looping blindly on a real failure.
        return {"pending": True, "ack": response}

    # The response's data blob uses phoneId (the string itself, not a
    # number) as the IV-derivation seed, not the request's numeric "time".
    decrypted = _legacy_aes_decrypt(legacy_phone_secret, phone_id, response["migrationData"])
    migration_data = json.loads(decrypted)
    hub_public_key = _ec_public_key_from_uncompressed_b64(migration_data["newPhoneSecretHubHalf"])
    shared_secret = ec_private_key.exchange(ec_crypto.ECDH(), hub_public_key)

    return {
        "bsid": bsid,
        "phoneId": phone_id,
        "phoneKey": rsa_private_pkcs8_b64,
        "hubKey": migration_data["newHubKey"],
        "phoneSecret": base64.b64encode(shared_secret).decode("ascii"),
        "phonePassword": new_phone_password,
    }


def v3migrate(
    bsid: str,
    phone_id: str,
    legacy_phone_secret: str,
    legacy_phone_password: str,
    user_password: str,
) -> dict:
    """One-shot convenience wrapper: v3migrate_prepare() + a single
    v3migrate_attempt(). Only useful if you expect the very first attempt to
    succeed - for real usage where the server needs a few retries (the
    normal case, matches the real app's own ~1s retry loop), call
    v3migrate_prepare() once yourself and retry v3migrate_attempt() on that
    SAME session instead of calling this repeatedly - see the module-level
    warning on v3migrate_prepare() for why reusing the session matters.
    """
    return v3migrate_attempt(v3migrate_prepare(bsid, phone_id, legacy_phone_secret, legacy_phone_password, user_password))


# --------------------------------------------------------------------------
# Crypto primitives shared by the RPC channel and v3migrate.
# --------------------------------------------------------------------------

def _aes_key_iv(key_str: str, iv_str: str) -> tuple[bytes, bytes]:
    key = hashlib.sha256(key_str.encode("utf-8")).digest()  # full 32 bytes -> AES-256
    iv = hashlib.sha256(iv_str.encode("utf-8")).digest()[:16]
    return key, iv


def aes_encrypt(key_str: str, iv_str: str, plaintext: str) -> str:
    key, iv = _aes_key_iv(key_str, iv_str)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    return base64.b64encode(ct).decode("ascii")


def aes_decrypt(key_str: str, iv_str: str, b64_ciphertext: str) -> str:
    key, iv = _aes_key_iv(key_str, iv_str)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    pt = unpad(cipher.decrypt(base64.b64decode(b64_ciphertext)), 16)
    return pt.decode("utf-8")


def hmac_sha256(key_str: str, message: str) -> str:
    mac = hmac.new(key_str.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def rsa_sign(phone_key_b64: str, message: str) -> str:
    """RSA-2048/SHA512withRSA (PKCS#1 v1.5) signature over message.
    phone_key_b64 is the PKCS#8 DER-encoded RSA private key, base64-encoded -
    the same form persisted as `phoneKey` elsewhere in this module.
    """
    key = RSA.import_key(base64.b64decode(phone_key_b64))
    h = SHA512.new(message.encode("utf-8"))
    return base64.b64encode(pkcs1_15.new(key).sign(h)).decode("ascii")


# --------------------------------------------------------------------------
# The RPC channel: POST appv3/message with an AES-encrypted path/data
# payload, wrapped with hubId/phoneId/requestId/time/mac/signature. `mac` is
# the literal string "NOKEY" when no session key exists yet - i.e. for the
# very first `auth` call. The RSA `signature` field is included whenever a
# phone_key is available, computed over the same
# "hubId:phoneId:time:requestId:encryptedRequest" string as the mac.
#
# IMPORTANT: appv3/message's own HTTP response is just an ack (e.g.
# {"bsid": "..."}, HTTP 202) - it is NOT the RPC result. The real result is
# delivered asynchronously via appv3/poll, whose response body is
# {"messages": [...]}, each entry carrying the same requestId as the request
# it answers plus its own encrypted `response` field (same AES scheme,
# IV-seeded by that entry's own `time`, not the original request's time).
# call_and_wait() below is what actually gets you real data; rpc_call()
# alone only gets you the ack.
# --------------------------------------------------------------------------

def _signed_message_body(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    plaintext: str,
    request_id: str,
    session_key: str | None = None,
    phone_key: str | None = None,
) -> dict:
    """Build the common signed/encrypted envelope every appv3/message and
    appv3/poll request needs - see the module comment above for the shape.
    """
    timestamp_ms = int(time.time() * 1000)
    encrypted = aes_encrypt(phone_secret, str(timestamp_ms), plaintext)
    signing_string = f"{bsid}:{phone_id}:{timestamp_ms}:{request_id}:{encrypted}"

    body = {
        "hubId": bsid,
        "phoneId": phone_id,
        "requestId": request_id,
        "request": encrypted,
        "time": timestamp_ms,
        "mac": hmac_sha256(session_key, signing_string) if session_key else "NOKEY",
    }
    if phone_key:
        body["signature"] = rsa_sign(phone_key, signing_string)
    return body


def rpc_call(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    path: str,
    data: dict,
    session_key: str | None = None,
    phone_key: str | None = None,
) -> dict:
    """Fire off an RPC call (auth, getDevices, sendDeviceCommand, ...) over the
    appv3/message channel and return ONLY the immediate HTTP ack (e.g.
    {"bsid": "..."}) - not the real result, which is delivered separately via
    poll()/call_and_wait(). Prefer call_and_wait() unless you specifically
    want fire-and-forget behaviour.

    Pass session_key once authenticate() has returned one, to MAC-sign the
    request properly instead of using the "NOKEY" bootstrap sentinel. Pass
    phone_key (the RSA private key, PKCS#8 DER base64) to include the
    required `signature` field - without it the server returns 403 Forbidden
    for every call.
    """
    envelope = json.dumps({"path": path, "data": data})
    body = _signed_message_body(bsid, phone_id, phone_secret, envelope, str(uuid.uuid4()), session_key, phone_key)
    return _post(APPV3_MESSAGE_PATH, body)


def poll(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    session_key: str | None = None,
    phone_key: str | None = None,
) -> list[dict]:
    """Long-poll appv3/poll once and return whatever real messages have
    arrived since the last call, decrypted. The request body's plaintext is
    always the literal string "{}" and its requestId is always "", unlike
    appv3/message where those fields carry the actual RPC.

    The "messages" array can contain two different shapes: RPC responses,
    keyed by "response" - the answer to a specific appv3/message call,
    correlated by requestId; and unsolicited "event" pushes (state-change
    notifications broadcast independently of any request we made, e.g.
    triggered by our own sendDeviceCommand's side effect), keyed by
    "event"/"eventType" instead, with no requestId at all.

    Returns a list of dicts, one per message:
    {"type": "response", "requestId": ..., "data": <decrypted parsed JSON>}
    or {"type": "event", "eventType": <int>, "data": <decrypted parsed JSON>}
    - may be empty if nothing new has arrived yet.
    """
    body = _signed_message_body(bsid, phone_id, phone_secret, "{}", "", session_key, phone_key)
    response = _post("appv3/poll", body)
    messages = []
    for msg in response.get("messages", []):
        if "response" in msg:
            plaintext = aes_decrypt(phone_secret, str(msg["time"]), msg["response"])
            messages.append({"type": "response", "requestId": msg["requestId"], "data": json.loads(plaintext)})
        elif "event" in msg:
            plaintext = aes_decrypt(phone_secret, str(msg["time"]), msg["event"])
            messages.append({"type": "event", "eventType": msg.get("eventType"), "data": json.loads(plaintext)})
    return messages


DEFAULT_POLL_TIMEOUT_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def call_and_wait(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    path: str,
    data: dict,
    session_key: str | None = None,
    phone_key: str | None = None,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Send an RPC call and block until its real (decrypted) response arrives
    via appv3/poll, matched by requestId - this is what actually gets you
    data, since appv3/message's own HTTP response is just an ack. Raises
    SddError if no matching poll response shows up within `timeout` seconds.
    """
    envelope = json.dumps({"path": path, "data": data})
    request_id = str(uuid.uuid4())
    body = _signed_message_body(bsid, phone_id, phone_secret, envelope, request_id, session_key, phone_key)
    _post(APPV3_MESSAGE_PATH, body)

    deadline = time.monotonic() + timeout
    while True:
        for message in poll(bsid, phone_id, phone_secret, session_key=session_key, phone_key=phone_key):
            if message["type"] == "response" and message["requestId"] == request_id:
                return message["data"]
        if time.monotonic() >= deadline:
            raise SddError(f"Timed out waiting for a poll response to {path!r} (requestId={request_id})")
        time.sleep(poll_interval)


def authenticate(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    phone_password: str,
    user_password: str | None = None,
    temporary: bool = False,
    phone_key: str | None = None,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """The session-establishment call (path "auth"), sent over appv3/message
    - not app/connect, which this client never uses.

    Returns the decrypted response:
    {"data": {"duration": {"value": ...}, "key": "<session key>",
    "expiresIn": ...}, "appTimeout": ..., "errorCode": 0, "state": 0} - the
    session key to pass as session_key to later calls is response["data"]["key"].
    """
    data = {"userPassword": user_password, "phonePassword": phone_password, "temporary": temporary}
    return call_and_wait(
        bsid, phone_id, phone_secret, "auth", data,
        phone_key=phone_key, timeout=timeout, poll_interval=poll_interval,
    )


def get_devices(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    session_key: str | None = None,
    phone_key: str | None = None,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Returns the decrypted device list, each device carrying
    position/lockLocked/offline/openControllable/closeControllable/
    stopControllable/pendingCommand/lightOn/auxiliaryOn/
    remoteControlLockoutOn/phoneLockoutOn/advancedAccess/advancedParameters/
    peBeam*/log (last command) and more. There is no single doorState enum
    string; infer state from position + pendingCommand + log.deviceCommand.
    """
    return call_and_wait(
        bsid, phone_id, phone_secret, "getDevices", {}, session_key, phone_key,
        timeout=timeout, poll_interval=poll_interval,
    )


def get_device_logs(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    device_id: str,
    session_key: str | None = None,
    phone_key: str | None = None,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Returns the decrypted per-device activity log - the closest thing the
    API has to an activity/notification history; there is no separate
    notification-history endpoint. Each entry in response["data"] has
    deviceCommand (see DEVICE_COMMAND), time, logType, logSource, logId, and
    userId/phoneId when a specific account triggered it (absent for
    hub-originated events like a sensor auto-close).
    """
    return call_and_wait(
        bsid, phone_id, phone_secret, "getDeviceLogs", {"deviceId": device_id}, session_key, phone_key,
        timeout=timeout, poll_interval=poll_interval,
    )


# The deviceCommand codes relevant to garage doors - the API's full command
# set is larger (it covers other device types too), only these are used here.
DEVICE_COMMAND = {
    "OPEN": 2,
    "STOP": 3,
    "CLOSE": 4,
    "PART_OPEN_1": 5,
    "PART_OPEN_2": 6,
    "PART_OPEN_3": 7,
    "LIGHT_ON": 16,
    "LIGHT_OFF": 17,
}


def send_device_command(
    bsid: str,
    phone_id: str,
    phone_secret: str,
    device_id: str,
    command: str,
    session_key: str | None = None,
    phone_key: str | None = None,
    timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> dict:
    """Send a device command (open/close/stop/light on/off - see
    DEVICE_COMMAND) and wait for its real result via call_and_wait().
    """
    code = DEVICE_COMMAND[command.upper()]
    return call_and_wait(
        bsid, phone_id, phone_secret, "sendDeviceCommand",
        {"deviceId": device_id, "deviceCommand": code}, session_key, phone_key,
        timeout=timeout, poll_interval=poll_interval,
    )
