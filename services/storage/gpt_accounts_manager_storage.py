from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from curl_cffi.requests import Session

from services.storage.base import StorageBackend
from services.storage.json_storage import JSONStorageBackend


class GPTAccountsManagerStorageBackend(StorageBackend):
    """GPT Accounts Manager API 存储后端。"""

    syncs_external_accounts = True

    def __init__(
            self,
            *,
            base_url: str,
            accounts_overlay_path: Path,
            auth_keys_path: Path,
            limit: int = 200,
            plan: str = "",
            session_factory: Callable[..., Any] | None = None,
    ):
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url:
            raise ValueError("GPT_ACCOUNTS_MANAGER_URL is required")
        self.limit = max(1, min(5000, int(limit or 200)))
        self.plan = str(plan or "").strip()
        self._local = JSONStorageBackend(accounts_overlay_path, auth_keys_path)
        self._session_factory = session_factory or Session

    def load_accounts(self) -> list[dict[str, Any]]:
        overlay_by_token = {
            str(item.get("access_token") or "").strip(): dict(item)
            for item in self._local.load_accounts()
            if isinstance(item, dict) and str(item.get("access_token") or "").strip()
        }

        accounts = []
        for item in self._fetch_remote_accounts():
            if not isinstance(item, dict):
                continue
            access_token = str(item.get("access_token") or "").strip()
            if not access_token:
                continue
            overlay = overlay_by_token.get(access_token, {})
            account = {
                **overlay,
                "access_token": access_token,
                "type": str(item.get("plan") or overlay.get("type") or "free").strip() or "free",
                "status": str(overlay.get("status") or self._map_status(item.get("status"))),
                "quota": int(overlay.get("quota") or 0),
                "image_quota_unknown": bool(overlay.get("image_quota_unknown", True)),
                "email": str(item.get("email") or overlay.get("email") or "").strip() or None,
                "gpt_account_id": item.get("id") or overlay.get("gpt_account_id"),
                "gpt_accounts_manager_updated_at": self._normalize_time(item.get("updated_at")),
                "source": "gpt-accounts-manager",
            }
            accounts.append(account)
        return accounts

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        self._local.save_accounts(accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        return self._local.load_auth_keys()

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        self._local.save_auth_keys(auth_keys)

    def health_check(self) -> dict[str, Any]:
        try:
            total = len(self._fetch_remote_accounts(limit=1))
            return {
                "status": "healthy",
                "backend": "gpt-accounts-manager",
                "base_url": self.base_url,
                "plan": self.plan,
                "limit": self.limit,
                "probe_accounts": total,
            }
        except Exception as exc:
            return {
                "status": "unhealthy",
                "backend": "gpt-accounts-manager",
                "base_url": self.base_url,
                "error": str(exc),
            }

    def get_backend_info(self) -> dict[str, Any]:
        return {
            "type": "gpt-accounts-manager",
            "description": "GPT Accounts Manager API 账号来源，本地保存账号运行态和 auth keys",
            "base_url": self.base_url,
            "plan": self.plan,
            "limit": self.limit,
            "syncs_external_accounts": True,
        }

    def _fetch_remote_accounts(self, limit: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, object] = {"limit": limit or self.limit}
        if self.plan:
            params["plan"] = self.plan
        session = self._session_factory()
        try:
            response = session.get(
                f"{self.base_url}/api/gpt-accounts/access-tokens",
                params=params,
                headers={"Accept": "application/json"},
                timeout=30,
            )
            if not getattr(response, "ok", False):
                status_code = getattr(response, "status_code", "unknown")
                text = str(getattr(response, "text", ""))[:200]
                raise RuntimeError(f"gpt accounts manager list failed: HTTP {status_code} {text}")
            payload = response.json()
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

        if not isinstance(payload, dict):
            raise RuntimeError("gpt accounts manager payload is invalid")
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            raise RuntimeError("gpt accounts manager accounts payload is invalid")
        return accounts

    @staticmethod
    def _map_status(value: object) -> str:
        status = str(value or "").strip().lower()
        if status == "active":
            return "正常"
        if status in {"account_deactivated", "unauthorized", "token_invalidated"}:
            return "异常"
        return "正常"

    @staticmethod
    def _normalize_time(value: object) -> str | None:
        if value is None:
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if isinstance(value, str):
            text = value.strip()
            return text or None
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
