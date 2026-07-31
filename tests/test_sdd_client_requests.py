"""
Tests that lock in the exact HTTP request shapes for each SDD API call, using
a mocked requests.post - no network access, no real credentials involved.

Copied from the canonical test suite in the sibling research repo
(../../garage-door/wan-api/client/tests/test_requests.py) - keep in sync
manually alongside sdd_client.py itself (see ../CLAUDE.md). These freeze in
what's been confirmed correct against the real server so a future refactor
can't silently drift away from a working request shape.
"""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Util.asn1 import DerSequence
from cryptography.hazmat.primitives.asymmetric import ec

import sdd_client


def _mock_response(json_body=None, status_code=200):
    resp = MagicMock()
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.text = json.dumps(json_body) if json_body is not None else ""
    resp.json.return_value = json_body or {}
    return resp


def _message_then_poll_side_effect(phone_secret, response_data, message_time=1234567890):
    """Mocks requests.post for the real two-step call_and_wait() flow: the
    appv3/message call gets a bare ack, then the appv3/poll call that follows
    gets a "messages" array containing response_data (encrypted, tagged with
    the SAME requestId call_and_wait() generated for the appv3/message call -
    captured off that call's own body). Matches the real server's own
    ack-then-poll delivery - see call_and_wait()/poll() in sdd_client.py.
    """
    state = {}

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        if url.endswith("appv3/message"):
            state["request_id"] = body["requestId"]
            return _mock_response({"bsid": body["hubId"]}, status_code=202)
        elif url.endswith("appv3/poll"):
            encrypted = sdd_client.aes_encrypt(phone_secret, str(message_time), json.dumps(response_data))
            return _mock_response({
                "messages": [{
                    "hubId": body["hubId"],
                    "phoneId": body["phoneId"],
                    "requestId": state["request_id"],
                    "response": encrypted,
                    "mac": "fake-mac",
                    "signature": "fake-signature",
                    "time": message_time,
                }]
            })
        raise AssertionError(f"unexpected URL posted to: {url}")

    return fake_post


@patch("sdd_client.requests.post")
def test_remote_register_request_shape(mock_post):
    mock_post.return_value = _mock_response(
        {"bsid": "b1", "phoneId": "p1", "phoneSecret": "s1", "phonePassword": "pp1"}
    )
    sdd_client.remote_register("JOINCODE", "hunter2", phone_name="TestPhone", phone_model="TestModel")

    args, kwargs = mock_post.call_args
    assert args[0] == "https://version3.smartdoordevices.com/app/remoteregister"
    assert kwargs["json"] == {
        "remoteRegistrationCode": "JOINCODE",
        "userPassword": "hunter2",
        "phoneName": "TestPhone",
        "phoneModel": "TestModel",
    }
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["app-version"] == sdd_client.SDK_VERSION
    assert kwargs["verify"]  # should point at the bundled CA file


@patch("sdd_client.requests.post")
def test_authenticate_request_shape(mock_post):
    # authenticate() is the REAL session-establishment call (path "auth" over
    # appv3/message) - see the sibling repo's wan-api/README.md "Which
    # endpoints are real".
    fake_response_data = {"data": {"duration": {"value": 0}, "key": "sess-key-1", "expiresIn": 0}, "errorCode": 0, "state": 0}
    mock_post.side_effect = _message_then_poll_side_effect("secret1", fake_response_data)
    result = sdd_client.authenticate(bsid="bsid1", phone_id="phone1", phone_secret="secret1", phone_password="pw1")

    args, kwargs = mock_post.call_args_list[0]
    assert args[0] == "https://version3.smartdoordevices.com/appv3/message"
    body = kwargs["json"]
    assert body["hubId"] == "bsid1"
    assert body["phoneId"] == "phone1"
    assert body["mac"] == "NOKEY"  # no session key exists yet for this first call

    decrypted = sdd_client.aes_decrypt("secret1", str(body["time"]), body["request"])
    envelope = json.loads(decrypted)
    assert envelope["path"] == "auth"
    assert envelope["data"] == {"userPassword": None, "phonePassword": "pw1", "temporary": False}

    # the real, poll-delivered response - not just the appv3/message ack
    assert result == fake_response_data


@patch("sdd_client.requests.post")
def test_get_devices_envelope_is_encrypted_and_decryptable(mock_post):
    fake_response_data = {"errorCode": 0, "state": 0, "data": [{"deviceId": "dev1", "position": 0}]}
    mock_post.side_effect = _message_then_poll_side_effect("secret1", fake_response_data)
    result = sdd_client.get_devices(bsid="bsid1", phone_id="phone1", phone_secret="secret1")

    args, kwargs = mock_post.call_args_list[0]
    assert args[0] == "https://version3.smartdoordevices.com/appv3/message"
    body = kwargs["json"]
    assert body["hubId"] == "bsid1"
    assert body["phoneId"] == "phone1"
    assert "requestId" in body and "time" in body
    assert body["mac"] == "NOKEY"  # no session_key passed -> bootstrap sentinel, not omitted
    assert "signature" not in body  # no phone_key passed -> field omitted entirely

    # confirm the encrypted body round-trips to the expected plaintext envelope
    decrypted = sdd_client.aes_decrypt("secret1", str(body["time"]), body["request"])
    assert json.loads(decrypted) == {"path": "getDevices", "data": {}}

    # the real, poll-delivered response - not just the appv3/message ack
    assert result == fake_response_data


@patch("sdd_client.requests.post")
def test_get_devices_includes_valid_signature_when_phone_key_given(mock_post):
    # Confirmed against the real server: a call built exactly this way
    # (phone_key present -> signature included, signed over the same string as
    # the mac) got a genuine success response, not 403 Forbidden.
    mock_post.side_effect = _message_then_poll_side_effect("secret1", {"errorCode": 0, "state": 0, "data": []})
    key = RSA.generate(2048)
    phone_key_b64 = base64.b64encode(key.export_key(format="DER", pkcs=8)).decode("ascii")

    sdd_client.get_devices(bsid="bsid1", phone_id="phone1", phone_secret="secret1", phone_key=phone_key_b64)

    body = mock_post.call_args_list[0].kwargs["json"]
    assert "signature" in body
    signing_string = f"{body['hubId']}:{body['phoneId']}:{body['time']}:{body['requestId']}:{body['request']}"
    h = SHA512.new(signing_string.encode("utf-8"))
    pkcs1_15.new(key.publickey()).verify(h, base64.b64decode(body["signature"]))  # raises if invalid


@patch("sdd_client.requests.post")
def test_send_device_command_uses_correct_code_and_real_mac_when_session_key_given(mock_post):
    mock_post.side_effect = _message_then_poll_side_effect("secret1", {"errorCode": 0, "state": 0, "data": {}})
    sdd_client.send_device_command(
        bsid="bsid1", phone_id="phone1", phone_secret="secret1",
        device_id="dev1", command="open", session_key="sesskey1",
    )

    args, kwargs = mock_post.call_args_list[0]
    body = kwargs["json"]
    assert body["mac"] != "NOKEY"

    decrypted = sdd_client.aes_decrypt("secret1", str(body["time"]), body["request"])
    envelope = json.loads(decrypted)
    assert envelope["path"] == "sendDeviceCommand"
    assert envelope["data"] == {"deviceId": "dev1", "deviceCommand": sdd_client.DEVICE_COMMAND["OPEN"]}


@patch("sdd_client.requests.post")
def test_get_device_logs_request_shape_and_response(mock_post):
    fake_response_data = {
        "errorCode": 0, "state": 0,
        "data": [{"deviceId": "dev1", "deviceCommand": sdd_client.DEVICE_COMMAND["LIGHT_ON"], "time": 111, "logType": 11}],
    }
    mock_post.side_effect = _message_then_poll_side_effect("secret1", fake_response_data)
    result = sdd_client.get_device_logs(bsid="bsid1", phone_id="phone1", phone_secret="secret1", device_id="dev1")

    body = mock_post.call_args_list[0].kwargs["json"]
    decrypted = sdd_client.aes_decrypt("secret1", str(body["time"]), body["request"])
    envelope = json.loads(decrypted)
    assert envelope["path"] == "getDeviceLogs"
    assert envelope["data"] == {"deviceId": "dev1"}

    assert result == fake_response_data


@patch("sdd_client.requests.post")
def test_poll_decrypts_and_correlates_messages_by_request_id(mock_post):
    mock_post.return_value = _mock_response({
        "messages": [
            {
                "hubId": "bsid1", "phoneId": "phone1", "requestId": "req-abc",
                "response": sdd_client.aes_encrypt("secret1", "999", json.dumps({"foo": "bar"})),
                "mac": "m", "signature": "s", "time": 999,
            },
        ]
    })
    messages = sdd_client.poll(bsid="bsid1", phone_id="phone1", phone_secret="secret1")

    assert messages == [{"type": "response", "requestId": "req-abc", "data": {"foo": "bar"}}]

    body = mock_post.call_args.kwargs["json"]
    assert body["requestId"] == ""
    decrypted = sdd_client.aes_decrypt("secret1", str(body["time"]), body["request"])
    assert decrypted == "{}"


@patch("sdd_client.requests.post")
def test_poll_decodes_unsolicited_event_messages_distinctly_from_responses(mock_post):
    # appv3/poll's "messages" array can contain unsolicited state-change
    # events (keyed by "event"/"eventType", no requestId at all) interleaved
    # with real RPC responses - matches the dispatcher in the decompiled SDK's
    # HubConnection.a(a, JsonObject), which branches on jsonObject.has(
    # "response") vs .has("event"). poll() must not crash on these, and must
    # not let call_and_wait() mistake one for the response it's waiting for.
    mock_post.return_value = _mock_response({
        "messages": [
            {
                "hubId": "bsid1", "phoneId": "phone1",
                "event": sdd_client.aes_encrypt("secret1", "555", json.dumps({"lightOn": True})),
                "eventType": 7, "mac": "m", "signature": "s", "time": 555,
            },
            {
                "hubId": "bsid1", "phoneId": "phone1", "requestId": "req-abc",
                "response": sdd_client.aes_encrypt("secret1", "999", json.dumps({"foo": "bar"})),
                "mac": "m", "signature": "s", "time": 999,
            },
        ]
    })
    messages = sdd_client.poll(bsid="bsid1", phone_id="phone1", phone_secret="secret1")

    assert messages == [
        {"type": "event", "eventType": 7, "data": {"lightOn": True}},
        {"type": "response", "requestId": "req-abc", "data": {"foo": "bar"}},
    ]


@patch("sdd_client.requests.post")
def test_call_and_wait_skips_unsolicited_events_and_finds_its_own_response(mock_post):
    # A live sendDeviceCommand triggered exactly this: the poll cycle that
    # eventually carried our response also carried an unrelated state-change
    # event first. call_and_wait() must not mistake the event for its answer
    # (it has no requestId at all) and must keep polling instead of crashing.
    state = {}

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        if url.endswith("appv3/message"):
            state["request_id"] = body["requestId"]
            return _mock_response({"bsid": body["hubId"]}, status_code=202)
        elif url.endswith("appv3/poll"):
            event = {
                "hubId": body["hubId"], "phoneId": body["phoneId"],
                "event": sdd_client.aes_encrypt("secret1", "555", json.dumps({"lightOn": True})),
                "eventType": 7, "mac": "m", "signature": "s", "time": 555,
            }
            answer = {
                "hubId": body["hubId"], "phoneId": body["phoneId"], "requestId": state["request_id"],
                "response": sdd_client.aes_encrypt("secret1", "999", json.dumps({"errorCode": 0})),
                "mac": "m", "signature": "s", "time": 999,
            }
            return _mock_response({"messages": [event, answer]})
        raise AssertionError(f"unexpected URL posted to: {url}")

    mock_post.side_effect = fake_post
    result = sdd_client.call_and_wait(
        bsid="bsid1", phone_id="phone1", phone_secret="secret1", path="sendDeviceCommand", data={},
    )
    assert result == {"errorCode": 0}


@patch("sdd_client.requests.post")
def test_call_and_wait_raises_on_timeout(mock_post):
    # no "messages" key at all in the poll response -> nothing ever arrives
    mock_post.return_value = _mock_response({"bsid": "bsid1"}, status_code=202)
    with pytest.raises(sdd_client.SddError):
        sdd_client.call_and_wait(
            bsid="bsid1", phone_id="phone1", phone_secret="secret1",
            path="getDevices", data={}, timeout=0.05, poll_interval=0.01,
        )


def test_unknown_device_command_raises():
    with pytest.raises(KeyError):
        sdd_client.send_device_command(
            bsid="b", phone_id="p", phone_secret="s", device_id="d", command="NOT_A_REAL_COMMAND",
        )


@patch("sdd_client.requests.post")
def test_http_error_raises_sdd_error(mock_post):
    mock_post.return_value = _mock_response({"message": "Bad request"}, status_code=400)
    with pytest.raises(sdd_client.SddError) as exc_info:
        sdd_client.remote_register(registration_code="code1", user_password="pw1")
    assert exc_info.value.status == 400


@patch("sdd_client.requests.post")
def test_v3migrate_full_round_trip_against_a_fake_server(mock_post):
    # No known-answer vector exists for this one (never fully decrypted a
    # real capture). Instead this plays the server's side for real: decrypts
    # our actual request with the crypto primitives sdd_client itself
    # exposes, independently derives what the resulting phoneSecret *should*
    # be (its own ECDH exchange against the client's real ephemeral EC key),
    # and returns a real encrypted response - then checks v3migrate()'s
    # return value matches that independently-derived expectation exactly.
    legacy_phone_secret = "legacy-phone-secret-1"
    legacy_phone_password = "legacy-pw-1"
    phone_id = "phone-abc123"
    bsid = "bsid-xyz789"

    server_ec_key = ec.generate_private_key(ec.SECP256R1())
    server_numbers = server_ec_key.public_key().public_numbers()
    server_ec_pub_b64 = base64.b64encode(
        b"\x04" + server_numbers.x.to_bytes(32, "big") + server_numbers.y.to_bytes(32, "big")
    ).decode("ascii")
    expected_hub_key = "fake-new-hub-key-value"

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        assert body["bsid"] == bsid
        assert body["phoneId"] == phone_id
        assert body["phonePassword"] == legacy_phone_password
        assert body["userPassword"] == "userpw1"

        # confirm the outer phoneKey is a valid bare PKCS#1 RSA public key
        rsa_pub_der = base64.b64decode(body["phoneKey"])
        n, e = DerSequence().decode(rsa_pub_der)
        client_rsa_pub = RSA.construct((n, e))

        # confirm the signature verifies against that same public key, over
        # the encrypted data string
        h = SHA512.new(body["data"].encode("utf-8"))
        pkcs1_15.new(client_rsa_pub).verify(h, base64.b64decode(body["signature"]))  # raises if invalid

        # decrypt the inner data blob with the real legacy AES scheme
        inner_plaintext = sdd_client._legacy_aes_decrypt(legacy_phone_secret, str(body["time"]), body["data"])
        inner = json.loads(inner_plaintext)
        assert inner["phoneKey"] == body["phoneKey"]
        client_ec_pub_raw = base64.b64decode(inner["newPhoneSecretPhoneHalf"])
        client_ec_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_ec_pub_raw)
        assert len(inner["newPhonePassword"]) == 43

        # independently derive the expected shared secret (server's side of ECDH)
        expected_shared_secret = server_ec_key.exchange(ec.ECDH(), client_ec_pub)

        # build and encrypt the real response, same as the real server would
        migration_data_plaintext = json.dumps(
            {"newHubKey": expected_hub_key, "newPhoneSecretHubHalf": server_ec_pub_b64}
        )
        migration_data = sdd_client._legacy_aes_encrypt(legacy_phone_secret, phone_id, migration_data_plaintext)

        fake_post.expected_shared_secret_b64 = base64.b64encode(expected_shared_secret).decode("ascii")
        fake_post.new_phone_password = inner["newPhonePassword"]
        return _mock_response({"bsid": bsid, "phoneId": phone_id, "migrationData": migration_data})

    mock_post.side_effect = fake_post

    result = sdd_client.v3migrate(
        bsid=bsid,
        phone_id=phone_id,
        legacy_phone_secret=legacy_phone_secret,
        legacy_phone_password=legacy_phone_password,
        user_password="userpw1",
    )

    assert result["hubKey"] == expected_hub_key
    assert result["phoneSecret"] == fake_post.expected_shared_secret_b64
    assert result["phonePassword"] == fake_post.new_phone_password
    assert result["bsid"] == bsid
    assert result["phoneId"] == phone_id

    # confirm the returned phoneKey is a usable PKCS#8 RSA private key that
    # can sign and be verified by its own public half
    returned_key = RSA.import_key(base64.b64decode(result["phoneKey"]))
    h = SHA512.new(b"round-trip-check")
    sig = pkcs1_15.new(returned_key).sign(h)
    pkcs1_15.new(returned_key.publickey()).verify(h, sig)  # raises if invalid


@patch("sdd_client.requests.post")
def test_v3migrate_returns_pending_on_interim_ack(mock_post):
    mock_post.return_value = _mock_response({"bsid": "bsid1"}, status_code=202)
    result = sdd_client.v3migrate(
        bsid="bsid1", phone_id="phone1", legacy_phone_secret="secret1",
        legacy_phone_password="pw1", user_password="userpw1",
    )
    # the raw ack is surfaced too, not just a bare "pending" flag - a caller
    # retrying on this needs to be able to tell an interim ack apart from a
    # real error that also lacks "migrationData" (e.g. wrong password)
    assert result == {"pending": True, "ack": {"bsid": "bsid1"}}


@patch("sdd_client.requests.post")
def test_v3migrate_attempt_reuses_the_same_keys_across_retries(mock_post):
    # Confirmed against a real capture: the real app generates its RSA/EC
    # keypair and random newPhonePassword exactly once and resends the SAME
    # phoneKey on every retry - all four attempts of a real migration carried
    # the identical phoneKey value, only time/data/signature differed. An
    # earlier version of this code regenerated fresh keys on every
    # v3migrate() call, which made the server treat each retry as an
    # unrelated attempt that never got to complete (stuck "pending" forever,
    # live-observed the same day). v3migrate_prepare() generates once;
    # v3migrate_attempt() must reuse that same session's keys.
    mock_post.return_value = _mock_response({"bsid": "bsid1"}, status_code=202)
    session = sdd_client.v3migrate_prepare(
        bsid="bsid1", phone_id="phone1", legacy_phone_secret="secret1",
        legacy_phone_password="pw1", user_password="userpw1",
    )
    first = sdd_client.v3migrate_attempt(session)
    second = sdd_client.v3migrate_attempt(session)

    assert first == {"pending": True, "ack": {"bsid": "bsid1"}}
    assert second == {"pending": True, "ack": {"bsid": "bsid1"}}

    first_body = mock_post.call_args_list[0].kwargs["json"]
    second_body = mock_post.call_args_list[1].kwargs["json"]
    assert first_body["phoneKey"] == second_body["phoneKey"]
    # but each attempt still re-encrypts with a fresh time/IV, so the
    # ciphertext and signature legitimately differ between attempts
    assert first_body["time"] != second_body["time"] or first_body["data"] != second_body["data"]
