# 🚀 Control D x Hagezi-Sync

Automatically syncs DNS blocklist folders from [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists) into your [Control D](https://controld.com) account via the Control D API.

Runs on a GitHub Actions schedule — no server required.

This repository is a **public template**. It ships with placeholder profile/folder names and **does not include** any synced blocklist JSON. Fork it, replace the examples with your Control D names, then enable Actions in *your* fork.

---

## 🔧 Features

- ✅ Automated twice-daily sync via cron (5 AM & 5 PM UTC)
- 🐍 Python-based sync script with retry logic
- 🔒 Pinned dependencies and commit-hash-locked Actions
- ✉️ Sends email with diff summary if files change
- 📁 Keeps the `controld/` folder in sync with upstream
- 🌐 Pushes domain changes to the Control D API automatically
- 🔄 Idempotent reconciliation — self-healing across retried or partial runs

---

## ⚙️ How It Works

The workflow runs twice daily (05:00 and 17:00 UTC) in three stages:

1. 📥 **Stage 1 — File sync** (`scripts/controld_sync.py`)  
   Downloads the target JSON files from the hagezi upstream repository and diffs them against the local copies, writing any changes into the working tree. Sets a `changed` flag for the next stage.

2. 🌐 **Stage 2 — API push** (`scripts/controld_api_push.py`)  
   Runs only when Stage 1 detected changes. Reads the updated JSON files and reconciles each mapped Control D folder against the desired state — adding new domains and removing stale ones. The reconciliation is always against the **live API state**, so the script is idempotent and self-healing if a previous run was interrupted.

3. 💾 **Stage 3 — Commit** (`scripts/controld_sync.py --commit`)  
   Commits and pushes the synced files, but **only after Stage 2 succeeded**. This ordering is what makes the retry work: if the API push fails, the files stay uncommitted, so the next scheduled run re-detects the same diff and retries the push. Committing first would make the next run see no change and skip Stage 2 indefinitely.

✉️ An email report is sent after Stage 2 summarising every domain added, removed, or skipped per profile and folder.

---

## 🚀 Quick Start

> 💡 **Privacy & security note:** This repo runs entirely within your own GitHub Actions environment. Your API token is used only for outbound requests to the Control D API, and email credentials only for outbound SMTP — neither is logged or persisted beyond the runner. For maximum privacy and security it is recommended to keep your fork **private** — this prevents your profile names, folder names, and workflow configuration from being publicly visible.

### 1. Fork this repo

Fork to your own GitHub account. Keep the fork **private** if you do not want your profile names, folder names, or synced JSON files to be public.

### 2. Configure your profile and folder mappings

The scripts ship with **example** names (`Home`, `Travel`, and sample folder names). They will not match your Control D account until you change them.

1. Edit `scripts/controld_api_push.py` and replace `FILE_MAPPINGS` with your Control D profile and folder names.
2. Edit `scripts/controld_sync.py` and set `TARGET_FILES` to the same upstream filenames (the keys of `FILE_MAPPINGS`).

See [CONFIGURATION.md](CONFIGURATION.md) for the full mapping format and a list of available upstream files.

Do this **before** the first workflow run. Running with the example names will fail because those profiles/folders will not exist in your account.

### 3. Set the required secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add the following:

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

Email is only sent when Stage 2 runs (i.e. files actually changed).

### 4. Run manually to verify

Go to **Actions → Sync Control D folders from upstream → Run workflow** to trigger an immediate run and verify everything is working before waiting for the schedule.

---

## 📂 Repository Structure

```
.github/
  dependabot.yml            # weekly auto-updates for Actions & pip deps
  workflows/
    sync-controld.yml       # workflow orchestrator
scripts/
  controld_sync.py          # Stage 1: file sync
  controld_api_push.py      # Stage 2: Control D API push
requirements.in             # direct Python dependencies (source of truth)
requirements.txt            # fully pinned deps with SHA-256 hashes (generated)
CONFIGURATION.md            # detailed setup & configuration reference
.gitignore
controld/                   # synced JSON files (empty until your first successful run)
```

---

## 🌐 Upstream Source

All blocklist JSON files come from [hagezi/dns-blocklists](https://github.com/hagezi/dns-blocklists/tree/main/controld). Hat tip to hagezi for maintaining these lists.
