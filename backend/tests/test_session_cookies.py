import unittest
from types import SimpleNamespace
from datetime import datetime

from session_cookies import load_pool_cookies
import api


class SessionCookieTests(unittest.TestCase):
    def test_empty_and_invalid_payloads_cannot_be_delivered(self):
        invalid_payloads = (
            "[]",
            "not-json",
            "{}",
            '[{"name":"cf_clearance","value":"challenge","domain":".bb.com.br"}]',
            '[{"name":"BBSSOToken","value":"session"}]',
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                account = SimpleNamespace(cookie_payload=payload, last_login_at=datetime.utcnow())
                self.assertEqual(load_pool_cookies(payload), [])
                self.assertFalse(api.can_deliver_cached_cookie(account))

    def test_usable_payload_still_delivers(self):
        payload = '[{"name":"BBSSOToken","value":"session","domain":".bb.com.br"}]'
        account = SimpleNamespace(cookie_payload=payload, last_login_at=datetime.utcnow())
        self.assertEqual(len(load_pool_cookies(payload)), 1)
        self.assertTrue(api.can_deliver_cached_cookie(account))


if __name__ == "__main__":
    unittest.main()
