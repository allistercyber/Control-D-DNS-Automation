#!/usr/bin/env python3
"""
Stage 2 — Control D API push.

For each monitored JSON file that changed, reconciles the matching Control D
folder against the desired state (file on disk) by:
  - Adding domains present in the file but missing from the live folder.
  - Removing domains present in the live folder but absent from the file.

Reconciliation is based on the LIVE API state, not git history, so the
script is idempotent and self-healing across retried/partial runs.

Which files map to which profiles/folders is defined in config.toml (see
config.example.toml and CONFIGURATION.md).

Exits 0 on full success, 1 if any operation failed (the workflow step fails).
"""

import os
import sys
import json
import time
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional, Set, Tuple

import requests
from urllib.parse import quote

from controld_config import Config, ConfigError, load_config


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# Which files go to which profiles/folders (and the delete-safety threshold) is
# configured in config.toml, not here.
# ══════════════════════════════════════════════════════════════════════════════

# Directory (relative to repo root) where Stage 1 stores the synced JSON files.
# Must match TARGET_DIR in controld_sync.py — no trailing slash.
CONTROLD_DIR = "controld"

BASE_URL           = "https://api.controld.com"
PAGE_SIZE          = 500    # max hostnames per POST /rules batch request
REQUEST_DELAY      = 0.5    # seconds between API calls (rate-limit headroom)
DELETE_DELAY       = 0.25   # seconds between individual DELETE calls


# ── API helpers ───────────────────────────────────────────────────────────────

API_MAX_RETRIES  = 3
API_RETRY_DELAYS = [2, 5, 10]  # seconds between attempts


def _headers(api_token: str) -> dict:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type":  "application/json",
    }


def _get(url: str, api_token: str) -> dict:
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt, delay in enumerate(
        [0] + API_RETRY_DELAYS[:API_MAX_RETRIES - 1], start=1
    ):
        if delay:
            log.warning(f"Retrying GET {url} in {delay}s (attempt {attempt}/{API_MAX_RETRIES})")
            time.sleep(delay)
        try:
            resp = requests.get(url, headers=_headers(api_token), timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < API_MAX_RETRIES:
                continue
    raise last_exc


def fetch_profiles(api_token: str) -> Dict[str, str]:
    """
    Returns {profile_display_name: profile_PK}.
    The API returns the display name in p["name"].
    """
    data = _get(f"{BASE_URL}/profiles", api_token)
    result: Dict[str, str] = {}
    for p in data.get("body", {}).get("profiles", []):
        name = p.get("name", "")
        pk   = p.get("PK", "")
        if name and pk:
            result[name] = pk
    # Count only: Actions logs are public on public repositories, so the names
    # of every profile in the account are not written to them.
    log.info(f"Discovered {len(result)} profile(s) in the account")
    return result


def fetch_folders(profile_pk: str, api_token: str) -> Dict[str, str]:
    """Returns {folder_display_name: folder_PK} for a given profile."""
    data = _get(f"{BASE_URL}/profiles/{profile_pk}/groups", api_token)
    result: Dict[str, str] = {}
    for g in data.get("body", {}).get("groups", []):
        name = g.get("group", "")
        pk   = g.get("PK", "")
        if name and pk:
            result[name] = pk
    log.info(f"  Discovered {len(result)} folder(s) in the profile")
    return result


def fetch_live_hostnames(profile_pk: str, folder_pk: str, api_token: str) -> Set[str]:
    """Returns the set of hostnames currently live in a Control D folder."""
    data = _get(f"{BASE_URL}/profiles/{profile_pk}/rules/{folder_pk}", api_token)
    body = data.get("body", data)          # handle both API response shapes
    rules = body.get("rules", [])
    hostnames: Set[str] = set()
    for rule in rules:
        h = rule.get("PK", "").strip().lower()
        if h:
            hostnames.add(h)
    log.info(f"  Live folder {folder_pk} contains {len(hostnames)} rules")
    return hostnames


def add_hostnames_batch(
    profile_pk: str,
    folder_pk: str,
    hostnames: List[str],
    api_token: str,
) -> int:
    """
    POSTs hostnames to a Control D folder in batches of PAGE_SIZE.
    action.do=0 means 'inherit action from folder'.
    Returns the total count of hostnames sent.
    """
    total = 0
    for i in range(0, len(hostnames), PAGE_SIZE):
        chunk = hostnames[i : i + PAGE_SIZE]
        payload = {
            "action":    {"do": 0, "status": 1},
            "group":     folder_pk,
            "hostnames": chunk,
        }
        last_exc: Exception = RuntimeError("No attempts made")
        for attempt, delay in enumerate(
            [0] + API_RETRY_DELAYS[:API_MAX_RETRIES - 1], start=1
        ):
            if delay:
                log.warning(f"Retrying POST rules in {delay}s (attempt {attempt}/{API_MAX_RETRIES})")
                time.sleep(delay)
            try:
                resp = requests.post(
                    f"{BASE_URL}/profiles/{profile_pk}/rules",
                    headers=_headers(api_token),
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= API_MAX_RETRIES:
                    raise last_exc
        total += len(chunk)
        log.info(f"    [add] POSTed {len(chunk)} hostnames (running total: {total})")
        time.sleep(REQUEST_DELAY)
    return total


def delete_hostname(profile_pk: str, hostname: str, api_token: str) -> None:
    """
    DELETEs a single hostname rule from a profile.
    404 is treated as success (already gone — idempotent).
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt, delay in enumerate(
        [0] + API_RETRY_DELAYS[:API_MAX_RETRIES - 1], start=1
    ):
        if delay:
            log.warning(f"Retrying DELETE {hostname} in {delay}s (attempt {attempt}/{API_MAX_RETRIES})")
            time.sleep(delay)
        try:
            resp = requests.delete(
                f"{BASE_URL}/profiles/{profile_pk}/rules/{quote(hostname, safe='')}",
                headers=_headers(api_token),
                timeout=30,
            )
            if resp.status_code == 404:
                log.debug(f"    [remove] {hostname} already absent (404)")
                return
            resp.raise_for_status()
            return
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= API_MAX_RETRIES:
                raise last_exc


# ── JSON parsing ──────────────────────────────────────────────────────────────

def extract_desired_hostnames(file_path: str) -> Optional[Set[str]]:
    """
    Parses a Hagezi Control D folder JSON and returns the set of hostnames.

    Hagezi format:
      {
        "group": {"group": "...", "action": {"do": N, "status": 1}},
        "rules": [{"PK": "example.com", "action": {...}}, ...]
      }

    Returns None if the file is missing or unparseable.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        log.error(f"File not found: {file_path}")
        return None
    except json.JSONDecodeError as exc:
        log.error(f"JSON parse error in {file_path}: {exc}")
        return None

    rules = data.get("rules")
    if rules is None:
        log.error(f"Missing 'rules' key in {file_path} — refusing to sync (would wipe folder)")
        return None

    hostnames: Set[str] = set()
    for rule in rules:
        h = rule.get("PK", "").strip().lower()
        if h:
            hostnames.add(h)

    if not hostnames:
        log.error(
            f"No hostnames extracted from {file_path} — refusing to treat empty set as "
            f"desired state (would wipe folder). Skipping."
        )
        return None

    return hostnames


# ── Folder reconciliation ─────────────────────────────────────────────────────

def sync_folder(
    profile_pk: str,
    folder_pk: str,
    folder_name: str,
    desired: Set[str],
    api_token: str,
    max_delete_percent: int,
) -> Tuple[bool, List[str], List[str]]:
    """
    Reconciles a single Control D folder to match `desired`.
    Fetches live state, then adds/removes the delta.
    Returns (success, actually_added, actually_removed).
    """
    log.info(f"  Reconciling folder '{folder_name}' (pk={folder_pk})")
    try:
        time.sleep(REQUEST_DELAY)
        live = fetch_live_hostnames(profile_pk, folder_pk, api_token)
    except requests.RequestException as exc:
        # RequestException (not just HTTPError) so timeouts/connection errors
        # that survive the retries fail this folder instead of crashing the run.
        log.error(f"  Failed to fetch live rules for '{folder_name}': {exc}")
        return False, [], []

    to_add    = sorted(desired - live)
    to_remove = sorted(live - desired)

    log.info(f"  Delta: +{len(to_add)} to add, -{len(to_remove)} to remove")

    # Safety guardrail: refuse to execute if removals are unusually large relative
    # to the live folder size.  This catches upstream truncation/breakage that
    # slipped past the empty-set check (e.g. a file legitimately cut from 5 000
    # to 10 entries would still reach here).
    if live and to_remove:
        remove_pct = len(to_remove) * 100 // len(live)
        if remove_pct > max_delete_percent:
            log.error(
                f"  Aborting sync for '{folder_name}': {len(to_remove)} removals "
                f"({remove_pct}% of {len(live)} live entries) exceeds the "
                f"{max_delete_percent}% safety threshold. "
                f"Raise max_delete_percent in config.toml if this is intentional."
            )
            return False, [], []

    success          = True
    actually_added:   List[str] = []
    actually_removed: List[str] = []

    # Additions (batched)
    if to_add:
        try:
            add_hostnames_batch(profile_pk, folder_pk, to_add, api_token)
            actually_added = to_add
            log.info(f"  Added {len(actually_added)} domains to '{folder_name}'")
        except requests.HTTPError as exc:
            # Log only the status code — response body may contain internal API
            # details that should not be persisted in CI logs.
            status = exc.response.status_code if exc.response is not None else "?"
            log.error(f"  Failed to add hostnames to '{folder_name}': HTTP {status}")
            success = False
        except Exception as exc:
            log.error(f"  Unexpected error adding to '{folder_name}': {exc}")
            success = False
    else:
        log.info(f"  No domains to add for '{folder_name}'")

    # Removals (individual, with delay)
    if to_remove:
        errors = 0
        for hostname in to_remove:
            try:
                delete_hostname(profile_pk, hostname, api_token)
                actually_removed.append(hostname)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                log.error(f"  Failed to delete '{hostname}': HTTP {status}")
                errors += 1
                success = False
            except Exception as exc:
                log.error(f"  Unexpected error deleting '{hostname}': {exc}")
                errors += 1
                success = False
            time.sleep(DELETE_DELAY)
        log.info(f"  Removed {len(actually_removed)} domains from '{folder_name}' ({errors} errors)")
    else:
        log.info(f"  No domains to remove for '{folder_name}'")

    return success, actually_added, actually_removed


# ── Main run ──────────────────────────────────────────────────────────────────

def run(api_token: str, config: Config) -> Tuple[bool, str]:
    """
    Processes every [[lists]] entry in config.toml, in order.

    Cross-folder deduplication: within each profile, domains claimed by an
    earlier-processed folder are excluded from later folders. This prevents
    the same domain appearing in both an allow and a block folder.
    Processing order follows the order of the [[lists]] entries.

    Returns (success, email_body): True only if every operation succeeded.
    """
    overall_success = True

    # {profile_name: [(folder_name, added, removed, n_skipped, error_note)]}
    report: Dict[str, List[Tuple[str, List[str], List[str], int, str]]] = {}

    # Tracks domains already assigned to a folder within a profile this run.
    claimed_per_profile: Dict[str, Set[str]] = {}

    # Fetch all profiles once upfront
    log.info("Fetching profiles from Control D...")
    try:
        profile_map = fetch_profiles(api_token)
    except Exception as exc:
        log.error(f"Cannot fetch profiles — aborting: {exc}")
        return False, ""

    # Cache folder listings per profile, fetched on first access
    folder_cache: Dict[str, Dict[str, str]] = {}

    def get_folders_cached(profile_pk: str) -> Optional[Dict[str, str]]:
        if profile_pk not in folder_cache:
            try:
                time.sleep(REQUEST_DELAY)
                folder_cache[profile_pk] = fetch_folders(profile_pk, api_token)
            except Exception as exc:
                log.error(f"Cannot fetch folders for profile {profile_pk}: {exc}")
                return None
        return folder_cache[profile_pk]

    # Process each file → profile/folder mapping
    for entry in config.lists:
        filename, mappings = entry.file, entry.targets
        file_path = f"{CONTROLD_DIR}/{filename}"
        log.info(f"\n📄 File: {filename}")

        desired = extract_desired_hostnames(file_path)
        if desired is None:
            log.error(f"  Skipping '{filename}' — could not read/parse file")
            overall_success = False
            continue

        log.info(f"  Desired state: {len(desired)} hostnames")

        for (profile_name, folder_name) in mappings:
            log.info(f"  → Target: profile='{profile_name}', folder='{folder_name}'")
            report.setdefault(profile_name, [])

            profile_pk = profile_map.get(profile_name)
            if not profile_pk:
                log.error(
                    f"    Profile '{profile_name}' not found among the account's "
                    f"{len(profile_map)} profile(s) — skipping (names are case-sensitive)"
                )
                overall_success = False
                report[profile_name].append((folder_name, [], [], 0, "profile not found"))
                continue

            folders = get_folders_cached(profile_pk)
            if folders is None:
                overall_success = False
                report[profile_name].append((folder_name, [], [], 0, "could not fetch folders"))
                continue

            folder_pk = folders.get(folder_name)
            if not folder_pk:
                log.error(f"    Folder '{folder_name}' not found in profile '{profile_name}' — skipping")
                overall_success = False
                report[profile_name].append((folder_name, [], [], 0, "folder not found"))
                continue

            # Cross-folder deduplication: exclude domains already claimed by
            # an earlier folder in this profile (allow before block).
            already_claimed  = claimed_per_profile.get(profile_name, set())
            effective_desired = desired - already_claimed
            n_skipped = len(desired) - len(effective_desired)

            if n_skipped:
                log.info(f"    Deduplication: {n_skipped} domain(s) skipped (already in another folder in this profile)")

            ok, added, removed = sync_folder(
                profile_pk, folder_pk, folder_name, effective_desired, api_token,
                config.max_delete_percent,
            )

            # Only claim these domains once the folder actually reconciled.
            # Claiming up-front would exclude them from every later folder in
            # this profile even when this folder failed or was aborted by the
            # max_delete_percent guardrail — the later folder would then treat
            # them as unwanted and delete them, leaving them in no folder at all.
            if ok:
                claimed_per_profile.setdefault(profile_name, set()).update(effective_desired)

            if not ok:
                overall_success = False
                report[profile_name].append((folder_name, added, removed, n_skipped, "completed with API errors"))
            else:
                report[profile_name].append((folder_name, added, removed, n_skipped, ""))

    # Build email report
    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sep = "=" * 44
    body_parts: List[str] = [
        f"Control D Sync Report — {run_time}",
        "",
    ]

    for profile_name, folder_results in report.items():
        body_parts += [sep, f"Profile: {profile_name}", sep, ""]
        for (folder_name, added, removed, n_skipped, error) in folder_results:
            body_parts.append(f"  Folder: {folder_name}")
            if error and not added and not removed:
                body_parts.append(f"    ❗ Error: {error}")
            else:
                if error:
                    body_parts.append(f"    ⚠️  Note: {error}")
                if n_skipped:
                    body_parts.append(f"    Skipped ({n_skipped}): already in another folder in this profile")
                body_parts.append(f"    Added ({len(added)}):" if added else "    Added (0): —")
                for h in added:
                    body_parts.append(f"      + {h}")
                body_parts.append(f"    Removed ({len(removed)}):" if removed else "    Removed (0): —")
                for h in removed:
                    body_parts.append(f"      - {h}")
            body_parts.append("")

    email_body = "\n".join(body_parts)

    return overall_success, email_body


# ── Email send ────────────────────────────────────────────────────────────────

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465


def send_email(email_body: str) -> None:
    """
    Sends the sync report by email.  Entirely optional: skipped unless both
    EMAIL_USERNAME and EMAIL_PASSWORD are set.

    Environment variables (see CONFIGURATION.md):
      EMAIL_USERNAME   SMTP login; also the sender, and the recipient unless
                       EMAIL_TO is set.
      EMAIL_PASSWORD   SMTP password / app password.
      EMAIL_TO         Recipient address (default: EMAIL_USERNAME).
      EMAIL_SMTP_HOST  SMTP server (default: smtp.gmail.com).
      EMAIL_SMTP_PORT  465 = implicit TLS (default); any other port uses
                       STARTTLS.
    """
    username  = os.environ.get("EMAIL_USERNAME", "").strip()
    password  = os.environ.get("EMAIL_PASSWORD", "").strip()

    if not username:
        log.warning("Email not configured (EMAIL_USERNAME missing) — skipping")
        return
    if not password:
        log.warning("EMAIL_PASSWORD missing — skipping email send")
        return

    recipient = os.environ.get("EMAIL_TO", "").strip() or username
    host      = os.environ.get("EMAIL_SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST
    port_text = os.environ.get("EMAIL_SMTP_PORT", "").strip() or str(DEFAULT_SMTP_PORT)
    try:
        port = int(port_text)
    except ValueError:
        log.error(f"EMAIL_SMTP_PORT must be a number (got {port_text!r}) — skipping email send")
        return

    msg = MIMEMultipart()
    msg["From"]    = username
    msg["To"]      = recipient
    msg["Subject"] = "Control D sync report"
    msg.attach(MIMEText(email_body, "plain"))

    try:
        context = ssl.create_default_context()
        if port == 465:
            smtp_cm = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            smtp_cm = smtplib.SMTP(host, port, timeout=30)
        with smtp_cm as smtp:
            if port != 465:
                smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(msg)
        log.info("Email sent successfully")
    except Exception as exc:
        log.error(f"Failed to send email: {exc}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    api_token = os.environ.get("CTRLD_API_TOKEN", "").strip()
    if not api_token:
        log.error("CTRLD_API_TOKEN environment variable is not set or empty")
        sys.exit(1)

    try:
        config = load_config()
    except ConfigError as exc:
        log.error(str(exc))
        sys.exit(1)

    log.info("=== Control D API push: start ===")
    success, email_body = run(api_token, config)

    if email_body:
        send_email(email_body)
    else:
        log.warning("No email body found — skipping email send")

    if success:
        log.info("=== Control D API push: all updates applied successfully ===")
        sys.exit(0)
    else:
        log.warning("=== Control D API push: finished with one or more errors (see above) ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
