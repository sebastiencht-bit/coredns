#!/usr/bin/env bash
# Install or update incus-mcp INSIDE the container that will run it (Debian/Ubuntu), as root.
#   bash install.sh
# Idempotent: existing token, keys, certificates and configuration are kept.
#
# Variables:
#   INCUS_MCP_LISTEN_HOST  address to listen on (default: this machine's source address on its default route)
#   INCUS_MCP_LISTEN_PORT  default 8444
#   INCUS_MCP_ALLOWED_NETWORKS  default 192.168.1.0/24
#   INCUS_MCP_CA_DIR       local CA used to sign the HTTPS certificate (default /etc/incus-mcp/ca;
#                          point it at an existing CA directory holding ca.crt + ca.key to reuse it)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX=/opt/incus-mcp
ETC=/etc/incus-mcp
LOGDIR=/var/log/incus-mcp
CA_DIR="${INCUS_MCP_CA_DIR:-$ETC/ca}"
PORT="${INCUS_MCP_LISTEN_PORT:-8444}"
NETS="${INCUS_MCP_ALLOWED_NETWORKS:-192.168.1.0/24}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -f "$SRC/pyproject.toml" ] || die "run from the incus-mcp source directory"

LISTEN="${INCUS_MCP_LISTEN_HOST:-$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") {print $(i+1); exit}}')}"
[ -n "$LISTEN" ] || die "cannot find this machine's address: set INCUS_MCP_LISTEN_HOST"

log "Packages"
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q --no-install-recommends python3 python3-venv openssl ca-certificates iproute2 logrotate
fi
command -v python3 >/dev/null || die "python3 is required"

log "System user and directories"
getent group incus-mcp >/dev/null || groupadd --system incus-mcp
getent passwd incus-mcp >/dev/null || useradd --system --gid incus-mcp --home-dir /nonexistent --shell /usr/sbin/nologin incus-mcp
install -d -m 0750 -o root -g incus-mcp "$ETC"
install -d -m 0750 -o incus-mcp -g incus-mcp "$LOGDIR"
install -d -m 0755 "$PREFIX"

log "Python environment in $PREFIX/venv"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q --upgrade "$SRC"
install -m 0644 "$SRC/README.md" "$PREFIX/README.md"

secret_file() { chown root:incus-mcp "$1"; chmod 0640 "$1"; }

if [ ! -s "$ETC/token" ]; then
    log "Generating the bearer token"
    (umask 077; head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 48 > "$ETC/token")
fi
secret_file "$ETC/token"

if [ ! -s "$ETC/client.key" ]; then
    log "Generating the Incus client certificate (to be trusted as restricted on the host)"
    (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes -sha384 -days 3650 \
        -subj "/CN=incus-mcp@$(hostname)" -keyout "$ETC/client.key" -out "$ETC/client.crt" 2>/dev/null)
fi
secret_file "$ETC/client.key"; chmod 0644 "$ETC/client.crt"

if [ ! -s "$CA_DIR/ca.key" ]; then
    log "Creating the local CA in $CA_DIR"
    install -d -m 0700 "$CA_DIR"
    (umask 077; openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes -sha384 -days 3650 \
        -subj "/CN=incus-mcp local CA ($(hostname))" -keyout "$CA_DIR/ca.key" -out "$CA_DIR/ca.crt" \
        -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null)
fi

san_of() { openssl x509 -in "$1" -noout -ext subjectAltName 2>/dev/null | tail -n +2 | tr -d ' '; }
if [ ! -s "$ETC/tls.crt" ] || [ "$(san_of "$ETC/tls.crt")" != "IPAddress:$LISTEN" ]; then
    log "HTTPS certificate for $LISTEN, signed by the local CA"
    tmp="$(mktemp -d)"
    (umask 077; openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-384 -nodes -sha384 \
        -subj "/CN=$LISTEN" -addext "subjectAltName=IP:$LISTEN" -addext "extendedKeyUsage=serverAuth" \
        -keyout "$ETC/tls.key" -out "$tmp/tls.csr" 2>/dev/null)
    openssl x509 -req -in "$tmp/tls.csr" -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
        -days 825 -sha384 -copy_extensions copy -out "$ETC/tls.crt" 2>/dev/null
    rm -rf "$tmp"
fi
secret_file "$ETC/tls.key"; chmod 0644 "$ETC/tls.crt"
install -m 0644 "$CA_DIR/ca.crt" "$ETC/ca.crt"

if [ ! -f "$ETC/incus-mcp.env" ]; then
    log "Configuration $ETC/incus-mcp.env"
    sed -e "s|^INCUS_MCP_LISTEN_HOST=.*|INCUS_MCP_LISTEN_HOST=$LISTEN|" \
        -e "s|^INCUS_MCP_LISTEN_PORT=.*|INCUS_MCP_LISTEN_PORT=$PORT|" \
        -e "s|^INCUS_MCP_ALLOWED_NETWORKS=.*|INCUS_MCP_ALLOWED_NETWORKS=$NETS|" \
        "$SRC/incus-mcp.env.example" > "$ETC/incus-mcp.env"
    chown root:incus-mcp "$ETC/incus-mcp.env"; chmod 0640 "$ETC/incus-mcp.env"
fi

install -m 0644 "$SRC/systemd/incus-mcp.logrotate" /etc/logrotate.d/incus-mcp

if [ -d /run/systemd/system ]; then
    install -m 0644 "$SRC/systemd/incus-mcp.service" /etc/systemd/system/incus-mcp.service
    systemctl daemon-reload
    systemctl enable incus-mcp >/dev/null 2>&1
    if [ -s "$ETC/server.crt" ]; then
        systemctl restart incus-mcp
        sleep 2
        systemctl is-active --quiet incus-mcp && log "incus-mcp is running" || {
            warn "incus-mcp did not start:"; journalctl -u incus-mcp -n 20 --no-pager >&2; exit 1; }
    else
        warn "Not started yet: $ETC/server.crt (the host's Incus certificate) is missing; host-setup.sh installs it."
    fi
else
    warn "No systemd here: start it with  set -a; . $ETC/incus-mcp.env; set +a; $PREFIX/venv/bin/incus-mcp"
fi

echo
log "Done."
echo "   MCP endpoint : https://$LISTEN:$PORT/mcp   (health: /healthz)"
echo "   CA for clients: $ETC/ca.crt"
echo "   Client certificate fingerprint (to trust on the host):"
echo "     $(openssl x509 -in "$ETC/client.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
echo "   Read the token yourself: cat $ETC/token"
