from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Condition, Lock
from typing import Any
from datetime import datetime

from services.config import config
from services.gpt_accounts_manager import GPTAccountsManagerAccount, fetch_access_tokens, is_gpt_accounts_manager_account_active
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import StorageBackend
from utils.helper import anonymize_token


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    def __init__(self, storage_backend: StorageBackend):
        self.storage = storage_backend
        self._lock = Lock()
        self._image_slot_condition = Condition(self._lock)
        self._index = 0
        self._accounts = self._load_accounts()
        self._image_inflight: dict[str, int] = {}

    def _load_accounts(self) -> dict[str, dict]:
        accounts = self.storage.load_accounts()
        return {
            normalized["access_token"]: normalized
            for item in accounts
            if (normalized := self._normalize_account(item)) is not None
        }

    def _save_accounts(self) -> None:
        self.storage.save_accounts(list(self._accounts.values()))

    def sync_external_accounts(self) -> dict[str, Any]:
        if not bool(getattr(self.storage, "syncs_external_accounts", False)):
            return {"synced": False, "added": 0, "removed": 0, "total": len(self.list_accounts())}

        loaded_accounts = self._load_accounts()
        with self._lock:
            old_tokens = set(self._accounts)
            new_tokens = set(loaded_accounts)
            self._accounts = loaded_accounts
            self._image_inflight = {
                token: count
                for token, count in self._image_inflight.items()
                if token in new_tokens and int(count or 0) > 0
            }
            if self._accounts:
                self._index %= len(self._accounts)
            else:
                self._index = 0
            self._image_slot_condition.notify_all()
            return {
                "synced": True,
                "added": len(new_tokens - old_tokens),
                "removed": len(old_tokens - new_tokens),
                "total": len(new_tokens),
            }

    @staticmethod
    def _is_image_account_available(account: dict) -> bool:
        if not isinstance(account, dict):
            return False
        if account.get("status") in {"禁用", "限流", "异常"}:
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        return int(account.get("quota") or 0) > 0

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized["access_token"] = access_token
        normalized["type"] = normalized.get("type") or "free"
        normalized["status"] = normalized.get("status") or "正常"
        normalized["quota"] = max(0, int(normalized.get("quota") if normalized.get("quota") is not None else 0))
        normalized["image_quota_unknown"] = bool(normalized.get("image_quota_unknown"))
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        limits_progress = normalized.get("limits_progress")
        normalized["limits_progress"] = limits_progress if isinstance(limits_progress, list) else []
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        return normalized

    def list_tokens(self) -> list[str]:
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(self, excluded_tokens: set[str] | None = None) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(excluded_tokens)
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    def _acquire_next_candidate_token(self, excluded_tokens: set[str] | None = None) -> str:
        with self._image_slot_condition:
            while True:
                if not self._list_ready_candidate_tokens(excluded_tokens):
                    raise RuntimeError("no available image quota")
                tokens = self._list_available_candidate_tokens(excluded_tokens)
                if tokens:
                    access_token = tokens[self._index % len(tokens)]
                    self._index += 1
                    self._image_inflight[access_token] = int(self._image_inflight.get(access_token, 0)) + 1
                    return access_token
                self._image_slot_condition.wait(timeout=1.0)

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            current_inflight = int(self._image_inflight.get(access_token, 0))
            if current_inflight <= 1:
                self._image_inflight.pop(access_token, None)
            else:
                self._image_inflight[access_token] = current_inflight - 1
            self._image_slot_condition.notify_all()

    def get_available_access_token(self) -> str:
        attempted_tokens: set[str] = set()
        while True:
            access_token = self._acquire_next_candidate_token(excluded_tokens=attempted_tokens)
            attempted_tokens.add(access_token)
            try:
                account = self.fetch_remote_info(access_token, "get_available_access_token")
            except Exception:
                self.release_image_slot(access_token)
                continue
            if self._is_image_account_available(account or {}):
                return access_token
            self.release_image_slot(access_token)

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        excluded = set(excluded_tokens or set())
        with self._lock:
            candidates = [
                token
                for account in self._accounts.values()
                if account.get("status") not in {"禁用", "异常"}
                   and (token := account.get("access_token") or "")
                   and token not in excluded
            ]
            if not candidates:
                return ""
            access_token = candidates[self._index % len(candidates)]
            self._index += 1
            return access_token

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_accounts()

    def remove_invalid_token(self, access_token: str, event: str) -> bool:
        if not config.auto_remove_invalid_accounts:
            self.update_account(access_token, {"status": "异常", "quota": 0})
            return False
        removed = bool(self.delete_accounts([access_token])["removed"])
        if removed:
            log_service.add(LOG_TYPE_ACCOUNT, "自动移除异常账号",
                            {"source": event, "token": anonymize_token(access_token)})
        elif access_token:
            self.update_account(access_token, {"status": "异常", "quota": 0})
        return removed

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def list_accounts(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._accounts.values()]

    def list_limited_tokens(self) -> list[str]:
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("status") == "限流"
                   and (token := item.get("access_token") or "")
            ]

    def add_accounts(self, tokens: list[str]) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts()}

        with self._lock:
            added = 0
            skipped = 0
            for access_token in tokens:
                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    current = {}
                else:
                    skipped += 1
                account = self._normalize_account(
                    {
                        **current,
                        "access_token": access_token,
                        "type": str(current.get("type") or "free"),
                    }
                )
                if account is not None:
                    self._accounts[access_token] = account
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        return {"added": added, "skipped": skipped, "items": items}

    def sync_gpt_accounts_manager(self) -> dict[str, Any]:
        base_url = config.gpt_accounts_manager_url
        if not base_url:
            return {"enabled": False, "added": 0, "updated": 0, "removed": 0, "items": self.list_accounts()}
        remote_accounts = fetch_access_tokens(
            base_url,
            limit=config.gpt_accounts_manager_limit,
            plan=config.gpt_accounts_manager_plan,
        )
        result = self.upsert_gpt_accounts_manager_accounts(remote_accounts)
        log_service.add(LOG_TYPE_ACCOUNT, "同步 GPT Accounts Manager 账号",
                        {key: result.get(key) for key in ("added", "updated", "removed", "received")})
        return result

    def upsert_gpt_accounts_manager_accounts(self, remote_accounts: list[GPTAccountsManagerAccount]) -> dict[str, Any]:
        source = "gpt_accounts_manager"
        with self._lock:
            token_by_remote_id = {
                str(item.get("gpt_account_id") or ""): token
                for token, item in self._accounts.items()
                if str(item.get("gpt_account_id") or "")
            }
            remote_tokens: set[str] = set()
            remote_ids: set[str] = set()
            added = 0
            updated = 0
            removed = 0

            def remove_manager_token(token: str) -> None:
                nonlocal removed
                if not token:
                    return
                current = self._accounts.get(token)
                if current is None:
                    return
                self._accounts.pop(token, None)
                self._image_inflight.pop(token, None)
                removed += 1

            for remote in remote_accounts:
                access_token = str(remote.access_token or "").strip()
                if not access_token:
                    continue
                remote_id = str(remote.gpt_account_id or "").strip()
                old_token = token_by_remote_id.get(remote_id) if remote_id else ""

                if not is_gpt_accounts_manager_account_active(remote.status):
                    remove_manager_token(old_token)
                    remove_manager_token(access_token)
                    continue

                remote_tokens.add(access_token)
                if remote_id:
                    remote_ids.add(remote_id)
                if old_token and old_token != access_token:
                    self._accounts.pop(old_token, None)
                    self._image_inflight.pop(old_token, None)
                    removed += 1

                current = self._accounts.get(access_token)
                if current is None:
                    added += 1
                    current = {}
                else:
                    updated += 1

                account = self._normalize_account({
                    **current,
                    "access_token": access_token,
                    "email": remote.email or current.get("email"),
                    "type": remote.plan or current.get("type") or "free",
                    "status": "正常",
                    "quota": current.get("quota", 0),
                    "image_quota_unknown": True,
                    "gpt_account_id": remote_id or current.get("gpt_account_id"),
                    "manager_status": remote.status,
                    "source": source,
                })
                if account is not None:
                    self._accounts[access_token] = account
            stale_tokens = [
                token
                for token, item in self._accounts.items()
                if str(item.get("gpt_account_id") or "")
                   and token not in remote_tokens
                   and str(item.get("gpt_account_id") or "") not in remote_ids
            ]
            for token in stale_tokens:
                self._accounts.pop(token, None)
                self._image_inflight.pop(token, None)
                removed += 1
            if self._accounts:
                self._index %= len(self._accounts)
            else:
                self._index = 0
            self._image_slot_condition.notify_all()
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
        return {
            "enabled": True,
            "received": len(remote_accounts),
            "added": added,
            "updated": updated,
            "removed": removed,
            "items": items,
        }

    def upsert_gpt_accounts_manager_account(self, remote: GPTAccountsManagerAccount) -> dict[str, Any]:
        remote_id = str(remote.gpt_account_id or "").strip()
        if not remote_id:
            raise ValueError("gpt_account_id is required")
        access_token = str(remote.access_token or "").strip()
        if not is_gpt_accounts_manager_account_active(remote.status):
            result = self.delete_gpt_accounts_manager_account(remote_id)
            return {
                "enabled": True,
                "action": "removed",
                "gpt_account_id": remote_id,
                "removed": result["removed"],
                "items": result["items"],
            }
        if not access_token:
            raise ValueError("access_token is required")

        with self._lock:
            old_tokens = [
                token
                for token, item in self._accounts.items()
                if str(item.get("gpt_account_id") or "").strip() == remote_id and token != access_token
            ]
            removed = 0
            for token in old_tokens:
                self._accounts.pop(token, None)
                self._image_inflight.pop(token, None)
                removed += 1

            current = self._accounts.get(access_token)
            added = current is None and not old_tokens
            if current is None:
                current = {}
            account = self._normalize_account({
                **current,
                "access_token": access_token,
                "email": remote.email or current.get("email"),
                "type": remote.plan or current.get("type") or "free",
                "status": "正常",
                "quota": current.get("quota", 0),
                "image_quota_unknown": True,
                "gpt_account_id": remote_id,
                "manager_status": remote.status,
                "source": "gpt_accounts_manager",
            })
            if account is None:
                raise ValueError("account payload is invalid")
            self._accounts[access_token] = account
            if self._accounts:
                self._index %= len(self._accounts)
            else:
                self._index = 0
            self._image_slot_condition.notify_all()
            self._save_accounts()
            items = [dict(item) for item in self._accounts.values()]
        return {
            "enabled": True,
            "action": "added" if added else "updated",
            "gpt_account_id": remote_id,
            "added": 1 if added else 0,
            "updated": 0 if added else 1,
            "removed": removed,
            "item": account,
            "items": items,
        }

    def delete_gpt_accounts_manager_account(self, gpt_account_id: str) -> dict[str, Any]:
        remote_id = str(gpt_account_id or "").strip()
        if not remote_id:
            raise ValueError("gpt_account_id is required")
        with self._lock:
            target_tokens = [
                token
                for token, item in self._accounts.items()
                if str(item.get("gpt_account_id") or "").strip() == remote_id
            ]
            removed = 0
            for token in target_tokens:
                self._accounts.pop(token, None)
                self._image_inflight.pop(token, None)
                removed += 1
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                self._image_slot_condition.notify_all()
            items = [dict(item) for item in self._accounts.values()]
        return {
            "enabled": True,
            "action": "removed",
            "gpt_account_id": remote_id,
            "removed": removed,
            "items": items,
        }

    def delete_accounts(self, tokens: list[str]) -> dict:
        target_set = set(token for token in tokens if token)
        if not target_set:
            return {"removed": 0, "items": self.list_accounts()}
        with self._lock:
            removed = sum(self._accounts.pop(token, None) is not None for token in target_set)
            for token in target_set:
                self._image_inflight.pop(token, None)
            if removed:
                if self._accounts:
                    self._index %= len(self._accounts)
                else:
                    self._index = 0
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, f"删除 {removed} 个账号", {"removed": removed})
            items = [dict(item) for item in self._accounts.values()]
        return {"removed": removed, "items": items}

    def update_account(self, access_token: str, updates: dict) -> dict | None:
        if not access_token:
            return None
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            account = self._normalize_account({**current, **updates, "access_token": access_token})
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                            {"token": anonymize_token(access_token), "status": account.get("status")})
            return dict(account)
        return None

    def mark_image_result(self, access_token: str, success: bool) -> dict | None:
        if not access_token:
            return None
        self.release_image_slot(access_token)
        with self._lock:
            current = self._accounts.get(access_token)
            if current is None:
                return None
            next_item = dict(current)
            next_item["last_used_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            image_quota_unknown = bool(next_item.get("image_quota_unknown"))
            if success:
                next_item["success"] = int(next_item.get("success") or 0) + 1
                if not image_quota_unknown:
                    next_item["quota"] = max(0, int(next_item.get("quota") or 0) - 1)
                if not image_quota_unknown and next_item["quota"] == 0:
                    next_item["status"] = "限流"
                    next_item["restore_at"] = next_item.get("restore_at") or None
                elif next_item.get("status") == "限流":
                    next_item["status"] = "正常"
            else:
                next_item["fail"] = int(next_item.get("fail") or 0) + 1
            account = self._normalize_account(next_item)
            if account is None:
                return None
            if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
                self._accounts.pop(access_token, None)
                self._save_accounts()
                log_service.add(LOG_TYPE_ACCOUNT, "自动移除限流账号", {"token": anonymize_token(access_token)})
                return None
            self._accounts[access_token] = account
            self._save_accounts()
            return dict(account)
        return None

    def fetch_remote_info(self, access_token: str, event: str = "fetch_remote_info") -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        try:
            from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI
            result = OpenAIBackendAPI(access_token).get_user_info()
        except InvalidAccessTokenError:
            self.remove_invalid_token(access_token, event)
            raise
        return self.update_account(access_token, result)

    def refresh_accounts(self, access_tokens: list[str]) -> dict[str, Any]:
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            return {"refreshed": 0, "errors": [], "items": self.list_accounts()}

        refreshed = 0
        errors = []
        max_workers = min(10, len(access_tokens))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.fetch_remote_info, token, "refresh_accounts"): token
                for token in access_tokens
            }
            for future in as_completed(futures):
                try:
                    account = future.result()
                except Exception as exc:
                    errors.append({"token": anonymize_token(futures[future]), "error": str(exc)})
                    continue
                if account is not None:
                    refreshed += 1

        return {
            "refreshed": refreshed,
            "errors": errors,
            "items": self.list_accounts(),
        }


account_service = AccountService(config.get_storage_backend())
