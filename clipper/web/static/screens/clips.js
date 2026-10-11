/* Ecran « Clips » v3 (SPEC-7715 E4 / E12 / T10 / T11, ADR-a308, SPEC-74e9 §4.5). Lit GET /api/clips ; la galerie
   9:16 par statut ouvre la fiche d'un clip dans le tiroir de la coquille (drawer, ui.js) sans perdre la liste,
   l'adresse suit (#/clips/<video>/<clip>). Rendu par rapprochement du DOM (morph), jamais innerHTML.
   Aucune logique video ici : le texte passe par PATCH /api/clips/.../ (publish.edit_caption), le titre d'ecran
   et « Re-rendre » par la file de traitement. Charge apres screens.js dont il remplace l'entree Screens.clips. */
"use strict";

const CLIP_FILTERS = [
  ["à valider", "À valider"], ["approved", "Approuvés"], ["scheduled", "Planifiés"],
  ["published", "Publiés"], ["failed", "Échecs"], ["rejected", "Refusés"], ["refused_by_platform", "Refusés par TikTok"], ["removed_from_platform", "Supprimés de la plateforme"], ["all", "Tous"],
];
const CLIP_STATUS = {
  "à valider": { label: "À valider", cls: "info" }, approved: { label: "Approuvé", cls: "ok" },
  scheduled: { label: "Planifié", cls: "info" }, published: { label: "Publié", cls: "ok" },
  failed: { label: "Échec", cls: "bad" }, rejected: { label: "Refusé", cls: "bad" },
  refused_by_platform: { label: "Refusé par TikTok", cls: "bad" },
  removed_from_platform: { label: "Supprimé de la plateforme", cls: "pending" },
  not_ready: { label: "Pas prêt", cls: "pending" }, // rendu ou contrôle qualité pas terminé : ni à valider ni refusé
};
// SPEC-74e9 §4.5 : une entree scheduled/published n'est pas editable sans repasser approved.
const CLIP_LOCKED = ["scheduled", "published"];
const CLIPS_STALE_MS = 4000;
const CLIPS_PAGE_SIZE = 24; // la galerie n'affiche que 24 clips a la fois (« Afficher plus »)
// Les 6 criteres de la grille (SPEC-4063 / SPEC-9216), dans l'ordre d'affichage de la fiche.
const CLIP_SCORE_KEYS = ["hook", "standalone", "payoff", "emotion", "value", "trend"];

const CLIPS_ARCHIVED = ["archived", "Archivés"]; // clips archivés par la veille (SPEC-bdd9 R9), masqués des autres filtres
const clipsUi = { data: null, error: null, loading: null, dirty: false, at: 0, filter: "à valider", channel: "", video: "", hashVideo: null, shown: CLIPS_PAGE_SIZE, selecting: false, selected: new Set(), open: null };

/* Vidéo visée par l'adresse : #/clips/<video_id> (lien « Voir les N clips » de la fiche vidéo), sinon "". */
const clipsHashVideo = () => decodeURIComponent((location.hash.replace(/^#\/?/, "").split("?")[0].split("/")[1] || ""));

/* Adresse complete de l'ecran : #/clips[/<video_id>[/<clip_id>]] (T10 : le tiroir ouvert est dans l'URL). */
function clipsHashTarget() {
  const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  return { video: decodeURIComponent(parts[1] || ""), clip: parts[2] ? decodeURIComponent(parts[2]) : null };
}
const clipsListHash = () => (clipsUi.video ? `#/clips/${encodeURIComponent(clipsUi.video)}` : "#/clips");
const clipsClipHash = (key) => `#/clips/${key.split("/").map(encodeURIComponent).join("/")}`;

/* Clips affichés : statut, style et vidéo choisis (chaque filtre vide = pas de filtre). L'ordre reçu (plus récents en haut) est gardé. */
function clipsFiltered(all, ui) {
  if (ui.filter === "archived") all = all.filter((c) => c.veille && c.veille.status === "archived");
  else all = all.filter((c) => !(c.veille && c.veille.status === "archived"));
  return all.filter((c) => (ui.filter === "all" || ui.filter === "archived" || c.publish_status === ui.filter) && (!ui.channel || c.channel === ui.channel) && (!ui.video || c.video_id === ui.video));
}

const clipKey = (c) => `${c.video_id}/${c.clip_id}`;
/* Libelle d'un compte avec son service (« compte_alpha (YouTube) ») : un meme nom peut exister sur TikTok et YouTube. */
const clipsAccountText = (a) => {
  const service = a.service_label || (a.service === "youtube" ? "YouTube" : a.service === "tiktok" ? "TikTok" : "");
  return `${a.label || a.id}${service ? ` (${service})` : ""}`;
};
const clipUrl = (c, tail) => `/api/clips/${encodeURIComponent(c.video_id)}/${encodeURIComponent(c.clip_id)}${tail || ""}`;
const clipStatus = (c) => CLIP_STATUS[c.publish_status] || { label: c.publish_status, cls: "pending" };
const clipSeconds = (s) => `${fr(s, 1)} s`;

/* Onglets de statut (E4) : « À valider » toujours (c'est la decision par defaut), « Tous » toujours, les autres
   statuts seulement s'ils comptent au moins un clip, plus le filtre courant pour pouvoir le quitter. Les compteurs
   suivent les filtres vidéo et style. Rend [[cle, libelle, nombre]]. */
function clipsTabs(all, ui) {
  const scoped = clipsFiltered(all, { filter: "all", channel: ui.channel, video: ui.video });
  const archived = all.filter((c) => c.veille && c.veille.status === "archived" && (!ui.channel || c.channel === ui.channel) && (!ui.video || c.video_id === ui.video)).length;
  const count = (k) => (k === "archived" ? archived : k === "all" ? scoped.length : scoped.filter((c) => c.publish_status === k).length);
  const filters = [...CLIP_FILTERS, ...(archived || ui.filter === "archived" ? [CLIPS_ARCHIVED] : [])];
  return filters.map(([k, l]) => [k, l, count(k)]).filter(([k, , n]) => k === "à valider" || k === "all" || k === ui.filter || n > 0);
}

/* Filtre vidéo (E4) : chaque vidéo par son titre (source_title du sidecar) suivi de son id ; sans titre, l'id seul,
   jamais un titre inventé (ADR-ad2e). La vidéo choisie reste proposée même si aucun de ses clips n'est chargé. */
function clipsVideoOptions(all, ui) {
  const titles = new Map();
  all.forEach((c) => { if (!titles.has(c.video_id) || (!titles.get(c.video_id) && c.source_title)) titles.set(c.video_id, c.source_title || ""); });
  if (ui.video && !titles.has(ui.video)) titles.set(ui.video, "");
  return Array.from(titles.keys()).sort().map((id) => ({ id, label: titles.get(id) ? `${titles.get(id)} · ${id}` : id }));
}

function loadClips() {
  if (clipsUi.loading) { clipsUi.dirty = true; return clipsUi.loading; }
  clipsUi.loading = (async () => {
    try {
      clipsUi.data = await api("/api/clips?archived=1");
      clipsUi.error = null;
    } catch (err) {
      clipsUi.error = err;
    } finally {
      clipsUi.loading = null;
      clipsUi.at = Date.now();
    }
    if (currentScreen === "clips") renderCurrent();
    if (clipsUi.dirty) { clipsUi.dirty = false; loadClips(); }
  })();
  return clipsUi.loading;
}

// Un evenement publish/video/queue rend la liste obsolete (le serveur reste la source).
document.addEventListener("clipper:event", () => {
  clipsUi.at = 0;
  if (currentScreen === "clips") loadClips();
});

function clipCaptionText(c) {
  return [c.description || "", (c.hashtags || []).join(" ")].filter(Boolean).join("\n\n");
}

function clipCard(c) {
  const s = clipStatus(c);
  const warn = c.qa_status === "rejected" ? "bad" : (c.issues && c.issues.length ? "warn" : "");
  const gone = !!c.video_deleted; // clip publié dont la vidéo a été supprimée (TASK-f909) : ni sélectionnable ni lisible
  const sel = clipsUi.selecting && !gone;
  const checked = sel && clipsUi.selected.has(clipKey(c));
  const label = sel
    ? `${checked ? "Désélectionner" : "Sélectionner"} ${esc(c.screen_title || c.clip_id)}`
    : `Ouvrir le clip ${esc(c.screen_title || c.clip_id)}`;
  return `<div class="clip${gone ? " gone" : ""}${sel ? " selecting" : ""}${checked ? " checked" : ""}${clipsUi.open === clipKey(c) ? " open" : ""}" data-clip="${esc(clipKey(c))}" tabindex="0" role="${sel ? "checkbox" : "button"}"${sel ? ` aria-checked="${checked}"` : ""} aria-label="${label}">
    <div class="clip-poster">${gone ? `<span class="ph">vidéo supprimée</span>` : `<img loading="lazy" decoding="async" width="270" height="480" src="${esc(c.thumbnail_url)}" alt="" tabindex="-1">`}
      <div class="top">${sel ? `<span class="clip-check${checked ? " on" : ""}">${checked ? icon("check", "i-xs") : ""}</span>` : `<span class="chip plain ${s.cls}">${esc(s.label)}</span>`}${gone ? `<span class="badge-qa warn">Vidéo supprimée</span>` : ""}${c.parts_total > 1 ? `<span class="badge-qa">${esc(c.part)}/${esc(c.parts_total)}</span>` : ""}</div>
      ${gone ? "" : `<span class="play">${icon("play")}</span>`}
      <div class="bottom"><b>${c.duration != null ? esc(clipSeconds(c.duration)) : ""}</b>${warn ? `<span class="badge-qa ${warn}">${icon("triangle-alert", "i-xs")}QA</span>` : ""}</div>
    </div>
    <div class="clip-title" title="${esc(c.screen_title || c.title || c.clip_id)}">${esc(c.screen_title || c.title || c.clip_id)}</div>
    <div class="clip-sub">${c.score != null ? `<span>score ${esc(fr(c.score))}</span>` : ""}<span class="mono">${esc(c.clip_id)}</span></div>
  </div>`;
}

/* Barre de selection groupee (TASK-e99b) : nombre selectionne, compte (prets seulement), approuver / annuler. */
function clipsSelectionBarHtml() {
  const n = clipsUi.selected.size;
  return `<div class="sel-bar" id="clips-selbar">
    <span class="num">${n} clip${n === 1 ? "" : "s"} sélectionné${n === 1 ? "" : "s"}</span>
    <select class="input" id="clips-sel-account" aria-label="Compte de publication"><option value="">Chargement…</option></select>
    <button type="button" class="btn" data-variant="outline" data-size="sm" data-clips-sel-all>${icon("list-checks", "i-xs")}Tout sélectionner</button>
    <button type="button" class="btn" data-variant="ghost" data-size="sm" data-clips-sel-clear${n ? "" : " disabled"}>${icon("x", "i-xs")}Vider la sélection</button>
    <span class="grow"></span>
    <button type="button" class="btn" data-variant="ghost" data-size="sm" data-clips-sel-cancel>Annuler</button>
    <button type="button" class="btn" data-variant="destructive" data-size="sm" data-clips-sel-delete${n ? "" : " disabled"}>${icon("trash-2", "i-xs")}Supprimer la sélection</button>
    <button type="button" class="btn" data-size="sm" data-clips-sel-approve${n ? "" : " disabled"}>${icon("check", "i-xs")}Approuver la sélection</button>
  </div>`;
}

function clipsEmpty(all) {
  if (!all.length) return emptyState("clapperboard", "Aucun clip", "Ajoute une vidéo : les clips rendus apparaîtront ici.", addVideoButton);
  return emptyState("party-popper", "Aucun clip dans cette catégorie", "Change de filtre pour voir les autres clips.");
}

function clipsView(body) {
  const all = clipsUi.data;
  const filtered = clipsFiltered(all, clipsUi);
  const channels = Array.from(new Set(all.map((c) => c.channel).filter(Boolean))).sort();
  const videos = clipsVideoOptions(all, clipsUi);
  const rest = filtered.length - clipsUi.shown;
  const more = rest > 0
    ? `<div class="clips-more"><button type="button" class="btn" data-variant="outline" data-clips-more>Afficher plus<span class="n">${esc(Math.min(rest, CLIPS_PAGE_SIZE))}</span></button></div>` : "";
  const html = `
    ${clipsUi.error ? `<div class="alert" data-variant="destructive" role="alert">${icon("circle-alert")}<h2>Actualisation impossible</h2><section>${esc(clipsUi.error.message || clipsUi.error)}</section></div>` : ""}
    <div class="toolbar">
      <div class="seg" id="clips-filter" role="radiogroup" aria-label="Statut">${clipsTabs(all, clipsUi).map(([k, l, n]) => `<button type="button" data-filter="${esc(k)}" class="${k === clipsUi.filter ? "on" : ""}" aria-pressed="${k === clipsUi.filter}">${esc(l)}<span class="n">${n}</span></button>`).join("")}</div>
      <span class="grow"></span>
      <select class="input" id="clips-video" aria-label="Vidéo"><option value="">Toutes les vidéos</option>${videos.map((v) => `<option value="${esc(v.id)}"${v.id === clipsUi.video ? " selected" : ""}>${esc(v.label)}</option>`).join("")}</select>
      ${clipsUi.video ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-clear-video aria-label="Retirer le filtre vidéo">${icon("x", "i-xs")}Toutes les vidéos</button>` : ""}
      <select class="input" id="clips-channel" aria-label="Style"><option value="">Tous les styles</option>${channels.map((n) => `<option value="${esc(n)}"${n === clipsUi.channel ? " selected" : ""}>${esc(n)}</option>`).join("")}</select>
      ${!clipsUi.selecting ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-clips-select>${icon("list-checks", "i-xs")}Sélectionner</button>` : ""}
    </div>
    ${filtered.length ? `<div class="clips">${filtered.slice(0, clipsUi.shown).map(clipCard).join("")}</div>${more}` : clipsEmpty(all)}
    ${clipsUi.selecting ? clipsSelectionBarHtml() : ""}`;
  morph(body, html);
}

/* Le filtre vidéo vit dans l'adresse (#/clips/<video_id>) : lien partageable, bouton Retour qui marche. */
function clipsSetVideo(video) {
  location.hash = video ? `#/clips/${encodeURIComponent(video)}` : "#/clips";
}

function clipsWire(body) {
  const video = $("#clips-video", body);
  if (video) video.onchange = () => clipsSetVideo(video.value);
  const clear = $("[data-clear-video]", body);
  if (clear) clear.onclick = () => clipsSetVideo("");
  $$("[data-filter]", body).forEach((b) => (b.onclick = () => { clipsUi.filter = b.dataset.filter; clipsUi.shown = CLIPS_PAGE_SIZE; renderCurrent(); }));
  const sel = $("#clips-channel", body);
  if (sel) sel.onchange = () => { clipsUi.channel = sel.value; clipsUi.shown = CLIPS_PAGE_SIZE; renderCurrent(); };
  const more = $("[data-clips-more]", body);
  if (more) more.onclick = () => { clipsUi.shown += CLIPS_PAGE_SIZE; renderCurrent(); };
  $$("[data-clip]", body).forEach((el) => {
    const act = () => (clipsUi.selecting && !el.classList.contains("gone") ? clipsToggleSelect(el.dataset.clip) : openClipDrawer(el.dataset.clip, { push: true }));
    el.onclick = act;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); act(); } };
  });
  const selectBtn = $("[data-clips-select]", body);
  if (selectBtn) selectBtn.onclick = () => { clipsUi.selecting = true; clipsUi.selected = new Set(); renderCurrent(); };
  const selBar = $("#clips-selbar", body);
  if (selBar) {
    const account = $("#clips-sel-account", selBar);
    if (!account.dataset.filled) { account.dataset.filled = "1"; clipsFillSelAccounts(account); } // morph garde le <select> : rempli une fois
    $("[data-clips-sel-cancel]", selBar).onclick = () => { clipsUi.selecting = false; clipsUi.selected = new Set(); renderCurrent(); };
    $("[data-clips-sel-all]", selBar).onclick = () => { clipsUi.selected = clipsAllKeys(clipsUi.data, clipsUi); renderCurrent(); };
    $("[data-clips-sel-clear]", selBar).onclick = () => { clipsUi.selected = new Set(); renderCurrent(); };
    const deleteBtn = $("[data-clips-sel-delete]", selBar);
    if (deleteBtn) deleteBtn.onclick = () => clipsDeleteSelection();
    const approveBtn = $("[data-clips-sel-approve]", selBar);
    if (approveBtn) approveBtn.onclick = () => clipsApproveSelection($("#clips-sel-account", selBar).value);
  }
}

/* « Tout sélectionner » : tous les clips du filtre courant (statut, vidéo, style), pas seulement la page affichée ;
   une série y est entière dès qu'une de ses parties passe le filtre (même règle que clipsToggleSelect). */
function clipsAllKeys(all, ui) {
  const keys = new Set();
  clipsFiltered(all, ui).filter((c) => !c.video_deleted).forEach((c) => {
    const series = clipSeries(c);
    (series.length ? series.map(clipKey) : [clipKey(c)]).forEach((k) => keys.add(k));
  });
  return keys;
}

/* Cocher une partie coche toute sa serie (TASK-e99b) : meme decision groupee que l'approbation groupee cote serveur. */
function clipsToggleSelect(key) {
  const c = clipsUi.data.find((o) => clipKey(o) === key);
  if (!c || c.video_deleted) return;
  const series = clipSeries(c);
  const group = series.length ? series.map(clipKey) : [key];
  const adding = !clipsUi.selected.has(key);
  group.forEach((k) => (adding ? clipsUi.selected.add(k) : clipsUi.selected.delete(k)));
  if (adding && group.length > 1) {
    toast({ kind: "info", title: "Série ajoutée", body: `${group.length} parties sélectionnées ensemble : cocher une partie coche toute sa série.`, ms: 3200 });
  }
  renderCurrent();
}

/* Compte de publication de la barre de selection : comptes prets seulement (SPEC-00d1 R4). */
async function clipsFillSelAccounts(select) {
  try {
    const out = await api("/api/publish/accounts");
    const ready = out.accounts.filter((a) => a.ready_to_publish);
    const options = ready.map((a) => `<option value="${esc(a.id)}">${esc(clipsAccountText(a))}</option>`);
    options.unshift(`<option value="">${ready.length ? "Choisis un compte" : "Aucun compte prêt à publier"}</option>`);
    select.innerHTML = options.join("");
  } catch (err) {
    select.innerHTML = `<option value="">Comptes indisponibles</option>`;
  }
}

/* Approbation groupee (TASK-e99b) : meme decision que /approve pour chaque clip, tout ou rien cote serveur. */
async function clipsApproveSelection(account) {
  if (!account) { toast({ kind: "warn", title: "Compte manquant", body: "Choisis le compte qui publiera la sélection." }); return; }
  const clips = Array.from(clipsUi.selected).map((key) => {
    const [video_id, clip_id] = key.split("/");
    return { video_id, clip_id };
  });
  if (!clips.length) return;
  if (!(await netGuard())) return;
  try {
    const approved = await api("/api/clips/approve", jsonBody("POST", { clips, account }));
    clipsUi.selecting = false;
    clipsUi.selected = new Set();
    renderCurrent();
    toast({ kind: "ok", title: "Clips approuvés", body: `${approved.length} clip${approved.length === 1 ? "" : "s"} approuvé${approved.length === 1 ? "" : "s"}.` });
    loadClips();
  } catch (err) {
    const refused = err.body && Array.isArray(err.body.refused) ? err.body.refused : null;
    toastError("Approbation impossible", refused ? Object.assign(new Error(`${err.message} : ${refused.join(" ; ")}`), { body: err.body }) : err);
  }
}

/* Suppression groupee (TASK-2322, TASK-f909) : confirmation, puis le serveur supprime ce qui est permis (jamais un
   clip programme, en cours ou en attente ; tout ou rien par serie ; un clip publie perd sa video mais garde ses
   infos et ses stats) et dit pourquoi le reste est refuse. */
async function clipsDeleteSelection() {
  const clips = Array.from(clipsUi.selected).map((key) => {
    const [video_id, clip_id] = key.split("/");
    return { video_id, clip_id };
  });
  if (!clips.length) return;
  const n = clips.length;
  if (!(await confirmDialog({ title: `Supprimer ${n} clip${n === 1 ? "" : "s"} ?`, body: `Supprimer ${n} clip${n === 1 ? "" : "s"} ? Irréversible. Une partie de série entraîne toute sa série. Les clips publiés gardent leurs infos (stats), seule la vidéo est supprimée.`, confirmLabel: "Supprimer" }))) return;
  if (!(await netGuard())) return;
  try {
    const out = await api("/api/clips/delete", jsonBody("POST", { clips }));
    clipsUi.selected = new Set();
    renderCurrent();
    const d = out.deleted.length, v = (out.video_deleted || []).length, r = out.refused.length;
    const body = `${d} clip${d === 1 ? "" : "s"} supprimé${d === 1 ? "" : "s"}, ${v} vidéo${v === 1 ? "" : "s"} supprimée${v === 1 ? "" : "s"} (infos et stats gardées)${r ? `, ${r} refusé${r === 1 ? "" : "s"} : ${out.refused.map((o) => `${o.clip.video_id}/${o.clip.clip_id} (${o.reason})`).join(" ; ")}` : ""}.`;
    toast({ kind: r ? "warn" : "ok", title: r ? "Suppression partielle" : "Clips supprimés", body });
    clipsUi.at = 0;
    loadClips();
  } catch (err) {
    toastError("Suppression impossible", err);
  }
}

/* ---------- Fiche d'un clip (tiroir) ---------- */

const clipSeries = (c) => (c.parts_total > 1
  ? clipsUi.data.filter((o) => o.video_id === c.video_id && o.parts_total === c.parts_total && o.clip_id.split("-p")[0] === c.clip_id.split("-p")[0])
    .sort((a, b) => a.part - b.part)
  : []);

/* Un probleme du controle qualite : objet {type, severity, detail} (qa.issues du sidecar) ou texte seul. */
function clipIssueHtml(q) {
  if (typeof q === "string") return `<div class="issue"><span>${esc(q)}</span></div>`;
  if (!q || typeof q !== "object") return `<div class="issue"><span>inconnu</span></div>`;
  const sev = q.severity == null || q.severity === "" ? "" : esc(q.severity);
  const type = q.type == null || q.type === "" ? "" : esc(q.type);
  const detail = q.detail == null || q.detail === "" ? "" : esc(q.detail);
  return `<div class="issue">${type ? `<span class="sev ${sev}" title="${sev || "avertissement"}">${type}</span>` : ""}<span>${detail || (type ? "" : "inconnu")}</span></div>`;
}

/* Controle qualite : etat (passe / refuse / non execute / absent) et ses avertissements, chacun avec type,
   severite et detail (E4 : « QA avec avertissements »). */
function clipQaBlock(c) {
  if (c.qa_status == null) return `<div class="section-head"><h3>Contrôle qualité</h3><span class="chip warn">absent</span></div><p class="muted">Contrôle qualité absent du sidecar de ce clip.</p>`;
  const issues = Array.isArray(c.issues) ? c.issues : [];
  const n = issues.length;
  const state = c.qa_status === "rejected" ? `<span class="chip bad">refusé</span>`
    : c.qa_status === "skipped" ? `<span class="chip">non exécuté</span>`
      : n ? `<span class="chip warn">passé, ${n} avertissement${n > 1 ? "s" : ""}</span>` : `<span class="chip ok">passé</span>`;
  const list = n ? `<div class="issues">${issues.map(clipIssueHtml).join("")}</div>` : `<p class="muted">Aucun problème relevé.</p>`;
  return `<div class="section-head"><h3>Contrôle qualité</h3>${state}</div>${list}`;
}

/* Les 6 criteres de la grille : valeur sur 10 et barre ; un critere absent du sidecar le dit (jamais 0, ADR-ad2e). */
function clipScoresHtml(scores) {
  const sc = scores && typeof scores === "object" ? scores : {};
  return `<div class="scores">${CLIP_SCORE_KEYS.map((k) => {
    const v = sc[k];
    const known = v != null && v !== "" && !Number.isNaN(Number(v));
    return `<div class="score">${known ? `<b>${esc(v)}</b>` : `<b class="muted" title="critère absent du sidecar">non noté</b>`}<span>${k}</span><div class="bar"><i style="--p:${known ? (Math.max(0, Math.min(10, Number(v))) / 10).toFixed(2) : 0}"></i></div></div>`;
  }).join("")}</div>`;
}

/* Confirmation d'un refus (T4) : une partie de serie refuse la serie entiere, et le dialogue le dit. */
function clipRejectConfirm(c) {
  if (c.parts_total > 1) {
    return {
      title: "Refuser la série entière ?",
      body: `Ce clip est la partie ${c.part}/${c.parts_total} : refuser une partie refuse toute la série (${c.parts_total} parties). Rien ne sera publié.`,
      confirmLabel: "Refuser la série",
    };
  }
  return { title: "Refuser ce clip ?", body: "Le clip ne sera pas publié.", confirmLabel: "Refuser" };
}

/* Liste parcourue par j / k : les clips du filtre courant (toutes pages), dans l'ordre affiché. */
const clipsDrawerKeys = () => clipsFiltered(clipsUi.data || [], clipsUi).map(clipKey);
/* [position 1-based, total] du clip dans la liste parcourue ; [0, total] s'il est hors filtre. */
function clipsDrawerIndex(key) {
  const keys = clipsDrawerKeys();
  return [keys.indexOf(key) + 1, keys.length];
}
/* Clé du clip voisin (dir = +1 suivant, -1 précédent), null au bout de la liste ou hors filtre. */
function clipsNeighbor(key, dir) {
  const keys = clipsDrawerKeys();
  const i = keys.indexOf(key);
  if (i < 0) return keys.length && dir > 0 ? keys[0] : null;
  return keys[i + dir] || null;
}

/* Bornes dans la VOD : « 1 h 05 min 12 s » lisible, secondes gardées. */
function clipClock(s) {
  const t = Math.max(0, Math.round(Number(s)));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min ${String(sec).padStart(2, "0")} s` : `${m} min ${String(sec).padStart(2, "0")} s`;
}

function clipDrawerHtml(c) {
  const s = clipStatus(c);
  const locked = CLIP_LOCKED.includes(c.publish_status);
  const series = clipSeries(c);
  const gone = !!c.video_deleted;
  const [pos, total] = clipsDrawerIndex(clipKey(c));
  const lockHint = locked ? `<p class="muted">Statut « ${esc(s.label.toLowerCase())} » : l'édition demande de repasser le clip en « approuvé ».</p>` : "";
  const sidecar = JSON.stringify(c, null, 2);
  const kbd = (k) => `<kbd class="kbd">${k}</kbd>`;
  return `
    <div class="drawer-head">
      <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-drawer-close aria-label="Fermer (Échap)">${icon("x")}</button>
      <h2 id="drawer-title" title="${esc(c.screen_title || c.clip_id)}">${esc(c.screen_title || c.clip_id)}</h2>
      <span class="chip ${s.cls}">${esc(s.label)}</span>
      <span class="mono muted drawer-pos">${pos ? `${pos}/${total}` : `hors filtre`}</span>
      <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-prev aria-label="Clip précédent (k)"${clipsNeighbor(clipKey(c), -1) ? "" : " disabled"}>${icon("chevron-left")}</button>
      <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-next aria-label="Clip suivant (j)"${clipsNeighbor(clipKey(c), 1) ? "" : " disabled"}>${icon("chevron-right")}</button>
    </div>
    <div class="drawer-body">
      <div class="clip-drawer-top">
        <div>
          ${gone ? `<div class="clip-poster"><span class="ph">vidéo supprimée</span></div><p class="reason warn">Vidéo supprimée : ce clip publié garde ses infos et ses stats, mais plus son fichier.</p>` : `<video src="${esc(c.video_url)}" poster="${esc(c.thumbnail_url)}" controls playsinline preload="metadata"></video>`}
          <p class="muted clip-drawer-meta">${c.duration != null ? `<span class="mono">${esc(clipSeconds(c.duration))}</span> · ` : ""}<span class="mono">1080×1920</span>${c.parts_total > 1 ? ` · partie ${esc(c.part)}/${esc(c.parts_total)}` : ""}</p>
          ${series.length ? `<div class="parts-label muted">Parties</div><div class="parts">${series.map((p) => `<button type="button" class="part${p.clip_id === c.clip_id ? " on" : ""}" data-part="${esc(clipKey(p))}" title="Partie ${esc(p.part)}" aria-label="Partie ${esc(p.part)}" aria-pressed="${p.clip_id === c.clip_id}">${esc(p.part)}</button>`).join("")}</div>` : ""}
        </div>
        <div>
          ${clipScoresHtml(c.scores)}
          <dl class="kv clip-kv">
            <dt>Score</dt><dd>${c.score != null ? `<b class="num">${esc(fr(c.score, 1))}</b> <span class="muted">/ 100</span>` : `<span class="muted">inconnu</span>`}</dd>
            <dt>Accroche</dt><dd>${c.hook_text ? esc(c.hook_text) : `<span class="muted">inconnue</span>`}</dd>
            <dt>Bornes</dt><dd class="mono">${c.start != null && c.end != null ? `${esc(clipClock(c.start))} → ${esc(clipClock(c.end))}` : "inconnues"}</dd>
            <dt>Format</dt><dd>${c.layout ? esc(c.layout) : `<span class="muted">inconnu</span>`}</dd>
            <dt>Raison</dt><dd class="t2">${c.reason ? esc(c.reason) : `<span class="muted">inconnue</span>`}</dd>
            <dt>Confiance du jury</dt><dd data-jury-confidence>${juryConfidenceHtml(c.jury_confidence, c.jury_judge_confidences)}</dd>
            <dt>Vidéo</dt><dd><span class="mono">${esc(c.video_id)}</span>${c.channel ? ` <span class="tag">${esc(c.channel)}</span>` : ""}</dd>
            ${c.slot_at ? `<dt>Créneau</dt><dd>${esc(fmtParis(c.slot_at, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}</dd>` : ""}
          </dl>
        </div>
      </div>
      ${c.publish_error ? `<div class="alert" data-variant="destructive" role="alert">${icon("circle-x")}<h2>Publication en échec</h2><section>${esc(c.publish_error)}</section></div>` : ""}
      <section class="drawer-section">${clipQaBlock(c)}</section>
      <section class="drawer-section">
        <div class="section-head"><h3>Légende</h3><div class="right"><button type="button" class="btn" data-variant="ghost" data-size="sm" data-copy>${icon("copy", "i-sm")}Copier</button></div></div>
        <div class="field"><label for="clip-desc">Description</label><textarea class="input" id="clip-desc" rows="4"${locked ? " disabled" : ""}>${esc(c.description)}</textarea></div>
        <div class="field"><label for="clip-tags">Hashtags</label><input class="input" id="clip-tags" value="${esc((c.hashtags || []).join(" "))}"${locked ? " disabled" : ""}><p>Séparés par des espaces. Enregistrés dans le sidecar, avec « Annuler » 5 s.</p></div>
        <div><button type="button" class="btn" data-variant="outline" data-size="sm" data-save-caption${locked ? " disabled" : ""}>${icon("check", "i-xs")}Enregistrer la description et les hashtags</button></div>
        <div class="field"><label for="clip-title">Titre d'écran</label><input class="input" id="clip-title" value="${esc(c.screen_title)}"${locked || gone ? " disabled" : ""}><p>Affiché en haut du clip. Le modifier relance le rendu puis le contrôle qualité de ce seul clip.</p></div>
        <div><button type="button" class="btn" data-variant="outline" data-size="sm" data-save-title${locked || gone ? " disabled" : ""}>${icon("refresh-cw", "i-xs")}Enregistrer et re-rendre</button></div>
        ${lockHint}
      </section>
      <section class="drawer-section">
        <div class="section-head"><h3>Publication</h3></div>
        <div class="field" data-clip-account-field><label for="clip-account">Compte de publication</label>
          <select class="input" id="clip-account" data-clip-account><option value="">Chargement…</option></select>
          <p data-clip-account-hint>Choisis le compte qui publiera ce clip ; seuls les comptes « prêts à publier » (écran Comptes) sont proposés, avec les créneaux du compte.</p></div>
        <div class="drawer-links">
          <a class="btn" data-variant="ghost" data-size="sm" href="#/clip/${encodeURIComponent(c.video_id)}/${encodeURIComponent(c.clip_id)}">${icon("external-link", "i-sm")}Fiche complète</a>
          ${gone ? "" : `<a class="btn" data-variant="ghost" data-size="sm" href="${esc(c.video_url)}" download="${esc(c.clip_id)}.mp4">${icon("download", "i-sm")}Télécharger</a>`}
        </div>
        <details><summary class="muted">Sidecar JSON (SPEC-6a86)</summary><pre class="code">${esc(sidecar)}</pre></details>
      </section>
    </div>
    <div class="drawer-foot">
      ${!gone && c.publish_status !== "rejected" ? `<button type="button" class="btn" data-variant="destructive" data-reject>${icon("thumbs-down", "i-sm")}Refuser ${kbd("r")}</button>` : ""}
      ${gone ? "" : `<button type="button" class="btn" data-variant="outline" data-rerender>${icon("rotate-ccw", "i-sm")}Re-rendre</button>`}
      <span class="spacer"></span>
      ${!gone && (c.publish_status === "à valider" || c.publish_status === "approved") ? `<button type="button" class="btn" data-variant="outline" data-publish-now>${icon("send", "i-sm")}Publier maintenant</button>` : ""}
      ${!gone && (c.publish_status === "à valider" || c.publish_status === "approved") ? `<button type="button" class="btn" data-approve>${icon("thumbs-up", "i-sm")}Approuver ${kbd("a")}</button>` : ""}
    </div>`;
}

/* Compte de publication (SPEC-00d1 R4, SPEC-6076 R2) : les comptes prêts, à choisir à chaque publication (un style n'a plus de compte). */
async function clipFillAccounts(c, d) {
  const select = $("#clip-account", d), hint = $("[data-clip-account-hint]", d);
  try {
    const out = await api("/api/publish/accounts");
    const ready = out.accounts.filter((a) => a.ready_to_publish);
    const options = ready.map((a) => `<option value="${esc(a.id)}">${esc(clipsAccountText(a))}</option>`);
    options.unshift(`<option value="">Choisis un compte</option>`);
    if (!ready.length) hint.textContent = "Aucun compte prêt à publier : connecte-en un dans l'écran Comptes.";
    const chosen = select.value;
    select.innerHTML = options.join("");
    if (chosen && ready.some((a) => a.id === chosen)) select.value = chosen; // j / k gardent le compte choisi
  } catch (err) {
    select.innerHTML = `<option value="">Comptes indisponibles</option>`;
    hint.textContent = String(err.message || err);
  }
}

function parseHashtags(text) {
  return text.split(/[\s,]+/).filter(Boolean).map((t) => (t.startsWith("#") ? t : `#${t}`));
}

/* Remplace le clip dans la liste locale et rafraichit la galerie sous la fiche. */
function clipsReplace(updated) {
  const i = clipsUi.data.findIndex((c) => c.video_id === updated.video_id && c.clip_id === updated.clip_id);
  if (i >= 0) clipsUi.data[i] = updated;
  renderCurrent();
}

async function clipSaveCaption(c, description, hashtags, undoable) {
  const previous = { description: c.description, hashtags: c.hashtags };
  try {
    const out = await api(clipUrl(c), jsonBody("PATCH", { description, hashtags }));
    clipsReplace(out.clip);
    toast({
      kind: "ok", title: undoable ? "Texte enregistré" : "Texte rétabli", body: c.screen_title || c.clip_id,
      undo: undoable ? () => clipSaveCaption(out.clip, previous.description, previous.hashtags, false) : null,
    });
    return out.clip;
  } catch (err) {
    toastError("Impossible d'enregistrer le texte", err);
    return null;
  }
}

/* Ouvre (ou rapproche, si deja ouvert sur un autre clip) le tiroir de la coquille sur ce clip. `push` : nouvelle
   entree d'historique (clic dans la galerie) ; sinon l'adresse est remplacee (j / k, parties). */
function openClipDrawer(key, opts) {
  const o = Object.assign({ push: false, url: true }, opts);
  const c = clipsUi.data.find((x) => clipKey(x) === key);
  if (!c) return null;
  const first = clipsUi.open == null;
  clipsUi.open = key;
  if (o.url) {
    if (o.push) history.pushState(null, "", clipsClipHash(key));
    else history.replaceState(null, "", clipsClipHash(key));
  }
  const el = drawer.open(clipDrawerHtml(c), { onOpen: (d) => clipsWireDrawer(c, d) });
  if (!first) { const body = $(".drawer-body", el); if (body) body.scrollTop = 0; }
  renderCurrent(); // la carte ouverte est marquée dans la galerie
  return el;
}

/* Fermeture par l'ecran (apres une decision) : le tiroir se ferme, l'adresse revient a la liste. */
function closeClipDrawer() {
  drawer.close();
}

function clipsGo(dir) {
  if (!clipsUi.open) return;
  const next = clipsNeighbor(clipsUi.open, dir);
  if (next) openClipDrawer(next);
}

function clipsWireDrawer(c, d) {
  $$("[data-part]", d).forEach((b) => (b.onclick = () => openClipDrawer(b.dataset.part)));
  $("[data-prev]", d).onclick = () => clipsGo(-1);
  $("[data-next]", d).onclick = () => clipsGo(1);
  $("[data-save-caption]", d).onclick = () => clipSaveCaption(c, $("#clip-desc", d).value, parseHashtags($("#clip-tags", d).value), true);
  $("[data-save-title]", d).onclick = async () => {
    const title = $("#clip-title", d).value.trim();
    if (!title) { toast({ kind: "warn", title: "Titre d'écran vide", body: "Écris un titre avant de re-rendre." }); return; }
    if (!(await confirmDialog({ title: "Re-rendre ce clip ?", body: `Le titre « ${title} » relance le rendu puis le contrôle qualité de ce clip. Le fichier vidéo sera remplacé.`, confirmLabel: "Re-rendre", danger: false }))) return;
    try {
      await api(clipUrl(c), jsonBody("PATCH", { screen_title: title, confirm: true }));
      closeClipDrawer();
      toast({ kind: "ok", title: "Re-rendu en file", body: `${c.video_id} : rendu puis contrôle qualité.` });
    } catch (err) { toastError("Impossible de relancer le rendu", err); }
  };
  clipFillAccounts(c, d);
  const now = $("[data-publish-now]", d);
  // « Publier maintenant » ouvre le formulaire de l'ecran Publication, prerempli avec ce clip (SPEC-1ed3 R5)
  if (now) now.onclick = () => { closeClipDrawer(); setTimeout(() => pubOpenForm({ video_id: c.video_id, clip_id: c.clip_id }), 260); };
  const approve = $("[data-approve]", d);  // absent hors « à valider » / approved (revue fable-comptes 3)
  if (approve) approve.onclick = async () => {
    const chosen = $("#clip-account", d).value;
    if (!chosen) { toast({ kind: "warn", title: "Compte manquant", body: "Choisis le compte qui publiera ce clip." }); $("#clip-account", d).focus(); return; }
    if (!(await netGuard())) return;
    try {
      await api(clipUrl(c, "/approve"), jsonBody("POST", { account: chosen }));
      toast({ kind: "ok", title: "Clip approuvé", body: c.screen_title || c.clip_id });
      const next = clipsNeighbor(clipKey(c), 1);
      if (next) openClipDrawer(next); else closeClipDrawer(); // enchaîner les décisions (E4)
      loadClips();
    } catch (err) { toastError("Impossible d'approuver le clip", err); }
  };
  const reject = $("[data-reject]", d);
  if (reject) $("[data-reject]", d).onclick = async () => {
    const series = c.parts_total > 1;
    const ok = await confirmDialog(clipRejectConfirm(c));
    if (!ok) return;
    try {
      await api(clipUrl(c, "/reject"), { method: "POST" });
      toast({ kind: "warn", title: series ? "Série refusée" : "Clip refusé", body: c.screen_title || c.clip_id });
      const next = clipsNeighbor(clipKey(c), 1);
      if (next) openClipDrawer(next); else closeClipDrawer();
      loadClips();
    } catch (err) { toastError("Impossible de refuser le clip", err); }
  };
  const rerenderBtn = $("[data-rerender]", d);
  if (rerenderBtn) rerenderBtn.onclick = async () => {
    if (!(await confirmDialog({ title: "Re-rendre ce clip ?", body: "Le rendu puis le contrôle qualité de ce clip sont relancés ; le fichier vidéo sera remplacé.", confirmLabel: "Re-rendre", danger: false }))) return;
    try {
      await api(clipUrl(c, "/rerender"), { method: "POST" });
      closeClipDrawer();
      toast({ kind: "ok", title: "Re-rendu en file", body: `${c.video_id} : rendu puis contrôle qualité.` });
    } catch (err) { toastError("Impossible de relancer le rendu", err); }
  };
  $("[data-copy]", d).onclick = () => copyText(clipCaptionText(c), "Description et hashtags");
}

/* Le tiroir ferme (Échap, fond, bouton, décision) : l'adresse revient à la liste, la carte n'est plus marquée. */
document.addEventListener("clipper:drawer-close", () => {
  if (clipsUi.open == null) return;
  clipsUi.open = null;
  if (currentScreen === "clips" && clipsHashTarget().clip) history.replaceState(null, "", clipsListHash());
  if (currentScreen === "clips") renderCurrent();
});

/* ---------- Raccourcis (T11) : j k a r, seulement sur l'écran Clips avec le tiroir ouvert ----------
   clipsShortcut(key, ctx) est pure : rien dans un champ de saisie, rien avec un modificateur, rien tant qu'un
   dialogue est ouvert ; j / k suivant / précédent, a approuver, r refuser. Aucune animation sur ces actions. */
function clipsShortcut(key, ctx) {
  if (ctx.screen !== "clips" || !ctx.drawerOpen || ctx.typing || ctx.dialogOpen || ctx.modifier) return null;
  return { j: "next", k: "prev", a: "approve", r: "reject" }[key] || null;
}

function clipsOnKey(e) {
  const t = e.target;
  const act = clipsShortcut(e.key, {
    screen: currentScreen === "clips" ? "clips" : currentScreen,
    drawerOpen: drawer.isOpen() && clipsUi.open != null,
    typing: Boolean(t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)),
    dialogOpen: Boolean(document.querySelector("dialog[open]")),
    modifier: e.ctrlKey || e.metaKey || e.altKey,
  });
  if (!act) return;
  e.preventDefault();
  const d = $("#drawer");
  if (act === "next") clipsGo(1);
  else if (act === "prev") clipsGo(-1);
  else if (act === "approve") { const b = $("[data-approve]", d); if (b) b.click(); else toast({ kind: "info", title: "Rien à approuver", body: "Ce clip n'est ni « à valider » ni « approuvé ».", ms: 2600 }); }
  else if (act === "reject") { const b = $("[data-reject]", d); if (b) b.click(); }
}
document.addEventListener("keydown", clipsOnKey);

Screens.clips = {
  render(body) {
    const section = body.closest(".screen");
    if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
    const target = clipsHashTarget();
    const wanted = target.clip && clipsUi.hashVideo === null ? target.video : clipsHashVideo();
    // l'adresse a changé : le filtre vidéo la suit (lien depuis une fiche vidéo : tous les statuts) ; un clip dans
    // l'adresse ne touche pas au filtre, sauf à l'arrivée (lien partagé : la vidéo du clip, tous les statuts)
    if ((!target.clip || clipsUi.hashVideo === null) && wanted !== clipsUi.hashVideo) {
      clipsUi.hashVideo = wanted;
      clipsUi.video = wanted;
      clipsUi.shown = CLIPS_PAGE_SIZE;
      if (wanted) clipsUi.filter = "all";
    }
    if (Date.now() - clipsUi.at > CLIPS_STALE_MS) loadClips();
    if (!clipsUi.data) {
      morph(body, clipsUi.error
        ? emptyState("circle-alert", "Chargement impossible", String(clipsUi.error.message || clipsUi.error))
        : `<div class="toolbar"><div class="skeleton sk-seg"></div></div><div class="clips">${"<div><div class=\"skeleton clip-poster\"></div><div class=\"skeleton sk-line\"></div></div>".repeat(6)}</div>`);
      return;
    }
    clipsView(body);
    clipsWire(body);
    // tiroir selon l'adresse : #/clips/<v>/<c> ouvre ce clip (lien partagé, Retour), sans clip le tiroir se ferme
    const key = target.clip ? `${target.video}/${target.clip}` : null;
    if (key && key !== clipsUi.open) {
      if (!openClipDrawer(key, { url: false })) toast({ kind: "warn", title: "Clip introuvable", body: `${key} n'est pas (ou plus) dans la liste des clips.` });
    } else if (!key && clipsUi.open != null) {
      clipsUi.open = null;
      drawer.close();
    }
  },
};
