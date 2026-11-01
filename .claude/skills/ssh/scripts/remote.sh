#!/usr/bin/env bash
# Accès SSH non interactif au serveur Incus/LXC. Voir ../SKILL.md.
set -uo pipefail

TARGET="${INCUS_SSH_TARGET:-incus@192.168.1.17}"
if [ "${1:-}" = "-t" ]; then TARGET="${2:?cible manquante après -t}"; shift 2; fi
CMD="${1:-help}"; [ $# -gt 0 ] && shift

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
KIT="$ROOT/deploy/incus-cockpit"
CTL="${TMPDIR:-/tmp}/claude-ssh-%C"
read -r -a EXTRA <<< "${INCUS_SSH_OPTS:-}"
OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new
      -o ServerAliveInterval=15 -o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=600
      ${EXTRA[@]+"${EXTRA[@]}"})

die() { echo "ERREUR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "commande locale « $1 » absente."; }
rsh() { ssh "${OPTS[@]}" "$TARGET" "$@"; }
# scp prend le port avec -P (ssh : -p).
rcp() { local o=() a; for a in "${OPTS[@]}"; do [ "$a" = "-p" ] && a="-P"; o+=("$a"); done; scp -r "${o[@]}" "$@"; }

# Hôte et port effectifs (tient compte de ~/.ssh/config et des alias).
host_port() {
    local cfg
    cfg="$(ssh -G ${EXTRA[@]+"${EXTRA[@]}"} "$TARGET" 2>/dev/null)" || { echo "${TARGET#*@} 22"; return; }
    printf '%s %s\n' "$(awk '$1=="hostname"{print $2; exit}' <<<"$cfg")" "$(awk '$1=="port"{print $2; exit}' <<<"$cfg")"
}

case "$CMD" in
check)
    need ssh
    read -r H P < <(host_port)
    if ! timeout 5 bash -c "exec 3<>/dev/tcp/$H/${P:-22}" 2>/dev/null; then
        echo "UNREACHABLE: $H:${P:-22} injoignable depuis $(hostname) (réseau privé, VPN, pare-feu ou Security Group)."
        exit 2
    fi
    echo "TCP=ok ($H:${P:-22})"
    if ! out="$(rsh 'echo "USER=$(id -un) HOST=$(hostname) OS=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}" | tr " " _)"' 2>&1)"; then
        echo "AUTH_FAILED: $out"
        exit 3
    fi
    echo "$out"
    rsh '
        if [ "$(id -u)" = 0 ]; then echo SUDO=root
        elif sudo -n true 2>/dev/null; then echo SUDO=nopasswd
        else echo SUDO=password; fi
        v=$(incus version 2>/dev/null | awk -F": " "/Server/{print \$2}"); echo "INCUS=${v:-absent}"
        v=$(lxc-ls --version 2>/dev/null); echo "LXC=${v:-absent}"
        v=$(systemctl is-active cockpit.socket 2>/dev/null); echo "COCKPIT=${v:-absent}"
        echo "MODULE=$([ -f /usr/share/cockpit/incus/manifest.json ] && echo installé || echo absent)"'
    ;;
run)
    [ $# -gt 0 ] || die "usage : remote.sh run '<commande>'"
    rsh "$*"
    ;;
sudo)
    [ $# -gt 0 ] || die "usage : remote.sh sudo '<commande>'"
    rsh "if [ \"\$(id -u)\" = 0 ]; then sh -c $(printf '%q' "$*"); else sudo -n sh -c $(printf '%q' "$*"); fi"
    ;;
copy)
    [ $# -eq 2 ] || die "usage : remote.sh copy <local> <distant>"
    rcp "$1" "$TARGET:$2"
    ;;
fetch)
    [ $# -eq 2 ] || die "usage : remote.sh fetch <distant> <local>"
    rcp "$TARGET:$1" "$2"
    ;;
deploy)
    [ -f "$KIT/install.sh" ] || die "kit introuvable : $KIT"
    dir="$(rsh 'mktemp -d /tmp/incus-cockpit.XXXXXX')" || die "connexion impossible (lancez « check »)."
    tar -C "$KIT" -czf - install.sh cockpit-incus | rsh "tar -xzf - -C '$dir'" || die "copie du kit échouée."
    rsh "
        if [ \"\$(id -u)\" = 0 ]; then bash '$dir/install.sh' $*
        elif sudo -n true 2>/dev/null; then sudo -n bash '$dir/install.sh' $*
        else echo 'ERREUR: sudo demande un mot de passe ; lancez deploy/incus-cockpit/deploy.sh vous-même.' >&2; rm -rf '$dir'; exit 4; fi
        rc=\$?; rm -rf '$dir'; exit \$rc"
    ;;
close)
    ssh "${OPTS[@]}" -O exit "$TARGET" 2>/dev/null || true
    ;;
help|-h|--help)
    sed -n '/^```bash/,/^```/p' "$(dirname "${BASH_SOURCE[0]}")/../SKILL.md" | sed '1d;$d'
    ;;
*)
    die "sous-commande inconnue : $CMD (help pour l'aide)"
    ;;
esac
