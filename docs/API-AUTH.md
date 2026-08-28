# API authentication

Hindsight ships with **no authentication at all**. Upstream expects it to come from
"a proxy in front of Hindsight" and provides no recipe. This stack instead enables
Hindsight's built-in API-key extension, which gates every endpoint in-process.

## Generating a key

```bash
openssl rand -hex 32
```

Put it in `.env`:

```bash
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
HINDSIGHT_API_TENANT_API_KEY=<the key>
```

Then `make restart`. That is the whole setup: the extension is built in, so nothing
needs installing.

## Reading the key back

```bash
make api-key              # print it
make api-key | pbcopy     # straight to the clipboard
```

It refuses and exits non-zero if the variable is unset, so a blank line never gets
mistaken for a valid key. The raw source is `.env`, which is gitignored:

```bash
grep '^HINDSIGHT_API_TENANT_API_KEY=' .env | cut -d= -f2
```

## What the key covers

**Everything except `/health`.** One key, not two, because
`ApiKeyTenantExtension.authenticate_mcp()` delegates to `authenticate()`, so the
REST API and the MCP endpoints validate against the same value.

Verified 2026-08-27:

| Request | No key | Valid key | Wrong key |
| --- | --- | --- | --- |
| `GET /health` | **200** | 200 | 200 |
| `GET /v1/default/banks` | 401 | 200 | 401 |
| `POST /mcp/work/` | 401 | 200 | 401 |

`/health` stays open on purpose. The Docker healthcheck cannot present a key, and
gating it would make the container permanently unhealthy.

## How clients send it

```
Authorization: Bearer <key>
```

A bare key with no `Bearer ` prefix also works: `get_request_context` in
`api/http.py` accepts either form.

Three clients are already wired up:

| Client | How it gets the key |
| --- | --- |
| `make` targets | `scripts/api.sh` reads it from `.env` and adds the header. Never passed on a command line, so it stays out of shell history and `ps`. |
| Control Plane | `HINDSIGHT_CP_DATAPLANE_API_KEY` in compose is set from the same variable. The CP is an API client like any other and the dashboard breaks without it. |
| Claude Code MCP | `make mcp-url` prints the `claude mcp add` line with the header filled in. |

## Rotating

1. Change `HINDSIGHT_API_TENANT_API_KEY` in `.env`
2. `make restart`
3. Re-run `make mcp-url` and update every registered MCP client

The `make` targets and the dashboard pick the new value up automatically, since
both read it from the same variable. External clients do not.

## One setting to never turn on

```bash
HINDSIGHT_API_TENANT_MCP_AUTH_DISABLED=true   # do not set this
```

It exempts MCP from authentication while leaving the REST API protected. MCP is the
surface carrying `retain`, `invalidate_memory`, `delete_document` and
`clear_memories`, so exempting it is strictly worse than exempting the REST API.
It exists for backwards compatibility, not as an option worth taking.

## Scope, and what this is not

This is a **single shared secret with full access to every bank**. There are no
per-client keys, no scopes, and no read-only mode. Anyone holding it can read and
delete everything.

That is proportionate for a personal stack on a private tailnet. It is not
multi-user access control. If teammates ever need access, the answer is a custom
`TenantExtension` that maps keys to schemas, which is the extension point upstream
documents for exactly this.
