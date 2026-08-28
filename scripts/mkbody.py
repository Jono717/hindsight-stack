#!/usr/bin/env python3
"""Build an API request body. Kept out of the Makefile on purpose.

Inline `python3 -c "...{'a':1,'b':2}..."` in a recipe is a trap: bash brace-expands
any {...} containing a top-level comma into separate words, so a two-key dict
silently becomes two broken commands. A single-key dict works, which makes the bug
look random. Building the JSON here sidesteps shell quoting entirely.

    mkbody.py retain "text to remember"
    mkbody.py recall "what do you know?"
"""

import json
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} {{retain|recall}} TEXT", file=sys.stderr)
        return 2

    kind, text = sys.argv[1], sys.argv[2]

    if kind == "retain":
        # async=False so an LLM failure surfaces as an error instead of a memory
        # that silently never appears.
        body = {"items": [{"content": text}], "async": False}
    elif kind == "recall":
        body = {"query": text}
    else:
        print(f"unknown body type: {kind}", file=sys.stderr)
        return 2

    print(json.dumps(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
