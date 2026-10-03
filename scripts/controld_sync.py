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
  python controld_sync.py            download + diff + write files (no commit)
  python controld_sync.py --commit   commit and push what the above wrote

The commit is deliberately a *separate* step so it can run only after the
Control D API push (Stage 2) has succeeded.  If Stage 2 fails, the working
tree is left uncommitted, so the next scheduled run re-detects the same diff
and retries the API push instead of silently skipping it forever.

Customisation: edit TARGET_FILES to control which files are downloaded.
The list here should match the keys in FILE_MAPPINGS in controld_api_push.py.
"""

import os
import sys
import time
import shutil
import subprocess
import difflib
import base64
from pathlib import Path
from typing import List, Tuple, Optional
from urllib.parse import urlparse

import requests


# ── User configuration ────────────────────────────────────────────────────────

UPSTREAM_API_URL = (
    "https://api.github.com/repos/hagezi/dns-blocklists/contents/controld"
)

# Used only as a fallback when the GitHub upstream above is unreachable
# after MAX_ATTEMPTS retries. Mirrors the same controld/ directory, serving
# each target file at <base>/<filename>.
MIRROR_RAW_BASE_URL = "https://hagezi-mirror.dnsbunker.org/controld/"

# Files to download from the upstream controld/ directory.
# Example only — replace with the filenames you want to track.
# Browse the full list at:
#   https://github.com/hagezi/dns-blocklists/tree/main/controld
# Must match the keys in FILE_MAPPINGS in controld_api_push.py.
TARGET_FILES: List[str] = [
    "apple-private-relay-allow-folder.json",
    "spam-tlds-folder.json",
]

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

class ControldSync:

    # ── Git helpers ───────────────────────────────────────────────────────────

    def setup_git(self) -> None:
        """Configure git identity for the commit made by this workflow."""
        subprocess.run(["git", "config", "user.name",  "GitHub Actions"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)

    def commit_and_push(self, github_token: str, repo_name: str) -> None:
        """Stage, commit, and push changes in TARGET_DIR."""
        subprocess.run(["git", "add", str(TARGET_DIR)], check=True)
        subprocess.run(
            ["git", "commit", "-m", "Sync controld folder from upstream"],
            check=True,
        )
        # Inject the token via http.extraheader stored in local git config so it
        # never appears in process arguments for the push (visible via /proc/<pid>/cmdline).
        # This mirrors the technique used by actions/checkout itself.
        # The git config call does receive the token in its argv, but capture_output=True
        # keeps it off stdout/stderr, and the CalledProcessError handler below ensures a
        # failure raises a sanitised RuntimeError (not the raw CalledProcessError whose
        # .cmd would contain the base64-encoded token).
        auth = base64.b64encode(f"x-access-token:{github_token}".encode()).decode()
        try:
            subprocess.run(
                [
                    "git", "config", "--local",
                    "http.https://github.com/.extraheader",
                    f"AUTHORIZATION: basic {auth}",
                ],
                check=True,
                capture_output=True,  # prevent git noise; also keeps args out of default output
            )
        except subprocess.CalledProcessError as exc:
            # Re-raise without the original exception so the CalledProcessError
            # (whose .cmd includes the base64-encoded token) is never printed to
            # workflow logs.  The raw exit code is still surfaced for diagnosis.
            raise RuntimeError(
                f"Failed to set git credential header (exit {exc.returncode})"
            ) from None
        try:
            branch = os.getenv("GITHUB_REF_NAME", "main").strip() or "main"
            remote_url = f"https://github.com/{repo_name}.git"
            subprocess.run(["git", "push", remote_url, branch], check=True)
            print("Changes pushed to repository")
        finally:
            # Always scrub the credential from local config, even on push failure.
            subprocess.run(
                [
                    "git", "config", "--local", "--unset",
                    "http.https://github.com/.extraheader",
                ],
                check=False,
            )

    # ── Temp dir helpers ──────────────────────────────────────────────────────

    def cleanup_temp(self) -> None:
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)

    # ── Download ──────────────────────────────────────────────────────────────

    def download_files(self, github_token: str = "") -> bool:
        """
        Download TARGET_FILES from the GitHub upstream, falling back to the
        mirror if GitHub is unreachable after all retry attempts.
        Returns True on success, False if both sources fail.
        """
        if self._download_from_github(github_token):
            return True

        print(
            "GitHub upstream unavailable after all attempts; "
            f"falling back to mirror at {MIRROR_RAW_BASE_URL} ..."
        )
        return self._download_from_mirror()

    def _download_from_github(self, github_token: str = "") -> bool:
        """
        Download TARGET_FILES from the GitHub upstream with retry logic.
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

                # Download only the files we care about
                upstream_files = {
                    f["name"]: f["download_url"]
                    for f in response.json()
                    if f.get("type") == "file" and f.get("name") in TARGET_FILES
                }

                for filename in TARGET_FILES:
                    url = upstream_files.get(filename)
                    if not url:
                        raise ValueError(f"File not found in upstream: {filename}")
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
        Download TARGET_FILES directly from the mirror as a fallback when the
        GitHub upstream is unreachable. Uses the same retry/timeout behaviour
        as the primary GitHub download.
        Returns True on success, False after all attempts are exhausted.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"Mirror fallback download attempt #{attempt}")
            try:
                self.cleanup_temp()
                TEMP_DIR.mkdir(exist_ok=True)

                for filename in TARGET_FILES:
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

    # ── Diff / sync ───────────────────────────────────────────────────────────

    def get_file_diff(
        self,
        existing: Optional[Path],
        incoming: Path,
    ) -> Tuple[str, List[str]]:
        """
        Returns (raw_unified_diff, emoji_formatted_lines).
        If `existing` is None (new file) every line is treated as an addition.
        """
        if existing is None or not existing.exists():
            lines = incoming.read_text(encoding="utf-8").splitlines()
            emoji_lines = [f"✅ {line}" for line in lines]
            return "\n".join(f"+ {l}" for l in lines), emoji_lines

        old_lines = existing.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines = incoming.read_text(encoding="utf-8").splitlines(keepends=True)

        diff = list(difflib.unified_diff(
            old_lines, new_lines,
            fromfile=str(existing), tofile=str(incoming),
            lineterm="",
        ))

        emoji_lines = []
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                emoji_lines.append(f"✅ {line[1:]}")
            elif line.startswith("-") and not line.startswith("---"):
                emoji_lines.append(f"❌ {line[1:]}")

        return "".join(diff), emoji_lines

    def sync_files(self) -> Tuple[Optional[bool], str]:
        """
        Copy changed files from TEMP_DIR into TARGET_DIR.
        Returns (changed, pretty_diff_output) where changed is True if any file
        changed, False if none, or None if an expected file was missing.
        """
        TARGET_DIR.mkdir(exist_ok=True)
        changed: List[str] = []
        pretty_output = ""

        for filename in TARGET_FILES:
            temp_file   = TEMP_DIR   / filename
            target_file = TARGET_DIR / filename

            if not temp_file.exists():
                print(f"ERROR: Expected file '{filename}' was not downloaded.")
                return None, ""

            diff_text, emoji_diff = self.get_file_diff(
                target_file if target_file.exists() else None,
                temp_file,
            )

            if diff_text:
                shutil.copy2(temp_file, target_file)
                changed.append(filename)
                if emoji_diff:
                    pretty_output += f"[{filename}]\n"
                    pretty_output += "\n".join(emoji_diff)
                    pretty_output += "\n\n"

        return bool(changed), pretty_output

    # ── Orchestration ─────────────────────────────────────────────────────────

    def run(self, github_token: str) -> None:
        """
        Download → diff → write files into the working tree.

        Deliberately does NOT commit.  The commit is performed by commit()
        in a later workflow step, once the Control D API push has succeeded,
        so a failed push leaves the diff intact to be retried next run.
        """
        try:
            print("Starting controld sync process...")

            if not self.download_files(github_token):
                sys.exit(1)

            has_changes, diff_output = self.sync_files()

            if has_changes is None:
                sys.exit(1)

            github_output_path = os.environ.get("GITHUB_OUTPUT", "")

            if has_changes:
                print("Changes detected — staged in the working tree.")

                if github_output_path:
                    with open(github_output_path, "a", encoding="utf-8") as fh:
                        fh.write("changed=true\n")
                        fh.write("diff_output<<EOF\n")
                        fh.write(diff_output)
                        fh.write("\nEOF\n")

                print("Sync completed with changes.")
            else:
                print("No changes detected.")

                if github_output_path:
                    with open(github_output_path, "a", encoding="utf-8") as fh:
                        fh.write("changed=false\n")

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

    args = sys.argv[1:]
    if not args:
        ControldSync().run(github_token)
    elif args == ["--commit"]:
        ControldSync().commit(github_token, repo_name)
    else:
        print(f"ERROR: unknown arguments: {args} (expected no argument or --commit)")
        sys.exit(2)


if __name__ == "__main__":
    main()
