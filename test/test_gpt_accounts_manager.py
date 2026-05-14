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
        self.assertEqual(get.call_args.kwargs["params"], {"limit": 50, "plan": "free"})
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].access_token, "web-token")
        self.assertEqual(accounts[0].email, "user@example.com")
        self.assertEqual(accounts[0].plan, "free")
        self.assertEqual(accounts[0].status, "active")
        self.assertEqual(accounts[0].gpt_account_id, "123")

    def test_fetch_access_tokens_omits_limit_when_unlimited(self) -> None:
        with patch("services.gpt_accounts_manager.requests.get", return_value=FakeResponse()) as get:
            accounts = fetch_access_tokens("http://manager/", limit=0)

        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"], {})
        self.assertEqual(len(accounts), 1)

    def test_import_sync_removes_inactive_and_stale_manager_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="old-token", email="one@example.com", gpt_account_id="1"),
                GPTAccountsManagerAccount(access_token="stale-token", email="two@example.com", gpt_account_id="2"),
            ])
            service.update_account("old-token", {"status": "限流"})

            result = service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="new-token", email="one@example.com", status="account_deactivated", gpt_account_id="1"),
            ])

        tokens = {item["access_token"] for item in result["items"]}
        self.assertEqual(tokens, set())
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["removed"], 2)

    def test_import_sync_adds_account_when_manager_becomes_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="token-1", email="one@example.com", status="account_deactivated", gpt_account_id="1"),
            ])

            result = service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="token-1", email="one@example.com", status="active", gpt_account_id="1"),
            ])

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["items"][0]["access_token"], "token-1")
        self.assertEqual(result["items"][0]["status"], "正常")
        self.assertEqual(result["items"][0]["manager_status"], "active")

    def test_single_account_sync_matches_by_gpt_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))

            added = service.upsert_gpt_accounts_manager_account(
                GPTAccountsManagerAccount(access_token="token-old", email="one@example.com", status="active", gpt_account_id="1")
            )
            updated = service.upsert_gpt_accounts_manager_account(
                GPTAccountsManagerAccount(access_token="token-new", email="one@example.com", status="active", gpt_account_id="1")
            )

        self.assertEqual(added["added"], 1)
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(updated["removed"], 1)
        self.assertEqual({item["access_token"] for item in updated["items"]}, {"token-new"})

    def test_single_account_sync_removes_by_gpt_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service.upsert_gpt_accounts_manager_account(
                GPTAccountsManagerAccount(access_token="token-1", email="one@example.com", status="active", gpt_account_id="1")
            )

            result = service.upsert_gpt_accounts_manager_account(
                GPTAccountsManagerAccount(access_token="", email="one@example.com", status="token_invalidated", gpt_account_id="1")
            )

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["items"], [])

    def test_single_account_sync_removes_without_source_by_gpt_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service._accounts["token-1"] = service._normalize_account({
                "access_token": "token-1",
                "gpt_account_id": "1",
                "status": "正常",
            })

            result = service.delete_gpt_accounts_manager_account("1")

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["items"], [])

    def test_single_account_sync_matches_existing_account_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            storage = JSONStorageBackend(base / "accounts.json", base / "auth_keys.json")
            storage.save_accounts([
                {
                    "access_token": "token-old",
                    "gpt_account_id": "1",
                    "email": "one@example.com",
                    "type": "free",
                    "status": "正常",
                    "quota": 0,
                }
            ])
            service = AccountService(storage)

            updated = service.upsert_gpt_accounts_manager_account(
                GPTAccountsManagerAccount(access_token="token-new", email="one@example.com", status="active", gpt_account_id="1")
            )
            removed = service.delete_gpt_accounts_manager_account("1")

        self.assertEqual(updated["removed"], 1)
        self.assertEqual({item["access_token"] for item in updated["items"]}, {"token-new"})
        self.assertEqual(removed["removed"], 1)
        self.assertEqual(removed["items"], [])

    def test_import_sync_overrides_local_status_for_same_manager_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            service = AccountService(JSONStorageBackend(base / "accounts.json", base / "auth_keys.json"))
            service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="token-1", email="one@example.com", status="active", gpt_account_id="1"),
            ])
            service.update_account("token-1", {"status": "限流"})

            result = service.upsert_gpt_accounts_manager_accounts([
                GPTAccountsManagerAccount(access_token="token-1", email="one@example.com", status="active", gpt_account_id="1"),
            ])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["items"][0]["status"], "正常")
        self.assertEqual(result["items"][0]["manager_status"], "active")


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

    def test_single_account_sync_route_upserts_by_gpt_account_id(self) -> None:
        class FakeAccountService:
            def upsert_gpt_accounts_manager_account(self, account):
                return {
                    "gpt_account_id": account.gpt_account_id,
                    "access_token": account.access_token,
                    "status": account.status,
                }

        router = api_accounts.create_router()
        route = next(item for item in router.routes if getattr(item, "path", "") == "/api/accounts/sync/gpt-accounts-manager/account")

        old_account_service = api_accounts.account_service
        api_accounts.account_service = FakeAccountService()
        try:
            payload = asyncio.run(route.endpoint(
                api_accounts.GPTAccountsManagerAccountSyncRequest(
                    gpt_account_id="123",
                    access_token="token-1",
                    status="active",
                ),
                authorization="Bearer test-auth",
            ))
        finally:
            api_accounts.account_service = old_account_service

        self.assertEqual(payload["gpt_account_id"], "123")
        self.assertEqual(payload["access_token"], "token-1")
        self.assertEqual(payload["status"], "active")

    def test_single_account_sync_route_deletes_by_gpt_account_id(self) -> None:
        class FakeAccountService:
            def delete_gpt_accounts_manager_account(self, gpt_account_id):
                return {"gpt_account_id": gpt_account_id, "removed": 1}

        router = api_accounts.create_router()
        route = next(item for item in router.routes if getattr(item, "path", "") == "/api/accounts/sync/gpt-accounts-manager/{gpt_account_id}")

        old_account_service = api_accounts.account_service
        api_accounts.account_service = FakeAccountService()
        try:
            payload = asyncio.run(route.endpoint("123", authorization="Bearer test-auth"))
        finally:
            api_accounts.account_service = old_account_service

        self.assertEqual(payload["gpt_account_id"], "123")
        self.assertEqual(payload["removed"], 1)


if __name__ == "__main__":
    unittest.main()
