# Container registry access on a proxied network

## The problem

On the network this stack was built on, the Colima Docker daemon routes all
registry traffic through a proxy on the host. That proxy allows `ghcr.io` and
refuses `docker.io` with **HTTP 403 Forbidden**. So the upstream Hindsight compose
example cannot work there unchanged: its Postgres image, `pgvector/pgvector`,
lives on Docker Hub.

If Docker Hub is reachable for you, none of this applies -- set
`HINDSIGHT_DB_IMAGE=pgvector/pgvector:pg18` in `.env` and skip the rest. This page
exists because the failure mode is a bare 403 with no explanation, and the fix is
not obvious.

The daemon config inside the VM shows the routing:

```bash
$ colima ssh -- cat /etc/docker/daemon.json
{
  "proxies": {
    "http-proxy":  "http://192.168.5.2:7004",
    "https-proxy": "http://192.168.5.2:7004"
  }
}
```

`192.168.5.2` is the host as seen from the Lima VM. Colima picks the proxy up from
the host environment at `colima start` time.

## What was tested

| Image | Registry | Result |
| --- | --- | --- |
| `ghcr.io/vectorize-io/hindsight:latest` | ghcr.io | **OK** |
| `ghcr.io/tensorchord/vchord-postgres:pg17-v0.4.3` | ghcr.io | **OK**, chosen |
| `ghcr.io/cloudnative-pg/postgresql:18` | ghcr.io | pulls, but unusable, see below |
| `docker.io/pgvector/pgvector:pg18` | Docker Hub | 403 Forbidden |
| `docker.io/library/postgres:18` | Docker Hub | 403 Forbidden |
| `docker.io/curlimages/curl:latest` | Docker Hub | 403 Forbidden |
| `quay.io/enterprisedb/postgresql:18` | quay.io | failed |

If your network has a Docker Hub mirror, adding it as a `registry-mirror` in the
daemon config is the cleaner long-term fix, and `HINDSIGHT_DB_IMAGE` can then point
back at `pgvector/pgvector`.

## Why `ghcr.io/tensorchord/vchord-postgres`

It was the only reachable image that satisfied all four requirements. Verified by
running it:

1. **Standard entrypoint.** Built on the official `postgres` base, so it keeps
   `docker-entrypoint.sh`. `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`,
   `PGDATA` and `/docker-entrypoint-initdb.d` all behave normally.
2. **Ships `vector`.** `CREATE EXTENSION vector` succeeds. It also ships `vchord`
   and `pg_trgm`, the latter being required by Hindsight's entity resolution.
3. **Honors `PGDATA`.** Confirmed `PG_VERSION` written to
   `/var/lib/postgresql/data`, which is what the named volume mounts.
4. **PostgreSQL 17**, comfortably above Hindsight's minimum of 14.

### Why not CloudNativePG

`ghcr.io/cloudnative-pg/postgresql:18` pulls fine but is built for the CNPG
Kubernetes operator, not for standalone use:

```bash
$ docker inspect ghcr.io/cloudnative-pg/postgresql:18 \
    --format 'Entrypoint={{.Config.Entrypoint}} Cmd={{.Config.Cmd}} User={{.Config.User}}'
Entrypoint=[] Cmd=[bash] User=26
```

No entrypoint, so it exits immediately and ignores every `POSTGRES_*` variable.
Making it work would mean reimplementing initdb, which is not worth it.

## A note on the vchord option

The chosen image also ships the `vchord` extension, which Hindsight supports as an
alternative vector backend. This stack stays on `pgvector` because that is
Hindsight's default and best-tested path. To try vchord instead, set
`HINDSIGHT_API_VECTOR_EXTENSION=vchord` in `.env`. The image already loads
`vchord.so` via `shared_preload_libraries`, so nothing else needs to change.

Do not switch on a populated database without a plan: the two extensions build
different index types, and Hindsight will want to rebuild them.

## Using a different image

`HINDSIGHT_DB_IMAGE` in `.env` overrides it. Any image that meets the four
requirements above will do:

```bash
# if Docker Hub access is ever restored
HINDSIGHT_DB_IMAGE=pgvector/pgvector:pg18
```
