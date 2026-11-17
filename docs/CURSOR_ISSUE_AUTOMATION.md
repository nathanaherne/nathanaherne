# GitHub issue to Cursor automation

`.github/workflows/cursor_issues.yml` is the only workflow that starts a Cursor cloud agent for a GitHub issue. It dispatches `scripts/dispatch_cursor_issue.py`. A second workflow must not be added for the same event.

The agent opens a pull request from a new branch. The issue workflow itself does not merge and does not deploy. On `nathanaherne/nathanaherne` only, a separate workflow can merge a low-risk profile change after the required check passes, then smoke-check the published GitHub profile. Whouno keeps its own engineering workflow.

## When it runs

| Trigger | Result |
| --- | --- |
| Issue opened or reopened | One dispatch, unless `no-cursor` is present |
| Label `cursor-ready` added | Dispatch again after a completed, failed, or blocked run |
| Actions → Cursor issue implementation → Run workflow | Dispatch that issue number |
| Any other label, an edit, a pull request, or `github-actions[bot]` | No dispatch |

A second event waits while the latest status is `queued` for 20 minutes or `running` for 360 minutes. Re-running the workflow is an explicit retry. Status comments are `queued`, `running`, `blocked`, `failed`, and `completed`. `running` links the Cursor agent. `completed` links the pull request.

## Repositories

This automation belongs on:

- `nathanaherne/techaus_com_au`
- `nathanaherne/artofgaslighting_com`
- `nathanaherne/cruisin` (`master`)
- `nathanaherne/nathanaherne`
- `nathanaherne/kids-electric-car`

`nathanaherne/nathanaherne_com` is a separate private repository. It does not get this workflow.

## Profile auto-merge

This section applies only to `nathanaherne/nathanaherne`. The public profile at <https://github.com/nathanaherne> is rendered from `README.md` on `main`. The repository has no GitHub Pages site, no deployment environment, and no hosting credential. Publishing a profile change means merging that file. `profile_publish.yml` then smoke-checks the raw README and the public profile page, and records a `github-profile` deployment whose environment URL is the existing profile. It does not create a new site or change DNS.

`cursor_automerge.yml` is inactive until it is on `main`, because `pull_request_target` and `workflow_run` run the default-branch copy. It checks out that copy and does not check out the pull request. A pull request merges only when every one of these is true:

| Gate | Failure status |
| --- | --- |
| Same repository, `cursor/issue-<number>-*` branch, Cursor agent body, and a matching dispatcher comment on that issue | `untrusted` |
| Diff is only a non-empty `README.md` with no secret material | `needs_human` |
| Latest review from every reviewer is not `CHANGES_REQUESTED` | `needs_human` |
| GitHub reports the branch mergeable and even with `main` | `behind` or `not_mergeable` |
| Check `profile-validation` succeeded on that commit | `checks_failed`, or `missing_ci` when the check is absent |

`missing_ci` is not treated as a pass. A second event for the same commit and status does not merge again and does not repeat the comment. Workflow, script, test, dependency, secret, auth, and any other path stay with a human, and the source issue comment mentions the owner. The activation change is one of those paths, so this workflow cannot merge its own pull request.

Open pull request #2 only adds a sentence to this document, so the gate will not merge it. `techaus_com_au` #3 belongs to another repository and is not evaluated here.

On a push to `main`, the publish workflow tries to require `profile-validation` when the branch has no protection. It does not add required reviews, does not allow force pushes, and does not rewrite protection that already exists. If the token cannot read or write protection, it comments the API error on the linked issue.

## Remaining secret

Whouno already has Actions secret `CURSOR_API_KEY`. GitHub does not let another repository read that value. A personal account has no account-level Actions secret, so each repository above needs its own copy. Create a user or team service-account key at [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations). Team Admin API keys do not work.

```bash
gh secret set CURSOR_API_KEY --repo nathanaherne/techaus_com_au
gh secret set CURSOR_API_KEY --repo nathanaherne/artofgaslighting_com
gh secret set CURSOR_API_KEY --repo nathanaherne/cruisin
gh secret set CURSOR_API_KEY --repo nathanaherne/nathanaherne
gh secret set CURSOR_API_KEY --repo nathanaherne/kids-electric-car
```

`gh` prompts for the value and does not print it. Without the secret, the workflow comments `blocked` on the issue.

The same Integrations page is where the Cursor GitHub app is granted access to a repository. If the app cannot open pull requests, the issue comment shows the Cursor API error. Approve repository access there. This workflow's own token can only read the repo and write issue comments.
