---
name: infra-incus
description: Règles pour gérer les conteneurs et VM Incus du projet « timon » avec les outils MCP du serveur « incus ». À utiliser dès qu'une tâche demande de créer, démarrer, arrêter, inspecter, sauvegarder ou supprimer une instance, ou d'exécuter une commande dans une instance.
---

# Gérer les instances Incus (projet « timon »)

Tu disposes des outils `mcp__incus__*`. Ils n'agissent que sur le projet Incus `timon`. Les
autres instances de l'hôte, notamment celles de production, te sont invisibles. Ne cherche pas
de moyen de les atteindre (SSH, socket, autre API). Si une tâche l'exige, dis-le et arrête-toi.

## Méthode
1. **Lire avant d'agir.** Commence par `instance_list`, puis `instance_get` sur l'instance
   concernée. Ne suppose jamais un état, une IP ou une configuration.
2. **Choisir l'action la moins destructrice.** Arrêter plutôt que supprimer, redémarrer plutôt
   que recréer. Pour une instance existante, prends un `snapshot_create` avant toute modification
   risquée : mise à jour, changement de configuration, commande qui écrit.
3. **Avant toute action destructrice, demande confirmation** avec l'outil de question de Timon.
   C'est le cas de `instance_delete`, de `snapshot_restore`, et de `instance_exec` quand il modifie
   le système. Indique l'instance, l'effet attendu et le retour arrière possible. Une carte de
   permission n'est pas une explication.
4. **Rapporte les erreurs telles quelles.** Cite le message Incus, n'invente pas de résultat.
   Après une action, vérifie l'état avec `instance_get`.

## Conventions
- **Noms** : lettres, chiffres et tirets, préfixés par le projet ou la tâche (`demo-web`, `ci-runner-1`).
- **Images** : `images:debian/13`, `images:ubuntu/24.04`. Ajoute `/cloud` si tu passes `cloud_init_user_data`.
- **Ressources** : précise toujours `cpu` et `memory` (par exemple `cpu=2`, `memory="2GiB"`).
  Le projet a des quotas ; un refus « limit » veut dire qu'ils sont atteints, pas qu'il faut contourner.
- **Traçabilité** : renseigne `user.owner` (ton nom d'agent) et `user.purpose` avec `instance_set_config`
  après la création.
- **`instance_exec`** : passe une liste d'arguments, par exemple `["sh", "-c", "apt-get update"]`. Pas de
  terminal, pas de saisie. Délai maximal de 120 s : découpe les longues opérations. Ne mets jamais de
  secret dans `command` ; utilise `env` si c'est inévitable.
- **Nettoyage** : supprime les instances de test que tu as créées, après confirmation, quand la tâche est finie.
