# Incus + LXC avec console dans Cockpit

Ce kit installe sur un serveur (par défaut `192.168.1.17`) :

- **Incus** : conteneurs système et machines virtuelles ;
- **LXC** : outils classiques `lxc-*` ;
- **Cockpit** : interface d'administration web sur le port 9090 ;
- un module Cockpit **« Incus & LXC »** qui liste les instances et ouvre une console dans le navigateur.

## Déploiement en une commande

Depuis votre poste, qui doit joindre `192.168.1.17` en SSH :

```bash
cd deploy/incus-cockpit
./deploy.sh                          # cible par défaut : seb@192.168.1.17
./deploy.sh root@192.168.1.17        # autre utilisateur SSH
./deploy.sh incus                    # alias « Host incus » de ~/.ssh/config
SSH_OPTS="-i ~/.ssh/ma_cle.rsa" ./deploy.sh seb@192.168.1.17
```

`deploy.sh` copie le kit sur le serveur, puis lance `install.sh` avec `sudo`.
`sudo` peut demander votre mot de passe une fois.

Pour une installation manuelle sur le serveur :

```bash
scp -r deploy/incus-cockpit seb@192.168.1.17:
ssh -t seb@192.168.1.17 'sudo bash incus-cockpit/install.sh'
```

Systèmes pris en charge : Debian 11, 12 et 13, Ubuntu 22.04 et 24.04, et Fedora.
Incus vient de la distribution quand elle le fournit. Sinon, il vient du
[dépôt Zabbly](https://github.com/zabbly/incus), maintenu par l'équipe Incus.
L'empreinte de la clé GPG est vérifiée avant l'ajout du dépôt.
Pour forcer Zabbly : `sudo INCUS_SOURCE=zabbly bash install.sh`.

Cockpit est installé sans paquets recommandés. Cela évite que NetworkManager
modifie le réseau de la VM Outscale.

## Accès à Cockpit

- Avec le port 9090 ouvert dans le Security Group Outscale : `https://192.168.1.17:9090`.
- Sans ouvrir de port, par tunnel SSH :

  ```bash
  ssh -L 9090:localhost:9090 seb@192.168.1.17
  # puis https://localhost:9090 dans le navigateur
  ```

Cockpit demande un compte Linux avec un **mot de passe**. Les images Outscale
n'utilisent souvent qu'une clé SSH : définissez un mot de passe avec
`sudo passwd <utilisateur>`.
Une fois connecté, cliquez sur **« Accès limité »** pour passer en
**accès administrateur**. Ce mode est nécessaire pour LXC, qui tourne en root,
et pour les actions Incus.

## Le module « Incus & LXC »

| | Incus | LXC |
|---|---|---|
| Liste et état, rafraîchis toutes les 5 s | `incus list` | `lxc-ls --fancy` |
| Créer | `incus launch <image> <nom> [--vm]` | `lxc-create -t download -- -d <distro> -r <version> -a <arch>` |
| Démarrer, arrêter, redémarrer | `incus start/stop/restart` | `lxc-start` / `lxc-stop` |
| **Shell** dans le navigateur | `incus exec <nom> -- bash -l` | `lxc-attach -n <nom> -- bash -l` |
| **Console** (tty / port série) | `incus console <nom>` | `lxc-console -n <nom> -t 0` |
| Supprimer, après confirmation | `incus delete --force` | `lxc-destroy -f` |

La console utilise un vrai pseudo-terminal, via un canal Cockpit et xterm.js.
Elle gère les couleurs, le redimensionnement et les programmes plein écran
comme `vim`, `htop` ou `mc`. Pour quitter une **Console** : `Ctrl+a` puis `q`.
Pour quitter un **Shell** : `exit` ou le bouton « Fermer ».

## Contenu

```
deploy/incus-cockpit/
├── deploy.sh              # lancé depuis votre poste (SSH)
├── install.sh             # lancé en root sur le serveur
└── cockpit-incus/         # installé dans /usr/share/cockpit/incus
    ├── manifest.json
    ├── index.html
    ├── incus.js
    ├── incus.css
    └── vendor/            # xterm.js 5.5.0 + addon-fit 0.10.0 (licence MIT)
```

Le serveur n'a pas besoin d'accéder à un CDN : xterm.js est fourni avec le kit.

## Dépannage

- Le menu n'apparaît pas : rechargez Cockpit (`Ctrl+Shift+R`) et vérifiez
  la présence de `/usr/share/cockpit/incus/manifest.json`.
- « commande introuvable » : `incus` ou `lxc-ls` n'est pas installé.
  Relancez `install.sh`.
- Pas d'IP pour les conteneurs LXC : `systemctl status lxc-net`, puis vérifiez le pont `lxcbr0`.
- Incus en tant qu'utilisateur non root : déconnectez-vous puis reconnectez-vous
  pour que l'ajout au groupe `incus-admin` prenne effet.
