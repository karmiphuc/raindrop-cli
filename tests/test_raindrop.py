import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    def test_cli_has_no_delete_command(self):
        with self.assertRaises(SystemExit):
            rd.build_parser().parse_args(["delete", "1"])

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
