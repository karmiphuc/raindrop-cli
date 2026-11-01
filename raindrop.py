#!/usr/bin/env python3
"""raindrop: a small, dependency-free CLI for Raindrop.io (REST API v1).

Standard library only (no third-party packages). No delete commands, and nothing is ever moved to Trash.
Bulk changes (apply) are dry runs unless --yes, skip items already in the target state, and log previous
state for `undo`.

Token (never printed): env RAINDROP_TOKEN, else the first line of ~/.config/raindrop/token.
Create a "Test token" at https://app.raindrop.io/settings/integrations (register an app, open it, copy Test token).

  raindrop whoami
  raindrop collections
  raindrop search "query" [-c COLLECTION] [-n 20] [--sort -created]
  raindrop get ID
  raindrop add URL [--title T] [--tags a,b] [-c COLLECTION] [--note N] [--no-parse]
  raindrop tags [-c COLLECTION]
  raindrop export [-c 0] [--format json|md] [-o FILE]

Every command takes --json for machine-readable output. Collection ids: 0 = all (except Trash),
-1 = Unsorted, -99 = Trash.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

__version__ = "0.1.0"
API = "https://api.raindrop.io/rest/v1"
PER_PAGE = 50  # API maximum
TOKEN_FILE = Path.home() / ".config" / "raindrop" / "token"
USER_AGENT = f"karmiphuc-raindrop-cli/{__version__}"

# A transport takes (method, url, headers, body) and returns (status, headers, body_bytes).
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class RaindropError(SystemExit):
    pass


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers or {}), err.read() or b""


def load_token(env: dict | None = None, token_file: Path = TOKEN_FILE) -> str:
    env = os.environ if env is None else env
    token = (env.get("RAINDROP_TOKEN") or "").strip()
    if not token and token_file.exists():
        lines = token_file.read_text(encoding="utf-8").splitlines()
        token = lines[0].strip() if lines else ""
    if not token:
        raise RaindropError(
            "No Raindrop token. Create a Test token at https://app.raindrop.io/settings/integrations, then set\n"
            f"the RAINDROP_TOKEN environment variable or put it on the first line of {token_file}."
        )
    return token


class Client:
    def __init__(self, token: str, transport: Transport = urllib_transport, sleep: Callable[[float], None] = time.sleep):
        self._token = token
        self._transport = transport
        self._sleep = sleep

    def request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> dict:
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        for attempt in range(4):
            status, resp_headers, raw = self._transport(method, url, headers, data)
            if status == 429 and attempt < 3:  # rate limited: wait until the reset time (bounded)
                reset = _header(resp_headers, "X-RateLimit-Reset")
                wait = max(1.0, min(60.0, float(reset) - time.time())) if reset and reset.isdigit() else 5.0
                self._sleep(wait)
                continue
            if status >= 500 and attempt < 3:  # server error: safe to retry
                self._sleep(2 ** attempt)
                continue
            break
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        if status >= 400 or payload.get("result") is False:
            msg = payload.get("errorMessage") or payload.get("error") or raw[:200].decode("utf-8", "replace")
            hint = " (check your token)" if status == 401 else ""
            raise RaindropError(f"Raindrop API {method} {path} failed: HTTP {status}: {msg}{hint}")
        return payload

    # --- API calls ---------------------------------------------------------
    def user(self) -> dict:
        return self.request("GET", "/user").get("user", {})

    def collections(self) -> list[dict]:
        roots = self.request("GET", "/collections").get("items", [])
        children = self.request("GET", "/collections/childrens").get("items", [])
        # Seen live: the two endpoints can return the same collection; keep the first copy by _id.
        seen: set = set()
        out = []
        for c in roots + children:
            if c.get("_id") not in seen:
                seen.add(c.get("_id"))
                out.append(c)
        return out

    def raindrops(self, collection: int = 0, search: str | None = None, sort: str | None = None,
                  limit: int | None = 20) -> list[dict]:
        out: list[dict] = []
        page = 0
        while limit is None or len(out) < limit:
            batch = self.request("GET", f"/raindrops/{collection}",
                                 {"search": search or None, "sort": sort, "perpage": PER_PAGE, "page": page}).get("items", [])
            out.extend(batch)
            if len(batch) < PER_PAGE:
                break
            page += 1
        return out if limit is None else out[:limit]

    def raindrop(self, rid: int) -> dict:
        return self.request("GET", f"/raindrop/{rid}").get("item", {})

    def add(self, link: str, title: str | None = None, tags: list[str] | None = None,
            collection: int | None = None, note: str | None = None, parse: bool = True) -> dict:
        body: dict[str, Any] = {"link": link}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = tags
        if collection is not None:
            body["collection"] = {"$id": collection}
        if note:
            body["note"] = note
        if parse:
            body["pleaseParse"] = {}
        return self.request("POST", "/raindrop", body=body).get("item", {})

    def tags(self, collection: int | None = None) -> list[dict]:
        path = "/tags" if collection is None else f"/tags/{collection}"
        return self.request("GET", path).get("items", [])

    def update(self, rid: int, body: dict) -> dict:
        return self.request("PUT", f"/raindrop/{rid}", body=body).get("item", {})

    def new_collection(self, title: str, parent: int | None = None) -> dict:
        body: dict[str, Any] = {"title": title}
        if parent is not None:
            body["parent"] = {"$id": parent}
        return self.request("POST", "/collection", body=body).get("item", {})


# --- safe bulk changes -------------------------------------------------------
# An op changes one bookmark: {"id", "add_tags", "remove_tags", "move", "append_note"}, or, for undo,
# {"id", "set_tags", "set_collection", "set_note"}. Nothing here deletes or moves to Trash.
TRASH = -99


def plan_change(item: dict, op: dict) -> tuple[dict, dict] | None:
    """Return (PUT body, previous state) or None when the bookmark is already in the target state."""
    cur_tags = list(item.get("tags") or [])
    coll = item.get("collection")
    cur_coll = coll.get("$id") if isinstance(coll, dict) else coll
    cur_note = item.get("note") or ""
    if "set_tags" in op or "set_collection" in op or "set_note" in op:
        new_tags = op.get("set_tags", cur_tags)
        new_coll = op.get("set_collection", cur_coll)
        new_note = op.get("set_note", cur_note)
    else:
        remove = set(op.get("remove_tags") or [])
        new_tags = [t for t in cur_tags if t not in remove]
        for t in op.get("add_tags") or []:
            if t not in new_tags:
                new_tags.append(t)
        new_coll = op["move"] if op.get("move") is not None else cur_coll
        add_note = (op.get("append_note") or "").strip()
        new_note = cur_note if not add_note or add_note in cur_note else (cur_note.rstrip() + "\n\n" + add_note).strip()
    if new_coll == TRASH:
        raise RaindropError(f"refusing to move bookmark {item.get('_id')} to Trash: this CLI never deletes")
    body: dict[str, Any] = {}
    if new_tags != cur_tags:
        body["tags"] = new_tags
    if new_coll != cur_coll:
        body["collection"] = {"$id": new_coll}
    if new_note != cur_note:
        body["note"] = new_note
    if not body:
        return None
    return body, {"id": item.get("_id"), "set_tags": cur_tags, "set_collection": cur_coll, "set_note": cur_note}


def apply_ops(client: Client, ops: list[dict], *, execute: bool, undo_log: Path | None,
              throttle: float = 0.55, sleep: Callable[[float], None] = time.sleep, out=None) -> dict:
    """Dry run unless execute=True. Writes each item's previous state to undo_log BEFORE changing it."""
    out = out or sys.stdout
    stats = {"changed": 0, "unchanged": 0, "would_change": 0}
    for op in ops:
        item = client.raindrop(int(op["id"]))
        planned = plan_change(item, op)
        if planned is None:
            stats["unchanged"] += 1
            continue
        body, previous = planned
        if not execute:
            stats["would_change"] += 1
            out.write(f"would update {op['id']}: {json.dumps(body, ensure_ascii=False)}\n")
            continue
        if undo_log is not None:
            undo_log.parent.mkdir(parents=True, exist_ok=True)
            with undo_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(previous, ensure_ascii=False) + "\n")
        client.update(int(op["id"]), body)
        stats["changed"] += 1
        sleep(throttle)  # each op is a GET + PUT; stays under 120 requests/minute
    return stats


def default_undo_log() -> Path:
    return Path.home() / ".local" / "state" / "raindrop" / f"undo-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def _header(headers: dict, name: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == name.lower():
            return str(v)
    return None


# --- formatting --------------------------------------------------------------
def collection_tree(items: list[dict]) -> list[str]:
    by_parent: dict[Any, list[dict]] = {}
    for c in items:
        by_parent.setdefault((c.get("parent") or {}).get("$id"), []).append(c)
    lines: list[str] = []

    def walk(parent: Any, depth: int) -> None:
        # Real data can have "sort": null and missing titles; never compare None with int/str.
        for c in sorted(by_parent.get(parent, []), key=lambda x: (x.get("sort") or 0, x.get("title") or "")):
            lines.append(f"{'  ' * depth}{c['_id']:>10}  {c.get('title', '')}  ({c.get('count', 0)})")
            walk(c["_id"], depth + 1)

    walk(None, 0)
    return lines


def raindrop_line(r: dict) -> str:
    tags = (" #" + " #".join(r["tags"])) if r.get("tags") else ""
    return f"{r.get('_id')}  {r.get('title') or r.get('link')}\n           {r.get('link')}{tags}"


def export_markdown(items: list[dict]) -> str:
    lines = ["# Raindrop export", ""]
    for r in items:
        tags = f" — tags: {', '.join(r['tags'])}" if r.get("tags") else ""
        coll = r.get("collection")
        coll = coll.get("$id") if isinstance(coll, dict) else coll  # raw API item or export_items row
        lines.append(f"- [{(r.get('title') or r.get('link') or '').replace(']', ')')}]({r.get('link')}) "
                     f"(id {r.get('_id')}, collection {coll}){tags}")
        if r.get("note"):
            lines.append(f"  - note: {r['note'].strip()}")
    return "\n".join(lines) + "\n"


EXPORT_FIELDS = ("_id", "link", "title", "excerpt", "note", "tags", "type", "domain", "important", "created", "lastUpdate")


def export_items(items: list[dict]) -> list[dict]:
    """Stable, sorted subset so repeated exports of unchanged data are byte-identical."""
    out = []
    for r in sorted(items, key=lambda x: x.get("_id", 0)):
        row = {k: r.get(k) for k in EXPORT_FIELDS if k in r}
        row["collection"] = (r.get("collection") or {}).get("$id")
        out.append(row)
    return out


# --- CLI ---------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="raindrop", description="Dependency-free Raindrop.io CLI (no deletes; bulk changes are dry-run first and undo-logged).")
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("whoami", parents=[common], help="show the authenticated account")
    sub.add_parser("collections", parents=[common], help="list collections as a tree")
    s = sub.add_parser("search", parents=[common], help="list or search bookmarks")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("-c", "--collection", type=int, default=0)
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--sort", default=None, help="-created (default), created, score, title, -title, domain, -domain")
    g = sub.add_parser("get", parents=[common], help="show one bookmark")
    g.add_argument("id", type=int)
    a = sub.add_parser("add", parents=[common], help="save a URL")
    a.add_argument("url")
    a.add_argument("--title")
    a.add_argument("--tags", help="comma-separated")
    a.add_argument("-c", "--collection", type=int)
    a.add_argument("--note")
    a.add_argument("--no-parse", action="store_true", help="don't ask Raindrop to fetch page metadata")
    t = sub.add_parser("tags", parents=[common], help="list tags with counts")
    t.add_argument("-c", "--collection", type=int)
    e = sub.add_parser("export", parents=[common], help="export all bookmarks (stable order)")
    e.add_argument("-c", "--collection", type=int, default=0)
    e.add_argument("--format", choices=["json", "md"], default="json")
    e.add_argument("-o", "--output", help="write to file instead of stdout")
    u = sub.add_parser("update", parents=[common], help="tag, move or annotate one bookmark (undo-logged)")
    u.add_argument("id", type=int)
    u.add_argument("--add-tags", help="comma-separated")
    u.add_argument("--remove-tags", help="comma-separated")
    u.add_argument("--move", type=int, help="target collection id (Trash is refused)")
    u.add_argument("--append-note", help="text appended to the note (skipped if already present)")
    u.add_argument("--dry-run", action="store_true")
    ap = sub.add_parser("apply", parents=[common], help="apply a JSON list of ops; dry run unless --yes")
    ap.add_argument("plan")
    ap.add_argument("--yes", action="store_true", help="actually change bookmarks")
    ap.add_argument("--undo-log", help="where to record previous state (default ~/.local/state/raindrop/)")
    un = sub.add_parser("undo", parents=[common], help="restore bookmarks from an undo log; dry run unless --yes")
    un.add_argument("log")
    un.add_argument("--yes", action="store_true")
    nc = sub.add_parser("new-collection", parents=[common], help="create a collection")
    nc.add_argument("title")
    nc.add_argument("--parent", type=int)
    return p


def _csv(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def run(argv: list[str], client: Client | None = None, out=None) -> int:
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    client = client or Client(load_token())

    def emit(obj: Any, text: str) -> None:
        out.write((json.dumps(obj, indent=2, ensure_ascii=False) if args.json else text) + "\n")

    if args.cmd == "whoami":
        u = client.user()
        emit({k: u.get(k) for k in ("_id", "fullName", "email", "pro")},
             f"{u.get('fullName')} <{u.get('email')}>  id {u.get('_id')}  pro={u.get('pro')}")
    elif args.cmd == "collections":
        items = client.collections()
        emit(items, "\n".join(collection_tree(items)) or "(no collections)")
    elif args.cmd == "search":
        items = client.raindrops(args.collection, args.query, args.sort, args.limit)
        emit(items, "\n".join(raindrop_line(r) for r in items) or "(no results)")
    elif args.cmd == "get":
        r = client.raindrop(args.id)
        emit(r, raindrop_line(r) + (f"\n  note: {r['note']}" if r.get("note") else ""))
    elif args.cmd == "add":
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        r = client.add(args.url, args.title, tags, args.collection, args.note, parse=not args.no_parse)
        emit(r, "saved " + raindrop_line(r))
    elif args.cmd == "tags":
        items = client.tags(args.collection)
        emit(items, "\n".join(f"{t.get('count', 0):>6}  {t.get('_id')}" for t in items) or "(no tags)")
    elif args.cmd == "export":
        items = export_items(client.raindrops(args.collection, limit=None))
        text = json.dumps(items, indent=2, ensure_ascii=False) + "\n" if args.format == "json" else export_markdown(items)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8", newline="\n")
            out.write(f"exported {len(items)} bookmark(s) to {args.output}\n")
        else:
            out.write(text)
    elif args.cmd == "update":
        op = {"id": args.id, "add_tags": _csv(args.add_tags), "remove_tags": _csv(args.remove_tags),
              "move": args.move, "append_note": args.append_note}
        stats = apply_ops(client, [op], execute=not args.dry_run, undo_log=default_undo_log(), throttle=0, out=out)
        emit(stats, f"changed={stats['changed']} unchanged={stats['unchanged']} would_change={stats['would_change']}")
    elif args.cmd in ("apply", "undo"):
        path = Path(args.plan if args.cmd == "apply" else args.log)
        text = path.read_text(encoding="utf-8")
        ops = [json.loads(line) for line in text.splitlines() if line.strip()] if args.cmd == "undo" else json.loads(text)
        if args.cmd == "undo":
            ops = list(reversed(ops))  # restore in reverse order of changes
        log = Path(args.undo_log) if args.cmd == "apply" and args.undo_log else default_undo_log()
        stats = apply_ops(client, ops, execute=args.yes, undo_log=log, out=out)
        note = f"  undo log: {log}" if args.yes and stats["changed"] else ("" if args.yes else "  (dry run; add --yes to apply)")
        emit(stats, f"changed={stats['changed']} unchanged={stats['unchanged']} would_change={stats['would_change']}{note}")
    elif args.cmd == "new-collection":
        c = client.new_collection(args.title, args.parent)
        emit(c, f"created collection {c.get('_id')}  {c.get('title')}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
