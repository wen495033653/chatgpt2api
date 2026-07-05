"""Trigger deployment webhook."""
import hashlib
import hmac
import json
import os
import uuid
import urllib.request


def main():
    webhook_url = os.environ["WEBHOOK_URL"]
    secret = os.environ["WEBHOOK_SECRET"].encode("utf-8")

    payload = {
        "action": "deploy",
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "workflow": os.environ.get("GITHUB_WORKFLOW", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
    }

    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = "sha256=" + hmac.new(secret, data, hashlib.sha256).hexdigest()
    delivery_id = str(uuid.uuid4())

    req = urllib.request.Request(webhook_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "GitHub-Actions-chatgpt2api-Deploy/1.0")
    req.add_header("X-GitHub-Event", "deploy")
    req.add_header("X-GitHub-Delivery", delivery_id)
    req.add_header("X-Hub-Signature-256", signature)

    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status < 200 or resp.status >= 300:
            raise SystemExit(f"webhook status {resp.status}")


if __name__ == "__main__":
    main()
