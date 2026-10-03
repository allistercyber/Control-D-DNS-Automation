#!/usr/bin/env python3
"""
Stage 1 — File sync.

Downloads the target JSON files from the hagezi/dns-blocklists upstream
repository, compares them against the local copies, writes any changes into
the working tree, and signals downstream steps via GITHUB_OUTPUT.

If the GitHub upstream is unreachable after all retry attempts, downloads
fall back to the hagezi mirror at MIRROR_RAW_BASE_URL before giving up
entirely.

Invocation modes:
  python controld_sync.py            download + compare + write files (no commit)
  python controld_sync.py --commit   commit and push what the above wrote

The commit is deliberately a *separate* step so it can run only after the
Control D API push (Stage 2) has succeeded.  If Stage 2 fails, the working
tree is left uncommitted, so the next scheduled run re-detects the same change
and retries the API push instead of silently skipping it forever.

Which files are downloaded is defined by config.toml (see config.example.toml
and CONFIGURATION.md); the same file drives Stage 2.

Set FORCE_PUSH=true to report "changed" even when no file differs, so Stage 2
re-applies the current files to Control D (e.g. after editing config.toml).
"""

import os
import sys
import time
import shutil
import difflib
import subprocess
import base64
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import requests

from controld_config import ConfigError, load_config


# ── Upstream source ───────────────────────────────────────────────────────────
# Which files to fetch is configured in config.toml, not here.

UPSTREAM_API_URL = (
    "https://api.github.com/repos/hagezi/dns-blocklists/contents/controld"
)

# Official Hagezi build mirror (listed in the hagezi/dns-blocklists README).
# Used only as a fallback when the GitHub upstream above is unreachable after
# MAX_ATTEMPTS retries, and only if `mirror_fallback` is true in config.toml.
# It mirrors the same controld/ directory, serving each file at
# <base>/<filename>.
MIRROR_RAW_BASE_URL = "https://hagezi-mirror.dnsbunker.org/controld/"

# ── Internal constants ────────────────────────────────────────────────────────

MAX_ATTEMPTS  = 5
RETRY_DELAY   = 60   # seconds between download retry attempts
TEMP_DIR      = Path("temp_controld")
TARGET_DIR    = Path("controld")

# Hosts the downloader is allowed to fetch file content from.
ALLOWED_GITHUB_HOST   = "raw.githubusercontent.com"
ALLOWED_GITHUB_SUFFIX = "githubusercontent.com"
ALLOWED_MIRROR_HOST   = "hagezi-mirror.dnsbunker.org"


# ── Sync class ────────────────────────────────────────────────────────────────

class UpstreamFileMissing(Exception):
    """A configured file does not exist upstream.  Retrying cannot fix that."""


class ControldSync:

    def __init__(self, target_files: List[str], mirror_fallback: bool) -> None:
        self.target_files = target_files
        self.mirror_fallback = mirror_fallback

    # ── Git helpers ───────────────────────────────────────────────────────────

    def setup_git(self) -> None:
        """Configure git identity for the commit made by this workflow."""
        subprocess.run(["git", "config", "user.name",  "github-actions[bot]"], check=True)
        subprocess.run(
            ["git", "config", "user.email",
             "41898282+github-actions[bot]@users.noreply.github.com"],
            check=True,
        )

    def commit_and_push(self, github_token: str, repo_name: str) -> None:
        """Stage, commit, and push changes in TARGET_DIR (no-op if there are none)."""
        subprocess.run(["git", "add", "--", str(TARGET_DIR)], check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", str(TARGET_DIR)]
        )
        if staged.returncode == 0:
            print("Nothing to commit - synced files already match the repository.")
            return
        subprocess.run(
            ["git", "commit", "-m", "Sync controld folder from upstream"],
            check=True,
        )
        # Hand the token to this one git process through GIT_CONFIG_* environment
        # variables.  Unlike `git config`, nothing is written to .git/config and
        # the token never appears in any process argument list (argv is visible
        # via /proc/<pid>/cmdline), so there is nothing to scrub afterwards.
        auth = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT":   "1",
            "GIT_CONFIG_KEY_0":   "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
        }
        branch = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
        remote_url = f"https://github.com/{repo_name}.git"
        result = subprocess.run(["git", "push", remote_url, f"HEAD:refs/heads/{branch}"], env=env)
        if result.returncode != 0:
            raise RuntimeError(f"git push failed (exit {result.returncode})")
        print("Changes pushed to repository")

    # ── Temp dir helpers ──────────────────────────────────────────────────────

    def cleanup_temp(self) -> None:
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)

    # ── Download ──────────────────────────────────────────────────────────────

    def download_files(self, github_token: str = "") -> bool:
        """
        Download the configured files from the GitHub upstream, falling back to the
        mirror if GitHub is unreachable after all retry attempts.
        Returns True on success, False if both sources fail.

        A configured file that does not exist upstream (a typo in config.toml)
        fails immediately: retrying, or asking the mirror for the same name,
        would only delay the error message by several minutes.
        """
        try:
            if self._download_from_github(github_token):
                return True
        except UpstreamFileMissing:
            return False

        if not self.mirror_fallback:
            print("GitHub upstream unavailable after all attempts; mirror fallback is disabled.")
            return False

        print(
            "GitHub upstream unavailable after all attempts; "
            f"falling back to mirror at {MIRROR_RAW_BASE_URL} ..."
        )
        return self._download_from_mirror()

    def _download_from_github(self, github_token: str = "") -> bool:
        """
        Download the configured files from the GitHub upstream with retry logic.
        Passing github_token uses the authenticated GitHub API rate limit
        (5 000 req/hr) instead of the shared unauthenticated limit (60 req/hr).
        Returns True on success, False after all attempts are exhausted.
        """
        api_headers = {}
        if github_token:
            api_headers["Authorization"] = f"Bearer {github_token}"

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"Download attempt #{attempt}")
            try:
                self.cleanup_temp()
                TEMP_DIR.mkdir(exist_ok=True)

                # List all files in the upstream controld/ directory
                response = requests.get(UPSTREAM_API_URL, headers=api_headers, timeout=30)
                response.raise_for_status()

                upstream_files = {
                    f["name"]: f["download_url"]
                    for f in response.json()
                    if f.get("type") == "file" and f.get("name")
                }

                # An empty listing is a GitHub hiccup, not proof that every file
                # is gone: let it be retried (and fall back to the mirror).
                if not upstream_files:
                    raise ValueError("upstream directory listing was empty")

                # Report every missing name at once, with the closest upstream
                # names, so one run is enough to fix all the typos.
                missing = [n for n in self.target_files if not upstream_files.get(n)]
                if missing:
                    for name in missing:
                        close = difflib.get_close_matches(name, upstream_files, n=3)
                        hint = f" Did you mean: {', '.join(close)}?" if close else ""
                        print(
                            f"ERROR: File not found in upstream: {name}. Check the "
                            f"'file' value in config.toml.{hint}"
                        )
                    raise UpstreamFileMissing(", ".join(missing))

                # Download only the files we care about
                for filename in self.target_files:
                    url = upstream_files[filename]
                    # Validate the download URL before fetching to prevent SSRF
                    # in case the GitHub API response is tampered with.
                    # Use .hostname (not .netloc): netloc also carries any
                    # userinfo and port, which would let
                    # "https://raw.githubusercontent.com@evil.test/" slip past.
                    parsed = urlparse(url)
                    if parsed.scheme not in ("http", "https"):
                        raise ValueError(
                            f"Unexpected URL scheme for '{filename}': {parsed.scheme!r}"
                        )
                    host = (parsed.hostname or "").lower()
                    # Match the exact host or a true subdomain. A bare
                    # endswith("githubusercontent.com") would also accept
                    # attacker-registrable names like "evil-githubusercontent.com".
                    if host != ALLOWED_GITHUB_HOST and not host.endswith(
                        f".{ALLOWED_GITHUB_SUFFIX}"
                    ):
                        raise ValueError(
                            f"Unexpected URL host for '{filename}': {host!r}"
                        )
                    file_response = requests.get(url, timeout=30)
                    file_response.raise_for_status()
                    (TEMP_DIR / filename).write_bytes(file_response.content)

                print("Download successful.")
                return True

            except UpstreamFileMissing:
                raise
            except requests.RequestException as exc:
                print(f"Network error on attempt {attempt}: {exc}")
            except Exception as exc:
                print(f"Unexpected error on attempt {attempt}: {exc}")

            if attempt < MAX_ATTEMPTS:
                print(f"Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

        print(f"All {MAX_ATTEMPTS} download attempts failed.")
        return False

    def _download_from_mirror(self) -> bool:
        """
        Download the configured files directly from the mirror as a fallback when the
        GitHub upstream is unreachable. Uses the same retry/timeout behaviour
        as the primary GitHub download.
        Returns True on success, False after all attempts are exhausted.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"Mirror fallback download attempt #{attempt}")
            try:
                self.cleanup_temp()
                TEMP_DIR.mkdir(exist_ok=True)

                for filename in self.target_files:
                    # rstrip keeps the join correct whether or not
                    # MIRROR_RAW_BASE_URL carries a trailing slash.
                    url = f"{MIRROR_RAW_BASE_URL.rstrip('/')}/{filename}"
                    # Validate the URL before fetching, mirroring the SSRF
                    # guard used for the GitHub download path.
                    parsed = urlparse(url)
                    host = (parsed.hostname or "").lower()
                    if parsed.scheme not in ("http", "https") or host != ALLOWED_MIRROR_HOST:
                        raise ValueError(f"Unexpected URL for '{filename}': {url!r}")
                    file_response = requests.get(url, timeout=30)
                    file_response.raise_for_status()
                    (TEMP_DIR / filename).write_bytes(file_response.content)

                print("Mirror fallback download successful.")
                return True

            except requests.RequestException as exc:
                print(f"Mirror network error on attempt {attempt}: {exc}")
            except Exception as exc:
                print(f"Mirror unexpected error on attempt {attempt}: {exc}")

            if attempt < MAX_ATTEMPTS:
                print(f"Retrying mirror in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)

        print(f"All {MAX_ATTEMPTS} mirror fallback download attempts failed.")
        return False

    # ── Compare / sync ────────────────────────────────────────────────────────

    def sync_files(self) -> Optional[List[str]]:
        """
        Copy new or changed files from TEMP_DIR into TARGET_DIR.
        Returns the list of filenames that changed, or None if an expected
        file was missing from the download.
        """
        TARGET_DIR.mkdir(exist_ok=True)
        changed: List[str] = []

        for filename in self.target_files:
            temp_file   = TEMP_DIR   / filename
            target_file = TARGET_DIR / filename

            if not temp_file.exists():
                print(f"ERROR: Expected file '{filename}' was not downloaded.")
                return None

            if target_file.exists() and target_file.read_bytes() == temp_file.read_bytes():
                continue

            shutil.copy2(temp_file, target_file)
            changed.append(filename)
            print(f"Updated: {filename}")

        return changed

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self, github_token: str) -> None:
        """
        Download → compare → write changed files into the working tree.

        Deliberately does NOT commit.  The commit is performed by commit()
        in a later workflow step, once the Control D API push has succeeded,
        so a failed push leaves the diff intact to be retried next run.
        """
        try:
            print("Starting controld sync process...")

            if not self.download_files(github_token):
                sys.exit(1)

            changed_files = self.sync_files()

            if changed_files is None:
                sys.exit(1)

            force = os.environ.get("FORCE_PUSH", "").strip().lower() == "true"
            has_changes = bool(changed_files) or force

            if changed_files:
                print(f"{len(changed_files)} file(s) changed - staged in the working tree.")
            elif force:
                print("No file changes, but FORCE_PUSH=true - Stage 2 will re-apply the current files.")
            else:
                print("No changes detected.")

            github_output_path = os.environ.get("GITHUB_OUTPUT", "")
            if github_output_path:
                with open(github_output_path, "a", encoding="utf-8") as fh:
                    fh.write(f"changed={'true' if has_changes else 'false'}\n")

        except Exception as exc:
            print(f"Sync failed: {exc}")
            sys.exit(1)
        finally:
            self.cleanup_temp()

    def commit(self, github_token: str, repo_name: str) -> None:
        """
        Commit and push the files that a previous run() wrote into TARGET_DIR.

        Invoked as a separate workflow step that runs only after the Control D
        API push succeeded, so the repository state never advances past what
        has actually been applied to Control D.
        """
        try:
            print("Committing synced files...")
            self.setup_git()
            self.commit_and_push(github_token, repo_name)
        except Exception as exc:
            print(f"Commit failed: {exc}")
            sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    repo_name    = os.getenv("GITHUB_REPOSITORY", "").strip()

    if not github_token or not repo_name:
        print("ERROR: GITHUB_TOKEN and GITHUB_REPOSITORY must be set.")
        sys.exit(1)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    sync = ControldSync(config.target_files, config.mirror_fallback)

    args = sys.argv[1:]
    if not args:
        sync.run(github_token)
    elif args == ["--commit"]:
        sync.commit(github_token, repo_name)
    else:
        print(f"ERROR: unknown arguments: {args} (expected no argument or --commit)")
        sys.exit(2)


if __name__ == "__main__":
    main()
