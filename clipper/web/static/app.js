/* Coquille de la console : routeur par hash (12 ecrans), client d'API avec
   jeton, temps reel SSE avec repli sur polling, theme (clair par defaut),
   notifications, ajout de video, palette Ctrl K et raccourcis clavier,
   barre laterale reductible. Les ecrans eux-memes vivent dans screens.js et
   screens/*.js ; le DOM est mis a jour par morph() (ui.js), jamais par innerHTML. */
"use strict";

const SCREEN_IDS = ["dashboard", "veille", "videos", "review", "clips", "clip", "channels", "publish", "stats", "accounts", "settings", "journal"];
const POLL_MS = 5000;
const THEME_KEY = "clipper-theme";
const NAV_KEY = "clipper-nav";
const NOTIF_KEY = "clipper-notifications";
const TOKEN_COOKIE = "clipper_token";
// Statuts de video qui declenchent un toast (SPEC-7715 T5).
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

/* ---------- Jeton d'acces (SPEC-7715 T7) ---------- */
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

/* ---------- Pays de l'IP publique (TASK-30cc, SPEC-7715 T13) : pastille compacte de la barre du haut ----------
   Icone + code pays ; rouge seulement hors du pays attendu. Le detail (IP, FAI, ville, attendu) est dans l'infobulle ;
   l'alerte complete n'apparait qu'au moment d'une publication (netGuard, ui.js). */
const NET_REFRESH_MS = 60000;

function paintNetwork(net) {
  setNetLast(net); // source unique de la garde réseau au clic (netGuard, ui.js)
  const pill = $("#net-pill");
  const label = $("#net-label");
  if (!net || net.ok === null || net.ok === undefined) {
    pill.className = "pill net unknown";
    label.textContent = "pays inconnu";
    pill.title = net && net.error ? `Pays de l'IP inconnu : ${net.error}` : "Pays de l'IP inconnu";
    return;
  }
  pill.className = `pill net ${net.ok ? "ok" : "bad"}`;
  label.textContent = net.country || "?";
  const where = net.city ? `${net.country} · ${net.city}` : net.country;
  pill.title = [net.ok ? `IP en ${net.country_name || where}` : `IP hors ${net.expected_country_name} (${net.country}) : ne publie pas`,
    `IP ${net.ip || "?"}`, net.isp ? `FAI ${net.isp}` : "", net.city ? `Ville ${net.city}` : "",
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
  // Fil d'Ariane : le groupe (Production / Configuration) vient du surtitre de l'ecran, absent sur la fiche clip.
  const eyebrow = $(".eyebrow", section);
  $("#crumb-group").textContent = eyebrow ? eyebrow.textContent : "";
  $("#crumb-group").hidden = !eyebrow;
  document.title = `${section.dataset.title} · Clipper`;
  window.scrollTo(0, 0);
  $(".sidenav").classList.remove("open");
  renderCurrent();
  if (anchor) {
    const target = document.getElementById(anchor);
    if (target) target.scrollIntoView();
  }
}

/* Ecrans non migres : ecrit body.innerHTML seulement si le HTML calcule differe
   du precedent, donc un rendu identique (evenement SSE sans changement, polling)
   ne touche pas au DOM. Les ecrans v3 passent par morph() et n'en ont pas besoin ;
   la garde disparait avec le dernier ecran migre (ADR-a308 §3). */
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
  return `<form id="assign-form">
    <header><h2>Attribuer un style</h2><p>${esc(videoId)} n'a pas de style : sans lui, ses clips ne peuvent être ni approuvés ni publiés. Le choix est journalisé et ne se change pas ensuite.</p></header>
    <section>
      <div class="field"><label for="assign-channel">Style</label>
        <select class="select" id="assign-channel" name="channel" required>${channels.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select></div>
    </section>
    <footer><button type="button" class="btn" data-variant="outline" data-dismiss>Annuler</button><button type="submit" class="btn">Attribuer</button></footer></form>`;
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
  openDialog(assignChannelHtml(videoId, channels), { size: "sm", onOpen: (dlg) => {
    $("#assign-form", dlg).onsubmit = async (e) => {
      e.preventDefault();
      const channel = $("#assign-channel", dlg).value;
      dlg.close();
      try {
        await api(`/api/videos/${encodeURIComponent(videoId)}/channel`, jsonBody("POST", { channel }));
        toast({ kind: "ok", title: "Style attribué", body: `${videoId} : ${channel}` });
        await afterVideoAction(videoId);
        if (typeof loadClips === "function") loadClips();
      } catch (err) { toastError("Attribution impossible", err); }
    };
  } });
}

/* ---------- Ajout de video (dialogue global : bouton de la barre, touche n, palette) ---------- */
async function openAddVideo(channel) {
  try { if (store.channels === null) await loadChannels(); } catch (err) { toastError("Styles indisponibles", err); }
  const channels = store.channels || [];
  openDialog(`<form id="add-form">
    <header><h2>Ajouter une vidéo</h2><p>YouTube ou VOD Twitch. Elle passe en file avec le preset de son style.</p></header>
    <section>
      <div class="field"><label for="add-url">Adresse (URL)</label><input class="input" id="add-url" name="url" type="url" required placeholder="https://…" autocomplete="off"></div>
      <div class="field"><label for="add-channel">Style</label>
        <select class="select" id="add-channel" name="channel"><option value="">Sans style (config.toml)</option>${channels.map((c) => `<option value="${esc(c)}"${c === channel ? " selected" : ""}>${esc(c)}</option>`).join("")}</select>
        ${channels.length ? "" : `<p>Aucun style : crée-en un dans l'écran Styles.</p>`}</div>
      <div class="field"><label><input type="checkbox" class="input" id="add-short" name="short_clips"> Clips courts <span class="muted" id="add-short-state"></span></label>
        <p>20-45 s, le clip démarre sur le moment fort. Non précisé : valeur du style.</p></div>
    </section>
    <footer><button type="button" class="btn" data-variant="outline" data-dismiss>Annuler</button><button type="submit" class="btn">Mettre en file</button></footer></form>`,
  { onOpen: (dlg) => {
    setTimeout(() => $("#add-url", dlg).focus(), 60);
    // Trois états : non précisé (valeur du style) -> coché (on) -> décoché (off) -> non précisé.
    const short = $("#add-short", dlg);
    let shortClips = null;
    const showShort = () => {
      short.indeterminate = shortClips === null;
      short.checked = shortClips === true;
      $("#add-short-state", dlg).textContent = shortClips === null ? "(valeur du style)" : shortClips ? "(oui)" : "(non)";
    };
    short.onclick = () => {  // pas de preventDefault : le navigateur annulerait l'état posé par showShort
      shortClips = shortClips === null ? true : shortClips === true ? false : null;
      showShort();
    };
    showShort();
    $("#add-form", dlg).onsubmit = async (e) => {
      e.preventDefault();
      const url = $("#add-url", dlg).value.trim();
      const channel = $("#add-channel", dlg).value || null;
      dlg.close();
      try {
        const entry = await api("/api/queue", jsonBody("POST", { url, channel, action: "run", ...(shortClips === null ? {} : { short_clips: shortClips }) }));
        toast({ kind: "ok", title: "Vidéo mise en file", body: entry.video_id });
        await Promise.all([loadVideos(), loadQueue()]);
        renderCurrent();
        updateCounts();
      } catch (err) { toastError("Ajout impossible", err); }
    };
  } });
}

/* ---------- Theme : clair par defaut, sombre memorise par navigateur (SPEC-7715 T12) ----------
   Le choix memorise est la seule source : jamais la preference du systeme (un seul theme par page, le README est
   en clair). L'inline de index.html applique la meme regle avant le premier rendu. `.dark` sur <html> pilote
   Basecoat et v3.css ; data-theme reste pose pour style.css le temps de la migration. */
function chooseTheme(saved) {
  return saved === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.documentElement.classList.toggle("dark", theme === "dark");
  morph($("#btn-theme"), icon(theme === "light" ? "moon" : "sun"));
  $('meta[name="theme-color"]').content = theme === "light" ? "#f6f4ef" : "#121210";
}

function toggleTheme() {
  const next = document.documentElement.classList.contains("dark") ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* stockage indisponible : choix non memorise */ }
  applyTheme(next);
}

/* ---------- Barre laterale reductible en rail (memorise par navigateur) ---------- */
function applyNav(rail) {
  if (rail) document.documentElement.dataset.nav = "rail"; else delete document.documentElement.dataset.nav;
  const btn = $("#btn-rail");
  btn.setAttribute("aria-pressed", String(rail));
  btn.setAttribute("aria-label", rail ? "Déployer la barre latérale" : "Réduire la barre latérale");
}

function toggleNav() {
  const rail = document.documentElement.dataset.nav !== "rail";
  try { localStorage.setItem(NAV_KEY, rail ? "rail" : "full"); } catch (e) { /* stockage indisponible */ }
  applyNav(rail);
}

/* ---------- Palette de commandes (Ctrl/⌘ K, SPEC-7715 T11) ----------
   paletteItems(query, store) est pure : ecrans (« Aller à »), actions, et avec une saisie les videos par titre
   ou id. Chaque entree porte une `action` serialisable que paletteRun execute. */
const PALETTE_SCREENS = [
  { id: "dashboard", label: "Tableau de bord", icon: "layout-dashboard", hash: "#/dashboard", kbd: "g d" },
  { id: "veille", label: "Veille", icon: "trending-up", hash: "#/veille" },
  { id: "videos", label: "Vidéos", icon: "film", hash: "#/videos", kbd: "g v" },
  { id: "review", label: "Revue des moments", icon: "sparkles", hash: "#/review" },
  { id: "clips", label: "Clips", icon: "clapperboard", hash: "#/clips", kbd: "g c" },
  { id: "publish", label: "Publication", icon: "send", hash: "#/publish", kbd: "g p" },
  { id: "channels", label: "Styles", icon: "tv", hash: "#/styles" },
  { id: "stats", label: "Statistiques", icon: "chart-column", hash: "#/stats", kbd: "g s" },
  { id: "accounts", label: "Comptes", icon: "lock", hash: "#/accounts" },
  { id: "settings", label: "Réglages", icon: "settings", hash: "#/settings" },
  { id: "journal", label: "Journal", icon: "activity", hash: "#/journal" },
];
const PALETTE_ACTIONS = [
  { id: "add-video", label: "Ajouter une vidéo", icon: "plus", kbd: "n" },
  { id: "new-publication", label: "Nouvelle publication", icon: "send" },
  { id: "theme", label: "Changer de thème", icon: "sun" },
  { id: "shortcuts", label: "Raccourcis clavier", icon: "keyboard", kbd: "?" },
];

// Comparaison sans accent ni casse : « video » trouve « Vidéos ».
function paletteNorm(s) {
  return String(s == null ? "" : s).normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
}

function paletteItems(query, store) {
  const q = paletteNorm(query).trim();
  const hit = (...fields) => !q || fields.some((f) => paletteNorm(f).includes(q));
  const screens = PALETTE_SCREENS.filter((s) => hit(s.label, s.id))
    .map((s) => ({ group: "Aller à", label: s.label, icon: s.icon, kbd: s.kbd || null, action: { kind: "screen", hash: s.hash } }));
  const actions = PALETTE_ACTIONS.filter((a) => hit(a.label))
    .map((a) => ({ group: "Actions", label: a.label, icon: a.icon, kbd: a.kbd || null, action: { kind: "action", id: a.id } }));
  const videos = !q ? [] : ((store && store.videos) || []).filter((v) => hit(v.title, v.video_id)).slice(0, 8)
    .map((v) => ({ group: "Vidéos", label: v.title || v.video_id, icon: "film", kbd: null,
      sub: `${v.video_id}${STATUS_LABELS[v.status] ? ` · ${STATUS_LABELS[v.status]}` : ""}`, action: { kind: "video", id: v.video_id } }));
  return [...screens, ...actions, ...videos];
}

let paletteList = [];
let paletteSel = 0;

function paletteRender() {
  paletteList = paletteItems($("#palette-q").value, store).slice(0, 16);
  paletteSel = Math.min(paletteSel, Math.max(0, paletteList.length - 1));
  let group = null;
  const html = paletteList.map((it, i) => {
    const head = it.group !== group ? `<li class="grp" role="presentation">${esc(it.group)}</li>` : "";
    group = it.group;
    return head + `<li role="option" id="palette-opt-${i}" aria-selected="${i === paletteSel}" data-i="${i}">${icon(it.icon, "i-sm")}<span>${esc(it.label)}${it.sub ? `<span class="sub">${esc(it.sub)}</span>` : ""}</span>${it.kbd ? `<kbd class="kbd">${esc(it.kbd)}</kbd>` : ""}</li>`;
  }).join("") || `<li class="grp" role="presentation">Aucun résultat</li>`;
  morph($("#palette-list"), html);
  $$("#palette-list li[role=option]").forEach((li) => (li.onclick = () => paletteRun(paletteList[Number(li.dataset.i)])));
  const sel = $(`#palette-opt-${paletteSel}`);
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

function paletteRun(item) {
  if (!item) return;
  $("#palette").close();
  const a = item.action;
  if (a.kind === "screen") { location.hash = a.hash; return; }
  if (a.kind === "video") { location.hash = `#/videos/${encodeURIComponent(a.id)}`; return; }
  if (a.id === "add-video") openAddVideo();
  else if (a.id === "theme") toggleTheme();
  else if (a.id === "shortcuts") $("#shortcuts").showModal();
  else if (a.id === "new-publication") {
    location.hash = "#/publish";
    if (typeof pubOpenForm === "function") setTimeout(() => pubOpenForm(), 0);
  }
}

function openPalette() {
  const dlg = $("#palette");
  if (dlg.open) return;
  $("#palette-q").value = "";
  paletteSel = 0;
  paletteRender();
  dlg.showModal();
  $("#palette-q").focus();
}

/* ---------- Raccourcis clavier (T11) ----------
   shortcutAction(key, pending, typing) est pure : `g` ouvre un accord puis d v c p s choisit l'ecran ; n ajoute une
   video ; / cherche dans l'ecran ; ? liste les raccourcis. Rien dans un champ de saisie, rien avec un modificateur,
   rien tant qu'un dialogue ou un panneau est ouvert. Aucune animation n'est jouee sur ces actions. */
const GOTO_KEYS = { d: "#/dashboard", v: "#/videos", c: "#/clips", p: "#/publish", s: "#/stats" };

function shortcutAction(key, pending, typing) {
  if (typing) return null;
  if (pending === "g") return GOTO_KEYS[key] ? { kind: "goto", hash: GOTO_KEYS[key] } : { kind: "none" };
  if (key === "g") return { kind: "chord" };
  if (key === "n") return { kind: "add" };
  if (key === "/") return { kind: "search" };
  if (key === "?") return { kind: "help" };
  return null;
}

let chord = null;
let chordTimer = null;

function onShellKey(e) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (e.key === "Escape") {
    if (drawer.isOpen()) { drawer.close(); e.preventDefault(); }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey || document.querySelector("dialog[open]") || closeCurrent) return;
  const t = e.target;
  const typing = Boolean(t && (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || t.isContentEditable));
  const act = shortcutAction(e.key, chord, typing);
  clearTimeout(chordTimer);
  chord = null;
  if (!act) return;
  if (act.kind === "chord") { chord = "g"; chordTimer = setTimeout(() => { chord = null; }, 900); return; }
  if (act.kind === "none") return;
  e.preventDefault();
  if (act.kind === "goto") location.hash = act.hash;
  else if (act.kind === "add") openAddVideo();
  else if (act.kind === "help") $("#shortcuts").showModal();
  else if (act.kind === "search") {
    const field = $(`#screen-${currentScreen} [data-screen-search]`);
    if (field) field.focus();
  }
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
  let savedTheme = null, savedNav = null;
  try { savedTheme = localStorage.getItem(THEME_KEY); savedNav = localStorage.getItem(NAV_KEY); } catch (e) { /* stockage indisponible */ }
  applyTheme(chooseTheme(savedTheme));
  applyNav(savedNav === "rail");
  paintNotifButton();

  $("#btn-theme").onclick = toggleTheme;
  $("#btn-rail").onclick = toggleNav;
  $("#btn-notif").onclick = toggleNotifications;
  $("#btn-add").onclick = () => openAddVideo();
  $("#btn-palette").onclick = openPalette;
  $("#btn-menu").onclick = () => {
    const sb = $(".sidenav");
    sb.classList.add("open");
    showOverlay(() => sb.classList.remove("open"));
  };
  $$("#nav a").forEach((a) => a.addEventListener("click", () => { if (closeCurrent) closeLayer(); drawer.close(); }));
  $("#palette-q").oninput = () => { paletteSel = 0; paletteRender(); };
  $("#palette-q").onkeydown = (e) => {
    if (e.key === "ArrowDown") { paletteSel = Math.min(paletteSel + 1, paletteList.length - 1); paletteRender(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { paletteSel = Math.max(paletteSel - 1, 0); paletteRender(); e.preventDefault(); }
    else if (e.key === "Enter") { paletteRun(paletteList[paletteSel]); e.preventDefault(); }
  };
  // Dialogues statiques de la coquille : clic sur le fond et boutons [data-dismiss] ferment.
  $$("#layers dialog").forEach((d) => {
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    $$("[data-dismiss]", d).forEach((b) => (b.onclick = () => d.close()));
  });
  document.addEventListener("keydown", onShellKey);

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
      if (body) morph(body, emptyState("circle-alert", "Chargement impossible", String(err.message || err)));
    });
})();
