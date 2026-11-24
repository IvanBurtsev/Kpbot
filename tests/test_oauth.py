import shutil
import time
import unittest
from pathlib import Path

from kpbot.config import BASE_DIR
from kpbot.oauth import (
    OAuthError,
    Token,
    TokenStore,
    auth_string,
    build_auth_url,
    ensure_access_token,
)

TMP = BASE_DIR / ".tmp" / "oauth"


class AuthUrlTest(unittest.TestCase):
    def test_contains_required_parameters(self) -> None:
        url = build_auth_url("client-123", "http://127.0.0.1:8765/", "state-abc")
        for part in (
            "client_id=client-123",
            "response_type=code",
            "access_type=offline",
            "prompt=consent",
            "state=state-abc",
            "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2F",
            "https%3A%2F%2Fmail.google.com%2F",
        ):
            self.assertIn(part, url)

    def test_requires_client_id(self) -> None:
        with self.assertRaises(OAuthError):
            build_auth_url("", "http://127.0.0.1:8765/", "s")


class TokenStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        TMP.mkdir(parents=True, exist_ok=True)
        self.path = TMP / "token.json"
        self.path.unlink(missing_ok=True)
        self.store = TokenStore(self.path)

    def tearDown(self) -> None:
        shutil.rmtree(TMP, ignore_errors=True)

    def test_roundtrip(self) -> None:
        self.store.save(Token(refresh_token="r", access_token="a", expires_at=time.time() + 3600, email="x@y.z"))
        loaded = self.store.load()
        self.assertEqual(loaded.refresh_token, "r")
        self.assertEqual(loaded.email, "x@y.z")
        self.assertFalse(loaded.expired)

    def test_missing_file_returns_empty_token(self) -> None:
        self.assertEqual(self.store.load().refresh_token, "")

    def test_expired_when_time_passed(self) -> None:
        self.store.save(Token(refresh_token="r", access_token="a", expires_at=time.time() - 10))
        self.assertTrue(self.store.load().expired)

    def test_corrupted_file_does_not_crash(self) -> None:
        self.path.write_text("{не json", encoding="utf-8")
        self.assertEqual(self.store.load().refresh_token, "")


class EnsureTokenTest(unittest.TestCase):
    def test_without_refresh_token_raises(self) -> None:
        path = TMP / "empty.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        with self.assertRaises(OAuthError) as ctx:
            ensure_access_token(TokenStore(path), "id", "secret")
        self.assertIn("oauth-login", str(ctx.exception))


class AuthStringTest(unittest.TestCase):
    def test_sasl_format(self) -> None:
        value = auth_string("user@example.com", "token123")
        self.assertEqual(
            value,
            b"user=user@example.com\x01auth=Bearer token123\x01\x01",
        )


if __name__ == "__main__":
    unittest.main()