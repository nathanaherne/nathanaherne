#!/usr/bin/env python3
"""Small GitHub REST helper for the profile auto-merge scripts.

The dispatcher keeps its own client. This module is only used by the
auto-merge and publish workflows.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

SECRET_RE = re.compile(
    r"(crsr_[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|"
    r"sk_live_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16})",
)


class GitHubError(Exception):
    def __init__(self, code: int, detail: str, method: str, url: str):
        self.code = code
        self.detail = detail
        self.method = method
        self.url = url
        super().__init__(f"HTTP {code} {method} {url}: {detail}")


def redact(text: str) -> str:
    return SECRET_RE.sub("[redacted]", text or "")


def request(url: str, method: str, token: str, payload: dict | None = None, accept: str = "") -> object:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {
        "Accept": accept or "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "nathanaherne-profile-gate",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    call = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(call, timeout=60) as response:
            raw = response.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode(errors="replace")[:800])
        raise GitHubError(exc.code, detail, method, url) from None


def request_text(url: str, timeout: int = 30) -> tuple[int, str]:
    call = urllib.request.Request(url, headers={"User-Agent": "nathanaherne-profile-gate"})
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        return exc.code, detail
