---
id: ADR-a308c4cafb5b
type: adr
slug: console-v3-composants-basecoat-vendored-mise-jou
title: "Console v3 : composants Basecoat vendored, mise à jour par rapprochement du DOM, direction « régie sobre », thème clair par défaut"
created: 2026-10-11T03:57:49Z
author: nicoc@zedk_ordi
status: accepted
scope:
  - clipper/web/static/**
  - tools/vendor_basecoat.py
  - tests/test_web*.py
constraint: |
  Le front de la console reste du HTML/CSS/JS statique sans compilation (ADR-49cd), bâti sur Basecoat 1.0.2 (MIT) et Idiomorph 0.8.0 (0BSD) copiés dans `clipper/web/static/vendor/` par `tools/vendor_basecoat.py` (version et sha256 épinglés, sélecteurs préfixés `.v3` pendant la migration), sans classe utilitaire Tailwind ni CSS pré-généré, avec `icons.js` (Lucide) comme seule famille d'icônes, les polices locales existantes, le thème clair par défaut, et aucune ressource externe.
ratified: 4dfb78733718
verified:
  - by: nicoc@zedk_ordi
    at: 2026-10-11T03:58:03Z
schema: 4
version: 2
---

### Contexte
La console (ADR-49cd, SPEC-c100) totalise 10 400 lignes de JS/CSS écrites écran par écran,
sans bibliothèque : chaque écran a ses boutons, ses onglets, ses modales, ses listes, et son
propre contournement pour ne pas réécrire tout son DOM à chaque événement SSE (Vidéos,
Revue, Publication). L'état des lieux du 11/10/2026 (`research/drafts/ui-v3/ETAT-DES-LIEUX.md`)
relève : tableau de bord de 5 600 px où les décisions sont noyées dans des statistiques,
journal de 5 711 px de large, 55 vidéos en cartes sans table ni tri, fiche clip sans action,
calendrier illisible sans compte choisi, aucune palette ni raccourci global, thème sombre par
défaut alors que le README est en clair. L'utilisateur demande une refonte complète,
« ergonomie, fluidité », avec une vraie bibliothèque de composants, sans build (SPEC-54ed :
l'installeur portable n'a pas Node).

Trois bibliothèques comparées (`CHOIX.md`) : Tabler 1.6.1 (MIT, 770 Ko, Bootstrap imposé),
Web Awesome 3.14.0 (MIT, web components Lit, shadow DOM incompatible avec des gabarits
`innerHTML` et des tests node, 9 Mo à embarquer), **Basecoat 1.0.2** (MIT, 218 Ko CSS +
44 Ko JS, markup sémantique shadcn, `<dialog>` natif, palette, select, menus, toasts,
tiroir, thème par variables, clair par défaut).

### Décision
1. **Basecoat vendored.** `tools/vendor_basecoat.py` télécharge `basecoat-css@1.0.2`
   (`dist/basecoat.cdn.min.css`, `dist/js/all.min.js`) et `idiomorph@0.8.0`
   (`dist/idiomorph.min.js`), vérifie les sha256 écrits dans `static/vendor/VERSIONS`, copie
   les licences, et réécrit le CSS pour préfixer chaque sélecteur par `.v3` (hors `:root`,
   `.dark`, `html`, `@keyframes`, `@property`, `@font-face`). Le résultat est commité ; le
   script ne tourne jamais à l'installation ni au lancement. Mettre à jour la version =
   relancer le script et commiter.
2. **Aucune classe utilitaire.** Les écrans n'utilisent que les classes de composants
   Basecoat (`btn`, `card`, `table`, `badge`, `tabs`, `dialog`, `dropdown-menu`, `select`,
   `command`, `toaster`, `empty`, `field`, `kbd`, `skeleton`…) et les classes propres
   `clipper` définies dans `static/v3.css` (jetons, coquille, calendrier, stepper, posters).
   Pas de Tailwind, pas de CSS pré-généré : il faudrait le régénérer à chaque écran.
3. **Mise à jour par rapprochement.** `renderCurrent()` et les écrans passent par
   `morph(body, html)` (Idiomorph, `morphStyle: "innerHTML"`) au lieu de `innerHTML =` :
   focus, défilement, lecteur vidéo et hauteurs survivent à un événement SSE. Les
   contournements par écran (DOM gardé à la main) sont retirés au fil de la migration.
4. **Direction** (`DIRECTION.md`) : neutres chauds, un accent (orange de marque, `#c25f00`
   clair / `#ff8a00` sombre), hairlines 1 px, Barlow / JetBrains Mono / Barlow Condensed
   (grands nombres), échelle 4-8-12-16-24-32-48-64, mouvement 140-220 ms ease-out, aucune
   animation sur action clavier, `prefers-reduced-motion` respecté.
5. **Thème clair par défaut** (localStorage `clipper-theme`, classe `.dark` sur `<html>`,
   `data-theme` conservé le temps de la migration). Sombre en option.
6. **Transition par écran.** Chaque `<section class="screen">` migrée reçoit `v3` ; la
   coquille (barre latérale, barre du haut, onglets mobiles, toasts, dialogues, tiroir) est
   migrée en premier et porte `v3`. Un écran non migré garde `style.css` à l'identique. Un
   test de garde refuse tout sélecteur non préfixé dans le CSS vendored. Dernière tâche :
   supprimer `style.css`, le préfixe et le script de préfixage.
7. **Icônes et polices** : `icons.js` (Lucide, ISC) reste la seule famille ; polices locales
   inchangées (OFL). Aucune ressource externe (test existant).

### Conséquences
- Poids servi : ≈ 270 Ko vendored + `v3.css` (objectif ≤ 40 Ko) + JS d'écrans ; `style.css`
  (124 Ko) disparaît à la fin.
- Les tests node qui extraient des fonctions par nom (`_js_def`) restent valables : chaque
  tâche d'écran adapte ses tests dans le même commit, jamais en les supprimant sans
  remplacement.
- ADR-49cd inchangé (page statique, aucune logique de traitement). SPEC-c100 remplacée par la
  SPEC ci-dessous. ADR-35b7 (SSE, worker, jeton) inchangé.
- Dette acceptée : un seul mainteneur pour Basecoat ; en cas d'abandon, le CSS vendored reste
  utilisable tel quel (pas de dépendance d'exécution).

### Références
ADR-49cd, ADR-35b7, ADR-ad2e (aucun repli silencieux : s'applique aux erreurs d'interface),
SPEC-54ed (pas de Node), SPEC-c100 (remplacée). Comparatif : `research/drafts/ui-v3/CHOIX.md`.
