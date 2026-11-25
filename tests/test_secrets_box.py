"""Секреты в базе: пароль ящика клиента не должен лежать открытым текстом."""

import support  # общий стенд: изолированные данные, сеть запрещена

import unittest

from kpbot.secrets_box import PREFIX, SecretError, decrypt, encrypt, is_encrypted

KEY = "unit-test-secret-key-0123456789"


class SecretsBoxTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        ciphertext = encrypt("app-password-123", KEY)
        self.assertTrue(ciphertext.startswith(PREFIX))
        self.assertNotIn("app-password", ciphertext)
        self.assertEqual(decrypt(ciphertext, KEY), "app-password-123")

    def test_ciphertext_is_different_each_time(self) -> None:
        first = encrypt("same", KEY)
        second = encrypt("same", KEY)
        self.assertNotEqual(first, second)
        self.assertEqual(decrypt(first, KEY), decrypt(second, KEY))

    def test_wrong_key_is_explained(self) -> None:
        ciphertext = encrypt("secret", KEY)
        with self.assertRaises(SecretError) as ctx:
            decrypt(ciphertext, "another-key")
        self.assertIn("KPBOT_SECRET_KEY", str(ctx.exception))

    def test_legacy_plaintext_still_works(self) -> None:
        self.assertEqual(decrypt("old-plain-password", KEY), "old-plain-password")
        self.assertFalse(is_encrypted("old-plain-password"))

    def test_empty_and_already_encrypted_are_untouched(self) -> None:
        self.assertEqual(encrypt("", KEY), "")
        once = encrypt("x", KEY)
        self.assertEqual(encrypt(once, KEY), once)

    def test_unicode_roundtrip(self) -> None:
        value = "пароль-приложения-Яндекс"
        self.assertEqual(decrypt(encrypt(value, KEY), KEY), value)


if __name__ == "__main__":
    unittest.main()
