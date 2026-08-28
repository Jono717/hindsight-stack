#!/usr/bin/env python3
"""Auth proxy that gives Hindsight a short-lived bearer token it can keep using.

Why this exists: Hindsight exposes one LLM base URL and one static API key. Its
key is read from config once, so there is no hook to refresh a credential. Some
providers -- anything fronted by an OIDC broker, an SSO gateway or an STS -- issue
bearers that expire in minutes to an hour. Point Hindsight at one of those and it
works until the token expires, then every retain fails with a 401 until the
container is restarted.

This shim is the equivalent of Claude Code's `apiKeyHelper` for anything that
cannot call one. Hindsight sends a dummy key; the shim swaps in a live bearer and
forwards upstream:

    hindsight  --(dummy key)-->  shim  --(Bearer <fresh>)-->  your provider

REFRESH MODEL
    Driven by the token itself rather than a fixed interval. The bearer is cached
    in memory and its `exp` claim is decoded; it is re-fetched once it is within
    SHIM_REFRESH_MARGIN seconds of expiry. With a 60-minute token and the default
    300s margin, the helper runs about once every 55 minutes.

    Lazy and on-demand: no background thread, no timer, no daemon, and nothing is
    written to disk. The cache is a request-path optimisation, not a token store.

    If a refresh fails while the cached token is still valid, the cached one keeps
    being served and a warning is logged, so a transient network drop does not
    take Hindsight down mid-retain.

Configuration (all via environment, or the matching flag):
    SHIM_UPSTREAM            REQUIRED. Upstream base URL, e.g. https://api.example.com
    SHIM_TOKEN_CMD           REQUIRED. Shell command printing a bearer to stdout.
    SHIM_PORT                listen port (default 8787)
    SHIM_REFRESH_MARGIN      refresh this many seconds before exp (default 300)
    SHIM_FALLBACK_TTL        cache lifetime if the token has no exp claim
                             (default 3300)
    SHIM_REJECT_TOKENS       comma-separated literal values that are NOT bearers.
                             Some helpers print a sentinel when they expect the
                             caller to authenticate another way (a client
                             certificate, for instance). Listing it here fails
                             loudly at startup instead of as an opaque upstream
                             401 several layers away.
    SHIM_EXTRA_HEADER        OPTIONAL header name to add, e.g. X-Project-Token
    SHIM_EXTRA_HEADER_VALUE  its literal value
    SHIM_EXTRA_HEADER_CMD    or a command that prints its value

Binds 127.0.0.1 by default -- it turns any request into an authenticated one, so
it must not be reachable off-box.
"""

import argparse
import base64
import http.server
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

# Hop-by-hop headers, plus the ones we replace. Forwarding the client's Host or
# its dummy Authorization would defeat the whole point.
STRIP = {
    "host", "authorization", "x-api-key",
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "content-length",
}


def log(msg: str) -> None:
    sys.stderr.write(f"[shim] {msg}\n")
    sys.stderr.flush()


def run(cmd: str) -> str:
    """Capture a credential from a helper command. Never logged, never echoed."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"helper failed to run: {e}") from e
    if p.returncode != 0:
        raise RuntimeError(f"helper exited {p.returncode}: {p.stderr.strip()[:300]}")
    out = p.stdout.strip()
    if not out:
        raise RuntimeError("helper returned nothing")
    return out


def jwt_expiry(token: str):
    """The token's `exp` as epoch seconds, or None if it is not a readable JWT.

    Payload only, no signature check -- this is our own token and we are reading
    the lifetime it already declares, not making a trust decision about it.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


class Bearer:
    """A lazily-refreshed bearer token, refreshed just before it expires."""

    def __init__(self, cmd: str, margin: float, fallback_ttl: float, reject: set):
        self._cmd = cmd
        self._margin = margin
        self._fallback_ttl = fallback_ttl
        self._reject = reject
        self._lock = threading.Lock()
        self._token = None
        self._refresh_at = 0.0   # when we start trying to refresh
        self._hard_exp = 0.0     # when the cached token is genuinely dead

    def get(self) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._refresh_at:
                return self._token

            try:
                token = run(self._cmd)
            except RuntimeError as e:
                # A transient failure should not take Hindsight down while we
                # still hold a usable token.
                if self._token and now < self._hard_exp:
                    log(f"WARNING: refresh failed, serving cached token "
                        f"({(self._hard_exp - now) / 60:.1f} min left): {e}")
                    return self._token
                raise

            if token in self._reject:
                raise RuntimeError(
                    f"helper returned the sentinel {token!r}, which SHIM_REJECT_TOKENS "
                    f"lists as not-a-bearer. The helper is on a different auth path; "
                    f"point SHIM_TOKEN_CMD at one that prints a token."
                )

            exp = jwt_expiry(token)
            if exp:
                self._hard_exp = exp
                self._refresh_at = max(exp - self._margin, now + 30)
                lifetime = (exp - now) / 60
            else:
                # Not a readable JWT: fall back to a fixed TTL.
                self._hard_exp = now + self._fallback_ttl
                self._refresh_at = now + self._fallback_ttl
                lifetime = self._fallback_ttl / 60

            self._token = token
            log(f"bearer refreshed, valid {lifetime:.1f} min, "
                f"next refresh in {(self._refresh_at - now) / 60:.1f} min")
            return token


class ExtraHeader:
    """Optional second credential some gateways want alongside the bearer."""

    def __init__(self):
        self.name = os.environ.get("SHIM_EXTRA_HEADER", "").strip() or None
        self._value = os.environ.get("SHIM_EXTRA_HEADER_VALUE", "").strip() or None
        self._cmd = os.environ.get("SHIM_EXTRA_HEADER_CMD", "").strip() or None

    @property
    def configured(self) -> bool:
        return bool(self.name and (self._value or self._cmd))

    def get(self):
        if not self.name:
            return None
        if self._value:
            return self._value
        if self._cmd:
            try:
                return run(self._cmd)
            except RuntimeError as e:
                log(f"WARNING: extra-header helper failed, sending none: {e}")
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""
    bearer: Bearer = None
    extra: ExtraHeader = None
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            log(fmt % args)

    def _proxy(self):
        body = None
        if length := int(self.headers.get("Content-Length") or 0):
            body = self.rfile.read(length)

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in STRIP
                   and (not self.extra.name or k.lower() != self.extra.name.lower())}
        try:
            # 401 rather than 502: the failure is this machine's credentials, not
            # anything the upstream did.
            headers["Authorization"] = f"Bearer {self.bearer.get()}"
        except RuntimeError as e:
            return self._fail(401, f"bearer unavailable: {e}\n")

        if tok := self.extra.get():
            headers[self.extra.name] = tok

        req = urllib.request.Request(
            f"{self.upstream}{self.path}", data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                self._relay(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            # Pass upstream errors through verbatim -- a mangled 429 or 401 is
            # undebuggable from inside Hindsight.
            payload = e.read()
            if e.code == 401:
                log("upstream 401: the bearer was rejected. Wrong account, or "
                    "missing the audience/scope the upstream requires?")
            self._relay(e.code, e.headers, payload)
        except urllib.error.URLError as e:
            self._fail(502, f"upstream unreachable: {e.reason}\n")

    def _relay(self, status, headers, payload):
        self.send_response(status)
        for k, v in headers.items():
            if k.lower() not in {"transfer-encoding", "connection", "content-length"}:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, status, msg):
        payload = msg.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _proxy


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("SHIM_PORT", 8787)))
    ap.add_argument("--bind", default="127.0.0.1",
                    help="listen address; the 127.0.0.1 default is the safe one")
    ap.add_argument("--upstream", default=os.environ.get("SHIM_UPSTREAM", ""))
    ap.add_argument("--token-cmd", default=os.environ.get("SHIM_TOKEN_CMD", ""))
    ap.add_argument("--refresh-margin", type=float,
                    default=float(os.environ.get("SHIM_REFRESH_MARGIN", 300)))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    for flag, value in (("--upstream / SHIM_UPSTREAM", a.upstream),
                        ("--token-cmd / SHIM_TOKEN_CMD", a.token_cmd)):
        if not value:
            print(f"error: {flag} is required", file=sys.stderr)
            return 2

    reject = {s.strip() for s in os.environ.get("SHIM_REJECT_TOKENS", "").split(",") if s.strip()}

    Handler.upstream = a.upstream.rstrip("/")
    Handler.verbose = a.verbose
    Handler.bearer = Bearer(
        a.token_cmd, a.refresh_margin,
        float(os.environ.get("SHIM_FALLBACK_TTL", 3300)), reject)
    Handler.extra = ExtraHeader()

    # Fail at startup, not on the first retain buried in a worker log.
    try:
        Handler.bearer.get()
    except RuntimeError as e:
        print(f"error: cannot get a bearer: {e}", file=sys.stderr)
        return 1

    if a.bind != "127.0.0.1":
        log(f"WARNING: bound to {a.bind}, not loopback. This proxy authenticates "
            f"every request that reaches it as you.")

    srv = http.server.ThreadingHTTPServer((a.bind, a.port), Handler)
    print(f"token shim: http://{a.bind}:{a.port}  ->  {Handler.upstream}")
    print(f"  token cmd:    {a.token_cmd}")
    print(f"  extra header: {Handler.extra.name if Handler.extra.configured else 'none'}")
    print(f"  point Hindsight at: HINDSIGHT_API_LLM_BASE_URL=http://<host>:{a.port}<upstream path>")
    print("  Hindsight's API key can be any non-empty dummy value. Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
