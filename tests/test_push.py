"""Unit tests for scripts/controld_api_push.py (no network, no real account).

The Control D API is replaced by FakeControlD, an in-memory stand-in for the few
endpoints the script uses.  It models the documented shapes, not undocumented
behaviour, so these tests prove the script's own logic (reconciliation, safety
checks, action handling) -- they cannot prove the live API accepts the requests.
"""

import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import controld_api_push as push  # noqa: E402
import controld_config as cc  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeControlD:
    """In-memory Control D: profiles -> folders -> rules (one rule per hostname)."""

    def __init__(self):
        self.profiles = {}    # profile_pk -> {"name": str, "folders": {folder_pk: name}}
        self.rules = {}       # profile_pk -> {hostname: {"folder", "do", "status"}}
        self.posts = []       # (profile_pk, payload)
        self.deletes = []     # (profile_pk, hostname)
        self.fail_posts = False
        self.fail_folders = set()   # folder PKs whose POSTs return HTTP 500
        self.rules_body = None   # override GET rules "body" (e.g. [] like an error)

    def add_profile(self, name, folder_names):
        pk = f"prof{len(self.profiles) + 1}"
        folders = {f"{len(self.profiles) + 1}0{i}": n for i, n in enumerate(folder_names, 1)}
        self.profiles[pk] = {"name": name, "folders": folders}
        self.rules[pk] = {}
        return pk, {n: fpk for fpk, n in folders.items()}

    def seed(self, profile_pk, folder_pk, hostnames, do=0):
        for h in hostnames:
            self.rules[profile_pk][h] = {"folder": folder_pk, "do": do, "status": 1}

    def folder_rules(self, profile_pk, folder_pk):
        return {h: r for h, r in self.rules[profile_pk].items() if r["folder"] == folder_pk}

    # -- requests.get / post / delete replacements ---------------------------
    def _path(self, url):
        assert url.startswith(push.BASE_URL), url
        return url[len(push.BASE_URL):].strip("/").split("/")

    def get(self, url, headers=None, timeout=None):
        parts = self._path(url)
        if parts == ["profiles"]:
            return FakeResponse(payload={"body": {"profiles": [
                {"PK": pk, "name": p["name"]} for pk, p in self.profiles.items()]}})
        if len(parts) == 3 and parts[2] == "groups":
            folders = self.profiles[parts[1]]["folders"]
            return FakeResponse(payload={"body": {"groups": [
                {"PK": fpk, "group": name} for fpk, name in folders.items()]}})
        if len(parts) == 4 and parts[2] == "rules":
            if self.rules_body is not None:
                return FakeResponse(payload={"body": self.rules_body})
            rules = self.folder_rules(parts[1], parts[3])
            return FakeResponse(payload={"body": {"rules": [
                {"PK": h, "action": {"do": r["do"], "status": r["status"]}}
                for h, r in rules.items()]}})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        parts = self._path(url)
        assert len(parts) == 3 and parts[2] == "rules", url
        if self.fail_posts or json["group"] in self.fail_folders:
            return FakeResponse(500)
        self.posts.append((parts[1], json))
        action = json["action"]
        for h in json["hostnames"]:
            self.rules[parts[1]][h] = {
                "folder": json["group"], "do": action["do"], "status": action["status"]}
        return FakeResponse(payload={"success": True})

    def delete(self, url, headers=None, timeout=None):
        parts = self._path(url)
        assert len(parts) == 4 and parts[2] == "rules", url
        host = unquote(parts[3])
        self.deletes.append((parts[1], host))
        if host not in self.rules[parts[1]]:
            return FakeResponse(404)
        del self.rules[parts[1]][host]
        return FakeResponse(payload={"success": True})


def rules_file(directory, name, rules, group_action=None):
    """Write a Hagezi-format file; `rules` is [(hostname, do_or_None)]."""
    data = {"group": {"group": "G"}, "rules": []}
    if group_action:
        data["group"]["action"] = group_action
    for host, do in rules:
        rule = {"PK": host}
        if do is not None:
            rule["action"] = {"do": do, "status": 1}
        data["rules"].append(rule)
    path = Path(directory) / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeControlD()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, replacement in (
            (push.requests, "get"), (push.requests, "post"), (push.requests, "delete"),
        ):
            patcher = mock.patch.object(target, replacement, getattr(self.api, replacement))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(push.time, "sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        # Keep INFO progress lines out of the test output; errors still surface.
        logging.disable(logging.INFO)
        self.addCleanup(logging.disable, logging.NOTSET)


class ExtractDesiredRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="f.json"):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_block_and_allow_actions_are_read_per_rule(self):
        path = rules_file(self.tmp.name, "mixed.json", [("a.example", 0), ("b.example", 1)])
        self.assertEqual(
            push.extract_desired_rules(path), {"a.example": (0, 1), "b.example": (1, 1)})

    def test_group_action_is_the_fallback_per_field(self):
        path = rules_file(self.tmp.name, "g.json", [("a.example", None)],
                          group_action={"do": 1, "status": 1})
        self.assertEqual(push.extract_desired_rules(path), {"a.example": (1, 1)})

    def test_rule_action_wins_over_group_action(self):
        path = rules_file(self.tmp.name, "g.json", [("a.example", 0)],
                          group_action={"do": 1, "status": 1})
        self.assertEqual(push.extract_desired_rules(path), {"a.example": (0, 1)})

    def test_hostnames_are_normalised_and_blank_ones_skipped(self):
        path = rules_file(self.tmp.name, "n.json", [("  A.Example ", 0), ("   ", 0)])
        self.assertEqual(push.extract_desired_rules(path), {"a.example": (0, 1)})

    def test_identical_duplicates_are_fine(self):
        path = rules_file(self.tmp.name, "d.json", [("a.example", 0), ("A.EXAMPLE", 0)])
        self.assertEqual(push.extract_desired_rules(path), {"a.example": (0, 1)})

    def test_refuses_untrustworthy_files(self):
        cases = {
            "missing": None,
            "not json": "{nope",
            "top-level list": "[]",
            "no rules": '{"group": {}}',
            "rules not a list": '{"rules": {"PK": "a.example"}}',
            "empty rules": '{"rules": []}',
            "rule not an object": '{"rules": ["a.example"]}',
            "PK null": '{"rules": [{"PK": null, "action": {"do": 0, "status": 1}}]}',
            "PK number": '{"rules": [{"PK": 5, "action": {"do": 0, "status": 1}}]}',
            "no action anywhere": '{"rules": [{"PK": "a.example"}]}',
            "do missing": '{"rules": [{"PK": "a.example", "action": {"status": 1}}]}',
            "status missing": '{"rules": [{"PK": "a.example", "action": {"do": 0}}]}',
            "spoof unsupported": '{"rules": [{"PK": "a.example", "action": {"do": 2, "status": 1}}]}',
            "bool do": '{"rules": [{"PK": "a.example", "action": {"do": true, "status": 1}}]}',
            "string do": '{"rules": [{"PK": "a.example", "action": {"do": "0", "status": 1}}]}',
            "conflicting duplicate": '{"rules": [{"PK": "a.example", "action": {"do": 0, "status": 1}},'
                                     '{"PK": "A.example", "action": {"do": 1, "status": 1}}]}',
        }
        for label, text in cases.items():
            path = str(Path(self.tmp.name) / "absent.json") if text is None else self.write(text)
            with self.subTest(label), self.assertLogs(push.log, level="ERROR"):
                self.assertIsNone(push.extract_desired_rules(path))

    def test_refuses_invalid_utf8(self):
        path = Path(self.tmp.name) / "bin.json"
        path.write_bytes(b'{"rules": [{"PK": "\xff\xfe"}]}')
        with self.assertLogs(push.log, level="ERROR"):
            self.assertIsNone(push.extract_desired_rules(str(path)))

    def test_hostnames_are_not_written_to_logs(self):
        path = self.write('{"rules": [{"PK": "secret-name.example"}]}')
        with self.assertLogs(push.log, level="ERROR") as logs:
            push.extract_desired_rules(path)
        self.assertNotIn("secret-name.example", "\n".join(logs.output))


class SyncFolderTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.pk, self.folders = self.api.add_profile("P", ["F"])
        self.fpk = self.folders["F"]

    def sync(self, desired, max_delete_percent=50):
        return push.sync_folder(self.pk, self.fpk, "F", desired, "token", max_delete_percent)

    def test_additions_carry_the_action_from_the_file(self):
        ok, added, removed = self.sync({"block.example": (0, 1), "allow.example": (1, 1)})
        self.assertTrue(ok)
        self.assertEqual(added, ["allow.example", "block.example"])
        self.assertEqual(removed, [])
        rules = self.api.folder_rules(self.pk, self.fpk)
        self.assertEqual(rules["block.example"]["do"], 0)
        self.assertEqual(rules["allow.example"]["do"], 1)
        # one POST per distinct action
        self.assertEqual(sorted(p["action"]["do"] for _, p in self.api.posts), [0, 1])
        for _, payload in self.api.posts:
            self.assertEqual(payload["group"], self.fpk)

    def test_removes_what_is_not_in_the_file_and_is_idempotent(self):
        self.api.seed(self.pk, self.fpk, ["keep.example", "stale.example"])
        desired = {"keep.example": (0, 1), "new.example": (0, 1)}
        self.assertEqual(self.sync(desired, 100), (True, ["new.example"], ["stale.example"]))
        self.assertEqual(set(self.api.folder_rules(self.pk, self.fpk)), set(desired))

        self.api.posts.clear()
        self.api.deletes.clear()
        self.assertEqual(self.sync(desired, 100), (True, [], []))
        self.assertEqual((self.api.posts, self.api.deletes), ([], []))

    def test_additions_are_batched(self):
        desired = {f"h{i}.example": (0, 1) for i in range(5)}
        with mock.patch.object(push, "PAGE_SIZE", 2):
            self.assertTrue(self.sync(desired)[0])
        self.assertEqual([len(p["hostnames"]) for _, p in self.api.posts], [2, 2, 1])

    def test_delete_guard_aborts_before_touching_anything(self):
        self.api.seed(self.pk, self.fpk, [f"h{i}.example" for i in range(10)])
        with self.assertLogs(push.log, level="ERROR"):
            ok, added, removed = self.sync({"h0.example": (0, 1), "new.example": (0, 1)}, 50)
        self.assertEqual((ok, added, removed), (False, [], []))
        self.assertEqual((self.api.posts, self.api.deletes), ([], []))
        self.assertEqual(len(self.api.folder_rules(self.pk, self.fpk)), 10)

    def test_delete_guard_allows_removals_within_the_threshold(self):
        self.api.seed(self.pk, self.fpk, [f"h{i}.example" for i in range(10)])
        desired = {f"h{i}.example": (0, 1) for i in range(5, 10)}   # removes exactly 50 %
        self.assertTrue(self.sync(desired, 50)[0])

    def test_delete_of_an_already_missing_rule_counts_as_success(self):
        with mock.patch.object(push, "fetch_live_hostnames", return_value={"gone.example"}):
            self.assertEqual(self.sync({"x.example": (0, 1)}, 100)[2], ["gone.example"])
        self.assertEqual(self.api.deletes, [(self.pk, "gone.example")])

    def test_failed_add_fails_the_folder(self):
        self.api.fail_posts = True
        with self.assertLogs(push.log, level="ERROR"), \
                mock.patch.object(push, "API_RETRY_DELAYS", [0, 0, 0]):
            ok, added, _ = self.sync({"a.example": (0, 1)})
        self.assertFalse(ok)
        self.assertEqual(added, [])

    def test_error_shaped_response_is_a_failure_not_an_empty_folder(self):
        # Error responses carry "body": []; reading that as "empty folder" would
        # re-add every rule and hide the real problem.
        self.api.rules_body = []
        with self.assertLogs(push.log, level="ERROR"), \
                mock.patch.object(push, "API_RETRY_DELAYS", [0, 0, 0]):
            self.assertEqual(self.sync({"a.example": (0, 1)}), (False, [], []))
        self.assertEqual(self.api.posts, [])


class RunTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(push, "CONTROLD_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pk, self.folders = self.api.add_profile("Home", ["Allow", "Block"])

    def config(self, *lists):
        return cc.parse_config({"settings": {"max_delete_percent": 100}, "lists": list(lists)})

    @staticmethod
    def entry(file, *targets):
        return {"file": file, "targets": [{"profile": p, "folder": f} for p, f in targets]}

    def test_allow_before_block_dedup_and_actions(self):
        rules_file(self.tmp.name, "allow.json", [("both.example", 1), ("allow.example", 1)])
        rules_file(self.tmp.name, "block.json", [("both.example", 0), ("block.example", 0)])
        config = self.config(
            self.entry("allow.json", ("Home", "Allow")),
            self.entry("block.json", ("Home", "Block")),
        )
        ok, body = push.run("token", config)

        self.assertTrue(ok)
        allow = self.api.folder_rules(self.pk, self.folders["Allow"])
        block = self.api.folder_rules(self.pk, self.folders["Block"])
        self.assertEqual({h: r["do"] for h, r in allow.items()},
                         {"both.example": 1, "allow.example": 1})
        self.assertEqual({h: r["do"] for h, r in block.items()}, {"block.example": 0})
        self.assertIn("Skipped (1)", body)

    def test_second_run_changes_nothing(self):
        rules_file(self.tmp.name, "allow.json", [("a.example", 1)])
        config = self.config(self.entry("allow.json", ("Home", "Allow")))
        self.assertTrue(push.run("token", config)[0])
        self.api.posts.clear()
        self.assertTrue(push.run("token", config)[0])
        self.assertEqual((self.api.posts, self.api.deletes), ([], []))

    def test_unknown_profile_and_folder_fail_the_run_without_writes(self):
        rules_file(self.tmp.name, "block.json", [("a.example", 0)])
        for target in (("Nope", "Block"), ("Home", "Nope")):
            config = self.config(self.entry("block.json", target))
            with self.subTest(target), self.assertLogs(push.log, level="ERROR"):
                ok, body = push.run("token", config)
            self.assertFalse(ok)
            self.assertIn("not found", body)
        self.assertEqual(self.api.posts, [])

    def test_unreadable_file_fails_the_run_but_other_files_still_sync(self):
        rules_file(self.tmp.name, "block.json", [("a.example", 0)])
        (Path(self.tmp.name) / "bad.json").write_text("{nope", encoding="utf-8")
        config = self.config(
            self.entry("bad.json", ("Home", "Allow")),
            self.entry("block.json", ("Home", "Block")),
        )
        with self.assertLogs(push.log, level="ERROR"):
            ok, _ = push.run("token", config)
        self.assertFalse(ok)
        self.assertEqual(set(self.api.folder_rules(self.pk, self.folders["Block"])), {"a.example"})
        self.assertEqual(self.api.folder_rules(self.pk, self.folders["Allow"]), {})

    def test_failed_folder_does_not_claim_its_domains(self):
        # If the allow folder fails, the block folder must not treat its
        # domains as already handled: otherwise the shared domain would end up
        # in neither folder.
        rules_file(self.tmp.name, "allow.json", [("shared.example", 1)])
        rules_file(self.tmp.name, "block.json", [("shared.example", 0), ("b.example", 0)])
        config = self.config(
            self.entry("allow.json", ("Home", "Allow")),
            self.entry("block.json", ("Home", "Block")),
        )
        self.api.fail_folders = {self.folders["Allow"]}
        with self.assertLogs(push.log, level="ERROR"), \
                mock.patch.object(push, "API_RETRY_DELAYS", [0, 0, 0]):
            ok, body = push.run("token", config)
        self.assertFalse(ok)
        self.assertEqual(self.api.folder_rules(self.pk, self.folders["Allow"]), {})
        block = self.api.folder_rules(self.pk, self.folders["Block"])
        self.assertEqual(set(block), {"shared.example", "b.example"})
        self.assertNotIn("Skipped", body)


class SendEmailTests(unittest.TestCase):
    def test_skipped_without_credentials(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(push.smtplib, "SMTP_SSL") as smtp, \
                self.assertLogs(push.log, level="WARNING"):
            push.send_email("body")
        smtp.assert_not_called()

    def test_bad_port_is_reported_not_raised(self):
        env = {"EMAIL_USERNAME": "u", "EMAIL_PASSWORD": "p", "EMAIL_SMTP_PORT": "abc"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch.object(push.smtplib, "SMTP_SSL") as smtp, \
                self.assertLogs(push.log, level="ERROR"):
            push.send_email("body")
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
