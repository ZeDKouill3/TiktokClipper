---
id: TASK-fc2f36ec0889
type: task
slug: veille-une-erreur-de-connexion-passag-re-ne-fait
title: "Veille : une erreur de connexion passagère ne fait plus tomber toute la source"
created: 2026-11-19T08:08:19Z
author: nicoc@zedk_ordi
status: done
scope:
  - clipper/veille_sources.py
  - clipper/veille.py
  - tests/test_veille_sources.py
  - tests/test_veille.py
  - CHANGELOG.md
  - clipper/web/static/screens/settings.js
  - tests/test_web_settings_v3.py
blocked_by: []
done_criteria: |
  1) Le transport par défaut de la veille (veille_sources.default_http) réessaie une requête qui échoue par erreur de connexion (httpx.ConnectError, ConnectTimeout, ReadTimeout, RemoteProtocolError) : nombre d'essais et pause bornés, portés par des réglages de [veille] dans CONFIG_DEFAULTS (défaut 3 essais, 2 s de pause), validés ; 1 essai = comportement actuel. 2) Un essai rattrapé est journalisé une fois en WARNING (hôte, numéro d'essai), jamais en silence ; après le dernier essai l'erreur remonte en SourceError comme aujourd'hui. 3) Les réponses HTTP (4xx, 5xx, 429) ne sont pas concernées : leur traitement actuel est inchangé. 4) Tests unitaires sans réseau (httpx simulé ou transport injecté, attente injectée : aucun vrai sleep), suite complète verte, ligne dans CHANGELOG.md.
criteria_by: creator
verify: [tests]
method: tdd
proof:
  - type: test
    ref: local/72881ca39bcc@d78803d
    tree: scope/7e5f51f6ef58
    criteria: 5b44c14d3ffb
    verifier: tests@c7b454d16c90
    via: verifier
schema: 4
version: 3
---

Constat du 19/11/2026 : le jour de la sortie de GTA VI, la veille a rendu 0 candidat deux fois de suite (07:40 et 08:00 UTC) : source twitch en erreur, « requête impossible sur https://api.twitch.tv/helix/streams (ConnectError) », alors que les autres sources passaient. Rejoué à la main, le collecteur Twitch fait une trentaine de requêtes, chacune sur une connexion neuve (httpx.request), et l'une d'elles échoue de temps en temps (helix/streams, puis helix/videos au deuxième essai) : une seule erreur de connexion fait perdre toute la source, donc toutes les VOD du jour. Avec 5 essais espacés de 1,5 s dans un transport de test, le relevé complet passe (95 jeux, 187 VOD). Regarde aussi si un httpx.Client partagé (connexions gardées ouvertes) est simple à mettre sans changer l'interface du transport injectable ; ce n'est pas exigé.
