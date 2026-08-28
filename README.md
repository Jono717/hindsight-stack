# hindsight-stack

Two containers that give you a self-hosted [Hindsight](https://hindsight.vectorize.io)
memory service on your own machine:

| Container | Image | What it does |
| --- | --- | --- |
| `hindsight-db` | `ghcr.io/tensorchord/vchord-postgres:pg17-v0.4.3` | PostgreSQL 17 with the `vector` extension. Stores every memory. |
| `hindsight-app` | `ghcr.io/vectorize-io/hindsight:latest` | The API on port 8888 and the Control Plane web UI on port 9999. |

The **full** image is used, so embeddings and reranking run inside the container.
The only external dependency is an LLM, which Hindsight calls to extract facts,
resolve entities, and generate answers.

Both ports bind to `127.0.0.1` only, and the API is protected by an API key. This
stack holds an LLM credential and everything you ever retain into it, so it is not
exposed to the network unauthenticated. See [docs/API-AUTH.md](docs/API-AUTH.md).

> **Not the upstream Postgres image.** Upstream uses `pgvector/pgvector` from
> Docker Hub. On the network this was built on, the Colima daemon routes through
> a host proxy that returns 403 for `docker.io` while allowing `ghcr.io`, so the
> default here is a ghcr.io image. If Docker Hub works for you, set
> `HINDSIGHT_DB_IMAGE=pgvector/pgvector:pg18`. See
> [docs/REGISTRY.md](docs/REGISTRY.md) for what was tested and why this image was
> chosen.

---

## Status

**Working end to end.** Verified on macOS with Colima, 2026-08-27:

- Both containers build, start, and report healthy
- `depends_on: service_healthy` correctly gates the API behind Postgres
- `sql/001-extensions.sql` runs, creating `vector` and `pg_trgm`
- API and Control Plane both answer on `127.0.0.1` from the host
- **Verified end to end**: `make check-llm` reports all three operations connected,
  and a real retain extracted facts which recall then returned with reranker scores

One thing to know before you start: if your LLM provider issues a **short-lived
bearer** rather than a long-lived API key, start the token shim first. Hindsight
reads its key from config once and has no refresh hook, so an expiring credential
401s the moment it lapses. [scripts/token-shim.py](scripts/token-shim.py) keeps a
live one and swaps it in: run `make shim` in its own terminal before `make up`.
See [docs/TOKEN-SHIM.md](docs/TOKEN-SHIM.md). With a plain API key, skip it.

---

## Before you start

You need three things:

1. **Docker running.** Tested with Colima. The full image wants about 2 GB
   for the API and 1 GB for Postgres, so give the VM room:
   ```bash
   colima start --cpu 4 --memory 8
   ```
2. **About 4 GB of disk** for the image pull (~3.7 GB on Apple Silicon).
3. **An LLM credential.** Any provider Hindsight supports. If yours issues a
   long-lived API key, that is all you need. If it issues a short-lived bearer,
   you also need the shim: see [docs/TOKEN-SHIM.md](docs/TOKEN-SHIM.md).

---

## Quick start

### Step 1: Create your config file

```bash
cd hindsight-stack
cp .env.example .env
```

### Step 2: Set a database password

Generate one and paste it into `.env` as `HINDSIGHT_DB_PASSWORD`:

```bash
openssl rand -base64 24
```

This password is only used between the two containers. You will not type it again.

### Step 3: Choose your LLM provider

Open `.env` and look at section 2. Either:

- **Plain API key (`2a`, the default):** uncomment one provider and fill in your
  key. Nothing else to run.
- **A credential that expires (`2b`):** read
  [docs/TOKEN-SHIM.md](docs/TOKEN-SHIM.md), then start the shim in its own
  terminal and leave it running:
  ```bash
  make shim
  ```

### Step 4: Check everything before starting

```bash
make preflight
```

This checks your config, that Docker is reachable, that the compose file is
valid, and (if you use the shim) that your token command returns a usable token,
how long it has left, and that a container can actually reach the shim. Fix
anything marked `FAIL` before continuing. It starts nothing and changes nothing,
so it is safe to run repeatedly.

### Step 5: Start the stack

```bash
make up
```

The first run pulls about 3.7 GB, then the API loads the embedder and reranker
into memory and runs its database migrations. **Expect several minutes before it
reports healthy.** `make up` polls for you and prints the URLs when it is ready.

### Step 6: Open the web UI

```
http://localhost:9999
```

The API is at `http://localhost:8888`.

---

## Everyday commands

```bash
make help        # list every target
make ps          # are both containers up
make health      # is the API answering
make logs        # follow the API log
make logs-db     # follow the PostgreSQL log
make psql        # psql shell on the database
make down        # stop, keeping all stored memories
make restart     # recreate both containers after editing .env
make pull        # pull newer images
```

`make down` keeps your data. The memories live in a Docker named volume
(`hindsight-stack_pg_data`) which survives `down`, `restart`, and reboots.

To delete everything, including every stored memory:

```bash
make wipe        # asks you to type DELETE
```

---

## Reaching it from your other devices

The ports stay bound to `127.0.0.1`. Remote access goes over Tailscale, which
fronts them with a real HTTPS cert reachable only by devices on your tailnet:

```bash
make serve-on        # dashboard on 443, API on 8443
make serve-status    # confirm what is exposed and to whom
make serve-off       # revoke
```

Then from any of your devices: `https://your-host.tailnet-name.ts.net/`

The MCP server rides the same tunnel. `make mcp-url` prints the exact
`claude mcp add` line to run on the remote device, token included.

Read [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md) before relying on this. Two
layers protect it: the tailnet limits *who can route to it*, and the API key limits
*who can call it*. Both matter, because Hindsight ships unauthenticated and MCP
exposes retain and delete on the same port.

---

## Verify it actually works

### First, check the LLM

This is the thing most likely to be wrong, and the hardest to diagnose from the
outside, so check it directly. It does one real round trip and reports the actual
error:

```bash
make check-llm
```

A working LLM reports `"ok": true` for each operation. A broken one looks like
this, which is what you get when the credential or base URL is wrong:

```json
{
  "bank_id": "test",
  "operations": [
    { "operation": "retain", "ok": false, "status": "unreachable", "latency_ms": 623.0 }
  ]
}
```

### Then store and query a memory

```bash
make retain TEXT="Ada is a systems engineer. She prefers beginner-friendly numbered steps in documentation."
make recall QUERY="How should I write documentation for Ada?"
make recall-text QUERY="How should I write documentation for Ada?"   # just the text
```

A working retain returns token usage, which is proof the LLM was actually called:

```json
{"success": true, "bank_id": "test", "items_count": 1, "async": false,
 "usage": {"input_tokens": 97, "output_tokens": 244, "total_tokens": 341}}
```

Recall then returns the extracted facts, as both a `world` fact and an
`observation`, with the hybrid scores that ranked them:

```
[world]       Ada prefers beginner-friendly numbered steps in documentation...
[observation] Ada prefers beginner-friendly numbered steps in documentation...
[world]       Ada is a systems engineer...
```

`make retain` stores synchronously (`async: false`) **on purpose**. The default
async path hands the work to a background worker, so an LLM failure shows up as a
memory that silently never appears. Synchronous retain returns the real error.

> **On truncated responses.** These targets go through
> [scripts/api.sh](scripts/api.sh), which calls the API from the host, validates
> that the JSON parsed, and silently retries inside the container if it did not.
> That exists because some environments run an HTTP proxy that rewrites localhost
> traffic and truncates larger responses, which shows up as invalid JSON rather
> than as an error. The API itself is not at fault: a direct socket request always
> returns the full payload.

Under the hood those targets call:

| Action | Endpoint |
| --- | --- |
| Retain | `POST /v1/default/banks/{bank}/memories` |
| Recall | `POST /v1/default/banks/{bank}/memories/recall` |
| LLM check | `POST /v1/default/banks/{bank}/health/llm` |
| Service health | `GET /health` |

Use a different memory bank with `BANK=`:

```bash
make retain BANK=work TEXT="..."
```

The Control Plane at `http://localhost:9999` does all of this in a UI.

---

## Changing configuration later

1. Edit `.env`
2. `make restart`

Two values cannot be changed on a live volume:

- **`HINDSIGHT_DB_PASSWORD`** is baked into the database on first init. Changing
  it in `.env` alone breaks the connection. Change it inside Postgres too:
  `make psql` then `ALTER USER hindsight_user WITH PASSWORD '...';`
- **The Postgres major version** in `HINDSIGHT_DB_IMAGE` cannot be changed in
  place. Postgres refuses to open a data directory written by a different major
  version. Dump, wipe, restore.

---

## Repo layout

```
docker-compose.yml          the two containers
.env.example                every setting, commented
Makefile                    make help
sql/001-extensions.sql      vector + pg_trgm, run once on first init
scripts/token-shim.py       credential proxy for a provider whose bearer expires
scripts/preflight.sh        pre-start checks
docs/API-AUTH.md            generating an API key; what it covers
docs/CURATION.md            retiring stale memories, and writing ones that age
docs/TOKEN-SHIM.md          the shim, its refresh model, and a local overlay
docs/REMOTE-ACCESS.md       reaching the stack from your other devices
scripts/api.sh              one API call, host-first with in-container fallback
scripts/show.py             renders a response; an empty stream is a failure
docs/REGISTRY.md            why the Postgres image is not upstream's
docs/SLIM.md                switching to the 500 MB image
docs/INSTALL-REFERENCE.md   upstream install doc, kept verbatim for reference
```

---

## Everyday commands, full list

```bash
make help        # list every target
make preflight   # check config, Docker, shim reachability
make up          # start both containers
make down        # stop, keeping all stored memories
make restart     # recreate both containers after editing .env
make ps          # container status
make health      # poll the API until healthy
make check-llm   # one real LLM round trip, prints the actual error
make banks       # list memory banks and fact counts
make retain      # TEXT="..." store a memory synchronously
make recall      # QUERY="..." query stored memories (full JSON)
make recall-text # QUERY="..." just the matched memory lines
make find        # QUERY="..." print matching memories with their ids
make invalidate  # ID=<uuid> REASON="..." soft-retire a stale memory
make serve-on    # expose dashboard + API to your tailnet over HTTPS
make serve-status# what is exposed, and which devices can reach it
make api-key     # print the API key (| pbcopy to copy)
make mcp-url     # print the claude mcp add command for a remote device
make serve-off   # stop exposing anything
make logs        # follow the API log
make logs-db     # follow the PostgreSQL log
make psql        # psql shell on the database
make shim        # run the credential shim
make models      # ask your provider which models you can use
make config      # fully-resolved compose config (contains secrets)
make pull        # pull newer images
make wipe        # DESTRUCTIVE, deletes every memory
```

---

## Where this came from

`docker-compose.yml` is based on the upstream
[`external-pg`](https://github.com/vectorize-io/hindsight/tree/main/docker/docker-compose/external-pg)
example, with four deliberate changes:

1. **`PGDATA` is pinned** to `/var/lib/postgresql/data`. Upstream mounts the
   volume at `/var/lib/postgresql/${VERSION}/docker`, which is only correct for
   the pg18 image, because Postgres 18 moved the default. Setting `PGDATA`
   explicitly makes the path correct at any version.
2. **A ghcr.io Postgres image**, because Docker Hub is blocked here. See
   [docs/REGISTRY.md](docs/REGISTRY.md).
3. **A healthcheck and `depends_on: service_healthy`.** Without it the API races
   Postgres on a cold start and the first `up` usually fails migrations.
4. **Ports bind to loopback**, `HINDSIGHT_API_WORKER_ID` is set to a stable value
   so tasks interrupted by a restart are not orphaned under a dead container ID,
   and the bank LLM health endpoint is enabled for diagnosis.
