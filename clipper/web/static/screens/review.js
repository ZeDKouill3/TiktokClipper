/* Ecran « Revue des moments » (SPEC-c100 E3, ADR-a308, SPEC-7715). Charge apres screens.js dont
   il remplace l'entree Screens.review. Lit GET /api/videos/{id}/moments, envoie
   chaque decision a POST /api/videos/{id}/moments/{mid}/decide et lance le rendu
   par POST /api/videos/{id}/render (file du worker, jamais dans le processus HTTP).
   Les donnees introuvables arrivent du serveur en null avec <champ>_error :
   affichees telles quelles, jamais remplacees par une valeur de secours.
   Le DOM est calcule d'un bloc depuis l'etat `rv` (rvView) puis rapproche par morph() : le lecteur, le focus
   et le defilement survivent a chaque evenement (SSE, temps de lecture, poignee glissee). */
"use strict";

const RV_MIN_GAP = 1;      // secondes minimum entre le debut et la fin d'un moment
const RV_CONTEXT = 30;     // secondes de contexte de chaque cote dans la timeline zoomee
const RV_DECISION_LABELS = { accepted: "accepté", rejected: "refusé", adjusted: "ajusté" };
const RV_DECISION_CHIP = { accepted: "ok", rejected: "bad", adjusted: "info" };
const RV_UPCOMING = ["pending", "queued", "running"];

const rv = {
  videoId: null, moments: null, error: null, loading: false,
  cur: 0, draft: null, window: null, body: null, busy: false,
  t: null, playing: false, editing: false, dragging: false,
};
const rvWired = new WeakSet();

const rvRound = (t) => Math.round(t * 10) / 10;
const rvClock = (t) => {
  const s = Math.max(0, t), m = Math.floor(s / 60);
  return `${m}:${fr(s - m * 60, 1).padStart(4, "0")}`;
};
const rvHashVideo = () => decodeURIComponent((location.hash.split("/")[2] || "").split("?")[0]);
const rvApi = (suffix) => `/api/videos/${encodeURIComponent(rv.videoId)}${suffix}`;
const rvMoment = () => (rv.moments || [])[rv.cur] || null;

/* Bornes en vigueur d'un moment : celles de la decision « ajustée » si elle existe. */
function rvBounds(m) {
  const d = m.decision;
  return d && d.decision === "adjusted" ? { start: d.start, end: d.end } : { start: m.start, end: m.end };
}
const rvDecisionName = (m) => (m.decision ? m.decision.decision : null);
const rvDuration = () => { const m = rvMoment(); return m ? m.source_duration : null; };
const rvDirty = () => {
  const m = rvMoment(), b = m && rvBounds(m);
  return Boolean(m && rv.draft && (rv.draft.start !== b.start || rv.draft.end !== b.end));
};

/* ---------- Donnees ---------- */
async function rvLoad(keepCur) {
  const id = rv.videoId;
  rv.loading = true;
  try {
    const moments = await api(`/api/videos/${encodeURIComponent(id)}/moments`);
    if (id !== rv.videoId) return;
    rv.moments = moments;
    rv.error = null;
    rv.cur = keepCur ? Math.min(rv.cur, moments.length - 1) : Math.max(0, moments.findIndex((m) => !m.decision));
    rvResetDraft();
  } catch (err) {
    if (id === rv.videoId) { rv.moments = null; rv.error = err; }
  } finally {
    rv.loading = false;
  }
  if (currentScreen === "review") renderCurrent();
}

function rvResetDraft() {
  const m = rvMoment();
  const b = m ? rvBounds(m) : null;
  rv.draft = b ? { start: b.start, end: b.end } : null;
  const dur = m ? m.source_duration : null;
  rv.window = b && dur ? { a: Math.max(0, b.start - RV_CONTEXT), b: Math.min(dur, b.end + RV_CONTEXT) } : null;
  rv.t = b ? b.start : null;
}

/* ---------- Decisions (toast « Annuler » : renvoie la decision precedente) ---------- */
async function rvSend(moment, payload) {
  return api(`/api/videos/${encodeURIComponent(rv.videoId)}/moments/${moment.id}/decide`, jsonBody("POST", payload));
}

function rvPayloadOf(decision) {
  if (!decision) return null;
  return decision.decision === "adjusted"
    ? { decision: "adjusted", start: decision.start, end: decision.end, comment: decision.comment || null }
    : { decision: decision.decision, comment: decision.comment || null };
}

async function rvDecide(kind) {
  const m = rvMoment();
  if (!m || rv.busy) return;
  const payload = kind === "adjusted"
    ? { decision: "adjusted", start: rv.draft.start, end: rv.draft.end }
    : { decision: kind };
  const previous = m.decision;   // decision a renvoyer si l'utilisateur annule
  const index = rv.cur;
  rv.busy = true;
  renderCurrent();
  try {
    await rvSend(m, payload);
    await Promise.all([rvLoad(true), reloadVideo(rv.videoId)]);
  } catch (err) {
    toastError("Décision non enregistrée", err);
    return;
  } finally {
    rv.busy = false;
    renderCurrent();
  }
  const label = { accepted: "Moment accepté", rejected: "Moment refusé", adjusted: "Bornes ajustées" }[kind];
  toast({
    kind: kind === "rejected" ? "warn" : "ok", title: label, body: m.hook_text || `Moment ${m.id}`,
    undo: () => rvUndo(m, index, previous),
  });
  const next = rv.moments.findIndex((x, i) => i > index && !x.decision);
  rvGo(next >= 0 ? next : rv.moments.findIndex((x) => !x.decision) >= 0 ? rv.moments.findIndex((x) => !x.decision) : index);
}

/* Annuler : renvoie la decision precedente du moment. Sans decision anterieure,
   l'API ne sait pas en effacer une : on revient sur le moment pour en prendre une autre. */
async function rvUndo(moment, index, previous) {
  const payload = rvPayloadOf(previous);
  if (!payload) {
    rvGo(index);
    toast({ kind: "info", title: "Aucune décision antérieure", body: "La décision reste enregistrée : choisis-en une autre pour la remplacer.", ms: 5200 });
    return;
  }
  try {
    await rvSend(moment, payload);
    await Promise.all([rvLoad(true), reloadVideo(rv.videoId)]);
    rvGo(index);
    toast({ kind: "ok", title: "Décision restaurée", body: RV_DECISION_LABELS[previous.decision], ms: 2600 });
  } catch (err) { toastError("Annulation impossible", err); }
}

/* ---------- Rendu : conditionne par state.awaiting ---------- */
function rvVideoState() {
  return (store.videos || []).find((v) => v.video_id === rv.videoId) || null;
}

/* Raison affichee tant que le rendu est impossible ; null si le rendu peut etre lance. */
function rvBlockedReason() {
  const v = rvVideoState();
  if (!v) return "État de la vidéo introuvable : le rendu ne peut pas être lancé.";
  const awaiting = v.awaiting || [];
  if (!awaiting.length) return null;
  return `${awaiting.length} moment${awaiting.length > 1 ? "s" : ""} sans décision (${awaiting.map((i) => `#${i}`).join(", ")}) : décide chacun avant de lancer le rendu.`;
}

async function rvRender() {
  const reason = rvBlockedReason();
  if (reason) { toast({ kind: "warn", title: "Rendu impossible", body: reason }); return; }
  const kept = rv.moments.filter((m) => rvDecisionName(m) !== "rejected").length;
  if (!(await confirmDialog({ title: "Lancer le rendu ?", body: `${kept} moment${kept > 1 ? "s retenus seront rendus" : " retenu sera rendu"} pour ${rv.videoId}.`, confirmLabel: "Lancer le rendu", danger: false }))) return;
  try {
    await api(rvApi("/render"), { method: "POST" });
    toast({ kind: "ok", title: "Rendu mis en file", body: rv.videoId });
    await reloadVideo(rv.videoId);
    renderCurrent();
  } catch (err) { toastError("Rendu impossible", err); }
}

/* ---------- Vues (HTML pur calcule depuis l'etat) ---------- */

/* Etat vide : cite le mode « review » et liste les videos candidates (celles qui y arriveront). */
function rvEmptyView(videos, message) {
  const upcoming = (videos || []).filter((v) => RV_UPCOMING.includes(v.status));
  const text = message || "Mode « review » : après l'étape « moments », chaque vidéo attend ici ta décision sur ses moments. En mode « auto », le jury décide seul et rien n'arrive dans cet écran.";
  const rows = upcoming.map((v) => `<tr>
      <td><a class="t" href="#/videos/${encodeURIComponent(v.video_id)}">${esc(v.title || v.video_id)}</a>${v.title && v.title !== v.video_id ? `<div class="s mono">${esc(v.video_id)}</div>` : ""}</td>
      <td data-l="Statut">${statusChip(v.status)}</td></tr>`).join("");
  const list = upcoming.length
    ? `<section class="rv-candidates"><div class="section-head"><h2>Vidéos candidates</h2><span class="n">${upcoming.length}</span></div>
        <p class="muted">Ces vidéos sont en cours de traitement : celles dont le mode est « review » arriveront ici.</p>
        <div class="table-container"><table class="table"><colgroup><col style="width:70%"><col style="width:30%"></colgroup>
        <thead><tr><th>Vidéo</th><th>Statut</th></tr></thead><tbody>${rows}</tbody></table></div></section>`
    : `<p class="muted rv-none">Aucune vidéo en cours de traitement : ajoute une vidéo pour qu'elle arrive ici.</p>`;
  return `${emptyState("sparkles", "Aucun moment à valider", text)}${list}`;
}

function rvToolbar(candidates) {
  const options = candidates.map((v) => `<option value="${esc(v.video_id)}"${v.video_id === rv.videoId ? " selected" : ""}>${esc(v.video_id)}</option>`).join("");
  const reason = rvBlockedReason();
  const decided = rv.moments.filter((x) => x.decision).length;
  return `<div class="toolbar rv-bar">
    <label class="rv-pick"><span class="muted">Vidéo</span><select class="input" id="rv-video" aria-label="Vidéo à relire">${options}</select></label>
    <span class="muted rv-progress">${decided} / ${rv.moments.length} décidés</span>
    <span class="grow"></span>
    <div class="rv-gate"><button type="button" class="btn" id="rv-render"${reason || rv.busy ? ` disabled aria-describedby="rv-reason"` : ""}>${icon("film", "i-sm")}Lancer le rendu</button>
      ${reason ? `<p class="reason warn" id="rv-reason">${esc(reason)}</p>` : ""}</div>
  </div>`;
}

function rvMomentRow(x, i) {
  const name = rvDecisionName(x), b = rvBounds(x);
  const chip = name ? `<span class="chip ${RV_DECISION_CHIP[name]}">${esc(RV_DECISION_LABELS[name])}</span>` : "";
  return `<button type="button" class="rv-item ${i === rv.cur ? "on" : ""} ${esc(name || "")}" data-moment="${i}"${i === rv.cur ? ` aria-current="true"` : ""}>
    <span class="rv-score ${x.score >= 80 ? "hi" : x.score >= 70 ? "mid" : ""}">${x.score == null ? "–" : fr(x.score)}</span>
    <span class="rv-item-body"><span class="rv-hook">${esc(x.hook_text || `Moment ${x.id}`)}</span>
      <span class="rv-meta"><span class="mono">${esc(rvClock(b.start))}</span><span>${fr(b.end - b.start)} s</span>${x.confidence != null ? `<span title="Confiance du jury">confiance ${fr(x.confidence)} %</span>` : ""}${x.parts_total > 1 ? `<span>${x.parts_total} parties</span>` : ""}${chip}</span></span>
  </button>`;
}

function rvTimeline(m) {
  const dur = rvDuration();
  if (!dur) return `<p class="reason bad">${esc(m.source_duration_error || "source_duration : donnée absente de la réponse.")}</p>`;
  const w = rv.window, span = w.b - w.a, d = rv.draft;
  const markers = rv.moments.map((x, i) => {
    const b = rvBounds(x);
    return `<span class="mk ${i === rv.cur ? "on" : ""} ${esc(rvDecisionName(x) || "")}" data-moment="${i}" title="${esc(x.hook_text || "")}" style="left:${(b.start / dur) * 100}%;width:${((b.end - b.start) / dur) * 100}%"></span>`;
  }).join("");
  const ticks = [0, 1, 2, 3, 4].map((k) => `<span>${esc(rvClock(w.a + (span * k) / 4))}</span>`).join("");
  const t = rvTime();
  return `<div class="rv-tl-head muted">Vidéo entière · ${esc(rvClock(dur))}</div>
    <div class="tl-overview" id="rv-ov">${markers}</div>
    <div class="tl-zoom" id="rv-zoom">
      <div class="sel-range${rv.dragging ? " dragging" : ""}" id="rv-sel" style="left:${((d.start - w.a) / span) * 100}%;width:${((d.end - d.start) / span) * 100}%"><span class="grip l" id="rv-grip-start" data-handle="start" role="slider" aria-label="Début du moment" aria-valuenow="${d.start}" tabindex="0"></span><span class="grip r" id="rv-grip-end" data-handle="end" role="slider" aria-label="Fin du moment" aria-valuenow="${d.end}" tabindex="0"></span></div>
      <div class="playhead" id="rv-ph" style="left:${((t - w.a) / span) * 100}%"></div><div class="tl-ticks">${ticks}</div></div>`;
}

/* Instant affiche : la lecture, bornee au brouillon. */
function rvTime() {
  const d = rv.draft;
  return Math.max(d.start, Math.min(d.end, rv.t == null ? d.start : rv.t));
}

function rvLayout() {
  const m = rvMoment(), d = rv.draft, dirty = rvDirty(), t = rvTime();
  const dur = rvDuration();
  const jury = `<div class="rv-jury-head"><div><div class="section-title">Justification du jury</div><div class="muted">moment #${m.id}${m.parts_total > 1 ? ` · ${m.parts_total} parties` : ""}</div></div>
      <div class="rv-big">${m.score == null ? "–" : fr(m.score)}<small>/100</small></div></div>
    ${m.justification ? `<p class="rv-why">${esc(m.justification)}</p>` : `<p class="reason bad">justification : aucune donnée pour ce moment.</p>`}
    <div class="rv-label">Confiance du jury</div>
    <div id="rv-confidence">${juryConfidenceHtml(m.confidence, m.judge_confidences)}</div>
    ${m.hook_text ? `<div class="rv-label">Accroche proposée</div><p>${esc(m.hook_text)}</p>` : ""}`;
  return `<div class="rv-layout" data-rv-video="${esc(rv.videoId)}">
    <aside class="rv-side"><div class="section-head"><h2>Proposés par le jury</h2><span class="n">${rv.moments.length}</span></div>
      <div class="rv-list" id="rv-list">${rv.moments.map(rvMomentRow).join("")}</div></aside>
    <section class="rv-main">
      <div class="rv-player" id="rv-player">
        <video id="rv-media" src="${esc(m.preview_url)}" preload="metadata" playsinline></video>
        <div class="p-badge"><span class="rv-pill" id="rv-score">${m.score == null ? "sans score" : `score ${fr(m.score)}`}</span></div>
        <div class="p-ctrl"><button type="button" class="btn-icon rv-play" id="rv-play" aria-label="${rv.playing ? "Pause" : "Lecture"}">${icon(rv.playing ? "pause" : "play")}</button>
          <span class="p-time" id="rv-time">${esc(rvClock(t))} · ${fr(t - d.start)} / ${fr(d.end - d.start)} s</span></div>
      </div>
      <div class="decide">
        <button type="button" class="btn rv-ok" id="rv-accept"${rv.busy ? " disabled" : ""}>${icon("check", "i-sm")}Accepter<kbd class="kbd">A</kbd></button>
        <button type="button" class="btn rv-bad" id="rv-reject"${rv.busy ? " disabled" : ""}>${icon("x", "i-sm")}Refuser<kbd class="kbd">R</kbd></button>
        <button type="button" class="btn" data-variant="outline" id="rv-adjust"${dirty && !rv.busy ? "" : " disabled"}>${icon("sliders-horizontal", "i-sm")}Ajuster</button>
      </div>
      <div class="rv-card">
        <div class="tl" id="rv-tl">${rvTimeline(m)}</div>
        <div class="bounds" id="rv-bounds">
          <label class="b">Début (s) <input class="input rv-num" type="number" data-field="start" step="0.1" min="0"${dur ? ` max="${dur}"` : ""} inputmode="decimal" value="${d.start}"></label>
          <label class="b">Fin (s) <input class="input rv-num" type="number" data-field="end" step="0.1" min="0"${dur ? ` max="${dur}"` : ""} inputmode="decimal" value="${d.end}"></label>
          <span class="b">Durée <b id="rv-dur">${fr(d.end - d.start, 1)} s</b></span>
          <span class="grow"></span>
          <button type="button" class="btn" data-variant="ghost" data-size="sm" id="rv-reset"${dirty ? "" : " disabled"}>${icon("rotate-ccw", "i-xs")}Bornes d'origine</button>
        </div>
      </div>
      <div class="shortcuts" id="rv-shortcuts">
        <span><kbd class="kbd">A</kbd> accepter</span><span><kbd class="kbd">R</kbd> refuser</span>
        <span><kbd class="kbd">J</kbd> suivant</span><span><kbd class="kbd">K</kbd> précédent</span>
        <span><kbd class="kbd">Espace</kbd> lecture / pause</span>
      </div>
      <div class="rv-card"><div class="section-head"><h2>Transcription</h2></div><div class="transcript" id="rv-transcript">${m.transcript
        ? `<p>${esc(m.transcript)}</p>`
        : `<p class="reason bad">${esc(m.transcript_error || "transcript : donnée absente de la réponse.")}</p>`}</div></div>
    </section>
    <aside class="rv-card rv-jury" id="rv-jury">${jury}</aside>
  </div>`;
}

function rvView(candidates) {
  return `${rvToolbar(candidates)}${rvLayout()}`;
}

/* ---------- Interactions ---------- */

/* Navigation entre moments (J / K, liste, reperes de la timeline). */
function rvGo(i) {
  if (!rv.moments || !rv.moments.length) return;
  rv.cur = (i + rv.moments.length) % rv.moments.length;
  rvResetDraft();
  const media = rv.body && $("#rv-media", rv.body);
  if (media && media.readyState > 0) media.currentTime = rv.draft.start;
  renderCurrent();
}

function rvSeek(t) {
  const media = rv.body && $("#rv-media", rv.body);
  rv.t = t;
  if (media && media.readyState > 0) media.currentTime = t;
}

/* Fixe une borne du brouillon dans [0, duree de la source] avec un ecart minimal. */
function rvSetBound(which, value) {
  const dur = rvDuration(), d = rv.draft;
  const max = dur || Infinity;
  if (which === "start") d.start = rvRound(Math.max(0, Math.min(value, d.end - RV_MIN_GAP)));
  else d.end = rvRound(Math.min(max, Math.max(value, d.start + RV_MIN_GAP)));
}

/* Poignee glissee : met a jour le brouillon (les champs, la selection et le lecteur suivent au rendu). */
function rvDrag(e, grip) {
  e.preventDefault();
  const which = grip.dataset.handle;
  grip.setPointerCapture(e.pointerId);
  rv.dragging = true;
  const move = (ev) => {
    const r = $("#rv-zoom", rv.body).getBoundingClientRect(), w = rv.window;
    rvSetBound(which, rvRound(w.a + ((ev.clientX - r.left) / r.width) * (w.b - w.a)));
    rvSeek(rv.draft[which]);
    renderCurrent();
  };
  const up = () => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", up);
    grip.removeEventListener("pointercancel", up);
    rv.dragging = false;
    renderCurrent();
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", up);
  grip.addEventListener("pointercancel", up);
}

/* Lecteur cale sur le moment : jamais hors de [debut, fin] du brouillon. Le <video> survit aux morphs :
   ses ecouteurs ne sont poses qu'une fois. */
function rvWirePlayer(body) {
  const media = $("#rv-media", body);
  if (!media || rvWired.has(media)) return;   // pas d'attribut data-* : le morph le retirerait
  rvWired.add(media);
  media.addEventListener("loadedmetadata", () => { if (rv.draft) media.currentTime = rv.draft.start; });
  media.addEventListener("timeupdate", () => {
    const d = rv.draft;
    if (!d) return;
    if (media.currentTime >= d.end) { media.pause(); media.currentTime = d.start; }
    else if (media.currentTime < d.start - 0.3) media.currentTime = d.start;
    rv.t = media.currentTime;
    renderCurrent();
  });
  media.addEventListener("play", () => { rv.playing = true; renderCurrent(); });
  media.addEventListener("pause", () => { rv.playing = false; renderCurrent(); });
  media.addEventListener("error", () => toast({ kind: "bad", title: "Lecture impossible", body: `La vidéo source de ${rv.videoId} n'a pas pu être chargée.`, ms: 7000 }));
}

function rvTogglePlay(body) {
  const media = $("#rv-media", body), d = rv.draft;
  if (!media || !d) return;
  if (!media.paused) { media.pause(); return; }
  if (media.currentTime < d.start || media.currentTime >= d.end) media.currentTime = d.start;
  media.play().catch((err) => toastError("Lecture impossible", err));
}

/* Accepter : avec des bornes modifiees, la decision est « ajustée » (bornes gardees dans review.json). */
const rvAccept = () => rvDecide(rvDirty() ? "adjusted" : "accepted");

/* Gestionnaires poses apres chaque morph (les noeuds neufs n'ont pas encore les leurs). */
function rvWire(body) {
  rvWirePlayer(body);
  $$("[data-moment]", body).forEach((b) => (b.onclick = () => rvGo(+b.dataset.moment)));
  $$("[data-handle]", body).forEach((g) => (g.onpointerdown = (e) => rvDrag(e, g)));
  $$("[data-field]", body).forEach((f) => {
    f.oninput = () => { rv.editing = true; };
    f.onblur = () => { rv.editing = false; };
    f.onchange = () => {
      const which = f.dataset.field, value = parseFloat(f.value);
      rv.editing = false;
      if (Number.isNaN(value) || value < 0) {
        toast({ kind: "bad", title: "Borne invalide", body: "Saisis un nombre de secondes positif.", ms: 4000 });
      } else {
        rvSetBound(which, rvRound(value));
        rvSeek(rv.draft[which]);
      }
      f.value = rv.draft[which];
      renderCurrent();
    };
  });
  $("#rv-play", body).onclick = () => rvTogglePlay(body);
  $("#rv-player", body).onclick = (e) => { if (!e.target.closest(".p-ctrl")) rvTogglePlay(body); };
  $("#rv-reset", body).onclick = () => { rvResetDraft(); renderCurrent(); };
  $("#rv-accept", body).onclick = () => rvAccept();
  $("#rv-reject", body).onclick = () => rvDecide("rejected");
  $("#rv-adjust", body).onclick = () => rvDecide("adjusted");
  $("#rv-render", body).onclick = rvRender;
  $("#rv-video", body).onchange = (e) => { location.hash = `#/review/${encodeURIComponent(e.target.value)}`; };
}

/* ---------- Raccourcis clavier : A R J K espace ---------- */
document.addEventListener("keydown", (e) => {
  if (currentScreen !== "review" || !rv.moments || !rv.body) return;
  if (e.ctrlKey || e.metaKey || e.altKey || document.querySelector("dialog[open]") || document.querySelector(".modal.show")) return;
  const target = e.target;
  if (target && (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable)) return;
  const key = e.key.toLowerCase();
  if (key === "a") rvAccept();
  else if (key === "r") rvDecide("rejected");
  else if (key === "j") rvGo(rv.cur + 1);
  else if (key === "k") rvGo(rv.cur - 1);
  else if (key === " ") rvTogglePlay(rv.body);
  else return;
  e.preventDefault();
});

/* ---------- Ecran ---------- */
Screens.review = {
  render(body, store) {
    const candidates = (store.videos || []).filter((v) => v.status === "awaiting_review");
    const id = rvHashVideo() || (candidates.find((v) => v.video_id === rv.videoId) || candidates[0] || {}).video_id || null;
    rv.body = null;
    if (!id) {
      rv.videoId = null; rv.moments = null;
      morph(body, rvEmptyView(store.videos));
      return;
    }
    if (id !== rv.videoId) {
      rv.videoId = id; rv.moments = null; rv.error = null; rv.loading = false; rv.playing = false;
      morph(body, `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`);
    }
    if (rv.error) {
      morph(body, `<div class="alert" data-variant="destructive" role="alert">${icon("circle-alert")}<h2>Moments indisponibles</h2><section>${esc(String(rv.error.message || rv.error))}
        <div><button type="button" class="btn" data-variant="outline" data-size="sm" id="rv-retry">${icon("rotate-ccw", "i-xs")}Réessayer</button></div></section></div>`);
      $("#rv-retry", body).onclick = () => { rv.error = null; rv.moments = null; renderCurrent(); };
      return;
    }
    if (rv.moments === null) {
      if (!rv.loading) rvLoad(false);
      return;
    }
    if (!rv.moments.length) {
      morph(body, rvEmptyView(store.videos, `Le jury n'a retenu aucun moment pour ${id}.`));
      return;
    }
    rv.body = body;
    if (rv.editing) return;   // une borne en cours de saisie ne doit pas etre ecrasee par un evenement SSE
    morph(body, rvView(candidates.length ? candidates : [{ video_id: id }]));
    rvWire(body);
  },
};
