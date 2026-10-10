/* Coquille de la console : routeur par hash (9 ecrans), client d'API avec
   jeton, temps reel SSE avec repli sur polling, theme, notifications, ajout
   de video. Les ecrans eux-memes vivent dans screens.js. */
"use strict";

const SCREEN_IDS = ["dashboard", "veille", "videos", "review", "clips", "clip", "channels", "publish", "stats", "accounts", "settings", "journal"];
const POLL_MS = 5000;
const THEME_KEY = "clipper-theme";
const NOTIF_KEY = "clipper-notifications";
const TOKEN_COOKIE = "clipper_token";
// Statuts de video qui declenchent un toast (SPEC-c100 T5).
const NOTIFY_STATUS = {
  done: { kind: "ok", title: "Vidéo terminée" },
  failed: { kind: "bad", title: "Échec du traitement" },
  awaiting_review: { kind: "warn", title: "Moments à valider" },
  queued: { kind: "info", title: "Vidéo remise en file" },
};

const store = { videos: null, queue: null, channels: null, veille: null };
let currentScreen = null;
let source = null;
let pollHandle = null;

/* ---------- Jeton d'acces (SPEC-c100 T7) ---------- */
let tokenPrompt = null;

function readCookie(name) {
  const hit = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
}

/* Affiche la page de saisie du jeton ; la promesse se resout une fois le
   cookie pose. Un seul affichage meme si plusieurs requetes recoivent 401. */
function askToken(rejected) {
  if (tokenPrompt) return tokenPrompt;
  tokenPrompt = new Promise((resolve) => {
    const view = $("#token-view");
    const input = $("#token-input");
    $("#token-error").textContent = rejected ? "Jeton refusé : vérifie-le et réessaie." : "";
    view.hidden = false;
    input.value = "";
    setTimeout(() => input.focus(), 30);
    $("#token-form").onsubmit = (e) => {
      e.preventDefault();
      document.cookie = `${TOKEN_COOKIE}=${encodeURIComponent(input.value.trim())}; path=/; SameSite=Strict`;
      view.hidden = true;
      tokenPrompt = null;
      resolve();
    };
  });
  return tokenPrompt;
}

/* ---------- Client d'API ---------- */
async function api(path, options, replayed) {
  const resp = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  if (resp.status === 401) {
    // Jeton manquant ou refuse : on le demande, puis on rejoue la requete.
    await askToken(replayed || Boolean(readCookie(TOKEN_COOKIE)));
    connectEvents();
    return api(path, options, true);
  }
  if (!resp.ok) {
    let detail = resp.statusText, payload = null;
    try {
      payload = await resp.json();
      detail = payload.detail || detail;
    } catch (err) {
      // reponse sans corps JSON : on garde le statusText.
    }
    const failure = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    failure.body = payload; // champs en plus du detail (ex. next_at : prochaine heure possible d'une publication)
    failure.status = resp.status; // reloadVideo distingue un 404 (video disparue) d'une panne serveur
    throw failure;
  }
  if (resp.status === 204) return null;
  return resp.json();
}

const jsonBody = (method, payload) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });

/* ---------- Donnees ---------- */
async function loadVideos() { store.videos = await api("/api/videos"); }
async function loadQueue() { store.queue = await api("/api/queue"); }
async function loadChannels() { store.channels = await api("/api/channels"); }

function notifyChange(previous, video) {
  if (!previous || previous.status === video.status || !NOTIFY_STATUS[video.status]) return;
  const n = NOTIFY_STATUS[video.status];
  toast({ kind: n.kind, title: n.title, body: video.reason ? `${video.video_id} : ${video.reason}` : video.video_id,
    action: { label: "Voir", run: () => { location.hash = "#/videos"; } } });
  if (notificationsOn() && typeof Notification !== "undefined" && Notification.permission === "granted") {
    new Notification(n.title, { body: video.video_id });
  }
}

/* Recharge uniquement la video visee par l'evenement. */
async function reloadVideo(id) {
  let video = null;
  try {
    video = await api(`/api/videos/${encodeURIComponent(id)}`);
  } catch (err) {
    // 404 : video disparue (workspace nettoye), on la retire de la liste.
    // Autre erreur (500...) : la video reste, la panne est signalee.
    if (err.status !== 404) {
      toastError("Actualisation impossible", err);
      return;
    }
  }
  const list = store.videos || [];
  const index = list.findIndex((v) => v.video_id === id);
  if (video) {
    notifyChange(index >= 0 ? list[index] : null, video);
    if (index >= 0) list[index] = video; else list.push(video);
  } else if (index >= 0) {
    list.splice(index, 1);
  }
  store.videos = list;
}

/* Notifications de publication TikTok (SPEC-9225 R4) : le worker ecrit un journal, l'API le
   sert depuis `since` ; un arret (captcha, connexion expiree...) s'affiche en erreur avec sa raison. */
let tiktokSince = new Date().toISOString(); // seuls les evenements posterieurs a l'ouverture de la page notifient
async function notifyTikTok() {
  try {
    const events = await api("/api/tiktok/events?since=" + encodeURIComponent(tiktokSince));
    if (events.length) tiktokSince = events[events.length - 1].at;
    events.forEach((e) => {
      const where = e.video_id ? `${e.video_id}/${e.clip_id}` : "";
      toast({
        kind: e.level === "error" ? "bad" : e.level === "warn" ? "warn" : "ok",
        title: e.level === "error" ? "Publication TikTok arrêtée" : e.level === "warn" ? "Publication TikTok reportée" : "Publication TikTok",
        body: `${where ? `${where} : ` : ""}${e.reason}`, ms: e.level === "error" ? 9000 : 5200,
      });
    });
  } catch (err) {
    toastError("Notifications TikTok illisibles", err);
  }
}

/* Evenement SSE {kind, id, at} : recharge l'objet concerne, pas la page. Le
   battement du worker (toutes les quelques secondes) ne met a jour que son
   voyant : il ne declenche ni rechargement de donnees ni nouveau rendu. */
async function onServerEvent(event) {
  if (event.kind === "worker") {
    document.dispatchEvent(new CustomEvent("clipper:worker", { detail: event }));
    return;
  }
  if (event.kind === "tiktok") await notifyTikTok();
  try {
    if (event.kind === "video") await reloadVideo(event.id);
    else if (event.kind === "queue") await loadQueue();
    // publish / watch / worker : les ecrans concernes ecoutent cet evenement.
    document.dispatchEvent(new CustomEvent("clipper:event", { detail: event }));
  } catch (err) {
    toastError("Actualisation impossible", err);
  }
  renderCurrent();
  updateCounts();
}

/* ---------- Pays de l'IP publique (TASK-30cc) : pastille de la barre du haut ---------- */
const NET_REFRESH_MS = 60000;

function paintNetwork(net) {
  setNetLast(net); // source unique de la garde réseau au clic (netGuard, ui.js)
  const pill = $("#net-pill");
  const label = $("#net-label");
  if (!net || net.ok === null || net.ok === undefined) {
    pill.className = "net-pill unknown";
    label.textContent = "pays inconnu";
    pill.title = net && net.error ? `Pays de l'IP inconnu : ${net.error}` : "Pays de l'IP inconnu";
    return;
  }
  const where = net.city ? `${net.country} · ${net.city}` : net.country;
  pill.className = `net-pill ${net.ok ? "ok" : "bad"}`;
  label.textContent = net.ok ? where : `IP hors ${net.expected_country_name} (${net.country}) : ne publie pas`;
  pill.title = [`IP ${net.ip || "?"}`, net.isp ? `FAI ${net.isp}` : "", net.city ? `Ville ${net.city}` : "",
    `${net.country_name || net.country} (attendu ${net.expected_country_name})`].filter(Boolean).join("\n");
}

async function refreshNetwork() {
  try {
    paintNetwork(await api("/api/network"));
  } catch (err) {
    paintNetwork({ ok: null, error: String(err.message || err) });
  }
}

/* ---------- Temps reel : SSE, repli sur polling (T3) ---------- */
function setLive(ok) {
  $("#conn-banner").hidden = ok;
  $("#live").classList.toggle("off", !ok);
  $("#live-label").textContent = ok ? "En direct" : "Hors ligne";
}

async function pollOnce() {
  try {
    await Promise.all([loadVideos(), loadQueue()]);
    renderCurrent();
    updateCounts();
  } catch (err) {
    // le bandeau « connexion perdue » est deja affiche ; on retentera dans 5 s.
  }
}

function startPolling() {
  if (pollHandle) return;
  pollHandle = setInterval(pollOnce, POLL_MS);
}

function stopPolling() {
  if (pollHandle) clearInterval(pollHandle);
  pollHandle = null;
}

function connectEvents() {
  if (typeof EventSource === "undefined") { setLive(false); startPolling(); return; }
  if (source) source.close();
  source = new EventSource("/api/events");
  source.onopen = () => {
    setLive(true);
    stopPolling();
    pollOnce(); // rattrape ce qui a change pendant la coupure
  };
  source.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch (err) { return; }
    onServerEvent(event);
  };
  source.onerror = () => {
    // Flux ferme : bandeau + polling toutes les 5 s ; EventSource retente seul.
    setLive(false);
    startPolling();
  };
}

/* ---------- Routeur par hash ---------- */
/* >>> hash-legacy */
// L'ecran « Styles » (id interne « channels ») vit sous #/styles ; les anciennes URL #/chaines et #/channels
// y redirigent (sous-routes comprises). Rend la nouvelle URL, ou null si le hash n'est pas une ancienne URL.
function legacyHash(hash) {
  const m = /^#\/?(chaines|channels)(?=$|[/?])(.*)$/.exec(hash);
  return m ? `#/styles${m[2]}` : null;
}
/* <<< hash-legacy */

function route() {
  const moved = legacyHash(location.hash);
  if (moved) { history.replaceState(null, "", moved); }
  const hashId = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0]) || "dashboard";
  // « #set-xxx » est l'ancre d'une section de Réglages, jamais un écran : on défile jusqu'à
  // elle (sans quitter l'écran), et seulement si Réglages n'est pas encore affiché on l'ouvre.
  const anchor = hashId.startsWith("set-") ? hashId : null;
  const id = anchor ? "settings" : (hashId === "styles" ? "channels" : hashId);
  if (anchor && currentScreen === "settings") {
    const target = document.getElementById(anchor);
    if (target) target.scrollIntoView();
    return;
  }
  const screen = SCREEN_IDS.includes(id) ? id : "dashboard";
  currentScreen = screen;
  $$("#view > .screen").forEach((s) => { s.hidden = s.id !== `screen-${screen}`; });
  $$("[data-screen]").forEach((a) => {
    const on = a.dataset.screen === screen;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  const section = $(`#screen-${screen}`);
  $("#crumb-title").textContent = section.dataset.title;
  document.title = `${section.dataset.title} · Clipper`;
  window.scrollTo(0, 0);
  $(".sidebar").classList.remove("open");
  renderCurrent();
  if (anchor) {
    const target = document.getElementById(anchor);
    if (target) target.scrollIntoView();
  }
}

/* Ecrit body.innerHTML seulement si le HTML calcule differe du precedent :
   un rendu identique (evenement SSE sans changement, polling) ne touche pas
   au DOM, donc ni clignotement, ni image rechargee, ni defilement perdu. */
function guardBodyHtml(body) {
  if (body.dataset.guarded) return;
  body.dataset.guarded = "1";
  const native = Object.getOwnPropertyDescriptor(Element.prototype, "innerHTML");
  let last = null;
  // Un ecran dont le DOM porte des saisies a jeter (reglages) force le prochain rendu.
  body.resetHtmlGuard = () => { last = null; };
  Object.defineProperty(body, "innerHTML", {
    configurable: true,
    get() { return native.get.call(this); },
    set(html) {
      if (html === last && this.childNodes.length) return;
      last = html;
      native.set.call(this, html);
    },
  });
}

function renderCurrent() {
  if (!currentScreen) return;
  const body = $(`#screen-${currentScreen} [data-body]`);
  guardBodyHtml(body);
  // Tant que les premieres donnees ne sont pas la : le squelette reste affiche.
  if (store.videos === null && (currentScreen === "dashboard" || currentScreen === "videos")) return;
  Screens[currentScreen].render(body, store);
  $$("[data-add]", body).forEach((b) => (b.onclick = () => openAddVideo()));
  wireActions(body);
}

function updateCounts() {
  const videos = store.videos || [];
  const set = (id, n) => $$(`[data-count-for="${id}"]`).forEach((el) => { el.textContent = n; el.hidden = !n; });
  set("videos", videos.filter((v) => v.status === "running").length);
  set("review", videos.filter((v) => v.status === "awaiting_review").length);
  const proposals = (store.veille && store.veille.day && store.veille.day.proposals) || [];
  set("veille", proposals.filter((p) => p.status === "proposed").length);
}

/* ---------- Actions de la file et des videos (T4 : confirmation) ---------- */
function wireActions(root) {
  $$("[data-front]", root).forEach((b) => (b.onclick = async () => {
    try {
      await api(`/api/queue/${encodeURIComponent(b.dataset.front)}/front`, { method: "POST" });
      await loadQueue();
      renderCurrent();
    } catch (err) { toastError("Impossible de passer la vidéo en tête", err); }
  }));
  $$("[data-remove]", root).forEach((b) => (b.onclick = async () => {
    const id = b.dataset.remove;
    if (!(await confirmDialog({ title: "Retirer de la file ?", body: `${id} ne sera plus traitée.`, confirmLabel: "Retirer" }))) return;
    try {
      await api(`/api/queue/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadQueue();
      renderCurrent();
      toast({ kind: "ok", title: "Retirée de la file", body: id });
    } catch (err) { toastError("Impossible de retirer la vidéo", err); }
  }));
  $$("[data-retry-video]", root).forEach((b) => (b.onclick = () => retryVideo(b.dataset.retryVideo, b.dataset.fromStep)));
  $$("[data-dismiss-video]", root).forEach((b) => (b.onclick = () => dismissVideo(b.dataset.dismissVideo)));
  $$("[data-restore-video]", root).forEach((b) => (b.onclick = () => restoreVideo(b.dataset.restoreVideo)));
  $$("[data-cancel]", root).forEach((b) => (b.onclick = async () => {
    const id = b.dataset.cancel;
    if (!(await confirmDialog({ title: "Annuler le traitement ?", body: `Le traitement de ${id} sera arrêté.`, confirmLabel: "Annuler le traitement" }))) return;
    try {
      await api(`/api/videos/${encodeURIComponent(id)}/cancel`, { method: "POST" });
      await reloadVideo(id);
      renderCurrent();
    } catch (err) { toastError("Impossible d'annuler le traitement", err); }
  }));
}

/* ---------- Relancer / retirer une video en echec (tableau de bord et fiche video) ---------- */
async function afterVideoAction(id) {
  await Promise.all([reloadVideo(id), loadQueue()]);
  // le tableau de bord, la liste et la fiche se rafraichissent comme sur un evenement serveur
  document.dispatchEvent(new CustomEvent("clipper:event", { detail: { kind: "video", id } }));
  renderCurrent();
  updateCounts();
}

async function retryVideo(id, step) {
  if (!step || !STEP_LABELS[step]) {
    toastError("Relance impossible", new Error(`étape inconnue pour ${id} : ouvre sa fiche et choisis l'étape à relancer.`));
    return;
  }
  const ok = await confirmDialog({
    title: `Relancer depuis « ${STEP_LABELS[step]} » ?`,
    body: `${id} repart de cette étape (et des suivantes). Les clips déjà rendus de cette vidéo seront remplacés.`,
    confirmLabel: "Relancer", danger: false,
  });
  if (!ok) return;
  try {
    await api(`/api/videos/${encodeURIComponent(id)}/retry`, jsonBody("POST", { from_step: step }));
    toast({ kind: "ok", title: "Relance mise en file", body: `${id} : depuis « ${STEP_LABELS[step]} »` });
    await afterVideoAction(id);
  } catch (err) { toastError("Relance impossible", err); }
}

async function dismissVideo(id) {
  const ok = await confirmDialog({
    title: "Retirer des échecs ?",
    body: `${id} sort des échecs et des compteurs. Son dossier est conservé : tu peux la rétablir depuis sa fiche.`,
    confirmLabel: "Retirer",
  });
  if (!ok) return;
  try {
    await api(`/api/videos/${encodeURIComponent(id)}/dismiss`, { method: "POST" });
    toast({ kind: "ok", title: "Vidéo retirée des échecs", body: id });
    await afterVideoAction(id);
  } catch (err) { toastError("Impossible de retirer la vidéo", err); }
}

async function restoreVideo(id) {
  try {
    await api(`/api/videos/${encodeURIComponent(id)}/restore`, { method: "POST" });
    toast({ kind: "ok", title: "Vidéo rétablie", body: id });
    await afterVideoAction(id);
  } catch (err) { toastError("Impossible de rétablir la vidéo", err); }
}

/* ---------- Attribuer un style a une video qui n'en a pas (fiche video, erreur d'approbation) ---------- */
function assignChannelHtml(videoId, channels) {
  return `
    <div class="modal-head"><h2>Attribuer un style</h2><p class="muted" style="margin-top:4px">${esc(videoId)} n'a pas de style : sans lui, ses clips ne peuvent être ni approuvés ni publiés. Le choix est journalisé et ne se change pas ensuite.</p></div>
    <form id="assign-form"><div class="modal-body">
      <div class="field"><label for="assign-channel">Style</label>
        <select class="input" id="assign-channel" name="channel" required>${channels.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></div>
    </div>
    <div class="modal-foot"><button type="button" class="btn btn-ghost" data-dismiss>Annuler</button><button type="submit" class="btn btn-primary">Attribuer</button></div></form>`;
}

async function openAssignChannel(videoId, knownChannels) {
  let channels = knownChannels && knownChannels.length ? knownChannels : null;
  if (!channels) {
    try { await loadChannels(); } catch (err) { toastError("Styles indisponibles", err); return; }
    channels = store.channels || [];
  }
  if (!channels.length) {
    toast({ kind: "warn", title: "Aucun style", body: "Crée un style dans l'écran Styles avant de l'attribuer à une vidéo." });
    return;
  }
  openPanel("modal", assignChannelHtml(videoId, channels), (el) => {
    $("#assign-form", el).onsubmit = async (e) => {
      e.preventDefault();
      const channel = $("#assign-channel", el).value;
      closeLayer();
      try {
        await api(`/api/videos/${encodeURIComponent(videoId)}/channel`, jsonBody("POST", { channel }));
        toast({ kind: "ok", title: "Style attribué", body: `${videoId} : ${channel}` });
        await afterVideoAction(videoId);
        if (typeof loadClips === "function") loadClips();
      } catch (err) { toastError("Attribution impossible", err); }
    };
  });
}

/* ---------- Ajout de video ---------- */
async function openAddVideo(channel) {
  try { if (store.channels === null) await loadChannels(); } catch (err) { toastError("Styles indisponibles", err); }
  const channels = store.channels || [];
  openPanel("modal", `
    <div class="modal-head"><h2>Ajouter une vidéo</h2><p class="muted" style="margin-top:4px">YouTube ou VOD Twitch. Elle passe en file avec le preset de son style.</p></div>
    <form id="add-form"><div class="modal-body">
      <div class="field"><label for="add-url">Adresse (URL)</label><input class="input" id="add-url" name="url" type="url" required placeholder="https://…" autocomplete="off"></div>
      <div class="field"><label for="add-channel">Style</label>
        <select class="input" id="add-channel" name="channel"><option value="">Sans style (config.toml)</option>${channels.map((c) => `<option value="${esc(c)}"${c === channel ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>
        ${channels.length ? "" : `<span class="hint">Aucun style : crée-en une dans l'écran Styles.</span>`}</div>
      <div class="field"><label><input type="checkbox" id="add-short" name="short_clips"> Clips courts <span class="muted" id="add-short-state"></span></label>
        <span class="hint">20-45 s, le clip démarre sur le moment fort. Non précisé : valeur du style.</span></div>
    </div>
    <div class="modal-foot"><button type="button" class="btn btn-ghost" data-dismiss>Annuler</button><button type="submit" class="btn btn-primary">Mettre en file</button></div></form>`,
  (el) => {
    setTimeout(() => $("#add-url", el).focus(), 60);
    // Trois états : non précisé (valeur du style) -> coché (on) -> décoché (off) -> non précisé.
    const short = $("#add-short", el);
    let shortClips = null;
    const showShort = () => {
      short.indeterminate = shortClips === null;
      short.checked = shortClips === true;
      $("#add-short-state", el).textContent = shortClips === null ? "(valeur du style)" : shortClips ? "(oui)" : "(non)";
    };
    short.onclick = () => {  // pas de preventDefault : le navigateur annulerait l'état posé par showShort
      shortClips = shortClips === null ? true : shortClips === true ? false : null;
      showShort();
    };
    showShort();
    $("#add-form", el).onsubmit = async (e) => {
      e.preventDefault();
      const url = $("#add-url", el).value.trim();
      const channel = $("#add-channel", el).value || null;
      closeLayer();
      try {
        const entry = await api("/api/queue", jsonBody("POST", { url, channel, action: "run", ...(shortClips === null ? {} : { short_clips: shortClips }) }));
        toast({ kind: "ok", title: "Vidéo mise en file", body: entry.video_id });
        await Promise.all([loadVideos(), loadQueue()]);
        renderCurrent();
        updateCounts();
      } catch (err) { toastError("Ajout impossible", err); }
    };
  });
}

/* ---------- Theme (sombre / clair) ---------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#btn-theme").innerHTML = icon(theme === "light" ? "moon" : "sun");
  $('meta[name="theme-color"]').content = theme === "light" ? "#f4f2ec" : "#0e0e0c";
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* stockage indisponible : choix non memorise */ }
  applyTheme(next);
}

function followSystemTheme() {
  if (!window.matchMedia) return;
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (err) { /* ignore */ }
    if (!saved) applyTheme(e.matches ? "light" : "dark"); // sans choix memorise, on suit le systeme
  });
}

/* ---------- Notifications du navigateur (reglage local) ---------- */
function notificationsOn() {
  try { return localStorage.getItem(NOTIF_KEY) === "on"; } catch (e) { return false; }
}

function paintNotifButton() {
  const on = notificationsOn() && typeof Notification !== "undefined" && Notification.permission === "granted";
  $("#btn-notif").classList.toggle("on", on);
  $("#btn-notif").setAttribute("aria-pressed", String(on));
}

async function toggleNotifications() {
  if (typeof Notification === "undefined") {
    toast({ kind: "warn", title: "Notifications indisponibles", body: "Ce navigateur ne gère pas les notifications." });
    return;
  }
  const turnOn = !notificationsOn();
  if (turnOn && Notification.permission !== "granted") {
    const answer = await Notification.requestPermission();
    if (answer !== "granted") {
      toast({ kind: "warn", title: "Notifications refusées", body: "Autorise-les dans les réglages du navigateur pour les recevoir." });
      return;
    }
  }
  try { localStorage.setItem(NOTIF_KEY, turnOn ? "on" : "off"); } catch (e) { /* stockage indisponible */ }
  paintNotifButton();
  toast({ kind: "info", title: turnOn ? "Notifications activées" : "Notifications désactivées", ms: 2600 });
}

/* ---------- Demarrage ---------- */
(function boot() {
  hydrateIcons(document);
  applyTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  followSystemTheme();
  paintNotifButton();

  $("#btn-theme").onclick = toggleTheme;
  $("#btn-notif").onclick = toggleNotifications;
  $("#btn-add").onclick = () => openAddVideo();
  $("#btn-menu").onclick = () => {
    const sb = $(".sidebar");
    sb.classList.add("open");
    showOverlay(() => sb.classList.remove("open"));
  };
  $$("#nav a").forEach((a) => a.addEventListener("click", () => { if (closeCurrent) closeLayer(); }));

  window.addEventListener("hashchange", route);
  route();
  connectEvents();
  refreshNetwork();
  setInterval(refreshNetwork, NET_REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshNetwork(); });
  loadVeille(); // compteur « Veille » de la navigation (propositions à décider) ; ses erreurs s'affichent dans l'écran
  Promise.all([loadVideos(), loadQueue(), loadChannels()])
    .then(() => { renderCurrent(); updateCounts(); })
    .catch((err) => {
      toastError("Chargement impossible", err);
      const body = $(`#screen-${currentScreen} [data-body]`);
      if (body) body.innerHTML = emptyState("circle-alert", "Chargement impossible", String(err.message || err));
    });
})();
