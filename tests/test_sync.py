"""Unit tests for scripts/controld_sync.py (no network, no git remote)."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import controld_sync as sync  # noqa: E402

RAW = "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/controld/"


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeUpstream:
    """Serves the GitHub contents listing, raw files and the mirror."""

    def __init__(self, files, download_hosts=None):
        self.files = files                       # name -> bytes
        self.download_hosts = download_hosts or {}   # name -> custom download_url
        self.listing_calls = 0
        self.mirror_calls = 0
        self.github_down = False

    def get(self, url, headers=None, timeout=None):
        if url == sync.UPSTREAM_API_URL:
            self.listing_calls += 1
            if self.github_down:
                raise requests.ConnectionError("github unreachable")
            return FakeResponse(payload=[
                {"name": n, "type": "file",
                 "download_url": self.download_hosts.get(n, RAW + n)}
                for n in self.files
            ] + [{"name": "sub", "type": "dir", "download_url": None}])
        if url.startswith(sync.MIRROR_RAW_BASE_URL):
            self.mirror_calls += 1
            name = url[len(sync.MIRROR_RAW_BASE_URL):]
            if name in self.files:
                return FakeResponse(content=self.files[name])
            return FakeResponse(404)
        if url.startswith(RAW):
            if self.github_down:
                raise requests.ConnectionError("github unreachable")
            return FakeResponse(content=self.files[url[len(RAW):]])
        raise AssertionError(f"unexpected request to {url}")


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.temp_dir, self.target_dir = root / "temp_controld", root / "controld"
        for name, value in (
            ("TEMP_DIR", self.temp_dir), ("TARGET_DIR", self.target_dir),
            ("MAX_ATTEMPTS", 2), ("RETRY_DELAY", 0),
        ):
            patcher = mock.patch.object(sync, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, upstream):
        patcher = mock.patch.object(sync.requests, "get", upstream.get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def download(self, names, upstream, mirror_fallback=True):
        self.install(upstream)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = sync.ControldSync(names, mirror_fallback).download_files("token")
        return ok, out.getvalue()


class DownloadTests(SyncTestCase):
    def test_downloads_only_the_configured_files(self):
        upstream = FakeUpstream({"a.json": b"A", "b.json": b"B", "c.json": b"C"})
        ok, _ = self.download(["a.json", "c.json"], upstream)
        self.assertTrue(ok)
        self.assertEqual(sorted(p.name for p in self.temp_dir.iterdir()), ["a.json", "c.json"])
        self.assertEqual((self.temp_dir / "c.json").read_bytes(), b"C")

    def test_typo_fails_immediately_and_names_every_bad_file(self):
        upstream = FakeUpstream({"spam-tlds-folder.json": b"x", "other.json": b"y"})
        ok, output = self.download(
            ["spam-tld-folder.json", "spam-tlds-folder.json", "nonexistent.json"], upstream)
        self.assertFalse(ok)
        self.assertEqual(upstream.listing_calls, 1, "a typo must not be retried")
        self.assertEqual(upstream.mirror_calls, 0, "a typo must not fall back to the mirror")
        self.assertIn("spam-tld-folder.json", output)
        self.assertIn("nonexistent.json", output)
        self.assertIn("Did you mean: spam-tlds-folder.json", output)

    def test_an_empty_listing_is_treated_as_transient_not_as_a_typo(self):
        upstream = FakeUpstream({})      # GitHub answers 200 with no files in it
        real_get = upstream.get

        def get(url, **kwargs):          # ... while the mirror still has the file
            if url.startswith(sync.MIRROR_RAW_BASE_URL):
                return FakeResponse(content=b"mirror")
            return real_get(url, **kwargs)

        upstream.get = get
        ok, output = self.download(["a.json"], upstream)
        self.assertTrue(ok)
        self.assertEqual(upstream.listing_calls, sync.MAX_ATTEMPTS)
        self.assertIn("falling back to mirror", output)

    def test_other_download_failures_are_retried_then_use_the_mirror(self):
        upstream = FakeUpstream({"a.json": b"from-mirror"})
        upstream.github_down = True
        ok, output = self.download(["a.json"], upstream)
        self.assertTrue(ok)
        self.assertEqual(upstream.listing_calls, sync.MAX_ATTEMPTS)
        self.assertIn("falling back to mirror", output)
        self.assertEqual((self.temp_dir / "a.json").read_bytes(), b"from-mirror")

    def test_mirror_fallback_can_be_disabled(self):
        upstream = FakeUpstream({"a.json": b"x"})
        upstream.github_down = True
        ok, output = self.download(["a.json"], upstream, mirror_fallback=False)
        self.assertFalse(ok)
        self.assertEqual(upstream.mirror_calls, 0)
        self.assertIn("disabled", output)

    def test_download_urls_off_the_allowed_hosts_are_never_fetched(self):
        hostile = [
            "https://evil.test/a.json",
            "https://raw.githubusercontent.com@evil.test/a.json",     # userinfo trick
            "https://evil-githubusercontent.com/a.json",              # look-alike suffix
            "https://githubusercontent.com.evil.test/a.json",
            "file:///etc/passwd",
            "ftp://raw.githubusercontent.com/a.json",
        ]
        for url in hostile:
            with self.subTest(url):
                upstream = FakeUpstream({"a.json": b"x"}, download_hosts={"a.json": url})
                real_get = upstream.get
                fetched = []

                def spy(u, **kw):
                    fetched.append(u)
                    return real_get(u, **kw)

                spy_upstream = mock.Mock(get=spy, **{})
                self.install(spy_upstream)
                with contextlib.redirect_stdout(io.StringIO()):
                    ok = sync.ControldSync(["a.json"], False).download_files("token")
                self.assertFalse(ok)
                self.assertNotIn(url, fetched)

    def test_githubusercontent_subdomains_are_accepted(self):
        url = "https://objects.githubusercontent.com/a.json"
        upstream = FakeUpstream({"a.json": b"x"}, download_hosts={"a.json": url})
        real_get = upstream.get
        upstream.get = lambda u, **kw: FakeResponse(content=b"x") if u == url else real_get(u, **kw)
        ok, _ = self.download(["a.json"], upstream, mirror_fallback=False)
        self.assertTrue(ok)

    def test_github_token_is_sent_only_to_the_listing_endpoint(self):
        seen = []
        upstream = FakeUpstream({"a.json": b"x"})

        def spy(url, headers=None, timeout=None):
            seen.append((url, headers))
            return upstream.get(url, headers=headers, timeout=timeout)

        self.install(mock.Mock(get=spy))
        with contextlib.redirect_stdout(io.StringIO()):
            sync.ControldSync(["a.json"], True).download_files("s3cret")
        for url, headers in seen:
            if url == sync.UPSTREAM_API_URL:
                self.assertEqual(headers, {"Authorization": "Bearer s3cret"})
            else:
                self.assertFalse(headers, f"token leaked to {url}")


class SyncFilesTests(SyncTestCase):
    def stage(self, **files):
        self.temp_dir.mkdir(exist_ok=True)
        for name, data in files.items():
            (self.temp_dir / name).write_bytes(data)

    def test_new_changed_and_unchanged_files(self):
        self.target_dir.mkdir()
        (self.target_dir / "same.json").write_bytes(b"1")
        (self.target_dir / "changed.json").write_bytes(b"old")
        self.stage(**{"same.json": b"1", "changed.json": b"new", "added.json": b"2"})
        engine = sync.ControldSync(["same.json", "changed.json", "added.json"], True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.sync_files(), ["changed.json", "added.json"])
        self.assertEqual((self.target_dir / "changed.json").read_bytes(), b"new")

    def test_a_missing_download_is_an_error_not_a_deletion(self):
        self.stage(**{"a.json": b"1"})
        engine = sync.ControldSync(["a.json", "b.json"], True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(engine.sync_files())


class RunTests(SyncTestCase):
    def run_stage1(self, upstream, force=None):
        """One Stage 1 run; returns the line it wrote to $GITHUB_OUTPUT."""
        self.install(upstream)
        output = Path(self.tmp.name) / "github_output"
        output.write_text("")     # a runner gives every step a fresh, empty file
        env = {"GITHUB_OUTPUT": str(output)}
        if force is not None:
            env["FORCE_PUSH"] = force
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            sync.ControldSync(["a.json"], False).run("token")
        return output.read_text().strip()

    def test_first_run_writes_the_file_and_cleans_up(self):
        upstream = FakeUpstream({"a.json": b"v1"})
        self.assertEqual(self.run_stage1(upstream), "changed=true")
        self.assertEqual((self.target_dir / "a.json").read_bytes(), b"v1")
        self.assertFalse(self.temp_dir.exists(), "temp dir must be cleaned up")

    def test_upstream_change_is_detected(self):
        self.assertEqual(self.run_stage1(FakeUpstream({"a.json": b"v1"})), "changed=true")
        self.assertEqual(self.run_stage1(FakeUpstream({"a.json": b"v1"})), "changed=false")
        self.assertEqual(self.run_stage1(FakeUpstream({"a.json": b"v2"})), "changed=true")

    def test_unchanged_files_report_no_change_unless_forced(self):
        self.target_dir.mkdir()
        (self.target_dir / "a.json").write_bytes(b"v1")
        upstream = FakeUpstream({"a.json": b"v1"})
        self.assertEqual(self.run_stage1(upstream), "changed=false")
        self.assertEqual(self.run_stage1(upstream, force="true"), "changed=true")
        self.assertEqual(self.run_stage1(upstream, force="false"), "changed=false")

    def test_failed_download_exits_nonzero(self):
        upstream = FakeUpstream({})
        self.install(upstream)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            sync.ControldSync(["a.json"], False).run("token")
        self.assertEqual(ctx.exception.code, 1)


class CommitTests(unittest.TestCase):
    def test_token_travels_in_the_environment_never_in_argv(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((list(args), kwargs.get("env")))
            return mock.Mock(returncode=1 if args[:3] == ["git", "diff", "--cached"] else 0)

        with mock.patch.object(sync.subprocess, "run", fake_run), \
                contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {"GITHUB_REF_NAME": "main"}):
            sync.ControldSync([], True).commit_and_push("s3cret-token", "owner/repo")

        for argv, _ in calls:
            self.assertNotIn("s3cret-token", " ".join(argv))
        push_argv, push_env = calls[-1]
        self.assertEqual(push_argv[:2], ["git", "push"])
        self.assertEqual(push_argv[2:], ["https://github.com/owner/repo.git", "HEAD:refs/heads/main"])
        self.assertEqual(push_env["GIT_CONFIG_KEY_0"], "http.https://github.com/.extraheader")
        self.assertIn("AUTHORIZATION: basic ", push_env["GIT_CONFIG_VALUE_0"])

    def test_nothing_staged_means_no_commit_and_no_push(self):
        calls = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return mock.Mock(returncode=0)

        with mock.patch.object(sync.subprocess, "run", fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            sync.ControldSync([], True).commit_and_push("t", "owner/repo")
        self.assertFalse(any(c[:2] in (["git", "commit"], ["git", "push"]) for c in calls))


if __name__ == "__main__":
    unittest.main()
