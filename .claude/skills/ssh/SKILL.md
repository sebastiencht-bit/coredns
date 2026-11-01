---
name: ssh
description: Se connecter en SSH au serveur Incus/LXC (par défaut incus@192.168.1.17) pour y exécuter des commandes, diagnostiquer, déployer le kit deploy/incus-cockpit (Incus + LXC + Cockpit avec console) ou gérer des conteneurs et VM. À utiliser dès que l'utilisateur parle de « ssh », du serveur incus, de 192.168.1.17, de Cockpit, de conteneurs LXC/Incus distants, ou demande d'agir « sur le serveur ».
---

# SSH vers le serveur Incus

Toutes les commandes passent par `scripts/remote.sh`. Il ouvre une connexion
SSH partagée (une seule authentification), non interactive (`BatchMode`), avec
un délai de connexion de 10 s.

```bash
R=.claude/skills/ssh/scripts/remote.sh
$R check                      # joignabilité, authentification, sudo, versions
$R run 'incus list'           # commande en tant qu'utilisateur SSH
$R sudo 'lxc-ls --fancy'      # commande en root (sudo -n, sans mot de passe)
$R copy ./fichier /tmp/       # copie locale → serveur
$R fetch /var/log/syslog ./   # copie serveur → local
$R deploy                     # installe/met à jour le kit deploy/incus-cockpit
$R close                      # ferme la connexion partagée
```

Cible : `-t user@hôte` ou variable `INCUS_SSH_TARGET` (défaut `incus@192.168.1.17`).
Options SSH en plus (clé, port…) : `INCUS_SSH_OPTS="-i ~/.ssh/cle -p 2222"`.
Les alias de `~/.ssh/config` fonctionnent (`-t incus`).

## Méthode

1. **Toujours commencer par `check`.** Il dit précisément ce qui bloque.
   - `UNREACHABLE` : le port 22 n'est pas joignable d'ici. Dans une session
     cloud, 192.168.x.x est un réseau privé, donc injoignable. Proposez à
     l'utilisateur de relier son ordinateur à la session (Remote Control,
     outil `list_computer_folders`), puis relancez depuis cet ordinateur.
     Sinon, donnez-lui les commandes à exécuter lui-même. N'inventez jamais
     de résultat.
   - `AUTH_FAILED` : aucune clé acceptée. Demandez quelle clé ou quel
     utilisateur utiliser (`INCUS_SSH_OPTS="-i …"`, `-t user@hôte`). Ne
     demandez jamais de mot de passe dans le chat, et ne l'écrivez dans
     aucune commande ni aucun fichier.
   - `SUDO=password` : `sudo` exige un mot de passe. Les sous-commandes `sudo`
     et `deploy` échoueront. Proposez à l'utilisateur de lancer lui-même
     `deploy/incus-cockpit/deploy.sh` (interactif), ou d'ajouter l'utilisateur
     au groupe `incus-admin` pour les commandes `incus`.
2. **Lire avant d'agir.** Regardez l'état actuel (`incus list`, `lxc-ls --fancy`,
   `systemctl status …`) avant toute modification, et rapportez la sortie réelle.
3. **Confirmer les actions destructrices ou visibles de l'extérieur.** Avant
   l'exécution, demandez l'accord explicite de l'utilisateur pour :
   - supprimer une instance (`incus delete`, `lxc-destroy`) ;
   - supprimer un pool ou un réseau ;
   - `apt remove`, `rm -rf` ;
   - un redémarrage du serveur ;
   - une modification du pare-feu, de SSH ou du réseau de l'hôte.

   Une modification de `sshd` ou du réseau peut couper l'accès au serveur.
   Proposez toujours un plan de retour arrière.
4. **Quoter correctement.** Passez la commande distante en **une seule chaîne**
   entre apostrophes : `$R run 'incus exec web1 -- sh -c "df -h /"'`.

## Mémo Incus / LXC

| Besoin | Incus | LXC |
|---|---|---|
| Lister | `incus list` | `sudo lxc-ls --fancy` |
| Créer | `incus launch images:debian/12 web1` (`--vm` pour une VM) | `sudo lxc-create -n ct1 -t download -- -d debian -r bookworm -a amd64` |
| Démarrer / arrêter | `incus start web1` / `incus stop web1` | `sudo lxc-start -n ct1` / `sudo lxc-stop -n ct1` |
| Commande dans l'instance | `incus exec web1 -- cmd` | `sudo lxc-attach -n ct1 -- cmd` |
| Infos / journaux | `incus info web1 --show-log` | `sudo lxc-info -n ct1` |
| Images disponibles | `incus image list images: debian` | — |
| Services hôte | `systemctl status incus cockpit.socket lxc-net` | |

Les consoles interactives (`incus console`, `lxc-console`, shells) ne
fonctionnent pas via ce script, qui n'a pas de terminal. Utilisez
`incus exec … -- cmd` pour des commandes ponctuelles, et orientez
l'utilisateur vers le module Cockpit « Incus & LXC » (port 9090) pour une
console interactive.

## Déploiement du kit Cockpit

`$R deploy` copie `deploy/incus-cockpit/` sur le serveur, puis lance
`install.sh` en root. L'opération est idempotente : on peut la relancer
après une modification du module. Elle exige root ou `sudo` sans mot de passe
(voir `check`). Ensuite, vérifiez :

```bash
$R run 'systemctl is-active cockpit.socket; ls /usr/share/cockpit/incus; incus list'
```
