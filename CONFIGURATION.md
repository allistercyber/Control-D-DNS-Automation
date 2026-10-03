# Control D × Hagezi Sync: guide and reference

This page has two parts. **[Start here](#start-here-what-this-does-in-plain-english)** explains the project in plain English. The **[Configuration reference](#configuration-reference)** below it lists every setting and default.

## Start here: what this does, in plain English

### What it does

[Hagezi](https://github.com/hagezi/dns-blocklists) publishes lists of domains to block or allow. [Control D](https://controld.com) is a DNS filtering service. This project copies Hagezi's lists into folders in your Control D profiles and keeps them up to date. It runs on GitHub (a free account is enough), so you do not need your own server.

### The moving parts

| Term | What it is |
|---|---|
| **Fork** | Your own copy of this repository. Everything runs from your copy. |
| **`config.toml`** | Your shopping list: which Hagezi lists go into which Control D profile and folder. TOML is just a plain text format with lines like `file = "name.json"`. |
| **Secrets** | Private values (such as your Control D API token) that GitHub stores for you and hands to the workflow. They are never shown in logs. |
| **Workflow** | The robot that does the work. GitHub runs it for you when you press a button (or on a schedule, if you turn one on). |
| **Folder** | A group of rules inside a Control D profile. Each folder mirrors exactly one Hagezi list. |

### Before you start

- [ ] A free GitHub account.
- [ ] A Control D account.
- [ ] A Control D profile, with **empty folders** already created in it. The scripts never create profiles or folders.
- [ ] A Control D API token with **write** access (Control D dashboard, API section).

### Quick start

1. **Fork this repository and make your copy private.** If GitHub will not let you switch a fork to private, create a new private repository and push a copy of this one into it. Open the *Actions* tab of your copy and confirm that you want to enable workflows. Details: [README step 1](README.md#1-fork-or-copy-the-repository).
2. **Copy `config.example.toml` to `config.toml` and replace the example names** with your real profile and folder names, then commit the file. The workflow reads the committed copy. Details: [`config.toml`](#configtoml).
3. **Run the offline check** (needs Python 3.11 or newer; makes no network calls): `python scripts/controld_config.py config.toml`. On success it prints a line starting `Configuration OK:`. Otherwise it prints `ERROR:` and what to fix. Details: [`config.toml`](#configtoml).
4. **Add the `CTRLD_API_TOKEN` secret** in *Settings → Secrets and variables → Actions*. Details: [Secrets and variables](#secrets-and-variables).
5. **Run the workflow once by hand on an empty test folder**: *Actions → Sync Control D folders from upstream → Run workflow*. Check the folder in Control D afterwards. Read [First run](#first-run--important) first. Details: [README step 4](README.md#4-run-it-manually).
6. **Only then turn on the schedule.** Details: [Enabling a schedule](#enabling-a-schedule).

### How it keeps your DNS safe

- It refuses to apply a file that is empty, has no `rules` list, or cannot be read. That file is skipped and the run is marked as failed.
- It stops syncing a folder if the sync would remove more than `max_delete_percent` of the rules the folder currently has (default 50). The folder is left untouched and the run is marked as failed. This check only applies to folders that already contain rules. See [`[settings]`](#settings-optional-table).
- Nothing runs on a schedule until you enable it. By default the workflow only runs when you press *Run workflow*.
- Your repository is updated with the new files only after Control D was updated without errors. If a run fails, the next run tries again.

### Things that surprise people

- The folder is made to match the file **exactly**. Rules you added to that folder by hand are removed.
- Names are case-sensitive and must match Control D exactly, including spaces.
- After editing `config.toml`, run the workflow with **force_push** switched on. Otherwise nothing is pushed unless a Hagezi file has changed.
- Keep your fork private. The workflow logs and `config.toml` show which profiles, folders and lists you use.
- The optional email report lists every domain that was added or removed.
- In `config.toml`, put allow-list entries **before** block-list entries. If the same domain is in two lists for one profile, the earlier entry keeps it.

### If something goes wrong

Open the failed run in the *Actions* tab and read the log of the step that has the red cross.

| What you see | Likely cause | What to do |
|---|---|---|
| The workflow stops at **Validate configuration** | `config.toml` is missing from the repository (not committed, still named `config.example.toml`, or committed to a different branch), or it is invalid (bad TOML, a misspelled key, the same file or folder listed twice). The log line starts with `ERROR:` and says which. A missing file reads `configuration file 'config.toml' not found`. | Fix the file, commit it, and run `python scripts/controld_config.py config.toml` locally until it prints `Configuration OK`. |
| The push step logs `Profile '…' not found among the account's … profile(s)` or `Folder '…' not found in profile '…'` | The name in `config.toml` differs from Control D (typo, capital letters, extra space), the folder was never created, or the API token belongs to a different account. | Copy the exact names from the Control D dashboard, create any missing folder there, then run again with **force_push**. |
| Nothing happened after you edited `config.toml` | The push step only runs when a downloaded file differs from the copy in `controld/`. The *Sync files from upstream* log then says `No changes detected.` Also check that you committed `config.toml`: the workflow does not see local edits. | Commit and push `config.toml`, then run the workflow with **force_push** switched on. |
| No email arrived | Email is skipped unless both `EMAIL_USERNAME` and `EMAIL_PASSWORD` are set. The report is also sent only when the push step runs, so a run with no changes sends nothing. If the API token is wrong, the profile list cannot be fetched and the log says `No email body found — skipping email send`. A mail server problem is logged as `Failed to send email:` and does not fail the run. | Set both secrets (for Gmail use an app password), check the log of the push step, and use **force_push** to test. See [Secrets and variables](#secrets-and-variables). |
| The push step logs `Aborting sync for '…'` and `exceeds the … safety threshold` | The sync would remove more of the folder's rules than `max_delete_percent` allows, often because the folder holds hand-made rules or is not the folder you meant. | Check the folder. If the removal is intended, raise `max_delete_percent` in `config.toml`, then run again. |
| The sync step logs `File not found in upstream: …` | The `file` name does not exist in Hagezi's `controld/` folder, or is misspelled. The step retries for several minutes before it gives up. | Pick a name from the [upstream list](https://github.com/hagezi/dns-blocklists/tree/main/controld). |
| The push step logs `CTRLD_API_TOKEN environment variable is not set or empty` or `Cannot fetch profiles` | The secret is missing or misspelled, or the token is invalid. | Re-add the `CTRLD_API_TOKEN` secret with a token that has write access. |

### Glossary

- **Profile**: a set of DNS filtering settings in Control D, with its own folders and rules.
- **Folder**: a group of rules inside a profile. This project fills a folder with one Hagezi list.
- **Rule**: one domain to block or allow.
- **Fork**: your own copy of a GitHub repository.
- **Secret**: a private value stored in GitHub settings that workflows can use but nobody can read back.
- **Workflow**: a job that GitHub runs for you from a file in `.github/workflows/`.
- **Cron**: a schedule written as five fields, such as `23 5,17 * * *` (05:23 and 17:23 UTC every day).
- **API token**: a long password that lets a program, here the workflow, change your Control D account.

---

## Configuration reference

Everything you can configure, with defaults. For an overview and setup steps see [README.md](README.md).

| What | Where |
|---|---|
| Lists to sync, profile/folder targets, safety settings | `config.toml` (you create it from `config.example.toml`) |
| Credentials | GitHub Actions **secrets** |
| Optional email server | GitHub Actions **variables** |
| When the workflow runs | `on:` block of `.github/workflows/sync-controld.yml` |

There is nothing to edit in the Python scripts.

---

## `config.toml`

Read by both stages. Location: `config.toml` in the repository root, or the path in the `CONTROLD_SYNC_CONFIG` environment variable. If the file is missing or invalid, the workflow fails in its *Validate configuration* step before any network call. Unknown keys are rejected, so typos are caught.

Validate locally (Python 3.11+, no network): `python scripts/controld_config.py [path]`.

### `[settings]` (optional table)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `max_delete_percent` | integer 1–100 | `50` | Abort the sync of a folder if it would remove more than this percentage of the folder's current rules. Protects against a truncated upstream file. Only applies when the folder already has rules. |
| `mirror_fallback` | boolean | `true` | If GitHub is unreachable after 5 attempts (60 s apart), download from the official Hagezi build mirror `hagezi-mirror.dnsbunker.org` instead. `false` = GitHub only. |

### `[[lists]]` (required, at least one)

One block per upstream file. Blocks are processed in file order.

| Key | Type | Meaning |
|---|---|---|
| `file` | string | Filename in [hagezi/dns-blocklists → `controld/`](https://github.com/hagezi/dns-blocklists/tree/main/controld), e.g. `spam-tlds-folder.json`. Must be a plain name ending in `.json` (no `/`, no leading `.`). |
| `targets` | array of `{ profile, folder }` | One or more Control D profile/folder pairs that mirror this file. |

Rules enforced at load time:

- each `file` appears once;
- each `profile`/`folder` pair appears in only one `[[lists]]` block (a folder reconciles to exactly one file; two files would keep deleting each other's domains);
- `profile` and `folder` are non-empty strings.

```toml
[settings]
max_delete_percent = 50
mirror_fallback = true

[[lists]]
file = "apple-private-relay-allow-folder.json"
targets = [{ profile = "Home", folder = "Apple Private Relay Allow" }]

[[lists]]
file = "spam-tlds-folder.json"
targets = [
  { profile = "Home",   folder = "Blocked TLDs" },
  { profile = "Travel", folder = "Blocked TLDs" },
]
```

### Profile and folder names

Names are matched **exactly and case-sensitively** against the Control D account the API token belongs to. Copy them from the dashboard (*Profiles* → profile name; folders are listed inside the profile). Folders must already exist; the scripts never create them. A name that is not found fails that target (logged and shown in the email report) and makes the run fail; the logs deliberately do not list the other names in your account.

### Order and cross-folder deduplication

Within one profile, domains already placed by an earlier `[[lists]]` block are removed from later blocks' desired set, so the same domain is not in two folders. A folder only "claims" its domains if it reconciled successfully. Put allow-list files before block-list files.

### Finding available files

Browse the [`controld/` directory](https://github.com/hagezi/dns-blocklists/tree/main/controld) upstream. Any `*.json` there in the Hagezi Control D folder format (`{"group": …, "rules": [{"PK": "<domain>"}, …]}`) can be used. A filename that does not exist upstream makes Stage 1 fail.

---

## Secrets and variables

Set under *Settings → Secrets and variables → Actions*. Only the step that needs a value receives it.

### Secrets

| Name | Required | Default | Used by | Description |
|---|---|---|---|---|
| `CTRLD_API_TOKEN` | **yes** | – | Stage 2 | Control D API token with **write** access. |
| `EMAIL_USERNAME` | no | – | Stage 2 | SMTP login and sender address. Email is skipped unless this and `EMAIL_PASSWORD` are both set. |
| `EMAIL_PASSWORD` | no | – | Stage 2 | SMTP password. For Gmail, an [app password](https://myaccount.google.com/apppasswords), not the account password. |
| `EMAIL_TO` | no | `EMAIL_USERNAME` | Stage 2 | Recipient of the report. |
| `GITHUB_TOKEN` | automatic | – | Stages 1 & 3 | Created by GitHub for each run; **do not create it**. Used to authenticate the upstream listing (higher rate limit) and to commit `controld/`. |

### Variables (not secret)

| Name | Default | Description |
|---|---|---|
| `EMAIL_SMTP_HOST` | `smtp.gmail.com` | SMTP server. |
| `EMAIL_SMTP_PORT` | `465` | `465` = implicit TLS; any other port connects in plain text and upgrades with STARTTLS (certificate verified). |

The email report ("Control D sync report") lists, per profile and folder, every domain added, removed, or skipped, plus errors. Email failures are logged but do not fail the run.

---

## Workflow behaviour (`sync-controld.yml`)

| Aspect | Setting |
|---|---|
| Triggers | `workflow_dispatch` only. The `schedule:` block is **commented out** (see below). No pull-request triggers. |
| Input `force_push` | boolean, default `false`. When `true`, Stage 2 runs even if no downloaded file differs from `controld/`. If nothing differs, Stage 3 then commits nothing. |
| Permissions | `contents: write` only (all other scopes none). |
| Concurrency | group `controld-sync`; runs queue, an in-progress run is **not** cancelled. |
| Timeout | 120 minutes. |
| Step order | checkout → Python 3.14 → `pip install --require-hashes -r requirements.txt` → validate config → Stage 1 → Stage 2 (if changed) → Stage 3 (if changed). |
| Branch | Stage 3 pushes to the branch the run was started on (`GITHUB_REF_NAME`). Run it from your default branch. |

### Enabling a schedule

Uncomment in `.github/workflows/sync-controld.yml`:

```yaml
  schedule:
    - cron: '23 5,17 * * *'   # 05:23 and 17:23 UTC daily
```

Standard cron syntax, UTC; [crontab.guru](https://crontab.guru) helps. Scheduled runs happen only on the default branch, may be delayed under load (avoid `:00`), and on public repositories are disabled by GitHub after 60 days without repository activity. Finish and manually test your configuration first.

### Script environment variables

Set by the workflow; listed for local debugging.

| Variable | Used by | Meaning |
|---|---|---|
| `CONTROLD_SYNC_CONFIG` | both | Path to the config file (default `config.toml`). |
| `GITHUB_TOKEN`, `GITHUB_REPOSITORY` | Stage 1/3 | Required. |
| `GITHUB_REF_NAME` | Stage 3 | Branch to push (default `main`). |
| `GITHUB_OUTPUT` | Stage 1 | Where `changed=true/false` is written. |
| `FORCE_PUSH` | Stage 1 | `true` forces `changed=true`. |
| `CTRLD_API_TOKEN` | Stage 2 | Required. |
| `EMAIL_*` | Stage 2 | See above. |

### Fixed internal values (not configurable without editing the code)

Upstream API `https://api.github.com/repos/hagezi/dns-blocklists/contents/controld`; Control D API `https://api.controld.com`; synced files directory `controld/`; download retries 5 × 60 s; API retries 3 (2 s, 5 s delays); 500 hostnames per add request; 0.5 s between API calls and 0.25 s between deletions. Downloads are only accepted from `*.githubusercontent.com` (or the Hagezi mirror host).

---

## First run — important

`controld/` starts empty (`.gitkeep` only), so on the first run every configured file counts as new and Stage 2 populates each target folder. Stage 2 always reconciles to the file, so **rules that already exist in a target folder but are not in the Hagezi file are deleted** (subject to `max_delete_percent`). Use empty or disposable folders for the first run. Stage 3 commits the files only after Stage 2 succeeded.

Stage 2 is skipped on later runs when no file changed. After changing `config.toml` (new profile, new folder, new list) run the workflow with `force_push` enabled.

---

## `controld/` directory

Holds the last successfully applied copy of each configured file. It is the baseline Stage 1 diffs against and is committed by Stage 3. Files for lists you remove from `config.toml` are not deleted automatically, and their Control D folders are left as they are.

---

## Python dependencies

`requests` is the only direct dependency (both scripts use it). `pip` is also pinned so the installer that enforces the hashes is itself hash-verified.

| File | Purpose |
|---|---|
| `requirements.in` | Human-edited: direct dependencies only. |
| `requirements.txt` | Generated lock file: every package pinned by version **and** SHA-256 hash. |

The workflows install with `pip install --require-hashes -r requirements.txt`. To change or refresh pins:

```bash
pip install pip-tools
pip-compile --allow-unsafe --generate-hashes requirements.in -o requirements.txt
# add --upgrade to also refresh transitive packages (certifi, urllib3, idna, charset-normalizer)
```

`--allow-unsafe` is required: pip-tools otherwise drops the `pip` pin.

## Automated maintenance

- **Dependabot** (`.github/dependabot.yml`): weekly (Monday 06:00 UTC), one grouped PR per ecosystem — GitHub Actions pins (commit SHAs) and pip. New releases are proposed after a 3-day cooldown; security updates are not delayed. Dependabot updates the direct dependency (`requests`) and the `uses:` SHAs; it may not bump transitive pins, so run `pip-compile --upgrade` occasionally.
- **CI** (`.github/workflows/ci.yml`): on pull requests and pushes to `main`: `compileall`, validation of `config.example.toml`, `python -m unittest discover -s tests`. Read-only token, no secrets.
- Action versions in use are the `uses:` lines of the workflow files (the trailing `# vX.Y.Z` comment names the release each SHA corresponds to).

## `clear-actions.yml` (optional)

Deletes old workflow runs with [Mattraks/delete-workflow-runs](https://github.com/Mattraks/delete-workflow-runs) (pinned SHA). Manual only; uncomment its `schedule:` to automate it. Permissions: `actions: write`, `contents: read`. Settings in the file: `retain_days: 30`, `keep_minimum_runs: 5`. Logs are the audit trail of what the sync did, so keep `retain_days` generous.
