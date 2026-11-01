#!/usr/bin/env bash
# Déploie Incus + LXC + Cockpit (module console) sur un serveur via SSH.
#
#   ./deploy.sh                       # cible par défaut : incus@192.168.1.17
#   ./deploy.sh outscale@192.168.1.17 # autre utilisateur
#   ./deploy.sh incus                 # alias défini dans ~/.ssh/config
#   ./deploy.sh incus@192.168.1.17 admin   # ajoute « admin » au groupe incus-admin
#
# Options SSH supplémentaires : SSH_OPTS="-i ~/.ssh/ma_cle.rsa -p 2222" ./deploy.sh
set -euo pipefail

TARGET="${1:-incus@192.168.1.17}"
ADMIN_USER="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Une seule connexion SSH partagée : la passphrase/le mot de passe n'est demandé qu'une fois.
CTL="${TMPDIR:-/tmp}/incus-cockpit-ssh-%C"
read -r -a EXTRA <<< "${SSH_OPTS:-}"
SSH=(ssh -o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=120 ${EXTRA[@]+"${EXTRA[@]}"})

echo "==> Connexion à $TARGET"
REMOTE_DIR="$("${SSH[@]}" "$TARGET" 'mktemp -d /tmp/incus-cockpit.XXXXXX')"

echo "==> Copie du kit dans $REMOTE_DIR"
tar -C "$HERE" -czf - install.sh cockpit-incus | "${SSH[@]}" "$TARGET" "tar -xzf - -C '$REMOTE_DIR'"

echo "==> Installation (sudo peut demander votre mot de passe)"
ARG=""
[ -n "$ADMIN_USER" ] && ARG="$(printf '%q' "$ADMIN_USER")"
"${SSH[@]}" -t "$TARGET" "
    if [ \"\$(id -u)\" -eq 0 ]; then bash '$REMOTE_DIR/install.sh' $ARG; else sudo bash '$REMOTE_DIR/install.sh' $ARG; fi
    rc=\$?; rm -rf '$REMOTE_DIR'; exit \$rc"

"${SSH[@]}" -O exit "$TARGET" 2>/dev/null || true
