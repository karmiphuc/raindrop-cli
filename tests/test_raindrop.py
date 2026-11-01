import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os  # noqa: E402
# Tests must never write undo logs into the user's real state folder.
_STATE = tempfile.mkdtemp(prefix="raindrop-test-state-")
os.environ["RAINDROP_STATE_DIR"] = _STATE

import raindrop as rd  # noqa: E402

TOKEN = "test-token-1234567890abcdef"


class FakeAPI:
    """Records requests and replays canned (status, headers, payload) responses per path."""

    def __init__(self, routes: dict):
        self.routes = routes  # (method, path) -> list of responses (popped in order) or one response
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body):
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.replace("/rest/v1", "", 1)
        self.calls.append({"method": method, "path": path, "query": urllib.parse.parse_qs(parsed.query),
                           "headers": headers, "body": json.loads(body) if body else None})
        resp = self.routes[(method, path)]
        if isinstance(resp, list):
            resp = resp.pop(0)
        status, hdrs, payload = resp
        return status, hdrs, json.dumps(payload).encode() if payload is not None else b""


def page(n: int, start: int = 0) -> tuple:
    return 200, {}, {"result": True, "items": [{"_id": start + i, "link": f"https://x/{start + i}", "title": f"t{start + i}"} for i in range(n)]}


class RaindropTests(unittest.TestCase):
    def client(self, routes):
        api = FakeAPI(routes)
        sleeps: list[float] = []
        return rd.Client(TOKEN, transport=api, sleep=sleeps.append), api, sleeps

    def test_auth_header_and_user(self):
        c, api, _ = self.client({("GET", "/user"): (200, {}, {"result": True, "user": {"_id": 1, "fullName": "K"}})})
        self.assertEqual(c.user()["fullName"], "K")
        self.assertEqual(api.calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_search_paginates_and_respects_limit(self):
        c, api, _ = self.client({("GET", "/raindrops/0"): [page(50), page(50, 50), page(7, 100)]})
        items = c.raindrops(0, "ai agents", "-created", limit=60)
        self.assertEqual(len(items), 60)
        self.assertEqual(len(api.calls), 2)  # stopped once the limit was reached
        self.assertEqual(api.calls[0]["query"]["search"], ["ai agents"])
        self.assertEqual(api.calls[1]["query"]["page"], ["1"])
        self.assertEqual(api.calls[0]["query"]["perpage"], ["50"])

    def test_export_reads_everything_and_is_stable(self):
        c, _, _ = self.client({("GET", "/raindrops/0"): [page(50, 100), page(3, 0)]})
        items = rd.export_items(c.raindrops(0, limit=None))
        self.assertEqual(len(items), 53)
        self.assertEqual([i["_id"] for i in items], sorted(i["_id"] for i in items))
        c2, _, _ = self.client({("GET", "/raindrops/0"): [page(3, 0), page(50, 100)][::-1]})
        again = rd.export_items(c2.raindrops(0, limit=None))
        self.assertEqual(json.dumps(items), json.dumps(again))

    def test_add_body_shape(self):
        c, api, _ = self.client({("POST", "/raindrop"): (200, {}, {"result": True, "item": {"_id": 9, "link": "https://a"}})})
        c.add("https://a", title="A", tags=["x", "y"], collection=-1, note="n")
        body = api.calls[0]["body"]
        self.assertEqual(body, {"link": "https://a", "title": "A", "tags": ["x", "y"],
                                "collection": {"$id": -1}, "note": "n", "pleaseParse": {}})
        self.assertEqual(api.calls[0]["headers"]["Content-Type"], "application/json")

    def test_retries_429_then_succeeds(self):
        c, api, sleeps = self.client({("GET", "/user"): [(429, {"X-RateLimit-Reset": "0"}, None),
                                                          (200, {}, {"result": True, "user": {"_id": 1}})]})
        self.assertEqual(c.user()["_id"], 1)
        self.assertEqual(len(sleeps), 1)

    def test_retries_5xx_but_not_4xx(self):
        c, _, sleeps = self.client({("GET", "/user"): [(502, {}, None), (200, {}, {"result": True, "user": {"_id": 2}})]})
        self.assertEqual(c.user()["_id"], 2)
        c, api, _ = self.client({("GET", "/user"): [(401, {}, {"result": False, "errorMessage": "Unauthorized"})]})
        with self.assertRaises(rd.RaindropError) as ctx:
            c.user()
        self.assertEqual(len(api.calls), 1)
        self.assertIn("check your token", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_token_sources_and_missing_message(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "token"
            self.assertEqual(rd.load_token({"RAINDROP_TOKEN": " abc "}, f), "abc")
            f.write_text("from-file\n", encoding="utf-8")
            self.assertEqual(rd.load_token({}, f), "from-file")
            f.unlink()
            with self.assertRaises(rd.RaindropError) as ctx:
                rd.load_token({}, f)
            self.assertIn("settings/integrations", str(ctx.exception))

    def test_collection_tree_tolerates_null_sort_and_title(self):
        # Seen live on 2026-11-01: some collections come back with "sort": null.
        items = [{"_id": 1, "title": "A", "sort": None, "count": 1},
                 {"_id": 2, "title": None, "sort": 3, "count": 0},
                 {"_id": 3, "title": "C", "count": 2, "parent": {"$id": 1}, "sort": None}]
        lines = rd.collection_tree(items)
        self.assertEqual(len(lines), 3)

    def test_collections_dedupes_overlap_between_endpoints(self):
        c, _, _ = self.client({
            ("GET", "/collections"): (200, {}, {"result": True, "items": [{"_id": 1, "title": "A"}]}),
            ("GET", "/collections/childrens"): (200, {}, {"result": True, "items": [
                {"_id": 1, "title": "A"}, {"_id": 2, "title": "B", "parent": {"$id": 1}}]}),
        })
        self.assertEqual([x["_id"] for x in c.collections()], [1, 2])

    def test_export_cli_both_formats_end_to_end(self):
        # Seen live: md export crashed on export_items rows (collection already flattened to an int).
        raw = [{"_id": 2, "link": "https://b", "title": "Bé ✓", "tags": ["x"], "collection": {"$id": -1}, "note": "n"},
               {"_id": 1, "link": "https://a", "title": "A", "collection": {"$id": 7}}]
        for fmt in ("json", "md"):
            c, _, _ = self.client({("GET", "/raindrops/0"): (200, {}, {"result": True, "items": [dict(r) for r in raw]})})
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / f"out.{fmt}"
                rd.run(["export", "--format", fmt, "-o", str(path)], client=c, out=io.StringIO())
                text = path.read_text(encoding="utf-8")
            if fmt == "json":
                self.assertEqual([r["collection"] for r in json.loads(text)], [7, -1])
            else:
                self.assertTrue(text.startswith("# Raindrop export"))
                self.assertIn("collection -1", text)
                self.assertIn("Bé ✓", text)

    def test_cli_has_no_delete_command(self):
        for cmd in ("delete", "remove", "trash"):
            with self.assertRaises(SystemExit):
                rd.build_parser().parse_args([cmd, "1"])

    def test_cli_json_and_tree_output(self):
        c, _, _ = self.client({
            ("GET", "/collections"): (200, {}, {"result": True, "items": [{"_id": 1, "title": "Root", "count": 2}]}),
            ("GET", "/collections/childrens"): (200, {}, {"result": True, "items": [{"_id": 2, "title": "Kid", "count": 1, "parent": {"$id": 1}}]}),
        })
        buf = io.StringIO()
        rd.run(["collections"], client=c, out=buf)
        lines = buf.getvalue().splitlines()
        self.assertIn("Root", lines[0])
        self.assertTrue(lines[1].startswith("  ") and "Kid" in lines[1])
        c, _, _ = self.client({("GET", "/tags"): (200, {}, {"result": True, "items": [{"_id": "ai", "count": 3}]})})
        buf = io.StringIO()
        rd.run(["tags", "--json"], client=c, out=buf)
        self.assertEqual(json.loads(buf.getvalue())[0]["_id"], "ai")


if __name__ == "__main__":
    unittest.main()


class StatefulFake:
    """Minimal in-memory Raindrop: GET/PUT /raindrop/{id}. Records every request."""

    def __init__(self, items):
        self.items = {i["_id"]: json.loads(json.dumps(i)) for i in items}
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = urllib.parse.urlparse(url).path.replace("/rest/v1", "", 1)
        self.calls.append((method, path))
        rid = int(path.rsplit("/", 1)[1])
        if method == "PUT":
            data = json.loads(body)
            item = self.items[rid]
            if "tags" in data:
                item["tags"] = data["tags"]
            if "collection" in data:
                item["collection"] = {"$id": data["collection"]["$id"]}
            if "note" in data:
                item["note"] = data["note"]
        return 200, {}, json.dumps({"result": True, "item": self.items[rid]}).encode()


class BulkChangeTests(unittest.TestCase):
    def setUp(self):
        self.fake = StatefulFake([
            {"_id": 1, "link": "https://a", "tags": ["bookmark", "ai"], "collection": {"$id": -1}, "note": ""},
            {"_id": 2, "link": "https://b", "tags": ["7/10/2025"], "collection": {"$id": 5}, "note": "keep me"},
        ])
        self.client = rd.Client(TOKEN, transport=self.fake, sleep=lambda s: None)
        self.ops = [
            {"id": 1, "remove_tags": ["bookmark"], "add_tags": ["agents"], "move": 42},
            {"id": 2, "remove_tags": ["7/10/2025"], "append_note": "Claude (2026-11-01): summary"},
        ]
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "undo.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_ops(self, ops, execute):
        return rd.apply_ops(self.client, ops, execute=execute, undo_log=self.log, sleep=lambda s: None, out=io.StringIO())

    def test_dry_run_changes_nothing(self):
        before = json.dumps(self.fake.items, sort_keys=True)
        stats = self.run_ops(self.ops, execute=False)
        self.assertEqual(stats["would_change"], 2)
        self.assertEqual(json.dumps(self.fake.items, sort_keys=True), before)
        self.assertNotIn("PUT", [m for m, _ in self.fake.calls])
        self.assertFalse(self.log.exists())

    def test_apply_then_reapply_is_idempotent_and_undo_restores(self):
        original = json.dumps(self.fake.items, sort_keys=True)
        self.assertEqual(self.run_ops(self.ops, execute=True)["changed"], 2)
        self.assertEqual(self.fake.items[1]["tags"], ["ai", "agents"])
        self.assertEqual(self.fake.items[1]["collection"]["$id"], 42)
        self.assertEqual(self.fake.items[2]["note"], "keep me\n\nClaude (2026-11-01): summary")
        again = self.run_ops(self.ops, execute=True)
        self.assertEqual((again["changed"], again["unchanged"]), (0, 2))  # second run is a no-op
        undo_ops = [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines()][::-1]
        self.run_ops(undo_ops, execute=True)
        self.assertEqual(json.dumps(self.fake.items, sort_keys=True), original)

    def test_undo_log_written_before_put(self):
        order = []
        real = self.fake.__call__

        def spy(method, url, headers, body):
            if method == "PUT":
                order.append(("put", self.log.exists()))
            return real(method, url, headers, body)
        self.client = rd.Client(TOKEN, transport=spy, sleep=lambda s: None)
        self.run_ops(self.ops[:1], execute=True)
        self.assertEqual(order, [("put", True)])

    def test_move_to_trash_is_refused(self):
        with self.assertRaises(rd.RaindropError):
            self.run_ops([{"id": 1, "move": -99}], execute=True)
        self.assertEqual(self.fake.items[1]["collection"]["$id"], -1)

    def test_cli_apply_is_dry_run_without_yes(self):
        plan = Path(self.tmp.name) / "plan.json"
        plan.write_text(json.dumps(self.ops), encoding="utf-8")
        buf = io.StringIO()
        rd.run(["apply", str(plan)], client=self.client, out=buf)
        self.assertIn("dry run", buf.getvalue())
        self.assertNotIn("PUT", [m for m, _ in self.fake.calls])


class NoteFileTests(unittest.TestCase):
    def test_append_note_file_keeps_all_lines(self):
        # Seen live: a multi-line --append-note through raindrop.cmd kept only the first line.
        fake = StatefulFake([{"_id": 7, "link": "https://x", "tags": [], "collection": {"$id": -1}, "note": ""}])
        client = rd.Client(TOKEN, transport=fake, sleep=lambda s: None)
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "note.md"
            f.write_text("line one\n- line two\n- line three", encoding="utf-8")
            rd.run(["update", "7", "--append-note-file", str(f), "--add-tags", "claude-done", "--move", "5"],
                   client=client, out=io.StringIO())
        item = fake.items[7]
        self.assertEqual(item["note"], "line one\n- line two\n- line three")
        self.assertEqual(item["tags"], ["claude-done"])
        self.assertEqual(item["collection"]["$id"], 5)


class StateIsolationTests(unittest.TestCase):
    def test_default_undo_log_uses_test_state_dir(self):
        self.assertTrue(str(rd.default_undo_log()).startswith(_STATE))
