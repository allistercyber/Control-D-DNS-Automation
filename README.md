# Control D × Hagezi Sync

Keeps folders in your [Control D](https://controld.com) profiles in sync with the DNS blocklist/allowlist folders published by [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists), using the Control D API and GitHub Actions. No server required.

This repository is a **template**. It contains no credentials, no account-specific values, and **no scheduled runs**: nothing executes here on its own. You fork it, describe your own profiles and folders in `config.toml`, add your secrets, and then (optionally) switch on a schedule in *your* fork.

> **Keep your fork private.** Workflow logs are public on public repositories and they name the profiles/folders you configure; `config.toml` and the synced `controld/` files also reveal which lists you use. Secrets are never printed, but a private fork avoids exposing the rest.

---

## What it does

Each run has three stages (see `.github/workflows/sync-controld.yml`):

1. **Stage 1 — download** (`scripts/controld_sync.py`): fetches the Hagezi `controld/*.json` files you selected, compares them with the copies stored in this repo's `controld/` directory, and writes any that changed into the working tree.
2. **Stage 2 — push** (`scripts/controld_api_push.py`): runs only if Stage 1 found changes (or you set *force_push*). For every selected file it compares the file with the **live** contents of the matching Control D folder, **adds** missing domains and **removes** domains that are not in the file. Because it works from live state it is idempotent and recovers from interrupted runs. If email is configured, a report is sent.
3. **Stage 3 — commit** (`scripts/controld_sync.py --commit`): commits the updated `controld/` files to your repo **only after Stage 2 succeeded**. If Stage 2 fails, nothing is committed, so the next run detects the same change and retries.

Safety behaviour: empty or unparsable files are refused (they would wipe a folder); a folder sync is aborted if it would delete more than `max_delete_percent` (default 50 %) of the folder's current rules; and within one profile a domain already placed in an earlier folder is skipped for later folders.

## Prerequisites

- A GitHub account (a free account is sufficient).
- A Control D account with at least one profile and **pre-created folders** to receive each list. The scripts never create profiles or folders.
- A Control D API token with **write** access (Control D dashboard, API section).
- Python 3.11+ only if you want to validate your config locally (the workflow itself uses Python 3.14).

## Setup

### 1. Fork or copy the repository

Fork it to your own account (or clone it and push it to a new repository), and make the copy **private**. In a fork, GitHub keeps Actions disabled until you open the *Actions* tab and confirm.

### 2. Create `config.toml`

Copy [`config.example.toml`](config.example.toml) to `config.toml` in the repository root and edit it:

```toml
[[lists]]
file = "spam-tlds-folder.json"          # a file from hagezi/dns-blocklists → controld/
targets = [
  { profile = "My Profile", folder = "Spam TLDs" },   # exact, case-sensitive Control D names
]
```

- `file` — pick from [hagezi/dns-blocklists/controld](https://github.com/hagezi/dns-blocklists/tree/main/controld).
- `targets` — one or more `profile`/`folder` pairs that should mirror that file.
- Put allow-list entries **before** block-list entries (earlier entries win on duplicate domains within a profile).

Check it without network access: `python scripts/controld_config.py config.toml`. Then commit `config.toml`. The full reference, with defaults, is in [CONFIGURATION.md](CONFIGURATION.md).

Without a `config.toml` the workflow stops at its first step, before doing anything.

### 3. Add secrets

*Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Required | Purpose |
|---|---|---|
| `CTRLD_API_TOKEN` | **yes** | Control D API token with write access |
| `EMAIL_USERNAME`, `EMAIL_PASSWORD` | no | SMTP login for the optional email report (defaults to Gmail + an app password) |
| `EMAIL_TO` | no | Report recipient (defaults to `EMAIL_USERNAME`) |

`GITHUB_TOKEN` is provided by GitHub automatically; you do not create it. Optional repository *variables* `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` select a non-Gmail mail server. Details in [CONFIGURATION.md](CONFIGURATION.md#secrets-and-variables).

### 4. Run it manually

*Actions → Sync Control D folders from upstream → Run workflow.*

Before the first run read [First run](CONFIGURATION.md#first-run--important): the push makes each target folder match its file **exactly**, so any rules already in those folders that are not in the Hagezi file are removed. The `max_delete_percent` guard helps but is not a substitute for checking. Start with a spare/test folder.

The *force_push* input re-applies the current files even if upstream has not changed. Use it after editing `config.toml` (for example after adding a second profile), because Stage 2 otherwise only runs when a downloaded file differs from the stored copy.

### 5. Enable scheduled runs in your fork

The workflow ships with the schedule commented out. Once a manual run has succeeded, edit `.github/workflows/sync-controld.yml` in your fork and uncomment the block:

```yaml
on:
  workflow_dispatch:
    ...
  schedule:
    - cron: '23 5,17 * * *'   # 05:23 and 17:23 UTC daily
```

Notes: cron times are UTC; scheduled workflows only run from the default branch; GitHub may delay runs at the top of the hour (hence `:23`); and on public repositories GitHub disables scheduled workflows after 60 days without repository activity.

The optional `clear-actions.yml` workflow (deletes old workflow runs) is manual-only too; uncomment its `schedule:` the same way if you want it.

## Security considerations and limitations

- **Destructive by design.** Folders are reconciled to the file, not merged. Don't point a folder holding hand-made rules at a Hagezi file.
- **Folders must already exist**, with exactly the configured names.
- **Rule action.** The script creates rules with `action.do = 0` and relies on the folder's own action. Check on a test folder that an allow-list folder really behaves as *allow* in your account before depending on it.
- **Stage 2 only runs on change.** Manual edits you make in the Control D dashboard to a managed folder are not corrected until the next upstream change or a *force_push* run.
- **Trust.** You are letting an upstream project and, if the mirror fallback is enabled (default), the official Hagezi build mirror decide what your DNS filter blocks and allows. Review Hagezi's lists and your folder actions accordingly. Set `mirror_fallback = false` to use GitHub only.
- **Token handling.** `CTRLD_API_TOKEN` and the email secrets are given only to the step that needs them. The Actions token has `contents: write` only so Stage 3 can commit `controld/`, and is passed to `git` via environment variables so it is not stored in `.git/config`. Action versions are pinned to commit SHAs; Python dependencies are installed with `--require-hashes`.
- **Logs and email.** Logs contain configured profile/folder names and counts, not secrets or domain lists. The optional email report **does** list every added/removed domain.
- **No `pull_request`/`pull_request_target` triggers** on the operational workflow, so forks' pull requests cannot reach your secrets.

## Maintenance automation

- **Dependabot** (`.github/dependabot.yml`) opens weekly grouped PRs for GitHub Actions pins and for the hash-locked Python requirements. Review and merge them.
- **CI** (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main` with a read-only token and no secrets: it compiles the scripts, validates `config.example.toml`, and runs the unit tests.

## Repository layout

```
.github/
  dependabot.yml             weekly updates: GitHub Actions + pip
  workflows/
    sync-controld.yml        the sync (manual; schedule commented out)
    clear-actions.yml        optional old-run cleanup (manual; schedule commented out)
    ci.yml                   compile + config validation + unit tests
scripts/
  controld_config.py         config.toml loader/validator (shared)
  controld_sync.py           Stage 1 (download) and Stage 3 (commit)
  controld_api_push.py       Stage 2 (Control D API push)
tests/test_config.py         unit tests for the config loader
config.example.toml          example configuration -- copy to config.toml
CONFIGURATION.md             configuration reference
requirements.in / .txt       Python dependencies (source / hash-locked)
controld/                    synced JSON files (empty until your first successful run)
```

## Upstream

All list data comes from [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists). Thanks to Hagezi for maintaining it. This project is not affiliated with Hagezi or Control D.
