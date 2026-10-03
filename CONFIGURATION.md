# Configuration reference

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
