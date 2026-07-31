"""
Standalone client for the B&D / "Smart Door Devices" (SDD) WAN cloud API.

**This is a copy of ../../wan-api/client/sdd_client.py** (that's the
canonical, tested copy - see its test suite at
../../wan-api/client/tests/). This copy exists because HACS only
distributes the contents of custom_components/<domain>/, so the integration
needs to be self-contained rather than importing across the parent repo.
Keep the two in sync manually when the protocol implementation changes.

Reconstructed entirely via static analysis of the official B&D Smart Garage
Access Android app (au.com.bnd.controlladoor) - see ../../wan-api/README.md
for the full protocol write-up this implements, and
../../session-notes-2026-07-30.md for how it was derived.

Status (2026-07-31): the entire fresh-client bootstrap chain - pair (
remote_register()) -> migrate (v3migrate_prepare()/v3migrate_attempt()) ->
auth (authenticate()) - plus get_devices()/send_device_command()/
get_device_logs(), is confirmed working end-to-end against the real server,
with no Frida extraction involved anywhere. See ../../wan-api/README.md
"Current status" for the full story.

Never hardcode real credentials in this file or commit them anywhere in this
repo - see ../../credentials.md.
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
# Endpoints - see ../README.md "Which endpoints are actually real"
# for the full story. Two generations of endpoint exist in the decompiled SDK;
# don't assume a path found in com.smartdoordevices.client.sdk.d.p is live.
#
# REAL (confirmed live, used by the current v3 web channel, e/g.java):
#   app/remoteregister  - pairing via join code (confirmed working 2026-07-30)
#   appv3/message        - the one true RPC endpoint: auth, getDevices,
#                          sendDeviceCommand, everything, all via the
#                          encrypted path/data envelope
#   appv3/poll           - long-poll channel for async message delivery
#   appv3/hubinfo        - not yet explored
#
# LEGACY-ONLY (only reachable via the v2->v3 credential migration path,
# d/j.java LegacyCredentialMigrator - NOT part of a fresh v3 client's flow):
#   app/connect          - what this client first (incorrectly) tried to use
#                          for session establishment; it 400's uniformly
#                          regardless of credentials because a fresh v3
#                          account's HubConnection.connect() (e/e.java) never
#                          calls it at all unless the account is flagged
#                          legacy
#   app/v3migrate
#   app/connectrestricted, app/time, app/action - constants exist in d/p.java
#                          but no call site was found anywhere in the SDK;
#                          likely dead/reserved
# --------------------------------------------------------------------------
APPV3_MESSAGE_PATH = "appv3/message"

REQUEST_TIMEOUT_SECONDS = 15

# The server presents a chain signed by SmartDoorDevices' own private CA, which
# is why plain requests (using the public CA bundle) rejects it with
# "self-signed certificate in certificate chain" - the app validates against
# this exact intermediate cert instead of the public trust store (see
# com.smartdoordevices.client.sdk.d.i.b() in the decompiled SDK). Extracted
# from the app's own bundled BKS keystore (res/raw/smartdoordevices_intermediate_v2.bks)
# via `keytool -exportcert`, see the sibling research repo's wan-api/README.md for how.
#
# Kept alongside this file (not a sibling "reference/" dir like the source
# copy) because HACS only ever pulls custom_components/bnd_smart_hub/ itself -
# anything this integration needs at runtime has to live inside that directory.
CA_BUNDLE_PATH = Path(__file__).parent / "sdd-cloud-ca.pem"

_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": USER_AGENT,
    "version": SDK_VERSION,
    "app-version": SDK_VERSION,  # com.instabug...Header.APP_VERSION = "app-version"
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
# Pairing / session establishment (com.smartdoordevices.client.sdk.d.s / d.b)
# --------------------------------------------------------------------------

def remote_register(
    registration_code: str,
    user_password: str,
    phone_name: str = "HomeAssistant",
    phone_model: str = "HomeAssistant",
) -> dict:
    """Pair a new client using a join/registration code shown in the B&D app.

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
# app/v3migrate (com.smartdoordevices.client.sdk.d.j LegacyCredentialMigrator)
# - the one-time bootstrap that turns a legacy (v2, no phoneKey) credential
# from remote_register() into a real v3 credential set. Normally triggered
# automatically by the real app's HubConnection.login() the first time it's
# called after a fresh registration - see the sequence diagram and "How a
# client gets its phoneKey" in ../README.md. Wire format confirmed correct
# via a real live captured exchange (2026-07-31) for every plaintext field;
# the two encrypted blobs (data/migrationData) weren't decryptable from that
# capture alone (needs the legacy phoneSecret, which requires also capturing
# app/remoteregister - not yet done), so this implementation's crypto is
# static-analysis-derived and not yet confirmed byte-correct end-to-end
# against the real server. See dead-ends.md for what didn't work finding
# this the hard way.
# --------------------------------------------------------------------------

_RANDOM_PASSWORD_CHARSET = string.digits + string.ascii_lowercase + string.ascii_uppercase


def _random_password(length: int) -> str:
    """Matches com.smartdoordevices.client.sdk.a.h.b(int) - alphanumeric only,
    no symbols.
    """
    return "".join(secrets.choice(_RANDOM_PASSWORD_CHARSET) for _ in range(length))


def _legacy_aes_key_iv(key_str: str, iv_str: str) -> tuple[bytes, bytes]:
    """The OLDER AES scheme app/v3migrate's `data` field uses - AES-128 with
    MD5-derived key/IV (com.smartdoordevices.client.sdk.d.a), distinct from
    aes_encrypt/decrypt's SHA-256/AES-256 scheme used everywhere else.
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
    """The RSA public key encoding app/v3migrate's outer `phoneKey` field (and
    the public key embedded in the encrypted `data` blob) uses - bare PKCS#1
    RSAPublicKey DER (SEQUENCE{modulus, publicExponent}), NOT the standard
    294-byte X.509 SubjectPublicKeyInfo wrapper. Matches
    com.smartdoordevices.client.sdk.a.g.a(PublicKey) exactly: confirmed
    byte-for-byte 2026-07-31 - Java slices the last 270 bytes off the full
    X.509 SPKI DER, which is exactly this 270-byte PKCS#1 structure, and the
    resulting base64 prefix ("MIIBCg...") matches a real captured phoneKey
    value exactly.
    """
    der = DerSequence([rsa_key.n, rsa_key.e]).encode()
    return base64.b64encode(der).decode("ascii")


def _ec_public_key_uncompressed_b64(public_key) -> str:
    """Raw uncompressed EC point (0x04 || X(32) || Y(32), 65 bytes total),
    matching com.smartdoordevices.client.sdk.a.e's public key encoding - also
    not the X.509 SPKI wrapper (Java manually prepends a fixed EC P-256
    AlgorithmIdentifier header only when *decoding* one of these, via
    a/e.java's b(String); the encoded form itself is just the raw point).
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
    every retry via v3migrate_attempt() - matches d/j.java's constructor,
    which generates its RSA/EC keypair and random newPhonePassword exactly
    once (cached in instance fields) and resends the SAME phoneKey/EC-half/
    newPhonePassword on every retry, confirmed against a real capture: all
    four attempts of one real migration sent the identical phoneKey value,
    only time/data/signature differed between attempts. Regenerating fresh
    keys on every retry (an earlier version of this code did exactly that)
    makes the server treat each retry as an unrelated migration attempt that
    never gets to complete - confirmed live 2026-07-31, it just stays
    "pending" forever.
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
    reusing its already-generated keys - matches d/j.java's performQuery(),
    called on a 1s-interval timer, which reuses the same instance fields on
    every call and only recomputes time/the encrypted data/the signature
    fresh each time.

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

    # com.smartdoordevices.client.sdk.d.j$b - the encrypted inner blob
    inner = json.dumps({
        "phoneKey": rsa_pub_b64,
        "newPhoneSecretPhoneHalf": ec_pub_b64,
        "newPhonePassword": new_phone_password,
    })
    # SystemClock.currentThreadTimeMillis() in the real app (confirmed live
    # 2026-07-31 - real captured values were small integers like 1/8/30/4546,
    # not wall-clock epoch millis). Any value works here as long as it's
    # consistent with what's reported in the "time" field below, since it's
    # purely a local IV-derivation seed, never validated by the server
    # against wall-clock time. Recomputed fresh on every attempt, unlike the
    # keys above.
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

    # Response's data blob uses phoneId (the string itself, not a number) as
    # the IV-derivation seed, not the request's numeric "time" - confirmed
    # via static analysis of d/j.java's handleResponse().
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
# Crypto (com.smartdoordevices.client.sdk.a.a / a.f) - verified byte-correct
# against the app's own baked-in self-test vectors, see verify_crypto.py
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
    """RSA-2048/SHA512withRSA (PKCS#1 v1.5) signature, matching
    com.smartdoordevices.client.sdk.a.g.a(PrivateKey, String) exactly:
    phone_key_b64 is the PKCS#8 DER-encoded RSA private key, base64-encoded
    (Android Base64.NO_WRAP), same as what's persisted as `phoneKey`.
    """
    key = RSA.import_key(base64.b64decode(phone_key_b64))
    h = SHA512.new(message.encode("utf-8"))
    return base64.b64encode(pkcs1_15.new(key).sign(h)).decode("ascii")


# --------------------------------------------------------------------------
# The one real RPC channel (com.smartdoordevices.client.sdk.e.g "WebChannel",
# e.a's static envelope builder). POST appv3/message, path/data payload
# AES-encrypted, wrapped with hubId/phoneId/requestId/time/mac/signature.
# `mac` is the literal string "NOKEY" when no session key exists yet
# (com.smartdoordevices.client.sdk.a.d.f() falls back to this exact sentinel)
# - i.e. for the very first `auth` call. The RSA `signature` field
# (com.smartdoordevices.client.sdk.e.a's static builder, confirmed via
# ../java-harness/) is computed unconditionally for every call, over the
# exact same "hubId:phoneId:time:requestId:encryptedRequest" string as the
# mac - requires a real phoneKey, which this codebase could only obtain by
# extracting one from an already-authenticated real app session (see
# ../frida/), not by deriving/bootstrapping one itself (that mechanism is
# still unknown - see ../README.md "Current status").
#
# IMPORTANT: appv3/message's own HTTP response is just an ack (e.g.
# {"bsid": "..."}, HTTP 202) - it is NOT the RPC result. The real result is
# delivered asynchronously via appv3/poll, whose response body is
# {"messages": [...]}, each entry carrying the same requestId as the request
# it answers plus its own encrypted `response` field (same AES scheme,
# IV-seeded by that entry's own `time`, not the original request's time).
# Confirmed live 2026-07-31 by decrypting real appv3/poll traffic - see
# ../README.md. call_and_wait() below is what actually gets you real data;
# rpc_call() alone only gets you the ack.
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
    real appv3/message channel and return ONLY the immediate HTTP ack (e.g.
    {"bsid": "..."}) - not the real result, which is delivered separately via
    poll()/call_and_wait(). Prefer call_and_wait() unless you specifically
    want fire-and-forget behaviour.

    Pass session_key once authenticate() has returned one, to MAC-sign the
    request properly instead of using the "NOKEY" bootstrap sentinel. Pass
    phone_key (the RSA private key, PKCS#8 DER base64) to include the
    required `signature` field - without it the real server returns 403
    Forbidden for every call, confirmed 2026-07-30.
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
    always the literal string "{}" and its requestId is always "" - matches
    the real app's own appv3/poll calls exactly (confirmed live 2026-07-31),
    unlike appv3/message where those fields carry the actual RPC.

    The "messages" array can contain two different shapes (confirmed live
    2026-07-31, and matches the dispatcher in HubConnection.a(a, JsonObject)
    in the decompiled SDK): RPC responses, keyed by "response" - the answer
    to a specific appv3/message call, correlated by requestId; and
    unsolicited "event" pushes (state-change notifications broadcast
    independently of any request we made, e.g. triggered by our own
    sendDeviceCommand's side effect), keyed by "event"/"eventType" instead,
    with no requestId at all.

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
    """The real session-establishment call (com.smartdoordevices.client.sdk.f.a,
    path "auth"), sent over appv3/message - NOT app/connect, which is
    legacy-v2-only and never called by a fresh v3 account.

    Returns the real decrypted response, confirmed live 2026-07-31:
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
    """Returns the real decrypted device list (confirmed live 2026-07-31),
    each device carrying position/lockLocked/offline/openControllable/
    closeControllable/stopControllable/pendingCommand/lightOn/auxiliaryOn/
    remoteControlLockoutOn/phoneLockoutOn/advancedAccess/advancedParameters/
    peBeam*/log (last command) and more - see ../README.md for the full
    field-by-field breakdown. There is no single doorState enum string; infer
    state from position + pendingCommand + log.deviceCommand.
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
    """Returns the real decrypted per-device activity log (confirmed live
    2026-07-31) - this is the closest thing the API has to the app's
    "messages"/activity list; there is no separate notification-history RPC
    (checked - none appeared in any captured traffic). Each entry in
    response["data"] has deviceCommand (see DEVICE_COMMAND), time, logType,
    logSource, logId, and userId/phoneId when a specific account triggered it
    (absent for hub-originated events like a sensor auto-close).
    """
    return call_and_wait(
        bsid, phone_id, phone_secret, "getDeviceLogs", {"deviceId": device_id}, session_key, phone_key,
        timeout=timeout, poll_interval=poll_interval,
    )


# com.smartdoordevices.client.sdk.model.device.DeviceCommand - the subset relevant to garage doors
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
    code = DEVICE_COMMAND[command.upper()]
    return call_and_wait(
        bsid, phone_id, phone_secret, "sendDeviceCommand",
        {"deviceId": device_id, "deviceCommand": code}, session_key, phone_key,
        timeout=timeout, poll_interval=poll_interval,
    )
