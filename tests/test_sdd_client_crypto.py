"""
Offline, deterministic tests for the crypto primitives in sdd_client.py.

These known-answer vectors come directly from the app's own self-test code.
If a refactor ever breaks the key/IV derivation or encoding, these fail
immediately with no network involved.
"""

import base64

from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

import sdd_client


def test_rsa_sign_verifies_with_matching_public_key():
    # No known-answer vector exists for this one (the app's own g.java self-test
    # was stripped) - but this was confirmed correct the strongest possible way
    # instead: signing with a real phoneKey extracted from a live authenticated
    # app session got a genuine success response from the real server, not 403
    # Forbidden. This test is a self-consistency regression guard, not the
    # original confirmation.
    key = RSA.generate(2048)
    phone_key_b64 = base64.b64encode(key.export_key(format="DER", pkcs=8)).decode("ascii")
    message = "hubid1:phoneid1:1700000000000:req1:ciphertext"

    signature_b64 = sdd_client.rsa_sign(phone_key_b64, message)

    h = SHA512.new(message.encode("utf-8"))
    pkcs1_15.new(key.publickey()).verify(h, base64.b64decode(signature_b64))  # raises if invalid


def test_aes_encrypt_matches_app_self_test_vector():
    # com.smartdoordevices.client.sdk.a.a.a(): AES256encrypt("mysecret", "123456", "my-message")
    assert sdd_client.aes_encrypt("mysecret", "123456", "my-message") == "tD8LAKdGZWf3xQSuoGhstA=="


def test_aes_round_trip():
    ciphertext = sdd_client.aes_encrypt("some-phone-secret", "1700000000000", '{"path": "getDevices", "data": {}}')
    plaintext = sdd_client.aes_decrypt("some-phone-secret", "1700000000000", ciphertext)
    assert plaintext == '{"path": "getDevices", "data": {}}'


def test_hmac_sha256_matches_app_self_test_vector():
    # com.smartdoordevices.client.sdk.a.f.a(): HMAC-SHA256("mykey-_+=/", "mymessage")
    assert sdd_client.hmac_sha256("mykey-_+=/", "mymessage") == "jtsrvkggflt3P9LkPcNzpJWYGIOrMgFXgpwmwO+Q+mk="
