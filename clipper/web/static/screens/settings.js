/* Écran « Réglages » (SPEC-c100 E8, ADR-35b7 §2 et §5). config.toml en
   formulaire : Général (mode), Dossiers, LLM (backend, modèles par niveau et
   par usage), Serveur web, Worker ; section Accès en lecture seule. Le serveur
   fournit le brut du fichier, les valeurs effectives et les CONFIG_DEFAULTS
   commentés ; aucune validation ici : un refus du serveur (422) revient au
   champ visé. Un champ égal à son défaut n'est pas écrit dans le fichier.
   Charge après screens.js dont il remplace l'entrée Screens.settings. */
"use strict";

const SET_SECTIONS = [
  ["general", "Général"], ["dirs", "Dossiers"], ["llm", "LLM et modèles"],
  ["web", "Serveur web"], ["worker", "Worker"], ["network", "Réseau"], ["veille", "Veille"], ["access", "Accès"],
];
const SET_TIERS = ["strong", "fast"];
// Clés d'API de la veille : jamais renvoyées par le serveur (seulement `<clé>_set`), jamais préremplies ; vide = on garde.
const SET_VEILLE_KEYS = ["twitch_client_id", "twitch_client_secret", "youtube_api_key"];
const SET_STALE_MS = 4000;

/* Libellé français de chaque champ (chemin pointé, comme dans le brouillon). La clé technique reste
   affichée en mono discret. tests/test_web_settings_v3.py compare cette table aux clés servies par le serveur. */
const SET_LABELS = {
  "mode": "Mode de traitement",
  "workspace_dir": "Dossier de travail",
  "output_dir": "Dossier des clips",
  "llm.backend": "Backend par défaut",
  "llm.repair_attempts": "Renvois en cas de réponse invalide",
  "llm.claude_cli.command": "Commande de l'interface Claude",
  "llm.claude_cli.timeout": "Délai maximum d'un appel (s)",
  "llm.claude_cli.models.strong": "Modèle du niveau strong",
  "llm.claude_cli.models.fast": "Modèle du niveau fast",
  "llm.claude_api.max_tokens": "Taille maximum de réponse (jetons)",
  "llm.claude_api.timeout": "Délai maximum d'un appel (s)",
  "llm.claude_api.models.strong": "Modèle du niveau strong",
  "llm.claude_api.models.fast": "Modèle du niveau fast",
  "llm.ollama.url": "Adresse du serveur Ollama",
  "llm.ollama.timeout": "Délai maximum d'un appel (s)",
  "llm.ollama.models.strong": "Modèle du niveau strong",
  "llm.ollama.models.fast": "Modèle du niveau fast",
  "web.host": "Adresse d'écoute",
  "web.port": "Port",
  "web.sse_poll_interval_s": "Rafraîchissement en direct de secours (s)",
  "worker.poll_interval_s": "Intervalle de lecture de la file (s)",
  "worker.cancel_grace_s": "Délai avant arrêt forcé (s)",
  "worker.queue_path": "Fichier de la file",
  "worker.heartbeat_interval_s": "Intervalle de signe de vie (s)",
  "worker.prefetch_download": "Télécharger la vidéo suivante en avance",
  "worker.prefetch_min_free_gb": "Espace libre minimum pour l'avance (Go)",
  "network.expected_country": "Pays attendu de l'IP",
  "network.geo_url": "Service de géolocalisation de l'IP",
  "network.cache_s": "Durée de cache de l'IP (s)",
  "network.block_browser": "Bloquer le navigateur hors du pays",
  "veille.enabled": "Veille activée",
  "veille.run_at": "Heure du relevé quotidien",
  "veille.timezone": "Fuseau horaire",
  "veille.language": "Langue des VOD",
  "veille.region": "Région des VOD",
  "veille.taste": "Goûts",
  "veille.default_channel": "Style par défaut des VOD",
  "veille.max_vods_per_day": "VOD proposées par jour",
  "veille.best_clips_per_day": "Meilleurs clips archivés par jour",
  "veille.baseline_days": "Jours de référence pour la montée",
  "veille.history_days": "Jours d'historique conservés",
  "veille.rise_min_pct": "Montée minimum pour être retenu (%)",
  "veille.vod_min_duration_s": "Durée minimum d'une VOD (s)",
  "veille.vod_max_age_h": "Âge maximum d'une VOD (h)",
  "veille.twitch_top_games": "Jeux Twitch les plus suivis relevés",
  "veille.twitch_vods_per_game": "VOD Twitch relevées par jeu",
  "veille.youtube_max_results": "Résultats YouTube maximum",
  "veille.youtube_min_duration_s": "Durée minimum d'une vidéo YouTube (s)",
  "veille.steam_top": "Jeux Steam les plus joués relevés",
  "veille.steam_name_lookups_max": "Recherches de noms Steam maximum",
  "veille.twitch_access_attempts": "Tentatives d'accès Twitch par jeu",
  "veille.twitch_access_retry_pause_s": "Pause entre deux tentatives Twitch (s)",
  "veille.twitch_access_workers": "Jeux testés en parallèle (Twitch)",
  "veille.steam_sellers_top": "Jeux Steam les plus vendus relevés",
  "veille.state_dir": "Dossier d'état de la veille",
  "veille.http_timeout_s": "Délai maximum d'une requête HTTP (s)",
  "veille.steam_rank_gain_min": "Gain de rang Steam minimum",
  "veille.steam_risers_max": "Jeux Steam en hausse retenus au maximum",
  "veille.upcoming_days": "Sorties à venir affichées (jours)",
  "veille.release_window_days": "Fenêtre des sorties (jours)",
  "veille.igdb_min_hypes": "Hype IGDB minimum",
  "veille.igdb_recent_max": "Sorties récentes IGDB maximum",
  "veille.igdb_upcoming_max": "Sorties à venir IGDB maximum",
  "veille.igdb_pages_max": "Pages IGDB maximum",
  "veille.youtube_game_min_chars": "Longueur minimum d'un nom de jeu pour YouTube",
  "veille.steam_players_lookups_max": "Jeux Steam interrogés pour les joueurs",
  "veille.steam_followers_lookups_max": "Jeux Steam interrogés pour les abonnés",
  "veille.steam_followers_pause_s": "Pause entre deux relevés d'abonnés Steam (s)",
  "veille.steam_followers_retry_max": "Nouvelles tentatives d'abonnés Steam",
  "veille.steam_followers_retry_wait_max_s": "Attente maximum avant nouvel essai d'abonnés (s)",
  "veille.community_min_steam_players": "Seuil de joueurs Steam simultanés",
  "veille.community_min_steam_followers": "Seuil d'abonnés Steam",
  "veille.community_min_twitch_viewers": "Seuil de spectateurs Twitch",
  "veille.community_min_hypes": "Seuil de hype IGDB",
  "veille.max_vods_per_game": "VOD maximum par jeu",
  "veille.trend_days": "Jours de tendance affichés",
  "veille.trend_games_max": "Jeux en tendance suivis au maximum",
  "veille.steam_reviews_pause_s": "Pause entre deux relevés d'avis Steam (s)",
  "veille.steam_reviews_retry_max": "Nouvelles tentatives d'avis Steam",
  "veille.steam_reviews_retry_wait_max_s": "Attente maximum avant nouvel essai d'avis (s)",
  "veille.twitch_history_pages_max": "Pages d'historique Twitch maximum",
  "veille.twitch_history_retry_max": "Nouvelles tentatives d'historique Twitch",
  "veille.twitch_history_retry_wait_max_s": "Attente maximum avant nouvel essai d'historique (s)",
  "veille.veille_deadline_s": "Échéance globale du relevé (s)",
  "veille.llm_retry_delay_min": "Délai entre deux appels à Claude (min)",
  "veille.llm_retry_max": "Nouvelles tentatives d'appel à Claude",
  "veille.twitch_client_id": "Identifiant client Twitch",
  "veille.twitch_client_secret": "Secret client Twitch",
  "veille.youtube_api_key": "Clé API YouTube",
};

const setUi = { data: null, draft: null, loading: null, at: 0, dirty: false, saving: false, error: "" };

const setClone = (v) => JSON.parse(JSON.stringify(v));
const setSame = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const setBackendKey = (name) => name.replace(/-/g, "_");

/* ---------- Données ---------- */

function setLoad(force) {
  if (setUi.loading) return setUi.loading;
  setUi.loading = (async () => {
    try {
      const data = await api("/api/settings");
      setUi.data = data;
      if (force || !setUi.dirty) { setUi.draft = setClone(data.raw); setUi.dirty = false; }
      setUi.error = "";
    } catch (err) {
      setUi.error = err.message;
      setUi.data = setUi.data || { failed: true };
      toastError("Réglages indisponibles", err);
    } finally {
      setUi.loading = null;
      setUi.at = Date.now();
    }
    if (currentScreen === "settings") renderCurrent();
  })();
  return setUi.loading;
}

/* Chemin « llm.claude_cli.models.strong » dans le brouillon (le brut du fichier). */
function setGet(obj, path) {
  return path.reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

/* Valeur affichée : le brouillon (brut du fichier + modifications) sinon le
   défaut du module, jamais l'état du serveur (un retour au défaut doit se voir). */
function setEffective(path) {
  const own = setGet(setUi.draft, path);
  return own !== undefined ? own : setDefault(path);
}

function setDefault(path) {
  const [head, ...rest] = path;
  if (head === "mode" || head === "workspace_dir" || head === "output_dir") return setUi.data.defaults.general[head].default;
  const doc = setUi.data.defaults[head] && setUi.data.defaults[head][rest[0]];
  return doc ? setGet(doc.default, rest.slice(1)) : undefined;
}

/* Écrit ou retire (valeur égale au défaut) ; les tables imbriquées devenues
   vides sont retirées (jamais la section du formulaire elle-même). */
function setPut(path, value) {
  const draft = setUi.draft;
  const same = setSame(value, setDefault(path));
  let node = draft;
  for (const key of path.slice(0, -1)) {
    if (!node[key] || typeof node[key] !== "object") { if (same) return; node[key] = {}; }
    node = node[key];
  }
  const last = path[path.length - 1];
  if (same) delete node[last]; else node[last] = value;
  for (let len = path.length - 1; len >= 2; len--) {
    const table = setGet(draft, path.slice(0, len));
    if (table && Object.keys(table).length) break;
    delete setGet(draft, path.slice(0, len - 1))[path[len - 1]];
  }
  setMarkDirty();
}

/* Usages : seules les entrées qui diffèrent du défaut sont écrites (clipper.llm
   fusionne le reste en profondeur). */
function setPutUsages(usages) {
  const defaults = setDefault(["llm", "usages"]) || {};
  const diff = {};
  Object.keys(usages).forEach((name) => { if (!(name in defaults) || !setSame(usages[name], defaults[name])) diff[name] = usages[name]; });
  setUi.draft.llm = setUi.draft.llm || {};
  if (Object.keys(diff).length) setUi.draft.llm.usages = diff; else delete setUi.draft.llm.usages;
  setMarkDirty();
}

function setMarkDirty() {
  setUi.dirty = !setSame(setUi.draft, setUi.data.raw);
  const bar = $("#set-savebar");
  if (bar) bar.classList.toggle("show", setUi.dirty);
}

/* ---------- Champs ---------- */

function setKind(def) {
  if (typeof def === "boolean") return "bool";
  if (typeof def === "number") return "number";
  return "text";
}

function setInput(id, path, kind, value, extra) {
  const data = `data-path="${esc(path.join("."))}" data-kind="${kind}"`;
  if (kind === "bool") return `<label class="switch"><input type="checkbox" id="${id}" ${data}${value ? " checked" : ""}><span></span></label>`;
  if (kind === "number") return `<input class="input mono" id="${id}" ${data} type="number" step="any" value="${esc(value)}">`;
  return `<input class="input${extra && extra.mono === false ? "" : " mono"}" id="${id}" ${data} type="text" value="${esc(value)}" autocomplete="off">`;
}

function setField(path, hint, kind) {
  const def = setDefault(path);
  const k = kind || setKind(def);
  const id = `set-${path.join("-")}`;
  return `<div class="field set-field" data-fpath="${esc(path.join("."))}">
    <label for="${id}">${esc(SET_LABELS[path.join(".")])}<code class="mono set-key">${esc(path[path.length - 1])}</code></label>
    ${setInput(id, path, k, setEffective(path))}
    ${hint ? `<span class="hint">${esc(hint)}</span>` : ""}${setHelpDetails(path, hint)}
    <span class="field-error" role="alert"></span></div>`;
}

/* Reste du commentaire du reglage (references techniques, cas particuliers) : replie. */
function setHelpDetails(path, hint) {
  const doc = ((setUi.data.defaults || {})[path[0]] || {})[path[path.length - 1]];
  return doc && doc.details && hint === doc.comment
    ? `<details class="set-help"><summary>détails</summary><p class="hint">${esc(doc.details)}</p></details>` : "";
}

function setSectionFields(section, skip) {
  const docs = setUi.data.defaults[section];
  return Object.keys(docs)
    .filter((k) => !(skip || []).includes(k) && (typeof docs[k].default !== "object" || docs[k].default === null))
    .map((k) => setField([section, k], docs[k].comment)).join("");
}

/* ---------- Sections ---------- */

function setGeneral() {
  const mode = setEffective(["mode"]);
  return `<section class="panel" id="set-general"><div class="panel-head"><h2>Général</h2><span class="muted mono">mode</span></div><div class="panel-pad">
    <div class="toggle-row"><div class="li-main"><div class="li-title">Mode</div>
      <div class="li-sub">review : tu valides les moments. auto : le jury décide. Relu par chaque nouvelle entrée de la file ; un style peut le redéfinir dans son preset.</div></div>
      <div class="seg" data-set-mode role="group" aria-label="Mode">${setUi.data.modes.map((m) => `<button type="button" data-v="${esc(m)}" class="${m === mode ? "on" : ""}">${esc(m)}</button>`).join("")}</div></div>
    <span class="field-error" data-fpath="mode" role="alert"></span>
  </div></section>`;
}

function setDirs() {
  return `<section class="panel" id="set-dirs"><div class="panel-head"><h2>Dossiers</h2></div><div class="panel-pad"><div class="form-grid">
    ${setField(["workspace_dir"], "Un sous-dossier par vidéo : audio, transcription, moments, état.")}
    ${setField(["output_dir"], "Clips rendus et leur fichier .json.")}
  </div></div></section>`;
}

function setLlmUsages() {
  const drafted = (setUi.draft.llm && setUi.draft.llm.usages) || {};
  const usages = { ...(setDefault(["llm", "usages"]) || {}), ...drafted };
  const backends = setUi.data.backends;
  const defaultUsages = setDefault(["llm", "usages"]) || {};
  const rows = Object.keys(usages).map((name) => {
    const u = usages[name];
    return `<tr data-usage="${esc(name)}"><td class="mono">${esc(name)}</td>
      <td><select class="input" data-usage-backend aria-label="Backend de ${esc(name)}"><option value="">backend global</option>${backends.map((b) => `<option value="${esc(b)}"${b === u.backend ? " selected" : ""}>${esc(b)}</option>`).join("")}</select></td>
      <td><input class="input mono" data-usage-model value="${esc(u.model || "")}" placeholder="strong, fast ou nom de modèle" aria-label="Modèle de ${esc(name)}"></td>
      <td class="r">${name in defaultUsages ? "" : `<button type="button" class="icon-btn" data-usage-del="${esc(name)}" aria-label="Retirer l'usage ${esc(name)}">${icon("x", "i-xs")}</button>`}</td></tr>`;
  }).join("");
  return `<h3 class="set-sub">Modèle par usage</h3>
    <p class="muted set-note">Un usage absent prend le backend global et le niveau fast. Chaque appel passe par clipper.llm.</p>
    <table class="table set-usages"><thead><tr><th>Usage</th><th>Backend</th><th>Modèle</th><th></th></tr></thead>
    <tbody>${rows || `<tr><td colspan="4" class="muted">Aucun usage redéfini.</td></tr>`}</tbody></table>
    <div class="set-add"><input class="input mono" data-usage-new placeholder="nom de l'usage, ex. moments" aria-label="Nouvel usage" autocomplete="off">
      <button type="button" class="btn btn-sm" data-usage-add>${icon("plus", "i-xs")}Ajouter un usage</button></div>
    <span class="field-error" data-fpath="llm.usages" role="alert"></span>`;
}

function setLlm() {
  const docs = setUi.data.defaults.llm;
  const backends = setUi.data.backends;
  const current = setEffective(["llm", "backend"]);
  const tables = backends.map((name) => {
    const key = setBackendKey(name);
    const def = docs[key] && docs[key].default;
    if (!def || typeof def !== "object") return "";
    const scalars = Object.keys(def).filter((k) => k !== "models" && typeof def[k] !== "object")
      .map((k) => setField(["llm", key, k])).join("");
    const models = def.models ? SET_TIERS.concat(Object.keys(def.models).filter((t) => !SET_TIERS.includes(t)))
      .filter((t) => t in def.models).map((t) => setField(["llm", key, "models", t], `Modèle utilisé pour le niveau ${t} par ${name}.`, "text")).join("") : "";
    return `<details class="set-backend" data-backend="${esc(name)}"${name === current ? " open" : ""}>
      <summary><span class="mono">${esc(name)}</span>${name === current ? ` <span class="chip running plain">actif</span>` : ""}</summary>
      <div class="form-grid">${scalars}${models}</div></details>`;
  }).join("");
  return `<section class="panel" id="set-llm"><div class="panel-head"><h2>LLM et modèles</h2><span class="muted mono">[llm]</span></div><div class="panel-pad">
    <div class="form-grid">
      <div class="field set-field" data-fpath="llm.backend"><label for="set-llm-backend">${esc(SET_LABELS["llm.backend"])}<code class="mono set-key">backend</code></label>
        <select class="input" id="set-llm-backend" data-path="llm.backend" data-kind="text">${backends.map((b) => `<option value="${esc(b)}"${b === current ? " selected" : ""}>${esc(b)}</option>`).join("")}</select>
        <span class="hint">${esc(docs.backend.comment || "Backend de tout appel LLM, sauf usage qui en redéfinit un. Texte et images fixes seulement.")}</span><span class="field-error" role="alert"></span></div>
      ${setField(["llm", "repair_attempts"], docs.repair_attempts.comment || "Réponse refusée (schéma JSON) : nombre de renvois au modèle ; 0 échoue tout de suite.")}
    </div>
    ${setLlmUsages()}
    <h3 class="set-sub">Backends et niveaux</h3>
    <p class="muted set-note">Chaque niveau (strong, fast) désigne un modèle propre au backend.</p>
    <div class="set-backends">${tables}</div>
  </div></section>`;
}

function setWeb() {
  return `<section class="panel" id="set-web"><div class="panel-head"><h2>Serveur web</h2><span class="muted mono">[web]</span></div><div class="panel-pad">
    <p class="muted set-note">Hôte et port ne s'appliquent qu'au prochain démarrage de « serve ». Le jeton ne se modifie pas ici (section Accès).</p>
    <div class="form-grid">${setSectionFields("web", ["token"])}</div></div></section>`;
}

function setWorker() {
  return `<section class="panel" id="set-worker"><div class="panel-head"><h2>Worker</h2><span class="muted mono">[worker]</span></div><div class="panel-pad">
    <div class="form-grid">${setSectionFields("worker")}</div></div></section>`;
}

const SET_COUNTRIES = [["FR", "France"], ["BE", "Belgique"], ["CH", "Suisse"], ["LU", "Luxembourg"], ["CA", "Canada"],
  ["GB", "Royaume-Uni"], ["IE", "Irlande"], ["DE", "Allemagne"], ["ES", "Espagne"], ["IT", "Italie"], ["PT", "Portugal"],
  ["NL", "Pays-Bas"], ["US", "États-Unis"], ["MA", "Maroc"], ["DZ", "Algérie"], ["TN", "Tunisie"]];

function setNetwork() {
  const value = String(setEffective(["network", "expected_country"]));
  const options = SET_COUNTRIES.map(([code, name]) => `<option value="${esc(code)}" label="${esc(name)}"></option>`).join("");
  const comment = (setUi.data.defaults.network.expected_country || {}).comment;
  return `<section class="panel" id="set-network"><div class="panel-head"><h2>Réseau</h2><span class="muted mono">[network]</span></div><div class="panel-pad">
    <p class="muted set-note">Le pays de l'IP publique s'affiche en haut à droite ; hors du pays attendu, la pastille passe au rouge et le navigateur piloté refuse de s'ouvrir.</p>
    <div class="form-grid">
      <div class="field set-field" data-fpath="network.expected_country"><label for="set-network-expected_country">${esc(SET_LABELS["network.expected_country"])}<code class="mono set-key">expected_country</code></label>
        <input class="input mono" id="set-network-expected_country" data-path="network.expected_country" data-kind="text" type="text" list="set-countries" maxlength="2" value="${esc(value)}" autocomplete="off">
        <datalist id="set-countries">${options}</datalist>
        <span class="hint">${esc(comment || "Pays attendu de l'IP publique : choisis dans la liste ou saisis un code ISO à deux lettres.")}</span>
        <span class="field-error" role="alert"></span></div>
      ${setSectionFields("network", ["expected_country"])}
    </div></div></section>`;
}

/* [veille] (SPEC-bdd9 R1, R8) : activation, goûts, nombres, heure, puis les clés en écriture seule. */
function setVeilleKey(key) {
  const isSet = Boolean((setUi.data.effective.veille || {})[`${key}_set`]);
  const typed = Boolean(setGet(setUi.draft, ["veille", key]));
  const id = `set-veille-${key}`;
  return `<div class="field set-field" data-fpath="veille.${key}">
    <label for="${id}">${esc(SET_LABELS[`veille.${key}`])}<code class="mono set-key">${esc(key)}</code></label>
    <input class="input mono" id="${id}" data-secret="veille.${key}" type="password" autocomplete="new-password" placeholder="${isSet ? "•••••••• (saisie, laisser vide pour la garder)" : "à saisir"}">
    <span class="hint">${esc(SET_LABELS[`veille.${key}`])} : ${isSet ? "déjà enregistrée dans config.toml" : "absente"}${typed ? " · nouvelle valeur à enregistrer" : ""}. Elle n'est jamais affichée ni renvoyée.</span>
    <span class="field-error" role="alert"></span></div>`;
}

function setVeille() {
  const shown = ["enabled", "run_at", "max_vods_per_day", "best_clips_per_day", "language", "region", "rise_min_pct", "baseline_days", "upcoming_days", "release_window_days", "igdb_min_hypes", "igdb_recent_max", "igdb_upcoming_max", "steam_players_lookups_max", "steam_followers_lookups_max", "steam_followers_pause_s", "community_min_steam_players", "community_min_steam_followers", "community_min_twitch_viewers", "community_min_hypes", "max_vods_per_game", "trend_days", "trend_games_max", "veille_deadline_s", "twitch_access_attempts", "twitch_access_retry_pause_s", "steam_reviews_pause_s", "twitch_history_pages_max"];
  const hidden = [...shown, "taste", "state_dir", ...SET_VEILLE_KEYS];
  const taste = setEffective(["veille", "taste"]);
  return `<section class="panel" id="set-veille"><div class="panel-head"><h2>Veille</h2><span class="muted mono">[veille]</span></div><div class="panel-pad">
    <p class="muted set-note">Chaque jour, Claude propose des VOD à clipper d'après Twitch, YouTube et Steam, et liste les sorties de jeux d'IGDB (mêmes clés Twitch). Rien ne tourne tant que « enabled » est faux. Les clés se créent dans les consoles Twitch et Google Cloud (docs/GUIDE.md) ; Steam n'en demande aucune.</p>
    <div class="form-grid">
      ${shown.map((k) => setField(["veille", k], (setUi.data.defaults.veille[k] || {}).comment)).join("")}
      <div class="field set-field full" data-fpath="veille.taste"><label for="set-veille-taste">${esc(SET_LABELS["veille.taste"])}<code class="mono set-key">taste</code></label>
        <textarea class="input" id="set-veille-taste" data-path="veille.taste" data-kind="text" rows="2" autocomplete="off">${esc(taste)}</textarea>
        <span class="hint">${esc((setUi.data.defaults.veille.taste || {}).comment || "Tes goûts, en texte libre : Claude les lit pour choisir les VOD.")}</span>
        <span class="field-error" role="alert"></span></div>
      ${SET_VEILLE_KEYS.map((k) => setVeilleKey(k)).join("")}
    </div>
    <details class="set-help"><summary>autres réglages</summary><div class="form-grid">${setSectionFields("veille", hidden)}</div></details>
  </div></section>`;
}

function setAccess() {
  const access = setUi.data.access;
  const restart = setUi.data.restart_required && !access.differs_from_config
    ? `<p class="reason" role="status">Le fichier a un hôte, un port ou un jeton différent de ceux de ce serveur : un redémarrage de « serve » est nécessaire pour les appliquer.</p>` : "";
  const differs = access.differs_from_config
    ? `<p class="reason" role="status">Ce serveur écoute sur ${esc(access.host)}:${esc(access.port)} (« serve --host/--port »), pas sur ${esc(access.config_host)}:${esc(access.config_port)} comme le dit ${esc(setUi.data.path)}. Les valeurs ci-dessous sont celles du serveur en cours ; le fichier ne s'appliquera qu'au prochain « serve » lancé sans --host/--port.</p>` : "";
  return `<section class="panel" id="set-access"><div class="panel-head"><h2>Accès</h2><span class="chip pending plain">lecture seule</span></div><div class="panel-pad">
    ${differs}${restart}
    <dl class="kv set-access">
      <dt>Hôte</dt><dd class="mono">${esc(access.host)}${access.loopback ? ` <span class="muted">(bouclage : aucun jeton exigé)</span>` : ""}</dd>
      <dt>Port</dt><dd class="mono">${esc(access.port)}</dd>
      <dt>Jeton</dt><dd class="mono">${access.token_set ? esc(access.token) : `<span class="muted">aucun jeton configuré</span>`}</dd>
      <dt>Commande</dt><dd><code class="set-cmd mono">${esc(access.command)}</code> <button type="button" class="btn btn-xs btn-ghost" data-set-copy>${icon("copy", "i-xs")}Copier</button></dd>
    </dl>
    <p class="muted set-note">Le jeton ne se modifie pas depuis l'interface : écris [web] token dans ${esc(setUi.data.path)}, puis redémarre « serve ». Toute adresse hors bouclage exige un jeton.</p>
  </div></section>`;
}

function setWarning() {
  if (!setUi.data.comments_lost) return "";
  return `<p class="set-warning" data-set-warning role="note">${icon("circle-alert", "i-sm")}<span>Enregistrer réécrit ${esc(setUi.data.path)} : les commentaires du fichier perdus (seules les valeurs sont conservées).</span></p>`;
}

function setHtml() {
  const nav = SET_SECTIONS.map(([id, label]) => `<a href="#set-${id}" data-set-nav="${id}">${esc(label)}</a>`).join("");
  return `<div class="set-layout">
    <nav class="set-nav" aria-label="Sections des réglages">${nav}</nav>
    <div class="stack set-stack">
      <p class="reason bad" data-set-error role="alert" hidden></p>
      ${setGeneral()}${setDirs()}${setLlm()}${setWeb()}${setWorker()}${setNetwork()}${setVeille()}${setAccess()}
      <div class="sticky-foot${setUi.dirty ? " show" : ""}" id="set-savebar">
        <p><span class="muted">${icon("pencil", "i-sm")}</span> Modifications non enregistrées dans <span class="mono">${esc(setUi.data.path)}</span></p>
        <button type="button" class="btn btn-sm btn-ghost" data-set-cancel>Annuler</button>
        <button type="button" class="btn btn-sm btn-primary" data-set-save>Enregistrer</button>
        ${setWarning()}
      </div>
    </div></div>`;
}

/* ---------- Erreurs du serveur ---------- */

function setClearErrors(root) {
  $$(".field-error", root).forEach((e) => { e.textContent = ""; });
  $$(".set-field.invalid", root).forEach((f) => f.classList.remove("invalid"));
  const top = $("[data-set-error]", root);
  if (top) top.hidden = true;
}

/* « [section] cle : ... » ou « cle : ... » : le champ dont le chemin se termine
   par ces noms, sinon l'erreur reste en tête du formulaire. */
function setShowError(root, message) {
  setClearErrors(root);
  const sec = (message.match(/\[(\w+)\]/) || [])[1];
  const fields = $$("[data-fpath]", root).filter((f) => f.classList.contains("set-field"));
  const word = (k) => new RegExp(`(^|[^\\w])${k}($|[^\\w])`).test(message);
  const hit = fields
    .filter((f) => { const p = f.dataset.fpath.split("."); return (!sec || p[0] === sec) && word(p[p.length - 1]); })
    .sort((a, b) => b.dataset.fpath.length - a.dataset.fpath.length)[0]
    || (!sec ? fields.find((f) => word(f.dataset.fpath)) : null);
  if (hit) {
    hit.classList.add("invalid");
    const det = hit.closest("details");
    if (det) det.open = true;
    $(".field-error", hit).textContent = message;
    hit.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  const mode = word("mode") && !sec ? $("span.field-error[data-fpath=\"mode\"]", root) : null;
  if (mode) { mode.textContent = message; return; }
  const top = $("[data-set-error]", root);
  top.textContent = message;
  top.hidden = false;
  top.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ---------- Actions ---------- */

async function setSave(root) {
  if (setUi.data.comments_lost && !(await confirmDialog({
    title: "Réécrire config.toml ?",
    body: "Les commentaires du fichier perdus : seules les valeurs sont conservées. Fais une copie du fichier si tu y tiens.",
    confirmLabel: "Enregistrer",
  }))) return;
  const button = $("[data-set-save]", root);
  button.disabled = true;
  try {
    const settings = setClone(setUi.draft);
    ["llm", "web", "worker", "network", "veille"].forEach((s) => { settings[s] = settings[s] || {}; });
    delete settings.web.token;                       // jamais lu ni écrit par l'interface
    Object.keys(settings.veille).filter((k) => k.endsWith("_set")).forEach((k) => delete settings.veille[k]); // drapeaux du serveur, pas des réglages
    ["mode", "workspace_dir", "output_dir"].forEach((k) => { if (!(k in settings)) settings[k] = setUi.data.defaults.general[k].default; });
    setUi.data = await api("/api/settings", jsonBody("PUT", { settings }));
    setUi.draft = setClone(setUi.data.raw);
    setUi.dirty = false;
    toast({ kind: "ok", title: "Réglages enregistrés", body: `${setUi.data.path} relu et validé.` });
    renderCurrent();
  } catch (err) {
    setShowError(root, err.message);
    toast({ kind: "bad", title: "Enregistrement refusé", body: "Le fichier est resté intact : corrige le champ signalé.", ms: 5200 });
  } finally {
    button.disabled = false;
  }
}

function setReadInput(el) {
  const kind = el.dataset.kind;
  if (kind === "bool") return el.checked;
  if (kind === "number") {
    const text = el.value.trim();
    return text !== "" && Number.isFinite(Number(text)) ? Number(text) : text;
  }
  return el.value;
}

function setUsagesFromDom(root) {
  const usages = {};
  $$("tr[data-usage]", root).forEach((row) => {
    const entry = {};
    const backend = $("[data-usage-backend]", row).value;
    const model = $("[data-usage-model]", row).value.trim();
    if (backend) entry.backend = backend;
    if (model) entry.model = model;
    usages[row.dataset.usage] = entry;
  });
  return usages;
}

function setWire(root) {
  root.onclick = (e) => {
    // Lien de section : on défile jusqu'à elle ; le hash ne change pas, le routeur n'est jamais sollicité.
    const link = e.target.closest("a[data-set-nav]");
    if (link) {
      e.preventDefault();
      const section = document.getElementById(`set-${link.dataset.setNav}`);
      if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const t = e.target.closest("button");
    if (!t) return;
    if (t.hasAttribute("data-set-save")) setSave(root);
    else if (t.hasAttribute("data-set-cancel")) { setUi.draft = setClone(setUi.data.raw); setUi.dirty = false; renderCurrent(); }
    else if (t.hasAttribute("data-set-copy")) copyText(setUi.data.access.command, "Commande");
    else if (t.dataset.v && t.closest("[data-set-mode]")) {
      $$("button", t.closest("[data-set-mode]")).forEach((b) => b.classList.toggle("on", b === t));
      setPut(["mode"], t.dataset.v);
    } else if (t.hasAttribute("data-usage-add")) {
      const input = $("[data-usage-new]", root);
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      setPutUsages({ ...setUsagesFromDom(root), [name]: {} });
      setUi.forceRender = true;
      renderCurrent();
    } else if (t.hasAttribute("data-usage-del")) {
      const usages = setUsagesFromDom(root);
      delete usages[t.dataset.usageDel];
      setPutUsages(usages);
      setUi.forceRender = true;
      renderCurrent();
    }
  };
  const changed = (e) => {
    const el = e.target;
    if (el.dataset && el.dataset.secret) {
      // clé d'API : saisie => écrite ; champ vidé => retirée du brouillon, donc gardée côté serveur (jamais effacée par oubli)
      const path = el.dataset.secret.split(".");
      const table = (setUi.draft[path[0]] = setUi.draft[path[0]] || {});
      if (el.value) table[path[1]] = el.value; else delete table[path[1]];
      setMarkDirty();
    } else if (el.dataset && el.dataset.path) {
      setPut(el.dataset.path.split("."), setReadInput(el));
      const field = el.closest(".set-field");
      if (field) { field.classList.remove("invalid"); $(".field-error", field).textContent = ""; }
    } else if (el.closest && el.closest("tr[data-usage]")) {
      // backend/modèle d'un usage modifié
      setPutUsages(setUsagesFromDom(root));
    }
  };
  root.oninput = changed;
  root.onchange = changed;
}

Screens.settings = {
  render(body) {
    const section = body.closest(".screen");
    if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
    if (setUi.data === null || (!setUi.dirty && Date.now() - setUi.at > SET_STALE_MS)) setLoad(false);
    if (setUi.data === null) {
      body.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`;
      return;
    }
    if (setUi.data.failed) {
      body.innerHTML = emptyState("circle-alert", "Réglages illisibles", setUi.error || "config.toml n'a pas pu être lu.");
      return;
    }
    // Un tableau modifié à moitié n'est jamais écrasé par un rechargement périodique.
    if (setUi.dirty && body.querySelector("#set-savebar") && !setUi.forceRender) return;
    setUi.forceRender = false;
    if (body.resetHtmlGuard) body.resetHtmlGuard();   // formulaire : toujours reconstruit, saisies jetees
    body.innerHTML = setHtml();
    setWire(body);
  },
};
