#!/usr/bin/env bash
# Prepare the Incus HOST for incus-mcp, then install incus-mcp in its container.
# Run on the Incus host, from this directory, as root:   sudo ./host-setup.sh          (shows the plan)
#                                                        sudo ./host-setup.sh --yes    (applies it)
# Idempotent. Without --yes nothing on the host is changed.
#
# What it does:
#   1. creates the restricted Incus project $PROJECT (with quotas) and gives its default profile a disk and a NIC;
#   2. makes the Incus API listen on $HTTPS_ADDR (loopback only) if no address is set yet;
#   3. adds the "incus-api" proxy device to $CONTAINER, so the container reaches that API at 127.0.0.1:8443;
#   4. installs incus-mcp in $CONTAINER (install.sh);
#   5. trusts the container's client certificate, RESTRICTED to $PROJECT;
#   6. copies the host's Incus server certificate into the container (pinned by incus-mcp), then runs "incus-mcp check".
set -euo pipefail

CONTAINER="${CONTAINER:-cockpit}"
PROJECT="${PROJECT:-timon}"
LIMIT_INSTANCES="${LIMIT_INSTANCES:-10}"
LIMIT_CPU="${LIMIT_CPU:-16}"
LIMIT_MEMORY="${LIMIT_MEMORY:-32GiB}"
DEFAULT_CPU="${DEFAULT_CPU:-1}"          # per-instance defaults (project profile), required by the quotas above
DEFAULT_MEMORY="${DEFAULT_MEMORY:-2GiB}"
HTTPS_ADDR="${HTTPS_ADDR:-127.0.0.1:8443}"
STORAGE_POOL="${STORAGE_POOL:-}"     # default: the pool of the default profile's root disk
NETWORK="${NETWORK:-}"               # default: the first managed bridge
INCUS_DIR="${INCUS_DIR:-/var/lib/incus}"
TRUST_NAME="${TRUST_NAME:-incus-mcp}"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLY=0; [ "${1:-}" = "--yes" ] && APPLY=1

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
plan() { printf '\033[1;33m[plan]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ $APPLY = 1 ]; then "$@"; else plan "$*"; fi; }

command -v incus >/dev/null || die "the incus command is missing"
incus info >/dev/null 2>&1 || die "cannot talk to Incus (run as root or as a member of incus-admin)"
[ "$PROJECT" != default ] || die "PROJECT must not be 'default'"
[ "$(incus list --project default -f csv -c ns 2>/dev/null | awk -F, -v n="$CONTAINER" '$1==n {print $2}')" = RUNNING ] \
    || die "container $CONTAINER (project default) is not running"
[ $APPLY = 1 ] || log "Dry run: nothing will be changed. Re-run with --yes to apply."

# --- 1. project ---------------------------------------------------------------------------------
if incus project show "$PROJECT" >/dev/null 2>&1; then
    log "Project $PROJECT exists"
else
    run incus project create "$PROJECT" -c features.images=false -c features.profiles=true \
        -c features.storage.volumes=true -c restricted=true \
        -c limits.instances="$LIMIT_INSTANCES" -c limits.cpu="$LIMIT_CPU" -c limits.memory="$LIMIT_MEMORY"
fi
[ -n "$STORAGE_POOL" ] || STORAGE_POOL="$(incus profile device get default root pool --project default 2>/dev/null || true)"
[ -n "$NETWORK" ] || NETWORK="$(incus network list --project default -f csv -c nmt 2>/dev/null | awk -F, '$2=="bridge" && $3=="YES" {print $1; exit}')"
[ -n "$STORAGE_POOL" ] || die "no storage pool found: set STORAGE_POOL"
[ -n "$NETWORK" ] || die "no managed bridge found: set NETWORK"
profile_has() { incus profile device show default --project "$PROJECT" 2>/dev/null | grep -q "^$1:"; }
profile_has root || run incus profile device add default root disk path=/ pool="$STORAGE_POOL" --project "$PROJECT"
profile_has eth0 || run incus profile device add default eth0 nic network="$NETWORK" name=eth0 --project "$PROJECT"
# With limits.cpu/limits.memory on the project, Incus refuses any instance that does not set them itself
# ("has no limits.cpu config, either directly or via a profile"): give the project's profile defaults.
for kv in limits.cpu="$DEFAULT_CPU" limits.memory="$DEFAULT_MEMORY"; do
    [ -n "$(incus profile get default "${kv%%=*}" --project "$PROJECT" 2>/dev/null || true)" ] \
        || run incus profile set default "$kv" --project "$PROJECT"
done
# restricted=true blocks privileged containers, raw.* keys, host paths and other ways out of the project.
# A restricted project still needs permission to use the host's managed networks:
run incus project set "$PROJECT" restricted.networks.access="$NETWORK"

# --- 2. Incus API on loopback --------------------------------------------------------------------
CURRENT="$(incus config get core.https_address 2>/dev/null || true)"
if [ -z "$CURRENT" ]; then
    run incus config set core.https_address="$HTTPS_ADDR"
    API_PORT="${HTTPS_ADDR##*:}"
else
    API_PORT="${CURRENT##*:}"; API_PORT="${API_PORT:-8443}"
    log "core.https_address is already $CURRENT: kept (incus-mcp connects to 127.0.0.1:$API_PORT)"
    case "$CURRENT" in 127.0.0.1:*|"[::1]":*) ;; *) log "  note: the Incus API is already reachable beyond loopback ($CURRENT)";; esac
fi

# --- 3. proxy device -----------------------------------------------------------------------------
if incus config device get "$CONTAINER" incus-api type --project default >/dev/null 2>&1; then
    log "Proxy device incus-api exists on $CONTAINER"
else
    run incus config device add "$CONTAINER" incus-api proxy bind=container \
        listen=tcp:127.0.0.1:8443 connect="tcp:127.0.0.1:$API_PORT" --project default
fi

# --- 4. install in the container -----------------------------------------------------------------
if [ $APPLY = 1 ]; then
    log "Installing incus-mcp in $CONTAINER"
    incus exec "$CONTAINER" --project default -- rm -rf /root/incus-mcp-src
    tar -C "$SRC" --exclude=__pycache__ --exclude='*.egg-info' --exclude=.pytest_cache -czf - . \
        | incus exec "$CONTAINER" --project default -- sh -c 'mkdir -p /root/incus-mcp-src && tar -xzf - -C /root/incus-mcp-src'
    incus exec "$CONTAINER" --project default -- bash /root/incus-mcp-src/install.sh
else
    plan "copy this directory to $CONTAINER:/root/incus-mcp-src and run install.sh there"
fi

# --- 5. restricted trust -------------------------------------------------------------------------
if [ $APPLY = 1 ]; then
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
    incus file pull "$CONTAINER/etc/incus-mcp/client.crt" "$tmp/client.crt" --project default
    fp="$(openssl x509 -in "$tmp/client.crt" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')"
    if incus config trust list -f csv 2>/dev/null | grep -qi "${fp:0:12}"; then
        log "Client certificate ${fp:0:12} already trusted"
    else
        incus config trust add-certificate "$tmp/client.crt" --restricted --projects "$PROJECT" --name "$TRUST_NAME"
        log "Client certificate ${fp:0:12} trusted, restricted to project $PROJECT"
    fi
else
    plan "trust $CONTAINER:/etc/incus-mcp/client.crt with --restricted --projects $PROJECT"
fi

# --- 6. pinned server certificate, start, self-check ---------------------------------------------
[ -r "$INCUS_DIR/server.crt" ] || die "cannot read $INCUS_DIR/server.crt (run as root, or set INCUS_DIR)"
if [ $APPLY = 1 ]; then
    incus file push "$INCUS_DIR/server.crt" "$CONTAINER/etc/incus-mcp/server.crt" --mode 0644 --project default
    incus exec "$CONTAINER" --project default -- systemctl restart incus-mcp
    log "Self-check from the container:"
    incus exec "$CONTAINER" --project default -- sh -c \
        'set -a; . /etc/incus-mcp/incus-mcp.env; set +a; /opt/incus-mcp/venv/bin/incus-mcp check' \
        || die "self-check failed: see above"
    incus exec "$CONTAINER" --project default -- systemctl is-active incus-mcp
    log "Done. Token: incus exec $CONTAINER -- cat /etc/incus-mcp/token   (read it yourself, keep it out of logs)"
else
    plan "push $INCUS_DIR/server.crt to $CONTAINER:/etc/incus-mcp/server.crt, restart incus-mcp, run 'incus-mcp check'"
fi
