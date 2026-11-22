# incus-mcp

Serveur MCP (transport Streamable HTTP) qui permet à des agents IA, par exemple ceux de
[Timon](https://github.com/ai-timon/timon), de piloter **un seul projet Incus**. Le projet
s'appelle `timon` par défaut. Il ne voit et ne peut toucher **rien d'autre** sur l'hôte.

```
agents (Timon, Claude Code…) ──HTTPS + Bearer──► incus-mcp (conteneur « cockpit », 192.168.1.50:8444)
                                                   │ certificat client RESTREINT au projet « timon »
                                                   │ certificat serveur Incus épinglé
                                                   ▼
                                  API Incus de l'hôte, 127.0.0.1:8443 (périphérique proxy « incus-api »)
```

## Sécurité, par couches

| Couche | Effet |
|---|---|
| Certificat client `--restricted --projects timon` | **Incus lui-même** refuse tout le reste : projet `default`, instances de production, configuration de l'hôte. |
| Projet `restricted=true` + quotas | Pas de conteneurs privilégiés, pas de clés `raw.*`, pas de dossiers de l'hôte. 10 instances, 16 vCPU et 32 Gio au maximum par défaut. |
| `incus-mcp check` | Exécuté avant chaque démarrage du service : refuse de démarrer si le projet `default` est lisible. |
| Projet forcé dans le code | Chaque requête porte `project=timon`, quel que soit l'appel. |
| Filtre réseau | Seules les adresses de `INCUS_MCP_ALLOWED_NETWORKS` sont acceptées. Les autres reçoivent une erreur 403. |
| Jeton Bearer | 48 caractères aléatoires, comparés en temps constant. Erreur 401 sinon. |
| HTTPS | Certificat signé par une autorité locale, `ca.crt`, à installer chez les clients. |
| API Incus sur la boucle locale | `core.https_address=127.0.0.1:8443`. Le conteneur y accède par un périphérique proxy, jamais par le LAN. |
| Journal d'audit | `/var/log/incus-mcp/audit.log`, une ligne JSON par appel : client, outil, arguments, résultat. `env` et `cloud_init_user_data` sont masqués. |
| systemd durci | Utilisateur dédié, `ProtectSystem=strict`, aucune capacité, filtre d'appels système. |

## Outils

| Outil | Type | Rôle |
|---|---|---|
| `instance_list`, `instance_get`, `instance_logs`, `image_list`, `snapshot_list`, `operation_get` | lecture | état, IP, configuration, journal console |
| `instance_create`, `instance_start`, `instance_stop`, `instance_restart`, `instance_set_config`, `snapshot_create` | écriture | création (conteneur ou VM, `images:debian/13`…), cycle de vie ; seules les clés `user.*` et `limits.*` sont modifiables |
| `instance_delete`, `snapshot_restore`, `instance_exec` | destructeur ou arbitraire | suppression, retour arrière, commande root dans l'instance |

Les annotations MCP `readOnlyHint` et `destructiveHint` sont renseignées. Dans Timon, ne mettez que
les outils **de lecture** dans « MCP tools allowed without asking ». Les autres passent par une
carte de permission.

## Installation

Sur l'hôte Incus, depuis ce dossier :

```bash
sudo ./host-setup.sh          # affiche le plan, ne change rien
sudo ./host-setup.sh --yes    # applique
```

Variables : `CONTAINER` (`cockpit`), `PROJECT` (`timon`), `LIMIT_INSTANCES`, `LIMIT_CPU`,
`LIMIT_MEMORY`, `HTTPS_ADDR` (`127.0.0.1:8443`), `STORAGE_POOL`, `NETWORK`.

Le script :
1. crée le projet ;
2. fait écouter l'API Incus sur la boucle locale, **seulement si aucune adresse n'est déjà configurée** ;
3. ajoute le périphérique proxy ;
4. installe le service dans le conteneur ;
5. ajoute le certificat client avec la restriction au projet ;
6. épingle le certificat serveur, puis lance `incus-mcp check`.

`install.sh` sert seul pour mettre à jour le service dans le conteneur, ce qui est idempotent :

```bash
incus exec cockpit -- bash /root/incus-mcp-src/install.sh
```

Si un pare-feu nftables filtre le conteneur, autorisez `tcp dport 8444` depuis `192.168.1.0/24`.

## Brancher Timon

1. Rendez l'autorité locale de confiance dans le conteneur Timon :
   - copiez `/etc/incus-mcp/ca.crt` dans `/usr/local/share/ca-certificates/incus-mcp.crt`, puis lancez `update-ca-certificates` ;
   - pour Node, ajoutez `NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt` à l'environnement du service Timon.
2. Dans Timon, **Capabilities → MCP → New MCP** :
   - URL : `https://192.168.1.50:8444/mcp` ;
   - en-tête : `Authorization: Bearer <jeton>`.

   Lisez le jeton vous-même avec `incus exec cockpit -- cat /etc/incus-mcp/token`. Timon le garde dans son coffre : l'agent ne le voit jamais.
3. **Settings → Missions → Permissions → MCP tools allowed without asking** : seulement
   `mcp__incus__instance_list, mcp__incus__instance_get, mcp__incus__instance_logs, mcp__incus__image_list, mcp__incus__snapshot_list, mcp__incus__operation_get`.
4. Créez un agent « Infra » avec le serveur `incus` et le skill [`skill-infra.md`](skill-infra.md).

Test rapide depuis le LAN :
```bash
curl --cacert ca.crt https://192.168.1.50:8444/healthz
```

## Développement

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

Les tests couvrent :
- les outils, contre un faux serveur Incus en mémoire ;
- le serveur HTTP réel avec le client MCP officiel : jeton, filtre réseau, audit ;
- le TLS réel : épinglage du certificat serveur, refus d'un certificat inconnu, et `incus-mcp check`
  qui échoue si le certificat n'est pas restreint.

Dépendance : SDK MCP Python **2.x** (`MCPServer`).

## Dépannage

- **Le service ne démarre pas, `check` affiche `FAIL … not reachable`.** Vérifiez trois choses :
  - le périphérique proxy : `incus config device show cockpit` ;
  - l'adresse de l'API : `incus config get core.https_address` ;
  - le certificat de confiance : `incus config trust list`.
- **`check` affiche `is READABLE`.** Le certificat n'est pas restreint. Retirez-le
  (`incus config trust remove <empreinte>`) et relancez `host-setup.sh --yes`.
- **Erreur `226/NAMESPACE` au démarrage dans un conteneur non privilégié.** Commentez une à une les options
  `Protect*` ou `PrivateDevices` de l'unité. Mettez-les dans un `systemctl edit incus-mcp`, pas dans le fichier livré.
- **Les clients reçoivent une erreur 403.** Leur adresse n'est pas dans `INCUS_MCP_ALLOWED_NETWORKS`.
