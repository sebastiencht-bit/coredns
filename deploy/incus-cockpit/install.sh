#!/usr/bin/env bash
# Installe Incus, LXC, Cockpit et le module Cockpit « Incus & LXC » (console web).
# À exécuter en root sur le serveur cible :  sudo bash install.sh [utilisateur-admin]
#
# Variables optionnelles :
#   INCUS_SOURCE=distro|zabbly   source des paquets Incus (défaut : distro si disponible, sinon zabbly)
#   SKIP_INCUS_INIT=1            ne pas lancer « incus admin init --minimal »
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADMIN_USER="${1:-${SUDO_USER:-}}"
PLUGIN_SRC="$HERE/cockpit-incus"
PLUGIN_DST="/usr/share/cockpit/incus"
ZABBLY_FPR="4EFC590696CB15B87C73A3AD82CC8797C838DCFD"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Ce script doit être lancé en root (sudo bash $0)."
[ -d "$PLUGIN_SRC" ] || die "Module introuvable : $PLUGIN_SRC"
[ -r /etc/os-release ] || die "/etc/os-release absent : distribution non reconnue."
# shellcheck disable=SC1091
. /etc/os-release
FAMILY="${ID_LIKE:-} ${ID}"

# --------------------------------------------------------------------------
install_debian() {
    export DEBIAN_FRONTEND=noninteractive
    log "Mise à jour des index APT"
    apt-get update -q
    apt-get install -y -q --no-install-recommends ca-certificates curl gnupg

    local candidate source="${INCUS_SOURCE:-}"
    candidate="$(apt-cache policy incus 2>/dev/null | awk '/Candidate:/ {print $2}')"
    if [ -z "$source" ]; then
        if [ -n "$candidate" ] && [ "$candidate" != "(none)" ]; then source=distro; else source=zabbly; fi
    fi

    if [ "$source" = zabbly ] && [ ! -f /etc/apt/sources.list.d/zabbly-incus-stable.sources ]; then
        log "Ajout du dépôt Incus Zabbly (paquets officiels de l'équipe Incus)"
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL https://pkgs.zabbly.com/key.asc -o /etc/apt/keyrings/zabbly.asc
        local fpr
        fpr="$(gpg --show-keys --with-colons /etc/apt/keyrings/zabbly.asc | awk -F: '/^fpr:/ {print $10; exit}')"
        [ "$fpr" = "$ZABBLY_FPR" ] || { rm -f /etc/apt/keyrings/zabbly.asc; die "Empreinte de la clé Zabbly inattendue : $fpr"; }
        cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources <<SRC
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: ${VERSION_CODENAME}
Components: main
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/zabbly.asc
SRC
        apt-get update -q
    fi

    log "Installation d'Incus (source : $source)"
    apt-get install -y -q incus

    log "Installation de LXC"
    apt-get install -y -q lxc
    apt-get install -y -q lxc-templates || warn "lxc-templates indisponible (le modèle « download » suffit)."

    # --no-install-recommends : évite d'installer NetworkManager/udisks/packagekit
    # qui pourraient perturber la configuration réseau d'une VM cloud.
    log "Installation de Cockpit"
    apt-get install -y -q --no-install-recommends cockpit
}

install_fedora() {
    log "Installation d'Incus, LXC et Cockpit (dnf)"
    dnf install -y incus lxc lxc-templates cockpit-ws cockpit-bridge cockpit-system
    systemctl enable --now incus.socket incus.service || true
}

case " $FAMILY " in
    *" debian "*|*" ubuntu "*) install_debian ;;
    *" fedora "*)              install_fedora ;;
    *) die "Distribution non prise en charge : ${PRETTY_NAME:-$ID} (Debian, Ubuntu ou Fedora attendus)." ;;
esac

# --------------------------------------------------------------------------
log "Configuration d'Incus"
systemctl enable --now incus.socket >/dev/null 2>&1 || true
if [ "${SKIP_INCUS_INIT:-0}" != 1 ]; then
    if incus storage list --format csv 2>/dev/null | grep -q .; then
        log "Incus est déjà initialisé (pool de stockage présent)."
    else
        incus admin init --minimal
        log "Incus initialisé (pool « default », pont « incusbr0 »)."
    fi
fi

log "Configuration du réseau LXC (pont lxcbr0)"
if [ -f /etc/default/lxc-net ] && grep -q '^USE_LXC_BRIDGE=' /etc/default/lxc-net; then
    sed -i 's/^USE_LXC_BRIDGE=.*/USE_LXC_BRIDGE="true"/' /etc/default/lxc-net
else
    echo 'USE_LXC_BRIDGE="true"' >> /etc/default/lxc-net
fi
if [ -f /etc/lxc/default.conf ] && grep -Eq '^lxc\.net\.0\.type *= *empty' /etc/lxc/default.conf; then
    sed -i -E 's/^lxc\.net\.0\.type *= *empty/lxc.net.0.type = veth\nlxc.net.0.link = lxcbr0\nlxc.net.0.flags = up/' /etc/lxc/default.conf
fi
systemctl enable lxc-net >/dev/null 2>&1 || true
systemctl restart lxc-net || warn "Impossible de démarrer lxc-net (vérifiez « journalctl -u lxc-net »)."

if [ -n "$ADMIN_USER" ] && [ "$ADMIN_USER" != root ] && id "$ADMIN_USER" >/dev/null 2>&1; then
    if getent group incus-admin >/dev/null; then
        usermod -aG incus-admin "$ADMIN_USER"
        log "Utilisateur « $ADMIN_USER » ajouté au groupe incus-admin."
    fi
fi

# --------------------------------------------------------------------------
log "Installation du module Cockpit dans $PLUGIN_DST"
rm -rf "$PLUGIN_DST"
install -d -m 0755 "$PLUGIN_DST" "$PLUGIN_DST/vendor"
install -m 0644 "$PLUGIN_SRC"/manifest.json "$PLUGIN_SRC"/index.html "$PLUGIN_SRC"/incus.js "$PLUGIN_SRC"/incus.css "$PLUGIN_DST/"
install -m 0644 "$PLUGIN_SRC"/vendor/* "$PLUGIN_DST/vendor/"

systemctl enable --now cockpit.socket
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 9090/tcp >/dev/null && log "Port 9090 ouvert dans ufw."
fi
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=cockpit >/dev/null && firewall-cmd --reload >/dev/null
    log "Service cockpit ouvert dans firewalld."
fi

# --------------------------------------------------------------------------
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
log "Terminé."
echo "   Incus : $(incus version 2>/dev/null | awk -F': ' '/Server/ {print $2}')    LXC : $(lxc-ls --version 2>/dev/null)"
echo
echo "   Cockpit : https://${IP:-<ip-du-serveur>}:9090  →  menu « Incus & LXC »"
echo "   Sans ouvrir le port 9090 : ssh -L 9090:localhost:9090 <utilisateur>@${IP:-<ip>}  puis https://localhost:9090"
echo
echo "   - Connectez-vous avec un compte Linux qui a un mot de passe (sudo passwd ${ADMIN_USER:-<utilisateur>})."
echo "   - Cliquez sur « Accès limité » en haut de Cockpit pour passer en accès administrateur."
echo "   - Sur Outscale, autorisez TCP 9090 dans le Security Group, ou utilisez le tunnel SSH ci-dessus."
