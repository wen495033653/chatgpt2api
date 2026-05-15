"""Resolve upstream latest release metadata for image labels."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request

UPSTREAM_REPO = "basketikun/chatgpt2api"
UPSTREAM_API = f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest"
UPSTREAM_REMOTE = f"https://github.com/{UPSTREAM_REPO}.git"


def get_latest_release_tag() -> str:
    request = urllib.request.Request(
        UPSTREAM_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "GitHub Actions",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    tag = payload.get("tag_name")
    if not tag:
        raise SystemExit(f"missing tag_name from {UPSTREAM_API}")
    return tag


def get_tag_revision(tag: str) -> str:
    completed = subprocess.run(
        ["git", "ls-remote", "--tags", UPSTREAM_REMOTE, f"refs/tags/{tag}*"],
        check=True,
        capture_output=True,
        text=True,
    )
    peeled_revision = None
    tag_revision = None

    for line in completed.stdout.splitlines():
        if not line or "\t" not in line:
            continue
        sha, ref = line.split("\t", 1)
        if ref == f"refs/tags/{tag}^{{}}":
            peeled_revision = sha
        elif ref == f"refs/tags/{tag}":
            tag_revision = sha

    revision = peeled_revision or tag_revision
    if not revision:
        raise SystemExit(f"missing tag revision for {tag} from {UPSTREAM_REMOTE}")
    return revision


def main() -> None:
    tag = get_latest_release_tag()
    revision = get_tag_revision(tag)

    output_path = os.environ["GITHUB_OUTPUT"]
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"tag={tag}\n")
        handle.write(f"revision={revision}\n")


if __name__ == "__main__":
    main()
