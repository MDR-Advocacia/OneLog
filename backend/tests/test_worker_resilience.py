import os
import unittest
from unittest import mock

os.environ.setdefault("BROWSER_COMMAND_TIMEOUT_SECONDS", "17")

import worker


class FakeRedis:
    def __init__(self):
        self.setex_calls = []

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        return True


class WorkerResilienceTests(unittest.TestCase):
    def test_gateway_pages_are_not_reported_as_bad_credentials(self):
        cases = (
            "Bem-vindo à Intranet BB UNABLE TO LOGIN Return to Login Page",
            "Loading... /cdn-cgi/challenge-platform/scripts/jsd/main.js",
            "Armadilha Cloudflare: campo de senha ausente",
            (
                "Tela de login do BB não carregou: campo de usuário ausente. "
                "Conteúdo visível: Loading... Ao acessar a Intranet BB"
            ),
        )
        for message in cases:
            with self.subTest(message=message):
                reason, kind = worker.classify_login_failure(message)
                self.assertEqual(kind, "gateway")
                self.assertIn("Cloudflare/portal BB", reason)

    def test_confirmed_failed_login_remains_an_auth_failure(self):
        reason, kind = worker.classify_login_failure(
            "Credencial BB rejeitada (#failedLogin)"
        )
        self.assertEqual(kind, "auth")
        self.assertEqual(reason, "Credencial BB rejeitada")

    def test_queue_lease_is_recreated_while_task_is_running(self):
        fake_redis = FakeRedis()
        with mock.patch.object(worker, "get_redis", return_value=fake_redis):
            worker.renew_queue_lock(14)
        self.assertEqual(
            fake_redis.setex_calls,
            [("lock:queue:14", worker.ACCOUNT_QUEUE_LOCK_TTL_SECONDS, "1")],
        )

    def test_browser_commands_receive_a_finite_timeout(self):
        with mock.patch.object(worker.RemoteConnection, "set_timeout") as setter:
            worker.configure_browser_command_timeout()
        setter.assert_called_once_with(worker.BROWSER_COMMAND_TIMEOUT_SECONDS)

    def test_forced_attempt_abort_only_kills_browser_helpers(self):
        chrome = mock.Mock()
        chrome.name.return_value = "chromedriver"
        chrome.cmdline.return_value = ["/usr/bin/chromedriver"]
        chrome.status.return_value = "sleeping"

        unrelated = mock.Mock()
        unrelated.name.return_value = "python"
        unrelated.cmdline.return_value = ["python", "worker.py"]
        unrelated.status.return_value = "sleeping"

        parent = mock.Mock()
        parent.children.return_value = [chrome, unrelated]
        with mock.patch.object(worker.psutil, "Process", return_value=parent):
            killed = worker.abort_browser_attempt(1, "BB_Robos", "teste")

        self.assertEqual(killed, 1)
        chrome.kill.assert_called_once_with()
        unrelated.kill.assert_not_called()

    def test_attempt_timeout_is_longer_than_command_timeout(self):
        self.assertGreater(
            worker.BROWSER_ATTEMPT_HARD_TIMEOUT_SECONDS,
            worker.BROWSER_COMMAND_TIMEOUT_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
