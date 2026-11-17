#!/usr/bin/env python3
"""Merge one authorised low-risk Cursor pull request, or record why it stopped.

This script is the entrypoint for `.github/workflows/cursor_automerge.yml`.
That workflow runs from the default branch only. The script reads the pull
request through the GitHub API and does not check out or execute its code.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import github_api  # noqa: E402
import profile_gate  # noqa: E402


def _event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() else default


def _comments(token: str, repo: str, number: int) -> list[dict]:
    comments: list[dict] = []
    for page in range(1, 6):
        batch = github_api.request(
            f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100&page={page}",
            "GET",
            token,
        )
        if not isinstance(batch, list) or not batch:
            break
        comments.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return comments


def _check_runs(token: str, repo: str, sha: str) -> list[dict]:
    runs: list[dict] = []
    for page in range(1, 6):
        payload = github_api.request(
            f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs?per_page=100&page={page}",
            "GET",
            token,
        )
        if not isinstance(payload, dict):
            break
        batch = payload.get("check_runs") or []
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            runs.append(
                {
                    "id": item.get("id") or 0,
                    "name": item.get("name") or "",
                    "status": item.get("status") or "",
                    "conclusion": item.get("conclusion") or "",
                }
            )
        if len(batch) < 100:
            break
    return runs


def _reviews(token: str, repo: str, number: int) -> list[dict]:
    reviews: list[dict] = []
    for page in range(1, 6):
        batch = github_api.request(
            f"https://api.github.com/repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}",
            "GET",
            token,
        )
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            reviews.append(
                {
                    "user": ((item.get("user") or {}).get("login") or ""),
                    "state": item.get("state") or "",
                    "submitted_at": item.get("submitted_at") or "",
                }
            )
        if len(batch) < 100:
            break
    return reviews


def _files(token: str, repo: str, number: int) -> list[dict]:
    files: list[dict] = []
    for page in range(1, 11):
        batch = github_api.request(
            f"https://api.github.com/repos/{repo}/pulls/{number}/files?per_page=100&page={page}",
            "GET",
            token,
        )
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if isinstance(item, dict):
                files.append({"filename": item.get("filename") or "", "status": item.get("status") or ""})
        if len(batch) < 100:
            break
    return files


def _readme(token: str, repo: str, sha: str) -> str | None:
    try:
        payload = github_api.request(
            f"https://api.github.com/repos/{repo}/contents/README.md?ref={sha}",
            "GET",
            token,
        )
    except github_api.GitHubError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not payload.get("content"):
        return None
    try:
        return base64.b64decode(str(payload["content"])).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _snapshot(token: str, repo: str, number: int, default_branch: str) -> dict:
    payload = github_api.request(f"https://api.github.com/repos/{repo}/pulls/{number}", "GET", token)
    if not isinstance(payload, dict):
        raise RuntimeError("pull request lookup returned an unexpected payload")
    head = payload.get("head") or {}
    base = payload.get("base") or {}
    head_repo = ((head.get("repo") or {}).get("full_name") or "")
    base_repo = ((base.get("repo") or {}).get("full_name") or "")
    pr = {
        "number": payload.get("number"),
        "state": payload.get("state") or "",
        "merged": bool(payload.get("merged")),
        "draft": bool(payload.get("draft")),
        "title": payload.get("title") or "",
        "body": payload.get("body") or "",
        "base_ref": base.get("ref") or "",
        "head_ref": head.get("ref") or "",
        "head_repo": head_repo,
        "base_repo": base_repo,
        "default_branch": default_branch,
        "mergeable": payload.get("mergeable"),
        "mergeable_state": payload.get("mergeable_state") or "",
        "sha": head.get("sha") or "",
        "merge_commit_sha": payload.get("merge_commit_sha") or "",
        "html_url": payload.get("html_url") or "",
        "user_login": ((payload.get("user") or {}).get("login") or ""),
    }
    source = profile_gate.provenance(pr, [])
    issue_number = source["issue"]
    comments: list[dict] = []
    issue = {}
    if isinstance(issue_number, int):
        try:
            issue_payload = github_api.request(
                f"https://api.github.com/repos/{repo}/issues/{issue_number}",
                "GET",
                token,
            )
        except github_api.GitHubError as exc:
            if exc.code != 404:
                raise
            issue_payload = {}
        if isinstance(issue_payload, dict) and issue_payload.get("number") and "pull_request" not in issue_payload:
            labels = [str((item or {}).get("name") or "") for item in issue_payload.get("labels") or []]
            issue = {
                "number": issue_payload.get("number"),
                "state": issue_payload.get("state") or "",
                "labels": labels,
                "user_login": ((issue_payload.get("user") or {}).get("login") or ""),
            }
            comments = _comments(token, repo, issue_number)
            # Pull-request conversation comments hold duplicate auto-merge markers.
            comments.extend(_comments(token, repo, number))
        else:
            comments = _comments(token, repo, number)
    else:
        comments = _comments(token, repo, number)
    files = _files(token, repo, number)
    readme = None
    if any(item["filename"] == "README.md" and item["status"] != "removed" for item in files):
        readme = _readme(token, repo, pr["sha"])
    return {
        "pr": pr,
        "files": files,
        "readme_text": readme,
        "check_runs": _check_runs(token, repo, pr["sha"]) if pr["sha"] else [],
        "reviews": _reviews(token, repo, number),
        "comments": comments,
        "issue": issue,
    }


def _fallback_numbers(token: str, repo: str, event: dict) -> list[int]:
    run = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else {}
    sha = str(run.get("head_sha") or "")
    branch = str(run.get("head_branch") or "")
    numbers: list[int] = []
    if sha:
        try:
            pulls = github_api.request(f"https://api.github.com/repos/{repo}/commits/{sha}/pulls", "GET", token)
        except github_api.GitHubError:
            pulls = []
        if isinstance(pulls, list):
            numbers.extend(item["number"] for item in pulls if isinstance(item, dict) and isinstance(item.get("number"), int))
    if not numbers and branch:
        owner = repo.split("/")[0]
        pulls = github_api.request(
            f"https://api.github.com/repos/{repo}/pulls?state=open&head={owner}:{branch}&per_page=20",
            "GET",
            token,
        )
        if isinstance(pulls, list):
            numbers.extend(item["number"] for item in pulls if isinstance(item, dict) and isinstance(item.get("number"), int))
    return numbers


def _post(token: str, repo: str, number: int, body: str) -> None:
    github_api.request(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments",
        "POST",
        token,
        {"body": body},
    )


def _comment(token: str, repo: str, run_id: str, decision: dict, pr: dict, sha: str, detail: str) -> None:
    body = profile_gate.render_comment(
        repo=repo,
        run_id=run_id,
        status=decision["status"],
        pr=decision["pr"],
        issue=decision["issue"],
        sha=sha,
        detail=detail,
        pr_url=str(pr.get("html_url") or ""),
        mention=bool(decision.get("mention")),
    )
    _post(token, repo, int(decision["pr"]), body)
    if decision.get("comment_on_issue") and decision.get("issue") not in (None, decision["pr"]):
        _post(token, repo, int(decision["issue"]), body)
    if decision.get("mention"):
        try:
            github_api.request(
                f"https://api.github.com/repos/{repo}/pulls/{decision['pr']}/requested_reviewers",
                "POST",
                token,
                {"reviewers": [repo.split("/")[0]]},
            )
        except github_api.GitHubError as exc:
            print(f"review request was not created: HTTP {exc.code} {exc.detail}", file=sys.stderr)


def _merge(token: str, repo: str, decision: dict, pr: dict) -> tuple[str, str]:
    issue = int(decision["issue"])
    try:
        result = github_api.request(
            f"https://api.github.com/repos/{repo}/pulls/{decision['pr']}/merge",
            "PUT",
            token,
            {
                "sha": pr["sha"],
                "merge_method": "squash",
                "commit_title": github_api.redact(str(pr.get("title") or f"Pull request #{decision['pr']}"))[:250],
                "commit_message": profile_gate.merge_message(int(decision["pr"]), issue),
            },
        )
    except github_api.GitHubError as exc:
        if exc.code == 405 and "already merged" in exc.detail.lower():
            return str(pr.get("sha") or ""), ""
        return "", f"GitHub merge failed: HTTP {exc.code} {exc.detail}"
    if not isinstance(result, dict) or not result.get("merged"):
        return "", "GitHub merge did not report merged=true"
    return str(result.get("sha") or ""), ""


def _detail(decision: dict) -> str:
    reasons = ", ".join(decision.get("reasons") or [])
    status = decision["status"]
    if status == "needs_human":
        return f"Human approval is required ({reasons}). The pull request was not merged."
    if status == "checks_failed":
        return f"Required checks failed ({reasons}). The pull request was not merged."
    if status == "missing_ci":
        return "The required profile-validation check is not present. A missing check is not treated as success."
    if status == "untrusted":
        return f"This pull request is not an authorised Cursor issue result ({reasons}). It was not merged."
    if status == "behind":
        return "The branch is behind the default branch. It was not merged."
    if status == "not_mergeable":
        return f"GitHub does not report the pull request as mergeable ({reasons}). It was not merged."
    if status == "not_ready":
        return "The pull request is still a draft. It was not merged."
    return reasons


def evaluate_once(token: str, repo: str, number: int, default_branch: str, ci_finished: bool) -> dict:
    context = _snapshot(token, repo, number, default_branch)
    context["ci_finished"] = ci_finished
    context["decision"] = profile_gate.decide(context)
    return context


def main() -> int:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not repo or not token or not run_id:
        print("REPO, GITHUB_TOKEN, and GITHUB_RUN_ID are required", file=sys.stderr)
        return 1
    event = _event()
    manual = os.environ.get("PULL_NUMBER", "").strip()
    numbers = profile_gate.pull_numbers_from_event(event, manual)
    if not numbers and event_name == "workflow_run":
        numbers = _fallback_numbers(token, repo, event)
    if not numbers:
        print(json.dumps({"action": "skip", "reason": "no_pull_request"}))
        return 0
    repository = github_api.request(f"https://api.github.com/repos/{repo}", "GET", token)
    default_branch = str(repository.get("default_branch") or "main") if isinstance(repository, dict) else "main"
    run = event.get("workflow_run") if isinstance(event.get("workflow_run"), dict) else {}
    ci_finished = event_name == "workflow_run" and str(run.get("event") or "") == "pull_request"
    if event_name == "workflow_dispatch":
        ci_finished = True
    waits = _int_env("AUTOMERGE_WAIT_ATTEMPTS", 6)
    pause = _int_env("AUTOMERGE_WAIT_SECONDS", 20)
    exit_code = 0
    for number in numbers:
        context = evaluate_once(token, repo, number, default_branch, ci_finished)
        decision = context["decision"]
        for _attempt in range(max(waits - 1, 0)):
            if decision["action"] != "wait":
                break
            time.sleep(pause)
            context = evaluate_once(token, repo, number, default_branch, ci_finished)
            decision = context["decision"]
        pr = context["pr"]
        print(json.dumps({"pr": number, "sha": pr.get("sha"), **decision}))
        if decision["action"] == "merge":
            sha, error = _merge(token, repo, decision, pr)
            if error:
                decision = {**decision, "action": "stop", "status": "merge_failed", "mention": False}
                _comment(token, repo, run_id, decision, pr, str(pr.get("sha") or ""), error)
                exit_code = 1
                continue
            _comment(
                token,
                repo,
                run_id,
                decision,
                pr,
                sha,
                "Merged after the trusted low-risk gate and profile-validation check passed. "
                "Profile publication is verified by the Profile publish workflow.",
            )
            continue
        if decision["action"] == "record":
            _comment(
                token,
                repo,
                run_id,
                decision,
                pr,
                str(pr.get("merge_commit_sha") or pr.get("sha") or ""),
                "The pull request is already merged. Profile publication is verified by the Profile publish workflow.",
            )
            continue
        if decision["action"] == "stop":
            _comment(token, repo, run_id, decision, pr, str(pr.get("sha") or ""), _detail(decision))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
