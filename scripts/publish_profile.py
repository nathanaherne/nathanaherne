#!/usr/bin/env python3
"""Record publication of the GitHub profile README and smoke-check it.

This repository has no Pages site and no external hosting credential. GitHub
renders https://github.com/<owner> from README.md on the default branch. A
failed smoke check is reported as deploy_failed and is not called verified.
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


def _readme_at(token: str, repo: str, sha: str) -> str | None:
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
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(str(payload.get("content") or "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _comments(token: str, repo: str, number: int) -> list[dict]:
    payload = github_api.request(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        "GET",
        token,
    )
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _already(comments: list[dict], status: str, sha: str, *, any_sha: bool = False) -> bool:
    return any(
        marker["status"] == status and (any_sha or marker.get("sha") == sha)
        for marker in profile_gate.parse_automerge_markers(comments)
    )


def _post(token: str, repo: str, number: int, body: str) -> None:
    github_api.request(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments",
        "POST",
        token,
        {"body": body},
    )


def _associated_prs(token: str, repo: str, sha: str, message: str) -> list[dict]:
    numbers = profile_gate.linked_pr_numbers(message)
    found: list[dict] = []
    try:
        pulls = github_api.request(f"https://api.github.com/repos/{repo}/commits/{sha}/pulls", "GET", token)
    except github_api.GitHubError:
        pulls = []
    if isinstance(pulls, list):
        found.extend(item for item in pulls if isinstance(item, dict))
    known = {item.get("number") for item in found}
    for number in numbers:
        if number in known:
            continue
        try:
            payload = github_api.request(f"https://api.github.com/repos/{repo}/pulls/{number}", "GET", token)
        except github_api.GitHubError:
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found


def _issues_for(message: str, pulls: list[dict]) -> list[int]:
    issues = profile_gate.linked_issues(message, "")
    for pull in pulls:
        issues.extend(profile_gate.linked_issues(message, str(pull.get("body") or "")))
    unique: list[int] = []
    for number in issues:
        if number not in unique:
            unique.append(number)
    return unique


def smoke(repo: str, sha: str, expected: str, attempts: int, pause: int) -> dict:
    owner = repo.split("/")[0]
    raw_url = profile_gate.raw_readme_url(repo, sha)
    page_url = profile_gate.profile_url(owner)
    last = {"ok": False, "status": "deploy_failed", "needle": "", "detail": "smoke check did not run"}
    for attempt in range(attempts):
        raw_status, raw_body = github_api.request_text(raw_url)
        page_status, page_body = github_api.request_text(page_url)
        if raw_status != 200:
            last = {
                "ok": False,
                "status": "deploy_failed",
                "needle": "",
                "detail": f"Raw README request returned HTTP {raw_status}",
            }
        elif page_status != 200:
            last = {
                "ok": False,
                "status": "deploy_failed",
                "needle": "",
                "detail": f"Profile page returned HTTP {page_status}",
            }
        else:
            last = profile_gate.assess_publication(
                expected_readme=expected,
                raw_readme=raw_body,
                profile_html=page_body,
            )
        if last["ok"] or attempt == attempts - 1:
            return last
        time.sleep(pause)
    return last


def record_deployment(token: str, repo: str, sha: str, run_id: str, smoke_result: dict) -> tuple[str, str]:
    owner = repo.split("/")[0]
    try:
        deployment = github_api.request(
            f"https://api.github.com/repos/{repo}/deployments",
            "POST",
            token,
            {
                "ref": sha,
                "environment": "github-profile",
                "auto_merge": False,
                "required_contexts": [],
                "description": "Record publication of the GitHub profile README",
                "production_environment": True,
                "transient_environment": False,
            },
        )
    except github_api.GitHubError as exc:
        return "", f"Deployment record failed: HTTP {exc.code} {exc.detail}"
    if not isinstance(deployment, dict) or not deployment.get("id"):
        return "", "Deployment record did not return an id"
    state = "success" if smoke_result["ok"] else "failure"
    detail = str(smoke_result.get("detail") or "")[:140]
    try:
        github_api.request(
            f"https://api.github.com/repos/{repo}/deployments/{deployment['id']}/statuses",
            "POST",
            token,
            {
                "state": state,
                "environment_url": profile_gate.profile_url(owner),
                "log_url": f"https://github.com/{repo}/actions/runs/{run_id}",
                "description": detail,
                "auto_inactive": True,
            },
        )
    except github_api.GitHubError as exc:
        return profile_gate.deployment_page(repo), f"Deployment status failed: HTTP {exc.code} {exc.detail}"
    return profile_gate.deployment_page(repo), ""


def ensure_protection(token: str, repo: str, branch: str) -> dict:
    url = f"https://api.github.com/repos/{repo}/branches/{branch}/protection"
    try:
        current = github_api.request(url, "GET", token)
        action = profile_gate.protection_action(200, current if isinstance(current, dict) else None)
    except github_api.GitHubError as exc:
        action = profile_gate.protection_action(exc.code, None, exc.detail)
    if action["action"] != "create":
        return action
    errors = []
    for payload in action["payloads"]:
        try:
            github_api.request(url, "PUT", token, payload)
            return {"action": "enabled", "detail": "Branch protection now requires profile-validation."}
        except github_api.GitHubError as exc:
            errors.append(f"HTTP {exc.code} {exc.detail}")
    return {"action": "blocked", "detail": " | ".join(errors)}


def _comment_issues(
    token: str,
    repo: str,
    run_id: str,
    issues: list[int],
    status: str,
    sha: str,
    detail: str,
    deployment: str,
    pull_url: str = "",
    pull_number: int | None = None,
) -> None:
    if not issues:
        print(f"status={status} sha={sha} detail={detail}")
        return
    body = profile_gate.render_comment(
        repo=repo,
        run_id=run_id,
        status=status,
        pr=pull_number,
        sha=sha,
        detail=detail,
        pr_url=pull_url,
        deployment_url=deployment,
        profile_url=profile_gate.profile_url(repo.split("/")[0]),
    )
    for number in issues:
        comments = _comments(token, repo, number)
        if _already(comments, status, sha, any_sha=status.startswith("protection_")):
            print(f"duplicate status={status} issue={number} sha={sha}")
            continue
        issue_body = body
        if status.startswith("protection_"):
            issue_body = profile_gate.render_comment(
                repo=repo,
                run_id=run_id,
                status=status,
                issue=number,
                sha=sha,
                detail=detail,
            )
        _post(token, repo, number, issue_body)


def main() -> int:
    repo = os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not repo or not token or not run_id:
        print("REPO, GITHUB_TOKEN, and GITHUB_RUN_ID are required", file=sys.stderr)
        return 1
    event = _event()
    sha = os.environ.get("PUBLISH_SHA", "").strip() or str((event.get("after") or ""))
    if not sha or sha == "0" * 40:
        print(json.dumps({"action": "skip", "reason": "no_commit"}))
        return 0
    commit = github_api.request(f"https://api.github.com/repos/{repo}/commits/{sha}", "GET", token)
    message = ""
    if isinstance(commit, dict):
        message = str(((commit.get("commit") or {}).get("message") or ""))
    repository = github_api.request(f"https://api.github.com/repos/{repo}", "GET", token)
    branch = str(repository.get("default_branch") or "main") if isinstance(repository, dict) else "main"
    pulls = _associated_prs(token, repo, sha, message)
    issues = _issues_for(message, pulls)
    pull_url = next((str(item.get("html_url") or "") for item in pulls if item.get("html_url")), "")
    pull_number = next((item.get("number") for item in pulls if isinstance(item.get("number"), int)), None)
    protection = ensure_protection(token, repo, branch)
    if protection["action"] == "enabled":
        _comment_issues(
            token,
            repo,
            run_id,
            issues,
            "protection_enabled",
            sha,
            protection["detail"] + " Force pushes stay disabled and review requirements were not added.",
            "",
            pull_url,
            pull_number,
        )
    elif protection["action"] == "blocked":
        _comment_issues(
            token,
            repo,
            run_id,
            issues,
            "protection_blocked",
            sha,
            "Branch protection was not changed. "
            + protection["detail"]
            + " A token with administration access must require the profile-validation check on the default branch, "
            "with strict update, without a bypass, and without adding required reviews that would stop low-risk README merges.",
            "",
            pull_url,
            pull_number,
        )
    expected = _readme_at(token, repo, sha)
    if expected is None:
        _comment_issues(
            token,
            repo,
            run_id,
            issues,
            "deploy_failed",
            sha,
            "The merged commit has no readable README.md to publish.",
            "",
            pull_url,
            pull_number,
        )
        return 1
    attempts = int(os.environ.get("PROFILE_SMOKE_ATTEMPTS", "4") or "4")
    pause = int(os.environ.get("PROFILE_SMOKE_PAUSE_SECONDS", "15") or "15")
    result = smoke(repo, sha, expected, max(attempts, 1), max(pause, 0))
    deployment_url, deployment_error = record_deployment(token, repo, sha, run_id, result)
    if result["ok"] and not deployment_error:
        status = "deploy_verified"
        detail = (
            f"{result['detail']} Sentence: {result['needle']}. "
            "There is no separate hosting provider; GitHub publishes this repository's README as the profile."
        )
        exit_code = 0
    elif result["ok"] and deployment_error:
        status = "deploy_unrecorded"
        detail = (
            f"The profile smoke check passed ({result['needle']}), but the deployment record did not. "
            + deployment_error
        )
        exit_code = 1
    else:
        status = "deploy_failed"
        detail = str(result["detail"])
        if deployment_error:
            detail = f"{detail} {deployment_error}"
        exit_code = 1
    _comment_issues(token, repo, run_id, issues, status, sha, detail, deployment_url, pull_url, pull_number)
    print(json.dumps({"status": status, "sha": sha, "issues": issues, "deployment": deployment_url}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
