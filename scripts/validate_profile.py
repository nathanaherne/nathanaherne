#!/usr/bin/env python3
"""Validate the profile README checked out by Profile CI.

This is the required check behind auto-merge. The trusted merge workflow
repeats the same README rules on the file it fetches from the GitHub API.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profile_gate  # noqa: E402


def main() -> int:
    path = Path("README.md")
    if not path.is_file():
        print("README.md is missing")
        return 1
    result = profile_gate.assess_readme(path.read_text(encoding="utf-8"))
    if result["ok"]:
        print("README.md passed profile validation")
        return 0
    print("README.md failed profile validation: " + ", ".join(result["reasons"]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
