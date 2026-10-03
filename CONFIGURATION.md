# Configuration Guide

>  💡 **Privacy & security note:** This repo runs entirely within your own GitHub Actions environment. Your API token is used only for outbound requests to the Control D API, and email credentials only for outbound SMTP — neither is logged or persisted beyond the runner. For maximum privacy and security it is recommended to keep your fork **private** — this prevents your profile names, folder names, and workflow configuration from being publicly visible.

This document covers every value you need to change to make this workflow work for your own Control D account.

---

## Files to edit

| File | What to change |
|------|----------------|
| `scripts/controld_api_push.py` | `FILE_MAPPINGS` — which upstream files map to which of your profiles/folders |
| `scripts/controld_sync.py` | `TARGET_FILES` — which upstream files to download (should match `FILE_MAPPINGS` keys) |

---

## `FILE_MAPPINGS` in `controld_api_push.py`

This is the primary configuration. It tells Stage 2 which Control D folder to push each JSON file's domains into.

### Format

```python
FILE_MAPPINGS: Dict[str, List[Tuple[str, str]]] = {
    "upstream-filename.json": [
        ("Your Profile Name", "Your Folder Name"),
    ],
}
```

Each entry maps one upstream filename to a list of `(profile_name, folder_name)` pairs. One file can sync to multiple profiles simultaneously — just add more pairs to the list.

### How to find your profile and folder names

1. Log into the [Control D dashboard](https://controld.com/dashboard)
2. Go to **Profiles** in the left sidebar
3. The profile name is shown at the top of each profile — **copy it exactly, it is case-sensitive**
4. Inside a profile, open the folder list on the left — **copy the folder name exactly**

### Example

```python
FILE_MAPPINGS: Dict[str, List[Tuple[str, str]]] = {
    # Sync Apple Private Relay list to one profile
    "apple-private-relay-allow-folder.json": [
        ("Home", "Apple Private Relay Block"),
    ],

    # Sync spam TLDs to two profiles at once
    "spam-tlds-folder.json": [
        ("Home",   "Blocked TLDs"),
        ("Travel", "Blocked TLDs"),
    ],
}
```

### Available upstream files

The hagezi repo publishes a large number of Control D-compatible JSON files covering everything from tracker allow-lists to spam TLDs. Browse the full list here:

**[hagezi/dns-blocklists — controld/](https://github.com/hagezi/dns-blocklists/tree/main/controld)**

Add any filename from that directory to `TARGET_FILES` and `FILE_MAPPINGS` to start syncing it. This workflow is not limited to any particular subset — use whatever files suit your setup.

### Processing order and cross-folder deduplication

Within a single profile, domains claimed by an **earlier** entry in `FILE_MAPPINGS` are excluded from **later** entries. This prevents the same domain appearing in both an allow folder and a block folder at the same time.

**Practical rule:** put allow-folders before block-folders in `FILE_MAPPINGS`.

---

## `TARGET_FILES` in `controld_sync.py`

This controls which files Stage 1 downloads from the upstream repo. It should match the keys you have in `FILE_MAPPINGS` — no more, no less.

```python
TARGET_FILES: List[str] = [
    "apple-private-relay-allow-folder.json",
    "spam-tlds-folder.json",
    # add or remove filenames here
]
```

If a filename is in `FILE_MAPPINGS` but not in `TARGET_FILES`, Stage 2 will fail to find the file and skip it with an error.

---

## Schedule

The workflow runs at 05:00 and 17:00 UTC by default. To change this, edit `.github/workflows/sync-controld.yml`:

```yaml
on:
  schedule:
    - cron: '0 5  * * *'   # 05:00 UTC
    - cron: '0 17 * * *'   # 17:00 UTC
```

Standard cron syntax applies. [crontab.guru](https://crontab.guru) is useful for building expressions.

> **Before your first scheduled run**, finish setting up `FILE_MAPPINGS`, `TARGET_FILES`, and the required GitHub secrets. Use **Actions → Sync Control D folders from upstream → Run workflow** to verify everything works before relying on the schedule.

---

## Required GitHub secrets

Set these under **Settings → Secrets and variables → Actions**:

#### 🔑 Required Secrets

| Secret | Value | Description |
|--------|-------|-------------|
| `GITHUB_TOKEN` | *(auto-provided)* | Provided automatically by GitHub Actions |
| `CTRLD_API_TOKEN` | Your Control D API token | Requires **write** permissions. Found in the Control D dashboard under **API**. |

#### ✉️ Email Notification Secrets (optional)

When changes are detected, the workflow can send an email report. Omit any to skip email:

| Secret | Value | Description |
|--------|-------|-------------|
| `EMAIL_USERNAME` | Your Gmail address | e.g. `you@gmail.com` — used for SMTP auth, sender, and recipient |
| `EMAIL_PASSWORD` | Your Gmail App Password | Generate one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) — **not** your regular Gmail password |

Gmail's SMTP server (`smtp.gmail.com`, port 465, implicit TLS) is used automatically — no server secret needed.

If `EMAIL_USERNAME` is missing, the email step is skipped silently.

---

## First run behaviour

On the very first run, `controld/` only contains a `.gitkeep` placeholder. Stage 1 will download the files listed in `TARGET_FILES` and treat them as new. Stage 2 will then push all domains in those files to the configured folders — treat this as an initial population, not an incremental diff. Stage 3 commits the files once Stage 2 has succeeded.

If you already have domains in your Control D folders that are not in the upstream files, they will be removed during the first run (Stage 2 always reconciles to the exact desired state). Make sure your folder contents align with what you expect before the first run, or review the Stage 2 log output carefully.

---

## `requirements.txt` — Python dependencies

`requests` is used by both scripts to make HTTP calls — Stage 1 uses it to download JSON files from the hagezi upstream repo, and Stage 2 uses it to call the Control D API.

`pip` itself is also pinned, so the installer that enforces every other hash is hash-verified too rather than being fetched unpinned from PyPI first.

Dependencies are managed with **[pip-tools](https://pip-tools.readthedocs.io)**:

| File | Purpose |
|------|---------|
| `requirements.in` | Human-edited source — list only direct dependencies here |
| `requirements.txt` | Auto-generated lock file — all packages pinned by version **and** SHA-256 hash |

The workflow installs with `pip install --require-hashes -r requirements.txt`, which means pip will refuse to install any package whose hash does not match. This prevents a compromised or tampered package on PyPI from being silently installed. Because `pip` is one of the pinned entries, that single command also brings pip itself to the pinned version under hash verification — there is no separate `pip install --upgrade pip` step.

### Manually Updating a dependency

1. Edit `requirements.in` (change the version pin or add/remove a package).
2. Regenerate the lock file:
   ```bash
   pip install pip-tools
   pip-compile --allow-unsafe --generate-hashes requirements.in -o requirements.txt
   ```
   `--allow-unsafe` is required: pip-tools classifies `pip`, `setuptools`, and
   `distribute` as "unsafe" to pin and will **drop the `pip` pin** without it.
3. Commit both `requirements.in` and `requirements.txt`.
4. Update the version entry in the [dependency reference](#-dependency--action-version-reference) below.

> **Note:** pip-tools uses pip's internals, so a very new `pip` in your local
> environment can break `pip-compile` itself (pip-tools 7.6.0 fails against pip
> 26.x). If regeneration errors out, run it in a venv with an older pip. This
> only affects manual regeneration — Dependabot uses its own environment.

---

## 📦 Dependency & action version reference

> **Dependabot handles updates automatically.**
> `.github/dependabot.yml` is configured to open weekly PRs (every Monday at 06:00 UTC) for both GitHub Actions pins and Python pip dependencies. You do not need to chase versions manually — just review and merge the Dependabot PRs when they arrive.

The entries below are kept for **transparency**: they document the version and commit hash that was current when each dependency was last manually reviewed, along with links to upstream release pages. Commit hashes are used for Actions (instead of tags) to prevent supply-chain attacks where a tag is silently moved to a different commit.

### Python packages

Versions below reflect the current `requirements.in` pin. All transitive
dependencies are locked with SHA-256 hashes in `requirements.txt` — run
`pip-compile --allow-unsafe --generate-hashes` after any change (see above).

```yaml
# - package: "requests"
#   url: "https://pypi.org/project/requests/"
#   version: "2.34.2"
#   date: "2026-05-14"
#   transitive-deps: "certifi, charset-normalizer, idna, urllib3"

# - package: "pip"
#   url: "https://pypi.org/project/pip/"
#   version: "26.2.1"
#   date: "2026-08-04"
#   note: "pip-tools 'unsafe' package — needs --allow-unsafe to stay pinned"
```

### GitHub Actions

Dates below are the commit date of the pinned SHA.

```yaml
# - action: "actions/checkout"
#   url: "https://github.com/actions/checkout/releases"
#   version: "v7.0.1"
#   date: "2026-07-17"
#   commit: "3d3c42e5aac5ba805825da76410c181273ba90b1"

# - action: "actions/setup-python"
#   url: "https://github.com/actions/setup-python/releases"
#   python-releases: "https://www.python.org/downloads/source/"
#   version: "v7.0.0"
#   date: "2026-07-19"
#   commit: "5fda3b95a4ea91299a34e894583c3862153e4b97"
#   with:
#     python-version: "3.14"

# - action: "Mattraks/delete-workflow-runs"
#   url: "https://github.com/Mattraks/delete-workflow-runs/releases"
#   version: "v2.1.0"
#   date: "2026-03-27"
#   commit: "b3018382ca039b53d238908238bd35d1fb14f8ee"
```

To manually update an action (outside of a Dependabot PR): find the new release tag and its corresponding full commit hash on the action's GitHub releases page, update the `uses:` line in `.github/workflows/sync-controld.yml` to the new commit hash, and update the entry above.
