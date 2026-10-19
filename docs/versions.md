# Versions de Clipper

Numérotation : versionnage sémantique, `MAJEUR.MINEUR.CORRECTIF`.

- `0.x.y` : en développement ; la config, les écrans et les fichiers d'état peuvent encore changer.
- `1.0.0` : première version stable ; une mise à jour ne casse plus la config ni les données sans migration automatique.
- Après 1.0.0 : `1.1.0` nouveauté compatible, `1.0.1` correction, `2.0.0` changement qui casse.

## Feuille de route

| Version | Contenu | Condition |
|---|---|---|
| v0.1.0 | Pipeline vidéo longue -> clips 9:16 (letterbox, stream), jury, sous-titres, première console | publiée |
| v0.2.0 | Console web v2 en neuf écrans (tableau de bord, vidéos, revue, clips, publication, chaînes, statistiques, comptes, réglages), chaînes par preset avec surveillance des VOD, agencement stream `split`, comptes dans le coffre de l'OS, publication TikTok par navigateur (immédiate ou programmée, testée en réel), statistiques TikTok par compte, jury à confiance pondérée, grille gaming, worker et file ([notes](releases/v0.2.0.md)) | publiée |
| v0.3.0 | Prête pour l'usage réel : l'étape `parts` découpe avec la grille de `moments` (clé `[parts] rubric_path` retirée), statistiques TikTok relevées seulement à l'usage (`stats_interval_h = 0`), liste Publications lue en entier, console filtrée par compte TikTok (« Chaîne » devient « Style »), radar du jury et calendrier plus lisibles, lanceur `Clipper.bat`, README refait ([notes](releases/v0.3.0.md)) | publiée |
| v0.4.0 | YouTube Shorts et publication avancée : comptes et publication YouTube par navigateur, styles sans compte ni créneaux (créneaux sur le compte), séries programmées, approbation groupée, éditeur d'agencement letterbox, journal global des actions ([détails](CHANGELOG.md)) | publiée |
| v0.4.1 | Installeur portable Windows et calendrier Jour/Semaine/Mois ([détails](CHANGELOG.md)) | publiée |
| v0.4.2 | Webcam du stream trouvée par période avec Claude, garde-fou du pays de l'IP, publication TikTok plus robuste, purge disque et reprise des vidéos interrompues ([détails](CHANGELOG.md)) | publiée |
| v0.5.0 | Veille des sujets chauds (Twitch, YouTube, Steam, choix de Claude), clips courts, heure libre par clip dans les séries programmées, légendes avec mots-clés de recherche, recalage de la webcam, statistiques TikTok relues sur la vraie page ([détails](CHANGELOG.md)) | publiée |
| v0.5.1 | Veille : miniatures des VOD, VOD privées écartées, libellé Steam corrigé ([détails](CHANGELOG.md)) | publiée |
| v0.5.2 | Veille : VOD Twitch réservées aux abonnés écartées par un test d'accès yt-dlp ; TikTok : vérification de contenu relancée si bloquée ou en erreur ([détails](CHANGELOG.md)) | publiée |
| v0.5.3 | Veille : section Sorties de jeux (IGDB, hypes, étiquette Portage), jeu des VOD YouTube déduit du titre ; Download : VOD abonnés = échec définitif, réessais rapides sur coupure réseau Twitch ([détails](CHANGELOG.md)) | publiée |
| v0.6.0 | Veille (historique de tendance, Steam officiel, calendrier des sorties), formats stream et webcam, styles gaming (candidats d'action), fiche clip, alerte 0 vue, préchargement du téléchargement, publication TikTok plus robuste ([détails](CHANGELOG.md)) | publiée |
| v0.7.0 | Console v3 (tous les écrans refaits), répartition automatique du lendemain, apprentissage branché sur les relevés réels, modération TikTok des légendes, amorce de gameplay, veille avec style par défaut, corrections de l'audit complet ([notes](releases/v0.7.0.md)) | publiée |
| v0.8.0 | Titre d'écran en trois styles (citation, promesse, promesse + emoji) et test A/B par compte TikTok, légendes au bon jeu et dans la langue de la vidéo, split (jeu centré, bande « Partie N » protégée), contrôle webcam de la veille, pays d'IP par compte, publication TikTok et YouTube plus robuste ([notes](releases/v0.8.0.md)) | publiée |
| v0.9.0 | Mode auto de bout en bout : vidéo en entrée, clips publiés sans intervention | après une semaine d'usage réel |
| v1.0.0 | Stable | quand tous les critères ci-dessous sont tenus |

## Critères de la v1.0.0

1. **Installation propre** : sur un PC neuf, le zip portable de la Release mène à un premier clip sans aide.
2. **Une semaine d'usage réel sans retouche du code** : vidéos traitées et clips publiés ; aucune perte silencieuse ni blocage sans raison affichée.
3. **Formats figés et documentés** : `config.toml`, presets de chaîne, fichiers d'état (`state/`, `workspace/<id>/pipeline.json`) et sidecars de clip ; tout changement ultérieur passe par une migration automatique.
4. **Tests** : suite complète verte, test de fumée réel (`CLIPPER_CLAUDE_INTEGRATION=1`) et test réel TikTok (`CLIPPER_TIKTOK_REAL=1`) verts sur la version publiée.
5. **Documentation** : README à jour (installation, chaînes, comptes, publication, risques TikTok) et journal des changements par version.
