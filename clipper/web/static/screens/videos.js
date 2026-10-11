/* Écran « Vidéos » v3 (SPEC-7715 E2 / T2 / T7 / T9, ADR-a308, SPEC-74e9 §4.3) : une table dense triée comme le
   serveur la renvoie (texte, statut et style filtrés côté serveur), et la fiche d'une vidéo (#/videos/<id>) avec
   son stepper des 13 étapes, la bande de ses clips et son journal en table. Tout est rendu par rapprochement du
   DOM (morph) : le focus d'un champ de filtre ne se perd jamais, et un événement SSE ne remplace rien.
   L'ajout d'une vidéo passe par le dialogue global (bouton [data-add] branché par app.js, touche « n »).
   Aucune logique vidéo ici : relancer, annuler, reprendre et purger sont des appels d'API confirmés.
   Chargé après screens.js (qui définit Screens, STEP_LABELS, STATUS_LABELS, emptyState, videoThumb) ; remplace
   l'entrée Screens.videos de la coquille. */
"use strict";

const VIDEOS_STEPS = Object.keys(STEP_LABELS);
const VIDEOS_STALE_MS = 4000; // le polling de repli (5 s) rafraîchit au-delà
const VIDEOS_STEP_ICON = { done: "check", failed: "x", running: "loader", pending: "clock", queued: "circle-pause" };
const VIDEOS_TABS = [["", "Toutes"], ["running", "En cours"], ["queued", "En attente"], ["awaiting_review", "À valider"],
  ["failed", "Échecs"], ["interrupted", "Interrompues"], ["done", "Terminées"]];
const VIDEOS_CHIP = { running: "accent", queued: "info", awaiting_review: "warn", failed: "bad", interrupted: "bad", done: "ok", pending: "pending" };
const VIDEOS_STRIP_MAX = 12; // la bande de la fiche montre les 12 premiers clips, « Voir les N clips » ouvre la galerie
const VIDEOS_RUBRICS = { builtin: "Standard", "builtin:gaming": "Gaming", "builtin:gaming-action": "Gaming action" };

const videosState = {
  filters: { channel: "", status: "", q: "" },
  list: null, listAt: 0, listBusy: false, listAgain: false, listError: null, disk: null, sel: -1,
  channelsAsked: false,
  detailId: null, detail: null, detailAt: 0, detailBusy: false, detailAgain: false, detailError: null,
  events: [], step: null, menu: null, follow: true, logQ: "", clips: null, clipsFor: null,
  jury: null, juryFor: null, juryUi: { key: null, round: null },
  purging: null, openLogs: new Set(),
};

/* ---------- Formats ---------- */

/* Octets en unités lisibles (Ko/Mo/Go, base 1024). */
function videosBytes(n) {
  if (n == null) return "taille inconnue";
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${i === 0 ? v : v.toFixed(v < 10 ? 2 : 1).replace(".", ",")} ${units[i]}`;
}

function videosDur(seconds) {
  if (seconds == null) return null;
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}

const videosClock = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? esc(iso) : d.toLocaleTimeString("fr-FR", { timeZone: CLIPPER_TZ });
};

const videosWhen = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : fmtParis(iso, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

const videosHashId = () => {
  const hit = location.hash.match(/^#\/videos\/([^/?#]+)/);
  return hit ? decodeURIComponent(hit[1]) : null;
};

const videosStatusLabel = (status) => (status === "interrupted" ? "interrompue" : STATUS_LABELS[status] || status);

/* « interrompue » (serveur ou PC arrêté en plein traitement) n'est pas un statut de pipeline.json : l'API le déduit
   d'une étape « running » sans processus. Ni « en cours » ni « en échec » : une pastille à part, rouge. */
const videosChip = (status) => `<span class="chip ${VIDEOS_CHIP[status] || "pending"}">${esc(videosStatusLabel(status))}</span>`;

/* Libellé d'une grille de notation : jamais un chemin (SPEC-9216 R4 : moments.json garde le chemin du fichier). */
function rubricLabel(path) {
  const value = String(path == null ? "" : path).trim();
  if (Object.prototype.hasOwnProperty.call(VIDEOS_RUBRICS, value)) return VIDEOS_RUBRICS[value];
  if (value.startsWith("builtin:")) return `Grille embarquée ${value.slice("builtin:".length)}`;
  const name = value.split(/[\\/]/).filter(Boolean).pop() || "inconnue";
  return `Fichier personnalisé (${name})`;
}

/* ---------- Liste : requête serveur, ligne de table ---------- */

const videosListUrl = () => {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(videosState.filters)) if (v) params.set(k, v);
  return `/api/videos?${params.toString()}`;
};

function videosSetFilter(key, value) {
  videosState.filters[key] = value;
  videosState.sel = -1;
}

async function videosLoad() {
  const s = videosState;
  if (s.listBusy) { s.listAgain = true; return; }
  s.listBusy = true;
  try {
    const list = await api(videosListUrl());
    if (!Array.isArray(list)) throw new Error("réponse inattendue de /api/videos");
    s.list = list;
    s.listError = null;
  } catch (err) {
    s.listError = err;
    toastError("Liste des vidéos indisponible", err);
  } finally {
    s.listBusy = false;
    s.listAt = Date.now();
  }
  videosLoadDisk();
  if (currentScreen === "videos") renderCurrent();
  if (s.listAgain) { s.listAgain = false; videosLoad(); }
}

async function videosLoadDisk() {
  try {
    videosState.disk = await api("/api/disk");
  } catch (err) {
    videosState.disk = { error: String(err.message || err) };
  }
  if (currentScreen === "videos" && !videosState.detailId) renderCurrent();
}

async function videosEnsureChannels() {
  if (videosState.channelsAsked) return;
  videosState.channelsAsked = true;
  try {
    store.channels = await api("/api/channels");
  } catch (err) {
    videosState.channelsAsked = false;
    toastError("Styles indisponibles", err);
    return;
  }
  if (currentScreen === "videos") renderCurrent();
}

function videosStepStatus(video, name) {
  return ((video.steps || {})[name] || {}).status || "pending";
}

/* Cellule « Étape » : étape courante avec sa fraction et son reste à faire, plus une barre (transform seulement) ;
   une vidéo terminée dit seulement « 13/13 étapes ». */
function videosStepCell(video) {
  const steps = video.steps || {};
  const total = VIDEOS_STEPS.length;
  const doneN = VIDEOS_STEPS.filter((n) => videosStepStatus(video, n) === "done").length;
  if (video.status === "done") return `<span class="muted">${doneN}/${total} étapes</span>`;
  const cur = video.current_step;
  if (!cur) return "";
  const live = (steps[cur] || {}).status === "running" ? (steps[cur] || {}).progress : null;
  const frac = live ? live.fraction : doneN / total;
  const pct = live ? ` · ${Math.round(live.fraction * 100)} %` : "";
  const eta = live && live.eta_s != null ? ` · reste ${videosDur(live.eta_s)}` : "";
  return `<span class="t2">${esc(STEP_LABELS[cur] || cur)}${pct}${eta}</span><div class="bar" style="--p:${+frac.toFixed(3)}"><i></i></div>`;
}

function videoRow(video, index) {
  const id = encodeURIComponent(video.video_id);
  const clips = (video.clips || []).length;
  return `<tr class="clickable" tabindex="0" data-video="${esc(video.video_id)}" aria-selected="${index === videosState.sel}">
    <td><div class="td-title">${videoThumb(video.video_id, video.platform_thumbnail)}<div style="min-width:0">
      <a class="t" href="#/videos/${id}">${esc(video.title || video.video_id)}</a>
      <div class="s"><span class="mono">${esc(video.video_id)}</span>${video.dismissed_at ? `<span class="chip pending plain">retirée</span>` : ""}${video.purged ? `<span class="chip pending plain">source purgée</span>` : ""}</div>
      ${video.reason ? `<div class="vreason-line${video.status === "failed" || video.status === "interrupted" ? " bad" : ""}" title="${esc(video.reason)}">${esc(video.reason)}</div>` : ""}
    </div></div></td>
    <td data-l="Style">${video.channel ? `<span class="tag">${esc(video.channel)}</span>` : `<span class="muted">sans style</span>`}</td>
    <td data-l="Statut">${videosChip(video.status)}</td>
    <td data-l="Étape" class="vstep-cell">${videosStepCell(video)}</td>
    <td data-l="Clips" class="r num">${clips || (video.status === "done" ? "0" : "")}</td>
    <td data-l="Mise à jour" class="r muted">${esc(videosWhen(video.updated_at))}</td></tr>`;
}

const VIDEOS_HEAD = `<colgroup><col style="width:40%"><col style="width:12%"><col style="width:12%"><col style="width:18%"><col style="width:7%"><col style="width:11%"></colgroup>
  <thead><tr><th>Vidéo</th><th>Style</th><th>Statut</th><th>Étape</th><th class="r">Clips</th><th class="r">Mise à jour</th></tr></thead>`;

const videosTabCount = (key) => (store.videos || []).filter((v) => (!videosState.filters.channel || v.channel === videosState.filters.channel) && (!key || v.status === key)).length;

function videosListBody(list, filtered) {
  const s = videosState;
  if (s.listError && list === null) {
    return `<div class="alert" data-variant="destructive" role="alert">${icon("circle-alert")}<h2>Liste indisponible</h2><section>${esc(String(s.listError.message || s.listError))}
      <div><button type="button" class="btn" data-variant="outline" data-size="sm" data-videos-retry>${icon("rotate-ccw", "i-xs")}Réessayer</button></div></section></div>`;
  }
  if (list === null) {
    return `<div class="table-container"><table class="table">${VIDEOS_HEAD}<tbody>${Array.from({ length: 5 }, () => `<tr><td colspan="6"><div class="skeleton sk-line" style="width:70%"></div></td></tr>`).join("")}</tbody></table></div>`;
  }
  if (!list.length) {
    return filtered
      ? emptyState("search", "Aucune vidéo ne correspond", "Change le filtre de statut ou le texte, ou ajoute une vidéo.")
      : emptyState("film", "Aucune vidéo", "Ajoute une vidéo par son adresse pour lancer un premier traitement.", `<button type="button" class="btn" data-size="sm" data-add>${icon("plus", "i-sm")}Ajouter une vidéo</button>`);
  }
  return `<div class="table-container"><table class="table" id="videos-table">${VIDEOS_HEAD}<tbody>${list.map(videoRow).join("")}</tbody></table></div>`;
}

function videosListView() {
  const s = videosState;
  const list = s.list;
  const filtered = Object.values(s.filters).some(Boolean);
  const channels = Array.isArray(store.channels) ? store.channels : [];
  const doneN = videosTabCount("done");
  const disk = s.disk && !s.disk.error ? ` · workspace ${videosBytes(s.disk.workspace_bytes)}` : "";
  const purge = s.purging
    ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-purge-completed disabled>${icon("loader", "i-xs")}${esc(s.purging)}</button>`
    : `<button type="button" class="btn" data-variant="outline" data-size="sm" data-purge-completed>${icon("trash-2", "i-xs")}Purger les terminées${doneN ? `<span class="n">${doneN}</span>` : ""}</button>`;
  const tabs = VIDEOS_TABS.map(([key, label]) => {
    const n = videosTabCount(key);
    return `<button type="button" data-status="${key}" aria-pressed="${s.filters.status === key}">${esc(label)}${store.videos && (n || !key) ? `<span class="n">${n}</span>` : ""}</button>`;
  }).join("");
  // l'ajout passe par le bouton « Ajouter une vidéo » de la barre du haut (dialogue global, touche n) ; l'état vide le propose aussi
  return `<div class="toolbar">
      <label class="input-ico">${icon("search", "i-sm")}<input class="input vsearch" type="search" name="q" data-screen-search placeholder="Titre, identifiant ou adresse" aria-label="Filtrer par texte" value="${esc(s.filters.q)}"><kbd class="kbd">/</kbd></label>
      <select class="input" name="filter-channel" aria-label="Filtrer par style"><option value="">Tous les styles</option>${channels.map((c) => `<option value="${esc(c)}"${c === s.filters.channel ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>
      <span class="grow"></span>
      <span class="muted vcount">${list ? `${list.length} vidéo${list.length > 1 ? "s" : ""}` : "Chargement…"}${disk}</span>
      ${purge}
    </div>
    <div class="toolbar"><div class="seg" role="radiogroup" aria-label="Statut">${tabs}</div></div>
    ${videosListBody(list, filtered)}`;
}

/* ---------- Purge disque (TASK-886a) : la taille d'abord, la confirmation ensuite ---------- */

async function videosPurge(id, withClips) {
  const enc = encodeURIComponent(id);
  let plan;
  try { plan = await api(`/api/purge/${enc}${withClips ? "?clips=true" : ""}`); }
  catch (err) { toastError("Purge impossible", err); return; }
  const ok = await confirmDialog({
    title: withClips ? "Purger la vidéo et ses clips ?" : "Purger cette vidéo ?",
    body: `${videosBytes(plan.total_bytes)} seront libérés (source téléchargée, audio, images)${withClips ? `, plus ${videosBytes(plan.clips_bytes)} de clips rendus : irréversible` : ""}. La vidéo reste listée avec son état${withClips ? "" : " et ses clips"} ; pour la retraiter, il faudra la retélécharger.`,
    confirmLabel: "Purger",
  });
  if (!ok) return;
  try {
    const done = await api(`/api/purge/${enc}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clips: Boolean(withClips) }) });
    toast({ kind: "ok", title: "Vidéo purgée", body: `${id} : ${videosBytes(done.freed_bytes)} libérés` });
    await loadVideos();
    videosLoad();
    if (videosState.detailId) videosRefreshDetail();
  } catch (err) { toastError("Purge impossible", err); }
}

/* Une seule purge groupée à la fois : le bouton reste désactivé et dit où elle en est (état de l'écran, repeint par morph). */
async function videosPurgeCompleted() {
  const s = videosState;
  if (s.purging) return;
  const paint = (label) => { s.purging = label; if (currentScreen === "videos") renderCurrent(); };
  try {
    paint("Calcul de l'espace libérable…");
    let plan;
    try { plan = await api("/api/purge-completed"); }
    catch (err) { toastError("Purge impossible", err); return; }
    if (!plan.videos.length) { toast({ kind: "ok", title: "Rien à purger", body: "Aucune vidéo terminée n'a de fichiers lourds." }); return; }
    paint(null);
    const ok = await confirmDialog({
      title: "Purger les vidéos terminées ?",
      body: `${plan.videos.length} vidéo${plan.videos.length > 1 ? "s" : ""} terminée${plan.videos.length > 1 ? "s" : ""} : ${videosBytes(plan.total_bytes)} seront libérés. Les clips rendus sont gardés ; pour retraiter une vidéo purgée, il faudra la retélécharger.`,
      confirmLabel: "Purger",
    });
    if (!ok) return;
    paint("Purge en cours…");
    try {
      const done = await api("/api/purge-completed", { method: "POST" });
      toast({ kind: "ok", title: "Vidéos purgées", body: `${done.videos.length} vidéo(s) : ${videosBytes(done.freed_bytes)} libérés${done.skipped.length ? ` · ${done.skipped.length} ignorée(s)` : ""}` });
      await loadVideos();
      videosLoad();
    } catch (err) { toastError("Purge impossible", err); }
  } finally {
    s.purging = null;
    if (currentScreen === "videos") renderCurrent();
  }
}

/* ---------- Fiche d'une vidéo ---------- */

function videosOpenDetail(id) {
  const s = videosState;
  s.detailId = id;
  s.detail = null; s.detailAt = 0; s.detailError = null;
  s.events = []; s.step = null; s.menu = null; s.logQ = ""; s.follow = true;
  s.clips = null; s.clipsFor = null; s.openLogs.clear();
}

async function videosRefreshDetail() {
  const s = videosState;
  const id = s.detailId;
  if (!id) return;
  if (s.detailBusy) { s.detailAgain = true; return; }
  s.detailBusy = true;
  try {
    const enc = encodeURIComponent(id);
    const query = new URLSearchParams();
    if (s.events.length) query.set("since", s.events[s.events.length - 1].at);
    const [detail, fresh] = await Promise.all([api(`/api/videos/${enc}`), api(`/api/videos/${enc}/events?${query.toString()}`)]);
    if (s.detailId !== id) return; // l'utilisateur a changé de vidéo entre-temps
    s.detail = detail;
    s.detailError = null;
    s.events = s.events.concat(Array.isArray(fresh) ? fresh : []);
  } catch (err) {
    if (s.detailId === id) s.detailError = err;
    toastError("Fiche vidéo indisponible", err);
  } finally {
    s.detailBusy = false;
    if (s.detailId === id) s.detailAt = Date.now();
  }
  if (s.detailId === id && currentScreen === "videos") renderCurrent();
  if (s.detailAgain) { s.detailAgain = false; videosRefreshDetail(); }
}

/* Clips de la vidéo pour la bande de la fiche : GET /api/videos/<id>/clips, relu quand leur nombre change. */
async function videosLoadClips(video) {
  const s = videosState;
  const key = `${video.video_id}|${(video.clips || []).length}`;
  if (s.clipsFor === key) return;
  s.clipsFor = key;
  try {
    const clips = await api(`/api/videos/${encodeURIComponent(video.video_id)}/clips`);
    if (s.clipsFor !== key) return;
    s.clips = Array.isArray(clips) ? clips : [];
  } catch (err) {
    if (s.clipsFor !== key) return;
    s.clips = [];
    toastError("Clips de la vidéo indisponibles", err);
  }
  if (s.detailId === video.video_id && currentScreen === "videos") renderCurrent();
}

function videosStepTime(video, name) {
  const step = (video.steps || {})[name] || {};
  if (step.status === "running") return step.progress ? `${Math.round(step.progress.fraction * 100)} %` : "en cours";
  if (step.status === "done") return videosDur((video.durations || {})[name]) || "durée inconnue";
  if (step.status === "failed") return "échec";
  return "en attente";
}

/* Stepper des 13 étapes : un bouton par étape, dans l'ordre du pipeline ; l'étape choisie est aria-pressed. */
function stepperHtml(video, selected) {
  return `<div class="stepper" role="group" aria-label="Étapes du traitement">${VIDEOS_STEPS.map((n) => {
    const st = videosStepStatus(video, n);
    return `<button type="button" class="step ${st}" data-step="${n}" aria-pressed="${n === selected}"><span class="dot">${icon(VIDEOS_STEP_ICON[st] || "clock")}</span><span class="lbl">${esc(STEP_LABELS[n])}</span><span class="dur">${esc(videosStepTime(video, n))}</span></button>`;
  }).join("")}</div>`;
}

/* Radar du jury (TASK-3a08) : GET /api/videos/{id}/jury, relu quand l'étape Moments se termine. */
function videosJuryBody() {
  const s = videosState;
  if (s.jury === null) return `<div class="skeleton sk-card"></div>`;
  if (s.jury.error) return `<div class="jr-empty"><b>Jury indisponible.</b> ${esc(s.jury.error)}</div>`;
  return juryPanelHtml(s.jury, s.juryUi);
}

async function videosLoadJury(video) {
  const s = videosState;
  const key = `${video.video_id}|${((video.steps || {}).moments || {}).finished_at || ""}`;
  if (s.juryFor === key) return;
  s.juryFor = key;
  s.jury = null;
  s.juryUi = { key: null, round: null };
  try {
    const data = await api(`/api/videos/${encodeURIComponent(video.video_id)}/jury`);
    if (s.juryFor !== key) return;
    s.jury = data;
  } catch (err) {
    if (s.juryFor !== key) return;
    s.jury = { error: String(err.message || err) };
  }
  if (s.detailId === video.video_id && s.step === "moments" && currentScreen === "videos") renderCurrent();
}

function videosStepPanel(video) {
  const name = videosState.step;
  const step = (video.steps || {})[name] || {};
  const st = videosStepStatus(video, name);
  const progress = step.progress;
  const dur = videosDur((video.durations || {})[name]);
  const canRetry = (st === "done" || st === "failed") && video.status !== "running" && video.status !== "interrupted";
  return `<div class="vstep-panel">
    <div class="section-head"><h3>${esc(STEP_LABELS[name])}</h3><span class="chip ${VIDEOS_CHIP[st] || "pending"}">${esc(videosStatusLabel(st))}</span>
      ${canRetry ? `<div class="right"><button type="button" class="btn" data-variant="outline" data-size="sm" data-retry="${name}">${icon("rotate-ccw", "i-sm")}Relancer depuis cette étape</button></div>` : ""}</div>
    <dl class="kv">
      <dt>Durée</dt><dd class="mono">${dur ? esc(dur) : (st === "done" ? "inconnue : horodatage absent de pipeline.json" : "pas terminée")}</dd>
      ${step.started_at ? `<dt>Début</dt><dd class="mono">${videosClock(step.started_at)}</dd>` : ""}
      ${step.finished_at ? `<dt>Fin</dt><dd class="mono">${videosClock(step.finished_at)}</dd>` : ""}
      ${progress ? `<dt>Progression</dt><dd>${Math.round(progress.fraction * 100)} %${progress.eta_s != null ? ` · reste ${esc(videosDur(progress.eta_s))}` : ""}${progress.message ? ` · ${esc(progress.message)}` : ""}
        <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(progress.fraction * 100)}" style="--p:${+progress.fraction.toFixed(3)}"><i></i></div></dd>` : ""}
      ${step.reason ? `<dt>Raison</dt><dd class="vreason">${esc(step.reason)}</dd>` : ""}
    </dl>
    ${name === "moments" && st === "done" ? `<div class="vjury"><h3>Jury par moment</h3>${videosJuryBody()}</div>` : ""}
  </div>`;
}

/* Niveau normalisé en trois classes : info, warning, error (critical compte comme error). */
function videosLevel(level) {
  const l = String(level || "info").toLowerCase();
  return l === "error" || l === "critical" ? "error" : l === "warning" || l === "warn" ? "warning" : "info";
}

/* Clé stable d'une ligne du journal : sert à garder « dépliée » une ligne malgré les repeints. */
const videosLogKey = (event) => `${event.at}|${event.step || ""}|${String(event.message || "").length}`;

/* Message replié sur deux lignes (le journal d'une vidéo terminée porte un récapitulatif de plusieurs Ko) ; un clic le déplie. */
function journalRow(event) {
  const level = videosLevel(event.level);
  const key = videosLogKey(event);
  return `<tr data-log="${esc(key)}"><td class="mono">${videosClock(event.at)}</td><td><span class="lvl ${level}">${esc(level)}</span></td><td class="mono muted">${esc(event.step || "")}</td><td class="wrap"><div class="vmsg${videosState.openLogs.has(key) ? " open" : ""}">${esc(event.message)}</div></td></tr>`;
}

function videosJournal(video) {
  const s = videosState;
  const q = s.logQ.trim().toLowerCase();
  const rows = s.events.filter((e) => !q || `${e.level || ""} ${e.step || ""} ${e.message || ""}`.toLowerCase().includes(q));
  const body = rows.length
    ? rows.map(journalRow).join("")
    : `<tr><td colspan="4" class="muted wrap">${s.events.length ? "Aucun événement ne correspond au filtre." : "Aucun événement pour l'instant."}</td></tr>`;
  return `<section class="vsec"><div class="section-head"><h2>Journal</h2><span class="mono muted vpath">workspace/${esc(video.video_id)}/events.jsonl</span>
      <div class="right">
        <label class="input-ico">${icon("search", "i-sm")}<input class="input vlogq" type="search" name="log-q" placeholder="Filtrer" aria-label="Filtrer le journal" value="${esc(s.logQ)}"></label>
        <div class="vfollow"><input id="videos-follow" type="checkbox" role="switch" data-follow${s.follow ? " checked" : ""}><label for="videos-follow">Suivre en direct</label></div>
        <button type="button" class="btn" data-variant="ghost" data-size="sm" data-log-copy>${icon("copy", "i-sm")}Copier</button>
      </div></div>
    <div class="table-container vlog" role="log" aria-live="polite"><table class="table"><colgroup><col style="width:96px"><col style="width:96px"><col style="width:130px"><col></colgroup>
      <thead><tr><th>Heure</th><th>Niveau</th><th>Étape</th><th>Message</th></tr></thead><tbody>${body}</tbody></table></div></section>`;
}

function videosClipsStrip(video) {
  const n = (video.clips || []).length;
  if (!n) return "";
  const enc = encodeURIComponent(video.video_id);
  const clips = videosState.clips;
  const items = clips === null
    ? Array.from({ length: Math.min(n, 6) }, () => `<div class="skeleton poster"></div>`).join("")
    : clips.slice(0, VIDEOS_STRIP_MAX).map((c) => `<a class="poster" href="#/clips/${enc}/${encodeURIComponent(c.clip_id)}" title="${esc(c.screen_title || c.clip_id)}">${c.thumbnail_url ? `<img loading="lazy" decoding="async" src="${esc(c.thumbnail_url)}" alt="">` : `<span class="ph">aperçu</span>`}</a>`).join("");
  return `<section class="vsec"><div class="section-head"><h2>Clips</h2><span class="n">${n}</span>
      <div class="right"><a class="btn" data-variant="ghost" data-size="sm" href="#/clips/${enc}">Voir les ${n} clips ${icon("arrow-right", "i-sm")}</a></div></div>
    <div class="strip">${items}</div></section>`;
}

const videosBack = () => `<a class="vback" href="#/videos">${icon("chevron-left", "i-xs")}Toutes les vidéos</a>`;

function videosActions(video) {
  const s = videosState;
  const enc = encodeURIComponent(video.video_id);
  const id = esc(video.video_id);
  const idle = video.status !== "running" && video.status !== "interrupted";
  const review = video.status === "awaiting_review" || (video.awaiting || []).length;
  const retrySteps = VIDEOS_STEPS.filter((n) => ["done", "failed"].includes(videosStepStatus(video, n)));
  const retryMenu = idle && retrySteps.length
    ? `<div class="vmenu-wrap"><button type="button" class="btn" data-variant="outline" data-size="sm" data-retry-menu aria-haspopup="menu" aria-expanded="${s.menu === "retry"}">${icon("rotate-ccw", "i-sm")}Relancer depuis…</button>
        ${s.menu === "retry" ? `<div class="vmenu" role="menu">${retrySteps.map((n) => `<button type="button" role="menuitem" data-retry-step="${n}">${esc(STEP_LABELS[n])}</button>`).join("")}</div>` : ""}</div>`
    : "";
  return `${review ? `<a class="btn" data-size="sm" href="#/review/${enc}">${icon("sparkles", "i-sm")}Revoir les moments${(video.awaiting || []).length ? ` (${video.awaiting.length})` : ""}</a>` : ""}
    ${(video.clips || []).length ? `<a class="btn" data-variant="outline" data-size="sm" href="#/clips/${enc}">${icon("clapperboard", "i-sm")}Voir les ${video.clips.length} clips</a>` : ""}
    ${retryMenu}
    ${!video.channel && idle ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-assign-channel="${id}">${icon("tv", "i-sm")}Attribuer un style</button>` : ""}
    ${video.status === "interrupted" ? `<button type="button" class="btn" data-size="sm" data-resume-video>${icon("play", "i-sm")}Reprendre</button>` : ""}
    ${video.status === "running" || video.status === "interrupted" ? `<button type="button" class="btn" data-variant="destructive" data-size="sm" data-cancel-video>${icon("ban", "i-sm")}Annuler le traitement</button>` : ""}
    ${(video.status === "failed" || video.status === "queued") && !video.dismissed_at ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-retry-video="${id}" data-from-step="${esc(video.current_step || "")}">${icon("rotate-ccw", "i-sm")}Relancer</button>
      <button type="button" class="btn" data-variant="ghost" data-size="sm" data-dismiss-video="${id}">Retirer</button>` : ""}
    ${video.dismissed_at ? `<span class="chip pending plain">retirée des échecs</span><button type="button" class="btn" data-variant="outline" data-size="sm" data-restore-video="${id}">Rétablir</button>` : ""}
    ${video.purged ? `<span class="chip pending plain">source purgée : retélécharger</span>` : `<button type="button" class="btn" data-variant="outline" data-size="sm" data-purge-video="${id}">${icon("trash-2", "i-sm")}Purger</button>`}
    ${video.status === "done" && !video.purged ? `<button type="button" class="btn" data-variant="ghost" data-size="sm" data-purge-video-clips="${id}">Purger aussi les clips</button>` : ""}`;
}

function videosDetailView() {
  const s = videosState;
  const video = s.detail;
  if (!video) {
    if (s.detailError) {
      return `${videosBack()}<div class="alert" data-variant="destructive" role="alert">${icon("circle-alert")}<h2>Vidéo indisponible</h2><section>${esc(String(s.detailError.message || s.detailError))}
        <div><button type="button" class="btn" data-variant="outline" data-size="sm" data-videos-retry>${icon("rotate-ccw", "i-xs")}Réessayer</button></div></section></div>`;
    }
    return `${videosBack()}<div class="skeleton sk-card"></div>`;
  }
  if (!s.step || !VIDEOS_STEPS.includes(s.step)) s.step = video.current_step || VIDEOS_STEPS[VIDEOS_STEPS.length - 1];
  const doneN = VIDEOS_STEPS.filter((n) => videosStepStatus(video, n) === "done").length;
  const total = VIDEOS_STEPS.reduce((t, n) => t + ((video.durations || {})[n] || 0), 0);
  const failed = video.status === "failed" || video.status === "interrupted";
  return `${videosBack()}
    <div class="vhead">
      <div class="vhead-main">
        ${videoThumb(video.video_id, video.platform_thumbnail)}
        <div style="min-width:0">
          <h2 class="vtitle">${esc(video.title)}</h2>
          ${video.title_reason ? `<p class="muted vsub">${esc(video.title_reason)}</p>` : ""}
          <div class="vmeta">${videosChip(video.status)}<button type="button" class="mono vcopy" data-copy="${esc(video.video_id)}" title="Copier l'identifiant">${esc(video.video_id)} ${icon("copy", "i-xs")}</button>${video.channel ? `<span class="tag">${esc(video.channel)}</span>` : `<span class="muted">sans style</span>`}${video.rubric ? `<span class="muted">Grille : ${esc(rubricLabel(video.rubric))}</span>` : ""}${video.short_clips ? `<span class="muted">Clips courts : ${video.short_clips.on ? `oui (${esc(video.short_clips.min)}-${esc(video.short_clips.max)} s)` : "non"}</span>` : ""}${video.source_url ? `<a class="mono vsource" href="${esc(video.source_url)}" target="_blank" rel="noopener">${esc(video.source_url)}</a>` : ""}</div>
        </div>
      </div>
      <div class="vactions">${videosActions(video)}</div>
    </div>
    ${video.reason ? `<div class="alert"${failed ? ` data-variant="destructive"` : ""}${failed ? ` role="alert"` : ""}>${icon(failed ? "circle-alert" : "info")}<h2>${failed ? "Échec" : "Information"}</h2><section>${esc(video.reason)}${video.retry_at ? ` · nouvelle tentative prévue ${videosClock(video.retry_at)}` : ""}</section></div>` : ""}
    <section class="vcard"><div class="section-head"><h2>Progression</h2><span class="muted">${doneN} / ${VIDEOS_STEPS.length} étapes · ${videosDur(total) || "0 s"} de calcul</span></div>
      ${stepperHtml(video, s.step)}
      ${videosStepPanel(video)}
    </section>
    ${videosClipsStrip(video)}
    ${videosJournal(video)}`;
}

/* ---------- Câblage (après chaque morph : les nœuds neufs n'ont pas encore leurs gestionnaires) ---------- */

/* Relancer : remet cette étape et les suivantes à zéro (SPEC-74e9 3.3). */
async function videosRetryFrom(video, step) {
  const after = VIDEOS_STEPS.slice(VIDEOS_STEPS.indexOf(step) + 1).map((n) => STEP_LABELS[n]);
  const ok = await confirmDialog({
    title: `Relancer depuis « ${STEP_LABELS[step]} » ?`,
    body: `Cette étape repart de zéro${after.length ? `, ainsi que : ${after.join(", ")}` : ""}. Les clips déjà rendus de cette vidéo seront remplacés.`,
    confirmLabel: "Relancer", danger: false,
  });
  if (!ok) return;
  try {
    await api(`/api/videos/${encodeURIComponent(video.video_id)}/retry`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ from_step: step }) });
    toast({ kind: "ok", title: "Relance mise en file", body: `${video.video_id} : depuis « ${STEP_LABELS[step]} »` });
    await Promise.all([loadVideos(), loadQueue()]);
    videosRefreshDetail();
  } catch (err) { toastError("Relance impossible", err); }
}

/* Reprendre : remet la vidéo interrompue en file, elle repart de l'étape interrompue (les étapes finies restent). */
async function videosResume(video) {
  const ok = await confirmDialog({
    title: "Reprendre le traitement ?",
    body: `${video.video_id} repart de l'étape « ${STEP_LABELS[video.current_step] || video.current_step} » ; les étapes déjà terminées ne sont pas refaites.`,
    confirmLabel: "Reprendre", danger: false,
  });
  if (!ok) return;
  try {
    await api(`/api/videos/${encodeURIComponent(video.video_id)}/resume`, { method: "POST" });
    toast({ kind: "ok", title: "Reprise mise en file", body: video.video_id });
    await Promise.all([loadVideos(), loadQueue()]);
    videosRefreshDetail();
  } catch (err) { toastError("Reprise impossible", err); }
}

async function videosCancel(video) {
  const ok = await confirmDialog({ title: "Annuler le traitement ?", body: `Le traitement de ${video.video_id} sera arrêté.`, confirmLabel: "Annuler le traitement" });
  if (!ok) return;
  try {
    await api(`/api/videos/${encodeURIComponent(video.video_id)}/cancel`, { method: "POST" });
    toast({ kind: "ok", title: "Traitement annulé", body: video.video_id });
    await loadVideos();
    updateCounts();
    videosRefreshDetail();
  } catch (err) { toastError("Annulation impossible", err); }
}

function videosWireList(body) {
  $$("[data-status]", body).forEach((b) => (b.onclick = () => { videosSetFilter("status", b.dataset.status); renderCurrent(); videosLoad(); }));
  const channel = $('[name="filter-channel"]', body);
  if (channel) channel.onchange = () => { videosSetFilter("channel", channel.value); renderCurrent(); videosLoad(); };
  const q = $('[name="q"]', body);
  let timer = null;
  // le texte est gardé tout de suite (le prochain morph ne l'écrase pas), seule la requête serveur est différée
  if (q) q.oninput = () => { videosSetFilter("q", q.value.trim()); clearTimeout(timer); timer = setTimeout(videosLoad, 250); };
  $$("tr.clickable", body).forEach((tr) => (tr.onclick = (e) => {
    if (e.target.closest("a, button")) return;
    location.hash = `#/videos/${encodeURIComponent(tr.dataset.video)}`;
  }));
  const purge = $("[data-purge-completed]", body);
  if (purge) purge.onclick = videosPurgeCompleted;
}

function videosWireDetail(body, video) {
  const s = videosState;
  $$("[data-step]", body).forEach((b) => (b.onclick = () => { s.step = b.dataset.step; renderCurrent(); }));
  $$("[data-jr-pick]", body).forEach((b) => (b.onclick = () => { s.juryUi = { key: b.dataset.jrPick, round: null }; renderCurrent(); }));
  $$("[data-jr-round]", body).forEach((b) => (b.onclick = () => { s.juryUi = { ...s.juryUi, round: Number(b.dataset.jrRound) }; renderCurrent(); }));
  const menu = $("[data-retry-menu]", body);
  if (menu) menu.onclick = () => { s.menu = s.menu === "retry" ? null : "retry"; renderCurrent(); };
  $$("[data-retry-step]", body).forEach((b) => (b.onclick = () => { s.menu = null; renderCurrent(); videosRetryFrom(video, b.dataset.retryStep); }));
  const retry = $("[data-retry]", body);
  if (retry) retry.onclick = () => videosRetryFrom(video, retry.dataset.retry);
  const cancel = $("[data-cancel-video]", body);
  if (cancel) cancel.onclick = () => videosCancel(video);
  const resume = $("[data-resume-video]", body);
  if (resume) resume.onclick = () => videosResume(video);
  $$("[data-purge-video]", body).forEach((b) => (b.onclick = () => videosPurge(b.dataset.purgeVideo, false)));
  $$("[data-purge-video-clips]", body).forEach((b) => (b.onclick = () => videosPurge(b.dataset.purgeVideoClips, true)));
  const assign = $("[data-assign-channel]", body);
  if (assign) assign.onclick = () => openAssignChannel(video.video_id);
  $$("[data-copy]", body).forEach((b) => (b.onclick = () => copyText(b.dataset.copy, "Identifiant")));
  const copyLog = $("[data-log-copy]", body);
  if (copyLog) copyLog.onclick = () => copyText(s.events.map((e) => `${e.at} ${e.level} ${e.step || ""} ${e.message}`).join("\n"), "Journal");
  const follow = $("[data-follow]", body);
  if (follow) follow.onchange = () => { s.follow = follow.checked; renderCurrent(); };
  const logq = $('[name="log-q"]', body);
  if (logq) logq.oninput = () => { s.logQ = logq.value; renderCurrent(); };
  $$("tr[data-log]", body).forEach((tr) => (tr.onclick = () => {
    const key = tr.dataset.log;
    if (s.openLogs.has(key)) s.openLogs.delete(key); else s.openLogs.add(key);
    renderCurrent();
  }));
  const log = $(".vlog", body);
  if (log && s.follow) log.scrollTop = log.scrollHeight;
}

/* j / k déplacent la sélection de la liste, Entrée ouvre la fiche (SPEC-7715 T7) ; rien dans un champ de saisie. */
function videosOnKey(e) {
  const s = videosState;
  if (currentScreen !== "videos" || s.detailId || !s.list || !s.list.length) return;
  if (e.ctrlKey || e.metaKey || e.altKey || drawer.isOpen() || document.querySelector("dialog[open]")) return;
  const t = e.target;
  if (t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable)) return;
  if (e.key === "j" || e.key === "k") {
    s.sel = Math.max(0, Math.min(s.list.length - 1, s.sel + (e.key === "j" ? 1 : -1)));
    e.preventDefault();
    renderCurrent();
    const row = $('tr[aria-selected="true"]');
    if (row) row.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && s.sel >= 0 && s.list[s.sel]) {
    e.preventDefault();
    location.hash = `#/videos/${encodeURIComponent(s.list[s.sel].video_id)}`;
  }
}
document.addEventListener("keydown", videosOnKey);

// le menu « Relancer depuis… » se ferme au clic ailleurs
document.addEventListener("click", (e) => {
  if (videosState.menu && currentScreen === "videos" && !e.target.closest(".vmenu-wrap")) { videosState.menu = null; renderCurrent(); }
});

/* Temps réel (T3) : un événement « video » rafraîchit la fiche ouverte (et son journal) ou la liste, jamais la page. */
document.addEventListener("clipper:event", (e) => {
  if (currentScreen !== "videos" || e.detail.kind !== "video") return;
  if (videosState.detailId) { if (e.detail.id === videosState.detailId) videosRefreshDetail(); } else videosLoad();
});

Screens.videos = {
  render(body) {
    const s = videosState;
    const section = body.closest(".screen");
    if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
    const id = videosHashId();
    if (id !== s.detailId) videosOpenDetail(id);
    videosEnsureChannels();
    if (id) {
      if (!s.detail || Date.now() - s.detailAt > VIDEOS_STALE_MS) videosRefreshDetail();
      if (s.detail) {
        if (!s.step || !VIDEOS_STEPS.includes(s.step)) s.step = s.detail.current_step || VIDEOS_STEPS[VIDEOS_STEPS.length - 1];
        videosLoadClips(s.detail);
        if (s.step === "moments" && videosStepStatus(s.detail, "moments") === "done") videosLoadJury(s.detail);
      }
    } else if (s.list === null || Date.now() - s.listAt > VIDEOS_STALE_MS) {
      videosLoad();
    }
    morph(body, id ? videosDetailView() : videosListView());
    const retry = $("[data-videos-retry]", body);
    if (retry) retry.onclick = () => (id ? videosRefreshDetail() : videosLoad());
    if (id) { if (s.detail) videosWireDetail(body, s.detail); } else videosWireList(body);
  },
};
