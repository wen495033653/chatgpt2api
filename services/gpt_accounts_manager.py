from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curl_cffi import requests


@dataclass(frozen=True)
class GPTAccountsManagerAccount:
    access_token: str
    email: str = ""
    plan: str = "free"
    status: str = "active"
    gpt_account_id: str = ""


def fetch_access_tokens(base_url: str, *, limit: int, plan: str = "", timeout: int = 30) -> list[GPTAccountsManagerAccount]:
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("GPT Accounts Manager URL is required")
    if limit <= 0:
        raise ValueError("GPT Accounts Manager limit must be positive")

    params: dict[str, Any] = {"limit": limit}
    if str(plan or "").strip():
        params["plan"] = str(plan).strip()
    response = requests.get(
        f"{base_url}/api/gpt-accounts/access-tokens",
        params=params,
        timeout=timeout,
    )
    if not (200 <= response.status_code < 300):
        raise RuntimeError(f"GPT Accounts Manager sync failed: status={response.status_code}, body={response.text[:1000]}")

    payload = response.json()
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        raise RuntimeError("GPT Accounts Manager response missing accounts list")

    result: list[GPTAccountsManagerAccount] = []
    for item in accounts:
        if not isinstance(item, dict):
            continue
        token = str(item.get("access_token") or "").strip()
        if not token:
            continue
        result.append(
            GPTAccountsManagerAccount(
                access_token=token,
                email=str(item.get("email") or "").strip(),
                plan=str(item.get("plan") or "free").strip() or "free",
                status=str(item.get("status") or "active").strip() or "active",
                gpt_account_id=str(item.get("id") or item.get("gpt_account_id") or "").strip(),
            )
        )
    return result
