import os
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from api import accounts as api_accounts
from services.account_service import AccountService
from services.gpt_accounts_manager import GPTAccountsManagerAccount, fetch_access_tokens
from services.storage.json_storage import JSONStorageBackend


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "status": "ok",
            "accounts": [
                {
                    "id": 123,
                    "email": "user@example.com",
                    "plan": "free",
                    "status": "active",
                    "access_token": "web-token",
                },
                {"email": "empty@example.com", "access_token": ""},
            ],
        }


class GPTAccountsManagerClientTests(unittest.TestCase):
    def test_fetch_access_tokens_normalizes_accounts(self) -> None:
        with patch("services.gpt_accounts_manager.requests.get", return_value=FakeResponse()) as get:
            accounts = fetch_access_tokens("http://manager/", limit=50, plan="free")

        get.assert_called_once()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].access_token, "web-token")
        self.assertEqual(accounts[0].email, "user@example.com")
        self.assertEqual(accounts[0].plan, "free")
        self.assertEqual(accounts[0].gpt_account_id, "123")

    def test_import_sync_removes_stale_manager_accounts_and_rotated_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="old-token", email="one@example.com", gpt_account_id="1"),
                GPTAccountsManagerAccount(access_token="stale-token", email="two@example.com", gpt_account_id="2"),
            ])

            result = service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="new-token", email="one@example.com", gpt_account_id="1"),
            ])

        tokens = {item["access_token"] for item in result["items"]}
        self.assertEqual(tokens, {"new-token"})
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["removed"], 2)


class GPTAccountsManagerSyncRouteTests(unittest.TestCase):
    def test_sync_route_uses_external_storage_sync(self) -> None:
        class FakeAccountService:
            class storage:
                syncs_external_accounts = True

            def sync_external_accounts(self):
                return {"synced": True, "added": 2, "removed": 1, "total": 3}

        router = api_accounts.create_router()
        route = next(item for item in router.routes if getattr(item, "path", "") == "/api/accounts/sync/gpt-accounts-manager")

        old_account_service = api_accounts.account_service
        api_accounts.account_service = FakeAccountService()
        try:
            payload = asyncio.run(route.endpoint(authorization="Bearer test-auth"))
        finally:
            api_accounts.account_service = old_account_service

        self.assertTrue(payload["synced"])
        self.assertEqual(payload["added"], 2)
        self.assertEqual(payload["removed"], 1)
        self.assertEqual(payload["total"], 3)


if __name__ == "__main__":
    unittest.main()
