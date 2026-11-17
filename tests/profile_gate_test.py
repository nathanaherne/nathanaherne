#!/usr/bin/env python3
"""Regression tests for the profile auto-merge and publish gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import automerge_cursor_pr  # noqa: E402
import profile_gate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AGENT = "bc-9e06c03e-e72a-47e9-8cda-69da156d0be3"
README = (
    "# Profile\n\n"
    "- I am a Business Model Expert, Executive, Board Member & Investor\n"
    "- How to reach me is on my LinkedIn Profile\n"
)


def comment(body):
    return {"body": body, "user": {"login": "github-actions[bot]"}}


def dispatch(status="running", issue=1, agent=AGENT, extra=""):
    agent_field = f" agent={agent}" if agent else ""
    return comment(
        f"<!-- cursor-issue-agent:v1 issue={issue} run=10 status={status}{agent_field} -->\n{extra}"
    )


def context(**overrides):
    pr = {
        "number": 8,
        "state": "open",
        "merged": False,
        "draft": False,
        "title": "Update the profile bio",
        "body": (
            "<!-- CURSOR_AGENT_PR_BODY_BEGIN -->\n"
            f"Cloud agent {AGENT}\n"
            "Closes #1\n"
            "<!-- CURSOR_AGENT_PR_BODY_END -->\n"
        ),
        "base_ref": "main",
        "head_ref": "cursor/issue-1-f36d",
        "head_repo": "nathanaherne/nathanaherne",
        "base_repo": "nathanaherne/nathanaherne",
        "default_branch": "main",
        "mergeable": True,
        "mergeable_state": "clean",
        "sha": "abc123",
        "html_url": "https://github.com/nathanaherne/nathanaherne/pull/8",
        "user_login": "nathanaherne",
    }
    payload = {
        "pr": pr,
        "files": [{"filename": "README.md", "status": "modified"}],
        "readme_text": README,
        "check_runs": [
            {"id": 2, "name": "profile-validation", "status": "completed", "conclusion": "success"}
        ],
        "reviews": [],
        "comments": [dispatch()],
        "issue": {"number": 1, "state": "open", "labels": [], "user_login": "nathanaherne"},
        "ci_finished": True,
    }
    for key, value in overrides.items():
        if key == "pr":
            payload["pr"].update(value)
        else:
            payload[key] = value
    return payload


class SafeMergeTest(unittest.TestCase):
    def test_readme_fix_merges(self):
        decision = profile_gate.decide(context())
        self.assertEqual(decision["action"], "merge")
        self.assertEqual(decision["status"], "merged")
        self.assertEqual(decision["issue"], 1)

    def test_failed_check_does_not_merge(self):
        decision = profile_gate.decide(
            context(
                check_runs=[
                    {
                        "id": 2,
                        "name": "profile-validation",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            )
        )
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["status"], "checks_failed")
        self.assertNotEqual(decision["action"], "merge")

    def test_other_failed_check_does_not_merge(self):
        decision = profile_gate.decide(
            context(
                check_runs=[
                    {"id": 2, "name": "profile-validation", "status": "completed", "conclusion": "success"},
                    {"id": 3, "name": "lint", "status": "completed", "conclusion": "failure"},
                ]
            )
        )
        self.assertEqual(decision["status"], "checks_failed")

    def test_a_later_successful_rerun_can_merge(self):
        decision = profile_gate.decide(
            context(
                check_runs=[
                    {"id": 1, "name": "profile-validation", "status": "completed", "conclusion": "failure"},
                    {"id": 4, "name": "Profile CI / profile-validation", "status": "completed", "conclusion": "success"},
                ]
            )
        )
        self.assertEqual(decision["action"], "merge")

    def test_untrusted_pr_does_not_merge(self):
        fork = profile_gate.decide(context(pr={"head_repo": "someone/nathanaherne"}))
        outsider = profile_gate.decide(context(pr={"head_ref": "feature/readme", "body": "please merge"}))
        unlinked = profile_gate.decide(context(comments=[dispatch(agent="bc-00000000-0000-0000-0000-000000000000")]))
        self.assertEqual(fork["status"], "untrusted")
        self.assertEqual(outsider["status"], "untrusted")
        self.assertEqual(unlinked["status"], "untrusted")
        self.assertEqual(fork["action"], "stop")
        self.assertNotEqual(outsider["issue"], 1)

    def test_completed_dispatch_comment_links_the_pull_request(self):
        body = (
            "<!-- CURSOR_AGENT_PR_BODY_BEGIN -->\nCloses #1\n<!-- CURSOR_AGENT_PR_BODY_END -->\n"
        )
        extra = "- **Pull request:** https://github.com/nathanaherne/nathanaherne/pull/8\n"
        decision = profile_gate.decide(
            context(pr={"body": body}, comments=[dispatch(status="completed", agent="", extra=extra)])
        )
        self.assertEqual(decision["action"], "merge")

    def test_missing_ci_does_not_merge(self):
        finished = profile_gate.decide(context(check_runs=[]))
        waiting = profile_gate.decide(context(check_runs=[], ci_finished=False))
        self.assertEqual(finished["action"], "stop")
        self.assertEqual(finished["status"], "missing_ci")
        self.assertEqual(waiting["action"], "wait")
        self.assertNotEqual(waiting["action"], "merge")

    def test_risky_change_does_not_merge(self):
        workflow = profile_gate.decide(
            context(
                files=[
                    {"filename": "README.md", "status": "modified"},
                    {"filename": ".github/workflows/cursor_automerge.yml", "status": "modified"},
                ]
            )
        )
        dependency = profile_gate.decide(
            context(files=[{"filename": "package.json", "status": "modified"}], readme_text=None)
        )
        secret = profile_gate.decide(
            context(readme_text=README + "\n" + "ghp_" + "a" * 20 + "\n")
        )
        unknown = profile_gate.decide(
            context(files=[{"filename": "notes.txt", "status": "added"}], readme_text=None)
        )
        deleted = profile_gate.decide(
            context(files=[{"filename": "README.md", "status": "removed"}], readme_text=None)
        )
        for decision in (workflow, dependency, secret, unknown, deleted):
            self.assertEqual(decision["action"], "stop")
            self.assertEqual(decision["status"], "needs_human")
            self.assertTrue(decision["mention"])
        self.assertIn("secret_material", secret["reasons"])
        self.assertTrue(any(reason.startswith("path_not_allowlisted:") for reason in unknown["reasons"]))

    def test_existing_cursor_test_pr_and_control_doc_stay_with_a_human(self):
        decision = profile_gate.decide(
            context(
                files=[{"filename": "docs/CURSOR_ISSUE_AUTOMATION.md", "status": "modified"}],
                readme_text=None,
            )
        )
        self.assertEqual(decision["status"], "needs_human")
        self.assertIn("path_not_allowlisted:docs/CURSOR_ISSUE_AUTOMATION.md", decision["reasons"])

    def test_changes_requested_does_not_merge_until_superseded(self):
        blocked = profile_gate.decide(
            context(
                reviews=[
                    {
                        "user": "reviewer",
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-11-17T01:00:00Z",
                    }
                ]
            )
        )
        cleared = profile_gate.decide(
            context(
                reviews=[
                    {
                        "user": "reviewer",
                        "state": "CHANGES_REQUESTED",
                        "submitted_at": "2026-11-17T01:00:00Z",
                    },
                    {"user": "reviewer", "state": "APPROVED", "submitted_at": "2026-11-17T02:00:00Z"},
                ]
            )
        )
        self.assertEqual(blocked["status"], "needs_human")
        self.assertEqual(cleared["action"], "merge")

    def test_behind_or_dirty_branch_does_not_merge(self):
        behind = profile_gate.decide(context(pr={"mergeable_state": "behind", "mergeable": False}))
        dirty = profile_gate.decide(context(pr={"mergeable_state": "dirty", "mergeable": False}))
        self.assertEqual(behind["status"], "behind")
        self.assertEqual(dirty["status"], "not_mergeable")

    def test_duplicate_event_does_not_merge_twice(self):
        first = profile_gate.decide(context())
        self.assertEqual(first["action"], "merge")
        marker = profile_gate.render_comment(
            repo="nathanaherne/nathanaherne",
            run_id="99",
            status="merged",
            pr=8,
            issue=1,
            sha="abc123",
            detail="Merged after the trusted low-risk gate.",
        )
        second = profile_gate.decide(context(comments=[dispatch(), comment(marker)]))
        failed = profile_gate.decide(
            context(
                check_runs=[
                    {
                        "id": 2,
                        "name": "profile-validation",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ]
            )
        )
        repeated = profile_gate.decide(
            context(
                check_runs=[
                    {
                        "id": 2,
                        "name": "profile-validation",
                        "status": "completed",
                        "conclusion": "failure",
                    }
                ],
                comments=[
                    dispatch(),
                    comment(
                        profile_gate.render_comment(
                            repo="nathanaherne/nathanaherne",
                            run_id="99",
                            status="checks_failed",
                            pr=8,
                            issue=1,
                            sha="abc123",
                        )
                    ),
                ],
            )
        )
        self.assertEqual(second["action"], "skip")
        already = profile_gate.decide(context(pr={"merged": True, "state": "closed"}))
        self.assertEqual(already["action"], "record")
        self.assertNotEqual(already["action"], "merge")
        self.assertEqual(failed["status"], "checks_failed")
        self.assertEqual(repeated["action"], "skip")
        detail = automerge_cursor_pr._detail(failed)
        self.assertNotIn("Merged", detail)

    def test_failure_comment_records_the_workflow_and_redacts_secrets(self):
        body = profile_gate.render_comment(
            repo="nathanaherne/nathanaherne",
            run_id="44044216113",
            status="needs_human",
            pr=8,
            issue=1,
            sha="abc123",
            detail="token ghp_" + "b" * 20,
            pr_url="https://github.com/nathanaherne/nathanaherne/pull/8",
            mention=True,
        )
        parsed = profile_gate.parse_automerge_markers([comment(body)])
        self.assertEqual(parsed[0]["status"], "needs_human")
        self.assertEqual(parsed[0]["sha"], "abc123")
        self.assertIn("https://github.com/nathanaherne/nathanaherne/actions/runs/44044216113", body)
        self.assertIn("@nathanaherne", body)
        self.assertNotIn("ghp_" + "b" * 20, body)
        self.assertIn("[redacted]", body)


class PublishAndProtectionTest(unittest.TestCase):
    def test_deploy_failure_is_not_success(self):
        mismatch = profile_gate.assess_publication(
            expected_readme=README,
            raw_readme=README + "\nextra",
            profile_html=README,
        )
        missing_page = profile_gate.assess_publication(
            expected_readme=README,
            raw_readme=README,
            profile_html="<html>profile</html>",
        )
        verified = profile_gate.assess_publication(
            expected_readme=README,
            raw_readme=README.replace("\n", "\r\n"),
            profile_html="<html>" + profile_gate.visible_needle(README) + "</html>",
        )
        self.assertEqual(mismatch["status"], "deploy_failed")
        self.assertEqual(missing_page["status"], "deploy_failed")
        self.assertFalse(mismatch["ok"])
        self.assertFalse(missing_page["ok"])
        self.assertEqual(verified["status"], "deploy_verified")
        self.assertNotEqual(mismatch["status"], "deploy_verified")

    def test_current_profile_readme_is_valid(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue(profile_gate.assess_readme(text)["ok"])
        self.assertIn("Business Model Expert", profile_gate.visible_needle(text))

    def test_protection_is_created_only_when_absent_and_does_not_weaken_reviews(self):
        created = profile_gate.protection_action(404, None)
        denied = profile_gate.protection_action(403, None, "Resource not accessible by integration")
        existing = profile_gate.protection_action(
            200,
            {"required_pull_request_reviews": {"required_approving_review_count": 1}},
        )
        self.assertEqual(created["action"], "create")
        payload = created["payloads"][0]
        self.assertNotIn("required_pull_request_reviews", payload)
        self.assertFalse(payload["allow_force_pushes"])
        self.assertFalse(payload["allow_deletions"])
        self.assertTrue(payload["required_status_checks"]["strict"])
        self.assertEqual(
            payload["required_status_checks"]["checks"][0]["context"],
            "profile-validation",
        )
        self.assertEqual(denied["action"], "blocked")
        self.assertEqual(denied["payloads"], [])
        self.assertIn("Resource not accessible by integration", denied["detail"])
        self.assertEqual(existing["action"], "blocked")
        self.assertEqual(existing["payloads"], [])

    def test_commit_trailers_link_the_issue_and_pull_request(self):
        message = profile_gate.merge_message(8, 1)
        self.assertEqual(profile_gate.linked_issues(message, "Closes #99"), [1])
        self.assertEqual(profile_gate.linked_pr_numbers(message), [8])
        self.assertEqual(
            profile_gate.pull_numbers_from_event({"pull_request": {"number": 8}}, ""),
            [8],
        )
        self.assertEqual(
            profile_gate.pull_numbers_from_event(
                {"workflow_run": {"pull_requests": [{"number": 8}]}},
                "",
            ),
            [8],
        )

    def test_workflows_keep_the_merge_job_on_the_default_branch(self):
        automerge = (ROOT / ".github/workflows/cursor_automerge.yml").read_text(encoding="utf-8")
        publish = (ROOT / ".github/workflows/profile_publish.yml").read_text(encoding="utf-8")
        publish_script = (ROOT / "scripts/publish_profile.py").read_text(encoding="utf-8")
        checks = (ROOT / ".github/workflows/profile_ci.yml").read_text(encoding="utf-8")
        issues = (ROOT / ".github/workflows/cursor_issues.yml").read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", automerge)
        self.assertIn("github.event.repository.default_branch", automerge)
        self.assertNotIn("pull_request.head.sha", automerge)
        self.assertNotIn("administration:", automerge)
        self.assertIn("persist-credentials: false", automerge)
        self.assertIn("-p '*_test.py'", checks)
        self.assertIn("name: profile-validation", checks)
        self.assertIn('"environment": "github-profile"', publish_script)
        self.assertIn("scripts/publish_profile.py", publish)
        self.assertNotIn("pull_request_target:", publish)
        self.assertIn("issues:", issues)
        self.assertNotIn("pull_request_target:", issues)


if __name__ == "__main__":
    unittest.main()
