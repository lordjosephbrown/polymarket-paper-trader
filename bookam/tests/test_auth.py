from __future__ import annotations

from app.auth import hash_password, make_session_token, parse_session_token, verify_password


class TestPasswords:
    def test_roundtrip(self):
        stored = hash_password("secret123")
        assert verify_password("secret123", stored)
        assert not verify_password("wrong", stored)

    def test_unique_salts(self):
        assert hash_password("x") != hash_password("x")

    def test_malformed_stored_hash(self):
        assert not verify_password("x", "not-a-valid-hash")
        assert not verify_password("x", "")


class TestSessionTokens:
    def test_roundtrip(self):
        token = make_session_token(42, "secret", 3600)
        assert parse_session_token(token, "secret") == 42

    def test_wrong_secret_rejected(self):
        token = make_session_token(42, "secret", 3600)
        assert parse_session_token(token, "other") is None

    def test_expired_rejected(self):
        token = make_session_token(42, "secret", -10)
        assert parse_session_token(token, "secret") is None

    def test_garbage_rejected(self):
        assert parse_session_token("garbage", "secret") is None
        assert parse_session_token("a.b.c", "secret") is None
        token = make_session_token(1, "secret", 3600)
        tampered = "999" + token[token.index(".") :]
        assert parse_session_token(tampered, "secret") is None
