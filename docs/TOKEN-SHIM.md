# The token shim: using a provider whose credential expires

Hindsight reads `HINDSIGHT_API_LLM_API_KEY` from config **once**, at startup. There
is no refresh hook. That is fine for a provider that issues a long-lived `sk-...`
key, and it is a problem for any provider fronted by an OIDC broker, an SSO
gateway or an STS, where the credential is a bearer that expires in minutes to an
hour.

Point Hindsight straight at a short-lived bearer and it works until the token
expires, then every retain and recall fails with a 401 until you restart the
container. Nothing in the logs says "your token expired" -- you get an opaque
upstream 401 from inside a worker.

[`scripts/token-shim.py`](../scripts/token-shim.py) is the fix. It is a tiny
loopback reverse proxy that holds a live bearer and swaps it in:

```
hindsight  --(dummy key)-->  shim on 127.0.0.1:8787  --(Bearer <fresh>)-->  provider
```

Hindsight's own API key becomes an unused placeholder. It still has to be
non-empty, because Hindsight refuses to start without one.

---

## 1. What you need

A **command that prints a bearer token to stdout** and exits 0. That is the whole
contract. Most SSO tooling already ships one; if yours does not, a three-line
wrapper around your provider's CLI is enough.

Verify it by hand first:

```bash
your-token-command
```

It should print one long string and nothing else. Diagnostics on stdout will be
sent upstream as the credential.

---

## 2. Configure it

In `.env`:

```bash
# Where the shim listens, and where it forwards to
SHIM_PORT=8787
SHIM_UPSTREAM=https://api.example.com
SHIM_TOKEN_CMD=/path/to/your-token-command

# Point Hindsight at the shim instead of at the provider. Use the container's
# view of the host, not 127.0.0.1 -- the container's loopback is its own.
HINDSIGHT_API_LLM_BASE_URL=http://host.docker.internal:8787/v1
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_API_KEY=via-shim          # any non-empty placeholder
HINDSIGHT_API_LLM_MODEL=your-model-id
```

Then start it in its own terminal and leave it running:

```bash
make shim
```

It prints the upstream, the token command, and how long the first token is valid.

---

## 3. How refresh works

The shim decodes the token's own `exp` claim and re-fetches once it is within
`SHIM_REFRESH_MARGIN` seconds of expiry (default 300). With a 60-minute token that
means the helper runs about every 55 minutes.

This is deliberately **lazy**: no background thread, no timer, no daemon, and
nothing written to disk. The token lives in memory for the life of the process and
is fetched on the request path. A shim that is not being called does not refresh
anything.

If the helper fails while the cached token is still valid, the cached token keeps
being served and a warning is logged. A brief network drop does not take Hindsight
down mid-retain.

If the token is not a readable JWT, the shim falls back to a fixed
`SHIM_FALLBACK_TTL` (default 3300 seconds).

---

## 4. The container cannot reach `127.0.0.1`

This is the failure that costs the most time, so check it before anything else.

The shim binds to the **host's** loopback. Inside the container, `127.0.0.1` is the
container's own loopback, so `HINDSIGHT_API_LLM_BASE_URL=http://127.0.0.1:8787/...`
connects to nothing. Use `host.docker.internal`, and make sure the compose file
maps it:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Under Colima there is a second layer: the container's host is the Lima VM, not
macOS. In practice Lima's user-mode networking maps `host.docker.internal` onto the
macOS loopback and it works, but that depends on the networking mode, so verify
rather than assume:

```bash
make preflight
```

`make preflight` runs exactly this check -- it starts a throwaway container and
tries to open the shim port from inside it.

---

## 5. Optional: a second credential

Some gateways want a project or tenant token alongside the bearer. Add it as a
header:

```bash
SHIM_EXTRA_HEADER=X-Project-Token
SHIM_EXTRA_HEADER_VALUE=...
# or, to fetch it fresh each time:
SHIM_EXTRA_HEADER_CMD=/path/to/print-project-token
```

The shim strips any incoming copy of that header before adding its own, so a
client cannot override it.

---

## 6. Optional: reject a sentinel value

Some token helpers print a fixed sentinel string instead of a token when they
expect the caller to authenticate a different way -- a client certificate, for
instance. Forwarding that sentinel as a bearer produces an opaque upstream 401
several layers from the actual cause.

List the sentinel and the shim refuses it at startup with a readable message:

```bash
SHIM_REJECT_TOKENS=some-sentinel,another-sentinel
```

---

## 7. Security posture

The shim **authenticates every request that reaches it, as you**. It is an open
credential-lending service to anything that can open the port.

- It binds `127.0.0.1` by default. Keep it there.
- `--bind` accepts something else and logs a loud warning if you use it. There is
  no good reason to on a workstation.
- It never writes the token to disk and never logs its value.

---

## 8. A local overlay for a provider you cannot describe publicly

If your provider's hostnames, token command or sentinel values are not something
you want in a public repo, keep the specifics out of git rather than out of the
stack. `.gitignore` already excludes:

```
scripts/*-shim.py     (except token-shim.py itself)
docs/LOCAL-*.md
```

So a `scripts/my-provider-shim.py` and a `docs/LOCAL-PROVIDER.md` live in the
working tree, run normally, and are never committed. Point `make shim` at yours:

```bash
SHIM_SCRIPT=scripts/my-provider-shim.py
```

In most cases you will not need a second script at all -- `SHIM_UPSTREAM`,
`SHIM_TOKEN_CMD` and `SHIM_EXTRA_HEADER` in a gitignored `.env` cover it.
