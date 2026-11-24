import unittest

from kpbot.oauth import TOKEN_ERRORS, OAuthError, auth_string, build_auth_url


class ErrorMappingTest(unittest.TestCase):
    def test_every_known_error_has_human_hint(self) -> None:
        markers = [marker for marker, _ in TOKEN_ERRORS]
        self.assertIn("invalid_client", markers)
        self.assertIn("redirect_uri_mismatch", markers)
        for marker, hint in TOKEN_ERRORS:
            self.assertTrue(hint.strip(), f"пустое пояснение для {marker}")
            self.assertNotEqual(hint, marker, "пояснение не должно быть кодом ошибки")
            self.assertGreater(len(hint), 15, f"слишком короткое пояснение для {marker}")

    def test_hint_is_readable(self) -> None:
        hint = dict(TOKEN_ERRORS)["invalid_client"]
        self.assertIn("Client ID", hint)
        self.assertTrue(hint[0].isupper())


class AuthUrlTest(unittest.TestCase):
    def test_scope_is_mail(self) -> None:
        url = build_auth_url("cid", "http://127.0.0.1:8765/", "st")
        self.assertIn("mail.google.com", url)
        self.assertIn("access_type=offline", url)

    def test_empty_client_id_rejected(self) -> None:
        with self.assertRaises(OAuthError):
            build_auth_url("", "http://127.0.0.1:8765/", "st")


class AuthStringTest(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(auth_string("u@e.com", "t"), b"user=u@e.com\x01auth=Bearer t\x01\x01")


if __name__ == "__main__":
    unittest.main()