#!/usr/bin/env bash
# Checks the things that actually break this stack, in the order they break it.
# Read-only: starts nothing, changes nothing.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

PASS=0 FAIL=0 WARN=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; WARN=$((WARN+1)); }
hint() { printf '        \033[2m%s\033[0m\n' "$1"; }

echo
echo "1. Configuration"

if [[ -f .env ]]; then
  ok ".env exists"
  set -a; # shellcheck disable=SC1091
  source .env 2>/dev/null; set +a
else
  bad ".env missing"
  hint "cp .env.example .env   then edit it"
fi

if [[ -n "${HINDSIGHT_DB_PASSWORD:-}" ]]; then
  ok "HINDSIGHT_DB_PASSWORD is set"
else
  bad "HINDSIGHT_DB_PASSWORD is empty -- compose will refuse to start"
  hint "openssl rand -base64 24"
fi

for v in HINDSIGHT_API_LLM_PROVIDER HINDSIGHT_API_LLM_MODEL HINDSIGHT_API_LLM_API_KEY; do
  if [[ -n "${!v:-}" ]]; then ok "$v is set"; else bad "$v is empty"; fi
done

echo
echo "2. Docker"

if ! command -v docker >/dev/null 2>&1; then
  bad "docker not on PATH"
elif docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"
else
  bad "docker daemon not reachable"
  hint "colima start --cpu 4 --memory 8   (the full image needs ~2 GB for the API alone)"
fi

if docker info >/dev/null 2>&1; then
  mem_bytes=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
  mem_gb=$(( mem_bytes / 1024 / 1024 / 1024 ))
  if (( mem_gb >= 4 )); then
    ok "docker VM memory ${mem_gb} GB"
  else
    warn "docker VM memory ${mem_gb} GB -- tight for the full image (API ~2 GB + Postgres ~1 GB)"
    hint "colima stop && colima start --cpu 4 --memory 8"
  fi

  if docker compose config >/dev/null 2>&1; then
    ok "docker-compose.yml is valid"
  else
    bad "docker compose config failed"
    hint "docker compose config    to see why"
  fi
fi

echo
echo "3. Token shim (skipped unless the base URL points at one)"

if [[ "${HINDSIGHT_API_LLM_BASE_URL:-}" != *:"${SHIM_PORT:-8787}"* ]]; then
  echo "        not configured for the shim -- nothing to check"
else
  shim_port="${SHIM_PORT:-8787}"
  token_cmd="${SHIM_TOKEN_CMD:-}"

  if [[ -z "$token_cmd" ]]; then
    bad "SHIM_TOKEN_CMD is not set -- the shim has no way to get a bearer"
    hint "see docs/TOKEN-SHIM.md section 2"
  elif [[ -x "${token_cmd%% *}" ]]; then
    ok "token command is executable: ${token_cmd%% *}"
    # A helper that runs is not enough: some print a fixed sentinel when they
    # are on a different auth path, and that sentinel is not a bearer.
    tok=$("$token_cmd" 2>/dev/null)
    reject="${SHIM_REJECT_TOKENS:-}"
    if [[ -n "$reject" ]] && printf '%s' ",$reject," | grep -qF ",$tok,"; then
      bad "token command returns the sentinel '$tok', which SHIM_REJECT_TOKENS lists as not-a-bearer"
      hint "see docs/TOKEN-SHIM.md section 6"
    elif [[ "$tok" == eyJ* ]]; then
      # Report the real remaining lifetime from the token's own exp claim.
      mins=$(printf '%s' "$tok" | python3 -c "
import sys,base64,json,time
p=sys.stdin.read().strip().split('.')[1]; p+='='*(-len(p)%4)
e=json.loads(base64.urlsafe_b64decode(p)).get('exp')
print(f'{(e-time.time())/60:.0f}' if e else '?')
" 2>/dev/null || echo '?')
      ok "token command returns a JWT, ${mins} min until it expires (shim refreshes it)"
    elif [[ -n "$tok" ]]; then
      warn "token command returned something that is not a JWT"
      hint "fine if your provider issues opaque tokens -- the shim will use SHIM_FALLBACK_TTL"
    else
      bad "token command returned nothing"
    fi
  else
    bad "token command not found or not executable: ${token_cmd%% *}"
  fi

  if [[ -n "${SHIM_EXTRA_HEADER:-}" ]]; then
    if [[ -n "${SHIM_EXTRA_HEADER_VALUE:-}${SHIM_EXTRA_HEADER_CMD:-}" ]]; then
      ok "SHIM_EXTRA_HEADER ${SHIM_EXTRA_HEADER} has a value"
    else
      bad "SHIM_EXTRA_HEADER is set but neither _VALUE nor _CMD is"
    fi
  fi

  if nc -z 127.0.0.1 "$shim_port" 2>/dev/null; then
    ok "shim is listening on 127.0.0.1:${shim_port}"
  else
    bad "nothing listening on 127.0.0.1:${shim_port}"
    hint "make shim    (in another terminal)"
  fi

  # The check that actually matters. Under Colima the container's "host" is the
  # Lima VM, not macOS, so a loopback-bound shim is not obviously reachable.
  # In practice Lima's user-mode networking maps host.docker.internal
  # (192.168.5.2) onto the macOS loopback, so it works -- but verify rather than
  # assume, because it depends on the Colima networking mode.
  #
  # alpine, not curlimages/curl: some networks allow one and 403 the other.
  host_in_url=$(printf '%s' "${HINDSIGHT_API_LLM_BASE_URL}" | sed -E 's#^https?://([^:/]+).*#\1#')
  if docker info >/dev/null 2>&1; then
    if docker run --rm --add-host "host.docker.internal:host-gateway" alpine:latest \
         sh -c "nc -z -w 5 ${host_in_url} ${shim_port}" >/dev/null 2>&1; then
      ok "a container can reach ${host_in_url}:${shim_port}"
    else
      bad "a container CANNOT reach ${host_in_url}:${shim_port}"
      hint "the shim is bound to host loopback and the Docker VM cannot see it"
      hint "see docs/TOKEN-SHIM.md section 4"
    fi
  fi
fi

echo
printf '  %d ok, %d warn, %d fail\n\n' "$PASS" "$WARN" "$FAIL"
(( FAIL == 0 ))
