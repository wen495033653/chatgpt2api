from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.storage.gpt_accounts_manager_storage import GPTAccountsManagerStorageBackend


class FakeResponse:
    def __init__(self, payload: dict, ok: bool = True, status_code: int = 200, text: str = ""):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return self._payload


class FakeSession:
    def __init__(self, payload: dict):
        self.payload = payload
        self.closed = False
        self.calls: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payload)

    def close(self) -> None:
        self.closed = True


class GPTAccountsManagerStorageTests(unittest.TestCase):
    def test_load_accounts_fetches_remote_tokens_and_merges_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            payload = {
                "status": "ok",
                "accounts": [
                    {
                        "id": 123,
                        "email": "user@example.com",
                        "plan": "plus",
                        "status": "active",
                        "access_token": "token-1",
                        "updated_at": "2026-05-11T08:00:00Z",
                    },
                    {"id": 456, "email": "empty@example.com", "plan": "free", "status": "active"},
                ],
            }
            session = FakeSession(payload)
            backend = GPTAccountsManagerStorageBackend(
                base_url="http://gpt-accounts-manager:19318/",
                accounts_overlay_path=base / "accounts.json",
                auth_keys_path=base / "auth_keys.json",
                limit=50,
                session_factory=lambda: session,
            )
            backend.save_accounts([
                {
                    "access_token": "token-1",
                    "status": "限流",
                    "quota": 3,
                    "image_quota_unknown": False,
                    "success": 2,
                }
            ])

            accounts = backend.load_accounts()

            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["access_token"], "token-1")
            self.assertEqual(accounts[0]["type"], "plus")
            self.assertEqual(accounts[0]["status"], "正常")
            self.assertEqual(accounts[0]["quota"], 3)
            self.assertEqual(accounts[0]["email"], "user@example.com")
            self.assertEqual(accounts[0]["gpt_account_id"], 123)
            self.assertEqual(accounts[0]["source"], "gpt-accounts-manager")
            self.assertEqual(session.calls[0]["params"], {"limit": 50})
            self.assertTrue(session.closed)

    def test_load_accounts_skips_inactive_remote_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            payload = {
                "status": "ok",
                "accounts": [
                    {
                        "id": 123,
                        "email": "user@example.com",
                        "plan": "plus",
                        "status": "account_deactivated",
                        "access_token": "token-1",
                        "updated_at": "2026-05-11T08:00:00Z",
                    }
                ],
            }
            session = FakeSession(payload)
            backend = GPTAccountsManagerStorageBackend(
                base_url="http://gpt-accounts-manager:19318/",
                accounts_overlay_path=base / "accounts.json",
                auth_keys_path=base / "auth_keys.json",
                limit=50,
                session_factory=lambda: session,
            )
            backend.save_accounts([
                {
                    "access_token": "token-1",
                    "status": "正常",
                    "quota": 3,
                }
            ])

            accounts = backend.load_accounts()

            self.assertEqual(accounts, [])
            self.assertEqual(session.calls[0]["params"], {"limit": 50})
            self.assertTrue(session.closed)

    def test_load_accounts_allows_large_limit_without_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            payload = {"status": "ok", "accounts": []}
            session = FakeSession(payload)
            backend = GPTAccountsManagerStorageBackend(
                base_url="http://gpt-accounts-manager:19318/",
                accounts_overlay_path=base / "accounts.json",
                auth_keys_path=base / "auth_keys.json",
                limit=6001,
                session_factory=lambda: session,
            )

            accounts = backend.load_accounts()

            self.assertEqual(accounts, [])
            self.assertEqual(backend.limit, 6001)
            self.assertEqual(session.calls[0]["params"], {"limit": 6001})
            self.assertTrue(session.closed)

    def test_load_accounts_omits_limit_when_unlimited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            payload = {"status": "ok", "accounts": []}
            session = FakeSession(payload)
            backend = GPTAccountsManagerStorageBackend(
                base_url="http://gpt-accounts-manager:19318/",
                accounts_overlay_path=base / "accounts.json",
                auth_keys_path=base / "auth_keys.json",
                limit=0,
                session_factory=lambda: session,
            )

            accounts = backend.load_accounts()

            self.assertEqual(accounts, [])
            self.assertEqual(backend.limit, 0)
            self.assertEqual(session.calls[0]["params"], {})
            self.assertTrue(session.closed)

    def test_health_check_reports_remote_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)

            class FailingSession(FakeSession):
                def get(self, url: str, **kwargs) -> FakeResponse:
                    self.calls.append({"url": url, **kwargs})
                    return FakeResponse({"error": "bad"}, ok=False, status_code=500, text="bad")

            backend = GPTAccountsManagerStorageBackend(
                base_url="http://manager",
                accounts_overlay_path=base / "accounts.json",
                auth_keys_path=base / "auth_keys.json",
                session_factory=lambda: FailingSession({}),
            )

            health = backend.health_check()

            self.assertEqual(health["status"], "unhealthy")
            self.assertEqual(health["backend"], "gpt-accounts-manager")
            self.assertIn("HTTP 500", health["error"])


if __name__ == "__main__":
    unittest.main()
