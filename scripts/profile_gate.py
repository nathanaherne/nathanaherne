#!/usr/bin/env python3
"""Pure decisions for merging a Cursor pull request and checking the profile.

The auto-merge workflow imports this module from the default branch. It does
not execute pull-request code. A missing check, an unknown path, or an
unlinked pull request does not merge.
"""

from __future__ import annotations

import re
from html import unescape

REQUIRED_CHECK = "profile-validation"
GITHUB_ACTIONS_APP_ID = 15368
ALLOWED_PATHS = frozenset({"README.md"})
MIN_README_CHARS = 40
AUTOMERGE_MARKER = "cursor-automerge:v1"
DISPATCH_MARKER_RE = re.compile(r"<!--\s*cursor-issue-agent:v1\b([^>]*)-->", re.I)
AUTOMERGE_MARKER_RE = re.compile(r"<!--\s*cursor-automerge:v1\b([^>]*)-->", re.I)
FIELD_RE = re.compile(r"(\w+)=([^\s]+)")
BRANCH_RE = re.compile(r"^cursor/issue-(\d+)-[A-Za-z0-9._-]+$")
CLOSING_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)")
AGENT_RE = re.compile(
    r"\bbc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)
TOKEN_RE = re.compile(
    r"(crsr_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"sk_live_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
TRAILER_ISSUE_RE = re.compile(r"(?m)^Cursor-Automerge-Issue:\s*(\d+)\s*$")
TRAILER_PR_RE = re.compile(r"(?m)^Cursor-Automerge-PR:\s*(\d+)\s*$")
DISPATCH_STATUSES = frozenset({"queued", "running", "blocked", "failed", "completed"})
AUTOMERGE_STATUSES = frozenset(
    {
        "merged",
        "merge_failed",
        "checks_failed",
        "missing_ci",
        "untrusted",
        "needs_human",
        "behind",
        "not_mergeable",
        "not_ready",
        "deploy_verified",
        "deploy_failed",
        "deploy_unrecorded",
        "protection_blocked",
        "protection_enabled",
    }
)
PASSING_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
MERGEABLE_STATES = frozenset({"clean", "has_hooks"})


def _fields(match: re.Match[str] | None) -> dict[str, str]:
    if not match:
        return {}
    return dict(FIELD_RE.findall(match.group(1)))


def parse_dispatch_markers(comments: list[dict]) -> list[dict]:
    found = []
    for comment in comments:
        fields = _fields(DISPATCH_MARKER_RE.search(comment.get("body") or ""))
        status = fields.get("status", "").lower()
        if not fields.get("issue", "").isdigit() or status not in DISPATCH_STATUSES:
            continue
        found.append(
            {
                "issue": int(fields["issue"]),
                "status": status,
                "agent": fields.get("agent", ""),
                "body": comment.get("body") or "",
                "user": ((comment.get("user") or {}).get("login") or ""),
            }
        )
    return found


def parse_automerge_markers(comments: list[dict]) -> list[dict]:
    found = []
    for comment in comments:
        fields = _fields(AUTOMERGE_MARKER_RE.search(comment.get("body") or ""))
        status = fields.get("status", "").lower()
        if status not in AUTOMERGE_STATUSES:
            continue
        found.append(
            {
                "issue": int(fields["issue"]) if fields.get("issue", "").isdigit() else None,
                "pr": int(fields["pr"]) if fields.get("pr", "").isdigit() else None,
                "sha": fields.get("sha", ""),
                "status": status,
            }
        )
    return found


def assess_readme(text: str | None) -> dict:
    if text is None:
        return {"ok": False, "reasons": ["readme_unreadable"]}
    if "\x00" in text:
        return {"ok": False, "reasons": ["readme_unreadable"]}
    stripped = text.strip()
    reasons = []
    if len(stripped) < MIN_README_CHARS:
        reasons.append("readme_too_small")
    if TOKEN_RE.search(text):
        reasons.append("secret_material")
    return {"ok": not reasons, "reasons": reasons}


def classify_risk(files: list[dict], readme_text: str | None) -> dict:
    """Fail closed. Only a readable profile README is low risk."""
    if not files:
        return {"risk": "unknown", "reasons": ["empty_diff"]}
    reasons = []
    saw_readme = False
    for item in files:
        path = str(item.get("filename") or "")
        status = str(item.get("status") or "")
        if path in ALLOWED_PATHS:
            saw_readme = True
            if status == "removed":
                reasons.append("readme_deleted")
            continue
        reasons.append(f"path_not_allowlisted:{path}")
    if saw_readme and "readme_deleted" not in reasons:
        reasons.extend(assess_readme(readme_text)["reasons"])
    if not saw_readme:
        reasons.append("readme_not_in_diff")
    if not reasons:
        return {"risk": "low", "reasons": []}
    control = any(
        reason.startswith("path_not_allowlisted:.github/")
        or reason.startswith("path_not_allowlisted:scripts/")
        or reason.startswith("path_not_allowlisted:tests/")
        or "secret_material" in reason
        or reason == "readme_deleted"
        for reason in reasons
    )
    return {"risk": "high" if control else "unknown", "reasons": reasons}


def _agent_ids(text: str) -> set[str]:
    return {match.group(0).lower() for match in AGENT_RE.finditer(text or "")}


def provenance(pr: dict, comments: list[dict]) -> dict:
    """Link a same-repo PR to a Cursor issue dispatch. Fail closed otherwise."""
    if pr.get("draft"):
        return {"ok": False, "pending": False, "issue": None, "reasons": ["draft"]}
    if pr.get("base_ref") != pr.get("default_branch"):
        return {"ok": False, "pending": False, "issue": None, "reasons": ["base_branch"]}
    if not pr.get("head_repo") or pr.get("head_repo") != pr.get("base_repo"):
        return {"ok": False, "pending": False, "issue": None, "reasons": ["fork"]}
    match = BRANCH_RE.match(str(pr.get("head_ref") or ""))
    if not match:
        return {"ok": False, "pending": False, "issue": None, "reasons": ["branch_name"]}
    issue = int(match.group(1))
    body = str(pr.get("body") or "")
    closed = {int(number) for number in CLOSING_RE.findall(body)}
    if "CURSOR_AGENT_PR_BODY_BEGIN" not in body:
        return {"ok": False, "pending": False, "issue": issue, "reasons": ["missing_cursor_body"]}
    if issue not in closed:
        return {"ok": False, "pending": False, "issue": issue, "reasons": ["issue_not_linked"]}
    markers = [item for item in parse_dispatch_markers(comments) if item["issue"] == issue]
    if not markers:
        return {"ok": False, "pending": False, "issue": issue, "reasons": ["dispatch_not_recorded"]}
    body_agents = _agent_ids(body)
    pr_url = str(pr.get("html_url") or "")
    branch = str(pr.get("head_ref") or "")
    for marker in markers:
        linked_agent = marker["agent"] and marker["agent"].lower() in body_agents
        linked_completion = marker["status"] == "completed" and (
            (pr_url and pr_url in marker["body"]) or f"`{branch}`" in marker["body"]
        )
        if linked_agent or linked_completion:
            return {"ok": True, "pending": False, "issue": issue, "reasons": []}
    if any(marker["status"] in ("queued", "running") for marker in markers):
        return {"ok": False, "pending": True, "issue": issue, "reasons": ["dispatch_link_pending"]}
    return {"ok": False, "pending": False, "issue": issue, "reasons": ["dispatch_not_linked"]}


def canonical_check_name(name: str) -> str:
    if name == REQUIRED_CHECK or name.endswith("/ " + REQUIRED_CHECK):
        return REQUIRED_CHECK
    return name


def latest_checks(check_runs: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for run in check_runs:
        name = canonical_check_name(str(run.get("name") or ""))
        current = latest.get(name)
        if current is None or int(run.get("id") or 0) >= int(current.get("id") or 0):
            latest[name] = {**run, "name": name}
    return list(latest.values())


def evaluate_checks(check_runs: list[dict], *, ci_finished: bool) -> dict:
    runs = latest_checks(check_runs)
    required = next((item for item in runs if item.get("name") == REQUIRED_CHECK), None)
    if required is None:
        if ci_finished:
            return {"state": "missing_ci", "reasons": ["required_check_absent"]}
        return {"state": "pending", "reasons": ["required_check_not_reported"]}
    status = str(required.get("status") or "")
    conclusion = str(required.get("conclusion") or "")
    if status != "completed":
        return {"state": "pending", "reasons": [f"required_check_{status or 'pending'}"]}
    if conclusion != "success":
        return {"state": "failed", "reasons": [f"required_check_{conclusion or 'not_success'}"]}
    for run in runs:
        if run.get("name") == REQUIRED_CHECK:
            continue
        run_status = str(run.get("status") or "")
        conclusion = str(run.get("conclusion") or "")
        if run_status != "completed":
            return {"state": "pending", "reasons": [f"check_pending:{run.get('name')}"]}
        if conclusion not in PASSING_CONCLUSIONS:
            return {"state": "failed", "reasons": [f"check_failed:{run.get('name')}:{conclusion}"]}
    return {"state": "passed", "reasons": []}


def evaluate_reviews(reviews: list[dict]) -> dict:
    latest: dict[str, tuple[str, str]] = {}
    for review in reviews:
        user = str(review.get("user") or "")
        if not user:
            continue
        stamp = str(review.get("submitted_at") or "")
        state = str(review.get("state") or "").upper()
        previous = latest.get(user)
        if previous is None or stamp >= previous[0]:
            latest[user] = (stamp, state)
    requesting = sorted(user for user, (_stamp, state) in latest.items() if state == "CHANGES_REQUESTED")
    if requesting:
        return {"ok": False, "reasons": [f"changes_requested:{user}" for user in requesting]}
    return {"ok": True, "reasons": []}


def _recorded_statuses(markers: list[dict], pr_number: int, sha: str) -> set[str]:
    return {
        marker["status"]
        for marker in markers
        if marker.get("pr") == pr_number and marker.get("sha") == sha
    }


def decide(context: dict) -> dict:
    """Return the only action the workflow may take for this snapshot.

    `merge` is returned only for a linked, low-risk, current, reviewed-clean
    pull request whose required check succeeded. Every other path stops or waits.
    """
    pr = context["pr"]
    number = int(pr["number"])
    sha = str(pr.get("sha") or "")
    comments = context.get("comments") or []
    recorded = _recorded_statuses(parse_automerge_markers(comments), number, sha)
    if "merged" in recorded:
        return _decision("skip", "merged", number, None, ["already_merged"])
    if pr.get("merged"):
        source = provenance(pr, comments)
        if not source["ok"]:
            return _decision("skip", "merged", number, source["issue"], ["already_merged"])
        return _decision(
            "record",
            "merged",
            number,
            source["issue"],
            ["already_merged"],
            comment_on_issue=True,
        )
    if pr.get("state") != "open":
        return _decision("skip", "merged", number, None, ["pull_request_closed"])

    source = provenance(pr, comments)
    issue = source["issue"]
    if source["reasons"] == ["draft"]:
        if "not_ready" in recorded:
            return _decision("skip", "not_ready", number, issue, ["draft"])
        return _decision("stop", "not_ready", number, issue, ["draft"])
    if not source["ok"]:
        status = "untrusted"
        if source["pending"]:
            return _decision("wait", "untrusted", number, issue, source["reasons"])
        if status in recorded:
            return _decision("skip", status, number, issue, source["reasons"])
        return _decision("stop", status, number, issue, source["reasons"], comment_on_issue=issue is not None)

    issue_payload = context.get("issue") or {}
    labels = {str(label).lower() for label in issue_payload.get("labels") or []}
    if "no-cursor" in labels or str(issue_payload.get("user_login") or "") == "github-actions[bot]":
        reason = "no_cursor" if "no-cursor" in labels else "bot_authored_issue"
        if "needs_human" in recorded:
            return _decision("skip", "needs_human", number, issue, [reason])
        return _decision("stop", "needs_human", number, issue, [reason], comment_on_issue=True)

    risk = classify_risk(context.get("files") or [], context.get("readme_text"))
    if risk["risk"] != "low":
        if "needs_human" in recorded:
            return _decision("skip", "needs_human", number, issue, risk["reasons"])
        return _decision("stop", "needs_human", number, issue, risk["reasons"], comment_on_issue=True)

    reviews = evaluate_reviews(context.get("reviews") or [])
    if not reviews["ok"]:
        if "needs_human" in recorded:
            return _decision("skip", "needs_human", number, issue, reviews["reasons"])
        return _decision("stop", "needs_human", number, issue, reviews["reasons"], comment_on_issue=True)

    state = str(pr.get("mergeable_state") or "")
    if state == "behind":
        if "behind" in recorded:
            return _decision("skip", "behind", number, issue, ["behind"])
        return _decision("stop", "behind", number, issue, ["behind"], comment_on_issue=True)
    if state == "dirty":
        if "not_mergeable" in recorded:
            return _decision("skip", "not_mergeable", number, issue, ["dirty"])
        return _decision("stop", "not_mergeable", number, issue, ["dirty"], comment_on_issue=True)

    checks = evaluate_checks(context.get("check_runs") or [], ci_finished=bool(context.get("ci_finished")))
    if checks["state"] == "pending":
        return _decision("wait", "checks_failed", number, issue, checks["reasons"])
    if checks["state"] == "missing_ci":
        if "missing_ci" in recorded:
            return _decision("skip", "missing_ci", number, issue, checks["reasons"])
        return _decision("stop", "missing_ci", number, issue, checks["reasons"], comment_on_issue=True)
    if checks["state"] != "passed":
        if "checks_failed" in recorded:
            return _decision("skip", "checks_failed", number, issue, checks["reasons"])
        return _decision("stop", "checks_failed", number, issue, checks["reasons"], comment_on_issue=True)
    if pr.get("mergeable") is True and state in MERGEABLE_STATES:
        return _decision("merge", "merged", number, issue, [], comment_on_issue=True)
    if state in ("", "unknown") or pr.get("mergeable") is None:
        return _decision("wait", "not_mergeable", number, issue, [state or "mergeability_unknown"])
    if "not_mergeable" in recorded:
        return _decision("skip", "not_mergeable", number, issue, [state or "not_mergeable"])
    return _decision("stop", "not_mergeable", number, issue, [state or "not_mergeable"], comment_on_issue=True)


def _decision(
    action: str,
    status: str,
    pr_number: int,
    issue: int | None,
    reasons: list[str],
    *,
    comment_on_issue: bool = False,
) -> dict:
    return {
        "action": action,
        "status": status,
        "pr": pr_number,
        "issue": issue,
        "reasons": reasons,
        "comment_on_issue": comment_on_issue,
        "mention": status == "needs_human" and action == "stop",
    }


def visible_needle(readme: str) -> str:
    text = re.sub(r"<!--.*?-->", " ", readme or "", flags=re.S)
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        line = re.sub(r"`([^`]*)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"[^A-Za-z0-9 ,.'&:/+-]", " ", line)
        line = re.sub(r"\s+", " ", line).strip(" -")
        if len(re.findall(r"[A-Za-z]", line)) >= 24:
            return line
    return ""


def _normalise(text: str) -> str:
    return (text or "").replace("\r\n", "\n").strip()


def assess_publication(*, expected_readme: str, raw_readme: str, profile_html: str) -> dict:
    needle = visible_needle(expected_readme)
    if not needle:
        return {"ok": False, "status": "deploy_failed", "needle": "", "detail": "README has no verifiable sentence"}
    if _normalise(expected_readme) != _normalise(raw_readme):
        return {
            "ok": False,
            "status": "deploy_failed",
            "needle": needle,
            "detail": "The published raw README does not match the merged commit",
        }
    page = unescape(profile_html or "")
    if needle not in page:
        return {
            "ok": False,
            "status": "deploy_failed",
            "needle": needle,
            "detail": "The public GitHub profile does not contain the merged README sentence",
        }
    return {"ok": True, "status": "deploy_verified", "needle": needle, "detail": "Profile smoke check passed"}


def render_comment(
    *,
    repo: str,
    run_id: str,
    status: str,
    pr: int | None = None,
    issue: int | None = None,
    sha: str = "",
    detail: str = "",
    pr_url: str = "",
    deployment_url: str = "",
    profile_url: str = "",
    mention: bool = False,
) -> str:
    fields = [f"status={status}"]
    if issue is not None:
        fields.append(f"issue={issue}")
    if pr is not None:
        fields.append(f"pr={pr}")
    if sha:
        fields.append(f"sha={sha}")
    # Keep the machine-readable fields in a stable order for the parser.
    ordered = []
    for key in ("issue", "pr", "sha", "status"):
        for field in fields:
            if field.startswith(key + "="):
                ordered.append(field)
    lines = [
        f"<!-- {AUTOMERGE_MARKER} {' '.join(ordered)} -->",
        "### Cursor auto-merge",
        f"- **Status:** {status}",
        f"- **Workflow run:** https://github.com/{repo}/actions/runs/{run_id}",
    ]
    if pr_url:
        lines.append(f"- **Pull request:** {pr_url}")
    if sha:
        lines.append(f"- **SHA:** `{sha}`")
    if deployment_url:
        lines.append(f"- **Deployment:** {deployment_url}")
    if profile_url:
        lines.append(f"- **Profile:** {profile_url}")
    if detail:
        lines.append(f"- **Detail:** {TOKEN_RE.sub('[redacted]', detail)[:2000]}")
    if mention:
        owner = repo.split("/")[0]
        lines.append(f"- **Notification:** @{owner} this change needs a human before it can merge.")
    return "\n".join(lines) + "\n"


def initial_protection() -> dict:
    return {
        "required_status_checks": {
            "strict": True,
            "checks": [{"context": REQUIRED_CHECK, "app_id": GITHUB_ACTIONS_APP_ID}],
        },
        "enforce_admins": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "required_conversation_resolution": False,
        "lock_branch": False,
        "allow_fork_syncing": True,
    }


def protection_put_bodies() -> list[dict]:
    with_app = {**initial_protection(), "restrictions": None}
    without_app = {
        **with_app,
        "required_status_checks": {"strict": True, "checks": [{"context": REQUIRED_CHECK}]},
    }
    without_restrictions = {key: value for key, value in without_app.items() if key != "restrictions"}
    return [with_app, without_app, without_restrictions]


def _protection_meets_bar(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    checks = (body.get("required_status_checks") or {}).get("checks") or []
    contexts = {str(item.get("context")) for item in checks if isinstance(item, dict)}
    strict = bool((body.get("required_status_checks") or {}).get("strict"))
    force = body.get("allow_force_pushes")
    force_enabled = bool(force.get("enabled")) if isinstance(force, dict) else bool(force)
    reviews = body.get("required_pull_request_reviews")
    return REQUIRED_CHECK in contexts and strict and not force_enabled and not reviews


def protection_action(status_code: int, body: dict | None, error_detail: str = "") -> dict:
    """Decide how to touch branch protection without weakening an owner rule.

    An existing review requirement or an unrecognised protection document is
    left as it is. The only automatic write is the initial document, and only
    when GitHub reports that the branch is unprotected.
    """
    if status_code == 404:
        return {"action": "create", "payloads": protection_put_bodies(), "detail": ""}
    if status_code == 403:
        return {"action": "blocked", "payloads": [], "detail": error_detail or "HTTP 403"}
    if status_code == 200 and _protection_meets_bar(body):
        return {"action": "noop", "payloads": [], "detail": "required check already enforced"}
    detail = "existing branch protection was left unchanged"
    if error_detail:
        detail = f"{detail}: {error_detail}"
    return {"action": "blocked", "payloads": [], "detail": detail}


def _unique(numbers: list[int]) -> list[int]:
    unique: list[int] = []
    for number in numbers:
        if number not in unique:
            unique.append(number)
    return unique


def issue_from_branch(pr: dict) -> int | None:
    match = BRANCH_RE.match(str(pr.get("head_ref") or ""))
    if not match:
        return None
    return int(match.group(1))


def linked_issues(commit_message: str, pr_body: str = "") -> list[int]:
    trailers = [int(value) for value in TRAILER_ISSUE_RE.findall(commit_message or "")]
    if trailers:
        return _unique(trailers)
    numbers = [int(value) for value in CLOSING_RE.findall(pr_body or "")]
    numbers.extend(int(value) for value in CLOSING_RE.findall(commit_message or ""))
    return _unique(numbers)


def linked_pr_numbers(commit_message: str) -> list[int]:
    numbers = [int(value) for value in TRAILER_PR_RE.findall(commit_message or "")]
    match = re.search(r"Merge pull request #(\d+)", commit_message or "")
    if match:
        numbers.append(int(match.group(1)))
    squash = re.search(r"\(#(\d+)\)\s*$", commit_message or "", re.M)
    if squash:
        numbers.append(int(squash.group(1)))
    unique: list[int] = []
    for number in numbers:
        if number not in unique:
            unique.append(number)
    return unique


def pull_numbers_from_event(event: dict, manual: str) -> list[int]:
    if str(manual or "").isdigit():
        return [int(manual)]
    pull = event.get("pull_request")
    if isinstance(pull, dict) and isinstance(pull.get("number"), int):
        return [pull["number"]]
    run = event.get("workflow_run")
    numbers = []
    if isinstance(run, dict):
        for item in run.get("pull_requests") or []:
            if isinstance(item, dict) and isinstance(item.get("number"), int):
                numbers.append(item["number"])
    return numbers


def merge_message(pr_number: int, issue_number: int) -> str:
    return (
        f"Auto-merge low-risk Cursor pull request #{pr_number} for issue #{issue_number}.\n\n"
        f"Cursor-Automerge-PR: {pr_number}\n"
        f"Cursor-Automerge-Issue: {issue_number}\n"
    )


def profile_url(owner: str) -> str:
    return f"https://github.com/{owner}"


def deployment_page(repo: str) -> str:
    return f"https://github.com/{repo}/deployments/github-profile"


def raw_readme_url(repo: str, sha: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{sha}/README.md"
