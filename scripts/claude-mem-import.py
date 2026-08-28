#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Import a claude-mem SQLite database into a Hindsight memory bank.

Written for the one-time migration of 2026-08-28, which moved 4,125 items
(3,875 observations + 250 session summaries) out of the claude-mem plugin and
into the `work` bank before that plugin was uninstalled. It is kept because the
same shape recurs: any external store with rows, timestamps and text can be
retained this way.

claude-mem itself is gone. Its database survives only as the gzipped archive
under ~/Documents/local-repo/claude-mem-archive/, which is the default --db.

Each row becomes one MemoryItem:

  document_id  claude-mem:obs:<id>   stable, so a re-run upserts and never
                                     duplicates -- this is what makes retrying
                                     a failed batch safe
  timestamp    the row's created_at  so history lands at its real date rather
                                     than collapsing onto the import date
  tags         project:<p>, obstype:<t>, origin:claude-mem
  metadata     provenance back to the source row

Things learned the expensive way, all of which this script now encodes:

* A shared-account provider quota is the binding constraint, not your own rate.
  The gateway this was written against allowed 640 requests per 4 minutes across
  everything on the account. Extraction fires several LLM calls per document, so
  the ceiling arrives well below 640 documents. Exceeding it surfaces as HTTP 500
  wrapping a 429, and the only useful response is to wait, which is what
  --backoff does. Deferring the batch just moves the same work to a later run
  that will hit the same wall.
* `metadata` values must be strings. An integer id returns a 422 that names the
  field but not the rule.
* Small batches win, decisively. Batch 2 ran 8-20s per request against batch
  5's 80-115s -- roughly 5x the throughput -- because a long request is also
  the one that trips timeouts.
* A long request proxied through `tailscale serve` returns 502 while the
  backend keeps working. The document never commits, so it must be resent.
* `RemoteDisconnected` is usually a lost acknowledgement rather than lost work:
  the server finished and the connection dropped before replying. The
  document_id upsert makes the retry a no-op, which is why those retries return
  in 0.2s instead of the usual 20s.

Usage:
    make import-mem                          # whole archive into BANK
    python3 scripts/claude-mem-import.py --dry-run
    python3 scripts/claude-mem-import.py --bank work --types root-cause

Resumable: completed document_ids are written to --state after every batch, so
an interrupted run picks up exactly where it stopped.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.expanduser(
    "~/Documents/local-repo/claude-mem-archive/claude-mem.db.gz"
)


def env_file(key: str) -> str:
    """Read one key from the repo's .env, matching scripts/api.sh."""
    path = os.path.join(REPO, ".env")
    if not os.path.exists(path):
        return ""
    with open(path) as fh:
        for line in fh:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return ""


def load_config() -> tuple[str, str]:
    """Resolve API base URL and key: environment, then .env, then the client config.

    The local port is preferred over the tailnet hostname. Going through
    `tailscale serve` adds a proxy that 502s on long requests, so an import run
    on the host that owns the stack should never leave localhost.
    """
    url = os.environ.get("HINDSIGHT_API_URL")
    if not url:
        port = os.environ.get("HINDSIGHT_API_PORT") or env_file("HINDSIGHT_API_PORT")
        url = f"http://localhost:{port or '8888'}"

    key = os.environ.get("HINDSIGHT_API_TENANT_API_KEY") or env_file(
        "HINDSIGHT_API_TENANT_API_KEY"
    )
    if not key:
        # Fall back to the coding-agent client config, which holds the same key.
        client = os.path.expanduser("~/.hindsight/coding-agent.json")
        if os.path.exists(client):
            with open(client) as fh:
                key = json.load(fh).get("apiToken", "")
    return url.rstrip("/"), key


def open_db(path: str) -> tuple[sqlite3.Connection, str | None]:
    """Open the database read-only, transparently decompressing a .gz archive."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit(f"no such database: {path}")
    tmp = None
    if path.endswith(".gz"):
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        path = tmp
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con, tmp


def jlist(raw: str | None) -> list[str]:
    """claude-mem stores several columns as JSON arrays, but not all of them."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return [raw]
    return [str(v) for v in val] if isinstance(val, list) else [str(val)]


def observation_content(row: sqlite3.Row) -> str:
    """Flatten one observation. `text` is null on every row; the content lives in
    title, subtitle, narrative and the facts array."""
    parts: list[str] = []
    if row["title"]:
        parts.append(f"# {row['title']}")
    if row["subtitle"]:
        parts.append(row["subtitle"])
    parts.append(
        f"(claude-mem observation {row['id']} | project {row['project']} "
        f"| type {row['type']} | {row['created_at']})"
    )
    for key in ("narrative", "text"):
        if row[key]:
            parts.append(row[key])
    if facts := jlist(row["facts"]):
        parts.append("Facts:\n" + "\n".join(f"- {f}" for f in facts))
    if concepts := jlist(row["concepts"]):
        parts.append("Concepts: " + ", ".join(concepts))
    if modified := jlist(row["files_modified"]):
        parts.append("Files modified: " + ", ".join(modified))
    return "\n\n".join(p for p in parts if p)


def summary_content(row: sqlite3.Row) -> str:
    parts = [
        f"# Session summary -- {row['project']} ({row['created_at']})",
        f"(claude-mem session_summary {row['id']} | session {row['memory_session_id']})",
    ]
    for label, key in (
        ("Request", "request"),
        ("Investigated", "investigated"),
        ("Learned", "learned"),
        ("Completed", "completed"),
        ("Next steps", "next_steps"),
        ("Notes", "notes"),
    ):
        if not (val := row[key]):
            continue
        items = jlist(val)
        if len(items) > 1 or (items and items[0] != val):
            parts.append(f"{label}:\n" + "\n".join(f"- {i}" for i in items))
        else:
            parts.append(f"{label}: {val}")
    return "\n\n".join(parts)


def item(content: str, doc_id: str, row: sqlite3.Row, obstype: str, table: str) -> dict:
    """Build one MemoryItem. Every metadata value must be a string -- an integer
    here returns a 422 naming the field but not the rule."""
    return {
        "content": content,
        "document_id": doc_id,
        "timestamp": row["created_at"],
        "context": f"claude-mem {table}, project {row['project']}",
        "tags": [
            f"project:{row['project']}",
            f"obstype:{obstype}",
            "origin:claude-mem",
        ],
        "metadata": {
            "origin": "claude-mem",
            "table": table,
            "row_id": str(row["id"]),
            "project": row["project"],
            "memory_session_id": row["memory_session_id"],
            "created_at": row["created_at"],
        },
    }


def fetch(args) -> list[dict]:
    con, tmp = open_db(args.db)
    items: list[dict] = []
    try:
        if not args.summaries_only:
            where, params = ["1=1"], []
            for col, vals, negate in (
                ("project", args.projects, False),
                ("type", args.types, False),
                ("type", args.exclude_types, True),
            ):
                if not vals:
                    continue
                marks = ",".join("?" * len(vals))
                where.append(f"{col} {'NOT ' if negate else ''}IN ({marks})")
                params += vals
            sql = f"SELECT * FROM observations WHERE {' AND '.join(where)} ORDER BY id"
            if args.limit:
                sql += f" LIMIT {args.limit}"
            for row in con.execute(sql, params):
                items.append(
                    item(
                        observation_content(row),
                        f"claude-mem:obs:{row['id']}",
                        row,
                        row["type"],
                        "observations",
                    )
                )

        if not args.observations_only:
            where, params = ["1=1"], []
            if args.projects:
                marks = ",".join("?" * len(args.projects))
                where.append(f"project IN ({marks})")
                params += args.projects
            sql = (
                f"SELECT * FROM session_summaries WHERE {' AND '.join(where)} "
                "ORDER BY id"
            )
            if args.limit:
                sql += f" LIMIT {args.limit}"
            for row in con.execute(sql, params):
                items.append(
                    item(
                        summary_content(row),
                        f"claude-mem:summary:{row['id']}",
                        row,
                        "session-summary",
                        "session_summaries",
                    )
                )
    finally:
        con.close()
        if tmp:
            os.unlink(tmp)
    return items


def post(url: str, key: str, bank: str, batch: list[dict], timeout: int) -> dict:
    endpoint = f"{url}/v1/default/banks/{urllib.parse.quote(bank, safe='')}/memories"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"items": batch, "async": False}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Import a claude-mem database into a Hindsight bank."
    )
    ap.add_argument("--db", default=DEFAULT_DB, help="claude-mem .db or .db.gz")
    ap.add_argument("--bank", default="work")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=2, help="keep small; see module docstring")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--backoff", type=int, default=45, help="seconds, doubling to 300")
    ap.add_argument("--projects", nargs="*")
    ap.add_argument("--types", nargs="*")
    ap.add_argument("--exclude-types", nargs="*", default=[])
    ap.add_argument("--observations-only", action="store_true")
    ap.add_argument("--summaries-only", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state", default="/tmp/claude-mem-import.state.json")
    args = ap.parse_args()

    url, key = load_config()
    items = fetch(args)
    chars = sum(len(i["content"]) for i in items)
    print(f"items={len(items)} chars={chars:,} bank={args.bank} url={url}")
    if not key:
        print("warning: no API key resolved -- the API may reject every request")

    if args.dry_run:
        if items:
            print("--- first item ---")
            print(items[0]["content"][:1200])
            print("--- tags ---", items[0]["tags"])
        return 0

    done: set[str] = set()
    if os.path.exists(args.state):
        with open(args.state) as fh:
            done = set(json.load(fh).get("done", []))
    todo = [i for i in items if i["document_id"] not in done]
    print(f"already done={len(done)} todo={len(todo)}")
    if not todo:
        return 0

    t0 = time.time()
    sent = 0
    batches = [todo[s : s + args.batch] for s in range(0, len(todo), args.batch)]

    def run(batch: list[dict]):
        """POST one batch, waiting out the provider quota rather than giving up."""
        transient = ("429", "rate limit", "RateLimitError", "502", "RemoteDisconnected")
        delay = args.backoff
        for attempt in range(args.retries + 1):
            bt = time.time()
            try:
                post(url, key, args.bank, batch, args.timeout)
                return batch, time.time() - bt, None
            except urllib.error.HTTPError as exc:
                err = f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"
            except Exception as exc:  # noqa: BLE001
                err = repr(exc)
            if attempt >= args.retries or not any(t in err for t in transient):
                return batch, time.time() - bt, err
            print(
                f"  retry {attempt + 1}/{args.retries} in {delay}s "
                f"({len(batch)} items): {err[:90]}",
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, 300)
        return batch, 0.0, "retries exhausted"

    failed = 0
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for batch, secs, err in pool.map(run, batches):
            if err:
                failed += len(batch)
                print(f"error on {len(batch)} items: {err}", flush=True)
                continue
            sent += len(batch)
            done.update(i["document_id"] for i in batch)
            with open(args.state, "w") as fh:
                json.dump({"done": sorted(done)}, fh)
            elapsed = time.time() - t0
            rate = sent / elapsed if elapsed else 0
            eta = (len(todo) - sent) / rate if rate else 0
            print(
                f"[{sent}/{len(todo)}] batch={len(batch)} {secs:.1f}s "
                f"rate={rate:.3f}/s eta={eta / 60:.1f}min",
                flush=True,
            )

    print(f"sent={sent} failed={failed} in {(time.time() - t0) / 60:.1f}min")
    if failed:
        print("re-run the same command -- the state file skips what already landed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
