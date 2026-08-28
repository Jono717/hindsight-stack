#!/usr/bin/env python3
"""Render an API response for a `make` target.

Every read target pipes `scripts/api.sh` into this. It exists so the display
logic is not five near-identical inline one-liners, and so all of them agree on
the one rule that matters:

    An empty stream is a FAILED CALL, not an empty result.

api.sh exits non-zero and explains itself on stderr when a call fails, so there
is nothing useful to add here -- this exits 1 quietly rather than printing
"no results", which is what previously turned a connection refusal into a
confident empty answer.

A malformed stream is also a failure, not zero results. Only a well-formed
response with an empty `results` list is genuinely "nothing matched".

Usage:  api.sh ... | show.py {json,recall-text,find,banks,invalidate}
"""

import json
import sys


def load():
    raw = sys.stdin.read().strip()
    if not raw:
        # api.sh has already said what went wrong. Adding to it just buries it.
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"response was not JSON ({e}); first 200 chars:\n{raw[:200]}",
              file=sys.stderr)
        sys.exit(1)


def results(d):
    """The `results` list, distinguishing absent from empty."""
    if not isinstance(d, dict) or "results" not in d:
        print("response has no `results` key -- this is not a recall response. "
              f"Keys: {sorted(d)[:8] if isinstance(d, dict) else type(d).__name__}",
              file=sys.stderr)
        sys.exit(1)
    return d["results"] or []


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "json"
    d = load()

    if mode == "json":
        print(json.dumps(d, indent=4))

    elif mode == "recall-text":
        rs = results(d)
        if not rs:
            print("no results")
        for r in rs:
            print(f"[{r.get('type')}] {r.get('text')}")

    elif mode == "find":
        rs = results(d)
        if not rs:
            print("no results")
        for r in rs:
            print(f"{r.get('id')}  [{r.get('type')}]  {str(r.get('text'))[:96]}")

    elif mode == "banks":
        if not isinstance(d, dict) or "banks" not in d:
            print(f"response has no `banks` key. Keys: "
                  f"{sorted(d)[:8] if isinstance(d, dict) else type(d).__name__}",
                  file=sys.stderr)
            return 1
        for b in d["banks"]:
            print(f"  {b.get('bank_id',''):<20} {b.get('fact_count',0):>5} facts   "
                  f"last write {b.get('last_write_at','-')}")

    elif mode == "invalidate":
        if not d.get("invalidated_at"):
            print("the API accepted the call but did not report an "
                  "`invalidated_at`; the memory may be unchanged", file=sys.stderr)
            print(json.dumps(d, indent=4), file=sys.stderr)
            return 1
        print("invalidated", d.get("id"))
        print("  at    :", d.get("invalidated_at"))
        print("  reason:", d.get("invalidation_reason"))
        print("  text  :", str(d.get("text"))[:90])

    else:
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
