# Reaching the stack from your other devices

The containers stay bound to `127.0.0.1`. Nothing about this exposes a new
listening port to the network. Access from your other machines goes over
**Tailscale**, which is already installed and running on the host.

## Why Tailscale rather than the alternatives

| Approach | Verdict |
| --- | --- |
| **`tailscale serve`** | **Used here.** Tailnet-only, real HTTPS cert, stable hostname, works off-corp, no inbound firewall hole. |
| Bind the containers to `0.0.0.0` | Rejected. The API has **no authentication at all**, so this hands full read/write/delete on every memory to anything that can reach the port. |
| SSH tunnel per device | Works, and needs no setup beyond Remote Login. But it is one tunnel per device per session, and a VPN-assigned address rotates. Kept below as a fallback. |
| VPN address directly | A VPN-assigned address changes on every reconnect, and a workstation usually has no DNS record, so there is nothing stable to point a browser at. |

The deciding factor is the missing API auth. Upstream expects authentication to be
supplied by "a proxy in front of Hindsight" and ships no auth recipe of its own, so
the only safe posture is to keep the ports private and let an authenticated mesh
carry the traffic.

## What is configured

```
https://your-host.tailnet-name.ts.net/        -> Control Plane (port 9999)
https://your-host.tailnet-name.ts.net:8443/   -> API (port 8888)
```

Both are reachable **only by devices signed into your tailnet**. Confirm the current config any time:

```bash
make serve-status
```

## Using it

On any of your other devices with Tailscale running, open:

```
https://your-host.tailnet-name.ts.net/
```

The TLS certificate is real and issued for that name, so there is no warning to
click through. Short name usually works too if MagicDNS is on:
`https://your-host/`.

For the API from a script on another device:

```bash
curl https://your-host.tailnet-name.ts.net:8443/health
```

## Using the MCP server from another device

This works, because `tailscale serve` proxies **all** paths on 8443, including
`/mcp`. On the remote device:

```bash
claude mcp add --transport http hindsight-work \
  https://your-host.tailnet-name.ts.net:8443/mcp/work/ \
  --header "Authorization: Bearer $(grep '^HINDSIGHT_API_MCP_AUTH_TOKEN=' .env | cut -d= -f2)"
```

You will not have `.env` on the remote device, so read the token once on the host
and paste it:

```bash
grep '^HINDSIGHT_API_MCP_AUTH_TOKEN=' .env | cut -d= -f2
```

### The key is not optional here

Hindsight ships **unauthenticated**. `HINDSIGHT_API_TENANT_API_KEY` must be set
whenever the API is served beyond loopback, because MCP exposes `retain`,
`invalidate_memory`, `delete_document` and `clear_memories` on the same port that
`make serve-on` publishes. Without it, anything reaching 8443 can wipe a bank.

One key covers everything: `ApiKeyTenantExtension.authenticate_mcp()` delegates to
`authenticate()`, so the REST API and MCP share it. See
[docs/API-AUTH.md](API-AUTH.md).

Verified 2026-08-27:

```
GET  /health          no key -> 200   (deliberately open, Docker healthcheck)
GET  /v1/default/banks no key -> 401
GET  /v1/default/banks key    -> 200
GET  /v1/default/banks wrong  -> 401
POST /mcp/work/       no key -> 401
POST /mcp/work/       key    -> 200   (hindsight-mcp-server 0.9.2)
```

Rotate by changing the value in `.env` and running `make restart`. Every client
needs the new value, including the `claude mcp add` registrations.

### Single-bank URLs, one server per bank

The bank is in the URL, so register one MCP server per bank rather than using
multi-bank mode:

```
.../8443/mcp/work/       -> work bank only
.../8443/mcp/personal/   -> personal bank only
.../8443/mcp             -> multi-bank: adds a bank_id param, list_banks, create_bank
```

Single-bank is the better default. The agent physically cannot write to the wrong
bank, and it does not spend context on a `bank_id` argument. Note that multi-bank
mode still cannot search **across** banks; it only lets the agent pick one.

### If you would rather not expose the API at all

Serve only the dashboard and drop 8443. MCP then works locally but not remotely:

```bash
tailscale serve --https=8443 off
```



Remote access does not remove any local requirement. All of this still applies:

1. **Colima** must be up, or the containers are not running at all.
2. **The containers** must be up: `make ps`.
3. **The shim** must be up for anything that calls the LLM. Browsing existing
   memories works without it; retain and recall do not.
4. **The host must be awake.** Tailscale cannot reach a sleeping machine. If you want
   it reliably available, either keep it from sleeping or move the stack to an
   always-on host.

## Turning it off

```bash
make serve-off        # removes both proxies
tailscale serve status # confirm nothing is left
```

## Two things this deliberately does not do

**No Funnel.** `tailscale funnel` would publish this to the public internet.
Pointing it at an unauthenticated memory store would be a bad idea, and check `make serve-status`, which reports whether your
tailnet is Funnel-capable at all.

**No API authentication added.** Everything on the tailnet can call the API without
credentials. That is acceptable while the tailnet is only your own devices, and
it is the thing to revisit first if that ever changes. Two options when it does:

- Set `HINDSIGHT_CP_ACCESS_KEY` in `.env` to put a shared-secret login on the UI.
  This does **not** protect the API on 8443, only the dashboard.
- Stop serving the API entirely and expose only the dashboard:
  `tailscale serve --https=8443 off`

## Fallback: SSH tunnel

If Tailscale is down or you would rather not serve anything, and Remote Login is enabled on the host. From the remote device:

```bash
ssh -N -L 9999:127.0.0.1:9999 -L 8888:127.0.0.1:8888 \
  you@your-host.tailnet-name.ts.net
```

Then use `http://localhost:9999` on that device while the tunnel is open. This adds
no listening service and authenticates with your existing SSH credentials. The
tradeoff is that it is per-device, per-session, and dies with the terminal.
