# Release protections

This page records who can start a Ferry release, what a pull request can reach, and which
protections the repository owner has set in GitHub. It answers issues #968 and #969. The
workflow files live in the repository, but most of the protections live in GitHub
settings, so this page lists both.

Observed on 2026-10-10 with the owner's `gh` login (admin on the repository). Every
request was a read. No setting was changed and no secret value was read.

## Result

| Issue | Control | Result |
|---|---|---|
| #969 | A pull request job cannot receive a write token, a secret or a PyPI token | Satisfied after this change. Before it, a pull request from a branch in this repository got a write token and an OIDC token |
| #968 | Only the release path can create `v*.*.*` tags | Failed. No tag rule exists. The only account with write access today is the owner |
| #968 | The PyPI publisher is bound to this repository, the workflow and an environment | Satisfied as far as public evidence shows. The publisher settings page itself was not readable |
| #968 | Release files are signed or attested | Partly. PyPI files carry attestations. The GitHub Release files carry none, and the Windows and macOS files are not signed |
| #968 | `main` is protected | Partly. Pull request and status checks are required, but the owner can bypass both |

## What a pull request can reach

Two workflows run on `pull_request`: `ci.yml` and `release.yml` (only for changes to
`ferry.spec`, `release.yml` or `core/http.py`). No workflow uses `pull_request_target` or
`workflow_run`.

| Workflow and job | Token permissions | Secrets and OIDC | Risk |
|---|---|---|---|
| `ci.yml` `lint-and-test`, `contract-test` | `contents: read` (now explicit) | None | Low |
| `ci.yml` `gates` | `contents: read`, `pull-requests: read` | The default `GITHUB_TOKEN` only | Low |
| `release.yml` `build-windows`, `build-macos` | `contents: read` (was `contents: write` and `id-token: write`) | `WINDOWS_CERT_PFX` is read only by the signing step, which now runs for tags only | Low after the change |
| `release.yml` `release` | `contents: write`, `id-token: write`, on this job only | Environment `release`, PyPI OIDC | Reached only by a tag |
| `auto-tag.yml` `auto-tag` | `contents: read` (was `contents: write`) | Environment `auto-tag`, GitHub App key | Push to `main` or manual run only |
| `docs.yml` `build`, `deploy` | `build` reads. `deploy` has `pages: write` and `id-token: write` | Environment `github-pages` | Push to `main` only |

The release workflow used to declare `contents: write` and `id-token: write` for the whole
file. A pull request from a branch in this repository therefore built with a write token
and the right to ask GitHub for an OIDC token (a short-lived identity proof that PyPI accepts in place of a password). A pull request from a fork never gets
either, because GitHub gives fork pull requests a read-only token and no secrets.
Contributor-controlled code does run in these builds: `ferry.spec` is Python that
PyInstaller (the tool that builds the executables) executes. That is why the write scope now sits on the tag-only `release` job.

A test in `tests/test_packaging.py` keeps this true. It fails if a workflow omits
`permissions:` or uses `pull_request_target`. It also fails if a workflow that runs on
pull requests grants write scope at the top level or on a job without a tag-only `if:`.
A last check fails if the signing step can run outside a tag.

## What was read from GitHub

| Setting | Result |
|---|---|
| Repository visibility | Public. Secret scanning and push protection are on |
| Rulesets | One, `main protection`, active, applies to `refs/heads/main` only. No tag ruleset |
| Branch protection (classic) on `main` | None (404). The ruleset is the only protection |
| Rules on `main` | Pull request required with 0 approvals. Status checks `Python 3.11`, `Python 3.12`, `Python 3.13` required, not strict. No rule against force pushes or deletion. Repository admin role bypasses everything, always |
| Tag protection (classic) | None (404) |
| Environments | `auto-tag`, `release`, `github-pages` |
| `release` environment | Deployment limited to tags matching `v*.*.*`. No required reviewers. Admins can bypass. No secrets or variables |
| `auto-tag` environment | No branch or tag limit, no reviewers. Holds secret `AUTO_TAG_APP_PRIVATE_KEY` and variable `AUTO_TAG_APP_CLIENT_ID` (names only) |
| `github-pages` environment | Deployment limited to branch `main` |
| Actions permissions | Enabled, all actions allowed, SHA pinning not required by the setting (the workflows pin every action anyway) |
| Default workflow token | `read`. Actions cannot approve pull request reviews |
| Fork pull request approval | `all_external_contributors` (every first-time outside contributor needs approval) |
| Repository secrets and variables | None. This means `WINDOWS_CERT_PFX` and `WINDOWS_CERT_PASSWORD` are not set, so the Windows signing step has never run |
| Deploy keys | None |
| Webhooks | None |
| Self-hosted runners | None |
| Collaborators | One, the owner, as admin |
| OIDC subject template | Default (`use_default: true`, no immutable subject) |
| Immutable releases | Off |
| GitHub App installations (`Ferry Auto-Tag Bot`) | Not readable with this token. It needs `admin:org` or the App owner's page |
| PyPI trusted publisher settings | Not readable. PyPI shows them only to project owners |
| Required reviewers on `release` and `auto-tag` | Not set, so there is nothing to read |

The PyPI publisher could be checked indirectly. The attestation PyPI serves for
`discord-ferry` 2.42.0 names publisher kind `GitHub`, repository
`nordscope-fi/Discord-stoat-ferry`, workflow `release.yml` and environment `release`. Its
signing certificate names `release.yml@refs/tags/v2.42.0` and the `push` trigger. That
shows the last release was published under that identity. It does not prove the publisher
rejects other identities, so keep the settings page check below.

## What the release path does

1. A version bump lands on `main`. `auto-tag.yml` runs in the `auto-tag` environment,
   mints a short-lived App token and pushes the tag `vX.Y.Z`.
2. The tag push starts `release.yml`. The build jobs run with a read-only token.
3. The `release` job runs only for a tag. It uses the `release` environment, creates the
   GitHub Release and publishes to PyPI through OIDC, with no stored PyPI token.

PyPI is the Python package index. Windows files are signed only if the signing secrets are set, and today they are not. macOS
files carry PyInstaller's ad-hoc signature and are not notarized (see the install page).

## Gaps and what the owner can do

Each line names where to click. None of these were changed by this work.

- **Block tag creation by others.** Settings, Rules, Rulesets, New tag ruleset: target
  `v*.*.*`, restrict creations, updates and deletions, and add the Ferry Auto-Tag Bot App
  as the only bypass actor.
- **Require a person for the release.** Settings, Environments, `release`, Required
  reviewers: add yourself. Leave "Prevent self-review" off while you are the only
  collaborator. This puts one click between a tag and PyPI.
- **Limit the auto-tag environment.** Settings, Environments, `auto-tag`, Deployment
  branches and tags: select "Selected branches and tags" and add `main`. A manual run of
  `auto-tag.yml` from another branch could otherwise reach the App key.
- **Stop force pushes and deletion of `main`.** Settings, Rules, Rulesets, `main
  protection`: turn on "Restrict deletions" and "Block force pushes".
- **Narrow the bypass.** Same ruleset, Bypass list: change the admin role from "Always"
  to "For pull requests only", so that even the owner goes through the checks.
- **Check the PyPI publisher.** pypi.org, project `discord-ferry`, Manage, Publishing:
  confirm one GitHub publisher with owner `nordscope-fi`, repository
  `Discord-stoat-ferry`, workflow `release.yml` and environment `release`.
- **Check the App scope.** Settings, GitHub Apps, Ferry Auto-Tag Bot, Configure: confirm
  it is installed on this repository only, with Contents read and write.
- **Turn on immutable releases.** Settings, General, Releases: enable "Immutable
  releases" so a published release and its tag cannot be edited.
- **Require pinned actions.** Settings, Actions, General, Actions permissions: tick
  "Require actions to be pinned to a full-length commit SHA".
- **Add the status checks that matter.** Ruleset `main protection`, Require status
  checks: add `Contract test (DCE binary)` and `Instruction-layer gates`.

## What is still open

- Attestations for the GitHub Release files. Adding `actions/attest-build-provenance` to
  the `release` job needs `attestations: write` and `id-token: write` there, and a version
  chosen by the owner, so it is a decision and not part of this change.
- Windows code signing and macOS notarization need certificates the owner has not bought.
- Anyone given write access later can push a branch and start `release.yml` on a pull
  request. With the changes above that run holds a read-only token and no secrets.
