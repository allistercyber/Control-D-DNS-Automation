# Control D × Hagezi Sync

Keeps folders in your [Control D](https://controld.com) profiles in sync with the DNS blocklist/allowlist folders published by [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists), using the Control D API and GitHub Actions. No server required.

This repository is a **template**. It contains no credentials, no account-specific values, and **no scheduled runs**: nothing executes here on its own. You make your own copy, describe your own profiles and folders in `config.toml`, add your secrets, and then (optionally) switch on a schedule in *your* copy.

New here? Start with the [plain-English guide](CONFIGURATION.md#start-here-what-this-does-in-plain-english) in CONFIGURATION.md.

> **Keep your copy private, which means it cannot be a plain fork.** GitHub does not let you make a fork of a public repository private. Workflow logs are public on public repositories and they name the profiles/folders you configure; `config.toml` and the synced `controld/` files also reveal which lists you use. Secrets are never printed, but a private copy avoids exposing the rest. [Step 1](#1-make-your-own-private-copy) shows how.

---

## What it does

Each run has three stages (see `.github/workflows/sync-controld.yml`):

1. **Stage 1 — download** (`scripts/controld_sync.py`): fetches the Hagezi `controld/*.json` files you selected, compares them with the copies stored in this repo's `controld/` directory, and writes any that changed into the working tree.
2. **Stage 2 — push** (`scripts/controld_api_push.py`): runs only if Stage 1 found changes (or you set *force_push*). For every selected file it compares the file with the **live** contents of the matching Control D folder, **adds** missing domains (each with the block or allow action the file gives it) and **removes** domains that are not in the file. Because it works from live state it is idempotent and recovers from interrupted runs. If email is configured, a report is sent.
3. **Stage 3 — commit** (`scripts/controld_sync.py --commit`): commits the updated `controld/` files to your repo **only after Stage 2 succeeded**. If Stage 2 fails, nothing is committed, so the next run detects the same change and retries.

Safety behaviour: empty, unparsable or ambiguous files are refused (they would wipe a folder or guess a rule's action); a folder sync is aborted if it would delete more than `max_delete_percent` (default 50 %) of the folder's current rules; a mistyped `file` name fails within seconds and tells you the closest real names; and within one profile a domain already placed in an earlier folder is skipped for later folders.

## Prerequisites

- A GitHub account (a free account is sufficient).
- A Control D account with at least one profile and **pre-created folders** to receive each list, one folder per list. The scripts never create profiles or folders. Give each folder the action that matches its list: **Block** for the block lists, **Bypass** (allow) for the `*-allow-folder.json` lists. Control D makes every rule in a folder inherit the folder's action.
- A Control D API token with **write** access and **no Allowed IPs restriction** (see [step 3](#3-add-secrets)).
- Python 3.11+ only if you want to validate your config locally (the workflow itself uses Python 3.14).

## Setup

### 1. Make your own private copy

A fork of a public repository is always public, so use one of these instead:

- **Use this template.** If the green **Use this template** button is shown at the top of this repository, choose *Create a new repository* and set it to **Private**. Easiest, but the copy has no shared history with this repository, so later updates have to be copied over by hand.
- **Duplicate with git.** Works from any copy of this repository and keeps the shared history, so you can merge later updates. First create an empty **private** repository on GitHub (no README, no license), then:

  ```bash
  git clone https://github.com/allistercyber/Control-D-DNS-Automation.git
  cd Control-D-DNS-Automation
  git remote rename origin upstream
  git remote add origin https://github.com/<you>/<your-private-repo>.git
  git push -u origin main
  ```

  If GitHub rejects the push with a message about the `workflow` scope, authenticate with `gh auth login` or SSH, or use a token that has the `workflow` scope: the repository contains workflow files.

If you fork anyway, the fork stays public: logs and `config.toml` will be visible to everyone, and GitHub keeps Actions disabled until you open the *Actions* tab and confirm.

To pick up later fixes in a copy made with git, run `git fetch upstream && git merge upstream/main`. Your `config.toml` is not in this repository, so it never conflicts.

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

Check it without network access: `python scripts/controld_config.py config.toml`. Then commit `config.toml`. No Python installed? Commit it anyway: the **CI** workflow validates a committed `config.toml` on every push and shows a red cross with the reason if it is wrong. The full reference, with defaults, is in [CONFIGURATION.md](CONFIGURATION.md).

Without a valid `config.toml` the sync workflow stops at its *Validate configuration* step, before touching the network.

### 3. Add secrets

First create the token: in the Control D dashboard open [API](https://controld.com/dashboard/api), click **+**, name it, choose **Write**, and leave **Allowed IPs** empty. GitHub's runners use changing addresses, so a token restricted to certain IPs is rejected and the run fails at `Cannot fetch profiles`.

Then add it in *Settings → Secrets and variables → Actions → New repository secret*:

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

### 5. Enable scheduled runs in your copy

The workflow ships with the schedule commented out. Once a manual run has succeeded, edit `.github/workflows/sync-controld.yml` in your copy and uncomment the block:

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
- **Rule actions.** Each rule is created with the action the Hagezi file records for it (`do` 0 = block, 1 = bypass/allow); a file with a missing or unsupported action is refused rather than guessed. Control D also makes rules inherit their folder's action, so create each folder with the matching one. A rule already in a folder is matched by hostname only: if an earlier run left a rule with the wrong action, it is not corrected. Empty that folder in the dashboard and run with *force_push*. Try an allow-list on a spare folder first, and prefer the separate `…-allow-folder.json` / block files over `spam-tlds-combined-folder.json`, which mixes both kinds in one folder.
- **Stage 2 only runs on change.** Manual edits you make in the Control D dashboard to a managed folder are not corrected until the next upstream change or a *force_push* run.
- **Trust.** You are letting an upstream project and, if the mirror fallback is enabled (default), the official Hagezi build mirror decide what your DNS filter blocks and allows. Review Hagezi's lists and your folder actions accordingly. Set `mirror_fallback = false` to use GitHub only.
- **Token handling.** `CTRLD_API_TOKEN` and the email secrets are given only to the step that needs them. The Actions token has `contents: write` only so Stage 3 can commit `controld/`, and is passed to `git` via environment variables so it is not stored in `.git/config`. Action versions are pinned to commit SHAs; Python dependencies are installed with `--require-hashes`.
- **Logs and email.** Logs contain configured profile/folder names and counts, not secrets or domain lists. The optional email report **does** list every added/removed domain.
- **No `pull_request`/`pull_request_target` triggers** on the operational workflow, so forks' pull requests cannot reach your secrets.

## Maintenance automation

- **Dependabot** (`.github/dependabot.yml`) opens weekly grouped PRs for GitHub Actions pins and for the hash-locked Python requirements. Review and merge them.
- **CI** (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main` with a read-only token and no secrets: it compiles the scripts, validates `config.example.toml` and, if you have committed one, your `config.toml`, and runs the unit tests (the Control D API and the upstream download are replaced by in-memory fakes, so they need no network or account).

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
tests/
  test_config.py             config loader
  test_sync.py               Stage 1 / Stage 3 (download checks, change detection, commit)
  test_push.py               Stage 2 (rule parsing, reconciliation, safety checks)
config.example.toml          example configuration -- copy to config.toml
CONFIGURATION.md             configuration reference
requirements.in / .txt       Python dependencies (source / hash-locked)
controld/                    synced JSON files (empty until your first successful run)
```

## Upstream

All list data comes from [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists). Thanks to Hagezi for maintaining it. This project is not affiliated with Hagezi or Control D.
