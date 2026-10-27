/* Ecran « Statistiques » (SPEC-47e2) : un tableau de bord par compte, alimente UNIQUEMENT par le releve de
   TikTok Studio (ou, pour un compte YouTube, de YouTube Studio : GET /api/stats/youtube, /api/stats/youtube/<compte>,
   /api/stats/youtube/<compte>/videos, POST /api/stats/youtube/open et /refresh, SPEC-5e50 R6), releve seulement quand on s'en sert. Lit GET /api/stats/tiktok (comptes, avec `refreshing`),
   /api/stats/tiktok/<compte>?period= (vue d'ensemble), /api/stats/tiktok/<compte>/videos (liste triable) et
   /api/stats/tiktok/<compte>/videos/<id> (fiche). A l'ouverture, POST /api/stats/tiktok/open lance un releve en tache
   de fond si le dernier est perime (le dernier releve connu reste affiche, « Relevé en cours » jusqu'a la fin) ; le
   bouton « Relever maintenant » fait POST /api/stats/tiktok/refresh. Un seul releve a la fois par compte (serveur). Adresses : #/stats[/<compte>[/videos[/<id>]]].
   Graphiques en SVG calcule ici, sans bibliotheque (ADR-09ad). Une valeur que TikTok n'affiche pas (null) est un
   tiret, jamais un 0. Les mesures internes (cout par video, duree par etape : GET /api/measures) ont leur section ici ; le Tableau
   de bord n'en garde que le resume. Charge apres screens.js dont il remplace l'entree Screens.stats. */
"use strict";

const STATS_STALE_MS = 4000;
const STATS_POLL_MS = 3000; // pendant un releve en tache de fond : relecture de l'etat des comptes
const STATS_VISIT_GAP_MS = 30000; // ecran quitte plus longtemps que ca : nouvelle ouverture, donc nouvelle demande
const STATS_PERIODS = [7, 28, 60, 365];
const STATS_METRICS = [
  { id: "views", label: "Vues de vidéo", icon: "eye" },
  { id: "profile_views", label: "Vues du profil", icon: "user" },
  { id: "likes", label: "J'aime", icon: "heart" },
  { id: "comments", label: "Commentaires", icon: "message-square" },
  { id: "shares", label: "Partages", icon: "share-2" },
];
// Colonnes de la liste des videos : cle -> [libelle, numerique]. La cle est celle de ?sort= (clipper.tiktok.VIDEO_SORTS).
const STATS_COLUMNS = [
  ["caption", "Légende", false], ["posted_at", "Date", false], ["views", "Vues", true], ["likes", "J'aime", true],
  ["comments", "Commentaires", true], ["shares", "Partages", true], ["avg_watch_s", "Temps moyen", true],
  ["watched_full", "% vu en entier", true],
];
const STATS_VISIBILITY = { public: "Public", friends: "Amis", private: "Privé" };
const STATS_VIDEO_TABS = [["overview", "Vue d'ensemble"], ["viewers", "Spectateurs"], ["engagement", "Engagement"]];
const STATS_VIEWER_SECTIONS = [["types", "Types de spectateurs"], ["age", "Âge"], ["gender", "Sexe"], ["locations", "Lieux"]];

/* Colonnes de la liste des Shorts d'un compte YouTube : cle -> [libelle, numerique] (cle de ?sort=, clipper.youtube.VIDEO_SORTS). */
const STATS_YT_COLUMNS = [
  ["title", "Short", false], ["posted_at", "Date", false], ["views", "Vues", true], ["likes", "J'aime", true],
  ["comments", "Commentaires", true], ["avg_view_s", "Durée moy. d'une vue", true], ["watched_share", "Restés à regarder", true],
  ["swiped_share", "Balayés", true], ["subscribers_gained", "Abonnés gagnés", true],
];
const STATS_YT_TILES = [
  ["views", "Vues"], ["likes", "J'aime"], ["comments", "Commentaires"], ["engaged_views", "Vues engagées"],
  ["watch_hours", "Heures regardées"], ["subscribers_gained", "Abonnés gagnés"], ["shorts", "Shorts publiés"],
];
const STATS_SERVICES = { tiktok: "TikTok", youtube: "YouTube" };
const statsIsYoutube = (account) => Boolean(account && account.service === "youtube");

const statsUi = {
  accounts: null, overview: null, videos: null, videosAccount: "", video: null, videoAccount: "", error: null, loading: null, dirty: false, at: 0, key: "",
  period: 28, metric: "views", sort: { key: "posted_at", dir: "desc" }, q: "", vtab: "overview", refreshing: false,
  opened: {}, lastRender: 0, poll: null, wasRefreshing: {}, learning: null, learningError: null, measures: null, measuresError: null,
};

/* ---------- formats ---------- */

const STATS_NOT_GIVEN = "non fourni par TikTok Studio";
/* Valeur d'une tuile : jamais un tiret, TikTok Studio ne l'a simplement pas donnee (un vrai 0 reste 0). */
const tileValue = (n) => (n === null || n === undefined ? STATS_NOT_GIVEN : fr(n));
const statsNum = (n) => (n === null || n === undefined ? "—" : fr(n));
const statsPctValue = (fraction, digits) => (fraction === null || fraction === undefined ? "—" : `${fr(fraction * 100, digits || 0)} %`);

/* « 12 s », « 1:05 » : secondes -> mm:ss ; null -> tiret. */
function statsDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/* Evolution : « ▲ +12,5 % » (vert), « ▼ -3 % » (rouge), « ■ 0 % » ; null -> tiret. */
function statsDelta(change) {
  if (change === null || change === undefined) return { cls: "flat", text: "variation non fournie" };
  const sign = change > 0.05 ? "▲" : change < -0.05 ? "▼" : "■";
  const cls = change > 0.05 ? "up" : change < -0.05 ? "down" : "flat";
  return { cls, text: `${sign} ${change > 0 ? "+" : ""}${fr(change, 1)} %` };
}

/* Date relevee sur la page (« 2026-09-30T14:05:00 », sans fuseau) -> « 30/09 · 14:05 » ; absente -> tiret.
   Jamais de conversion de fuseau : c'est l'heure que TikTok Studio affiche. */
function statsPostedAt(iso, text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/.exec(iso || "");
  if (!m) return text || "—";
  return m[4] ? `${m[3]}/${m[2]} · ${m[4]}:${m[5]}` : `${m[3]}/${m[2]}/${m[1]}`;
}

/* Date d'un releve (ISO avec fuseau) en heure de Paris, comme tout le reste de la console. */
const statsWhen = (iso) => fmtParis(iso, { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).replace(",", " ·");

/* Vignette verticale : degrade stable tire de l'identifiant du post (TikTok ne donne pas d'image hors ligne). */
function statsPoster(postId) {
  let h = 0;
  for (const ch of String(postId)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `background:linear-gradient(160deg,hsl(${h % 360} 45% 32%),hsl(${(h >> 8) % 360} 50% 22%))`;
}

/* ---------- adresse : #/stats[/<compte>[/videos[/<id>]]] ---------- */

function statsRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/").slice(1).map((p) => decodeURIComponent(p));
  return { account: parts[0] || "", tab: parts[1] === "videos" ? "videos" : "overview", post: parts[1] === "videos" ? parts[2] || "" : "" };
}

function statsHref(account, tab, post) {
  const base = `#/stats/${encodeURIComponent(account)}`;
  if (tab === "videos") return post ? `${base}/videos/${encodeURIComponent(post)}` : `${base}/videos`;
  return base;
}

/* Compte affiche : celui de l'adresse s'il existe, sinon le dernier consulte (s'il existe encore), sinon le
   premier qui a des vues (cherche au chargement, statsPickDefault), sinon le premier. */
const STATS_LAST_KEY = "clipper-stats-account";

function statsLastConsulted() {
  try { return localStorage.getItem(STATS_LAST_KEY) || ""; } catch (err) { return ""; } // stockage indisponible : pas de memoire
}

function statsAccount() {
  const accounts = statsUi.accounts || [];
  const find = (id) => accounts.find((a) => a.account === id);
  return find(statsRoute().account) || find(statsLastConsulted()) || find(statsUi.defaultAccount) || accounts[0] || null;
}

/* Memorise le compte consulte (adresse explicite seulement : le choix par defaut n'est pas une consultation). */
function statsRemember() {
  const wanted = statsRoute().account;
  if (!wanted || !(statsUi.accounts || []).some((a) => a.account === wanted)) return;
  try { localStorage.setItem(STATS_LAST_KEY, wanted); } catch (err) { /* stockage indisponible : le choix ne sera pas retenu */ }
}

/* Aucun compte demande ni retenu : le premier dont la vue d'ensemble compte des vues (releves de TikTok Studio). */
async function statsPickDefault() {
  const accounts = statsUi.accounts || [];
  if (statsRoute().account || accounts.some((a) => a.account === statsLastConsulted())) return;
  for (const a of accounts.filter((x) => !statsIsYoutube(x))) {
    const overview = await api(`/api/stats/tiktok/${encodeURIComponent(a.account)}?period=${statsUi.period}`);
    if (overview.tiles && overview.tiles.views && overview.tiles.views.value > 0) { statsUi.defaultAccount = a.account; return; }
  }
  statsUi.defaultAccount = "";
}

/* ---------- chargement ---------- */

function statsKey() {
  const account = statsAccount();
  if (!account) return "";
  const r = statsRoute();
  return [account.account, r.tab, r.post, statsUi.period, statsUi.sort.key, statsUi.sort.dir, statsUi.q].join("|");
}

function statsLoad() {
  if (statsUi.loading) { statsUi.dirty = true; return statsUi.loading; }
  statsUi.loading = (async () => {
    try {
      statsUi.accounts = (await api("/api/stats/tiktok")).accounts.map((a) => ({ ...a, service: "tiktok" }));
      try { // une panne de la liste YouTube ne masque pas TikTok, mais elle est dite a l'ecran (jamais silencieuse)
        statsUi.accounts.push(...(await api("/api/stats/youtube")).accounts.map((a) => ({ ...a, service: "youtube" })));
        statsUi.youtubeError = null;
      } catch (err) { statsUi.youtubeError = err; }
      statsRemember();
      if (statsUi.defaultAccount === undefined) await statsPickDefault();
      try { statsUi.learning = await api("/api/learning"); statsUi.learningError = null; } catch (err) { statsUi.learningError = err; }
      try { statsUi.measures = await api("/api/measures"); statsUi.measuresError = null; } catch (err) { statsUi.measuresError = err; }
      const account = statsAccount();
      if (account) {
        const r = statsRoute();
        const youtube = statsIsYoutube(account);
        const base = `${youtube ? "/api/stats/youtube" : "/api/stats/tiktok"}/${encodeURIComponent(account.account)}`;
        if (r.tab === "overview") {
          statsUi.overview = await api(youtube ? base : `${base}?period=${statsUi.period}`);
        } else if (youtube) {
          const sort = STATS_YT_COLUMNS.some(([key]) => key === statsUi.sort.key) ? statsUi.sort : { key: "posted_at", dir: "desc" };
          const params = new URLSearchParams({ sort: sort.key, dir: sort.dir, q: statsUi.q });
          statsUi.videos = (await api(`${base}/videos?${params}`)).videos;
          statsUi.videosAccount = account.account;
        } else if (r.post) {
          statsUi.video = (await api(`${base}/videos/${encodeURIComponent(r.post)}`)).video;
          statsUi.videoAccount = account.account;
        } else {
          const params = new URLSearchParams({ sort: statsUi.sort.key, dir: statsUi.sort.dir, q: statsUi.q });
          statsUi.videos = (await api(`${base}/videos?${params}`)).videos;
          statsUi.videosAccount = account.account;
        }
      }
      statsUi.error = null;
      statsUi.key = statsKey();
    } catch (err) {
      statsUi.error = err;
      statsUi.key = statsKey();
    } finally {
      statsUi.loading = null;
      statsUi.at = Date.now();
    }
    if (currentScreen === "stats") {
      statsNotifyDone();
      renderCurrent();
      const account = statsAccount();
      if (account) statsOpen(account);
      statsSchedulePoll();
    }
    if (statsUi.dirty) { statsUi.dirty = false; statsLoad(); }
  })();
  return statsUi.loading;
}

/* Releve en cours pour ce compte : lance depuis l'ecran (bouton) ou en tache de fond sur le serveur. */
const statsRefreshing = (account) => statsUi.refreshing || Boolean(account && account.refreshing);

/* A l'ouverture de l'ecran (une fois par compte et par visite) : le serveur lance un releve en tache de fond si le
   dernier est perime ou absent (R4a) ; on affiche tout de suite le dernier releve connu, « Relevé en cours » jusqu'a la fin. */
async function statsOpen(account) {
  if (statsUi.opened[account.account] || !account.ready) return;
  statsUi.opened[account.account] = Date.now();
  try {
    const sent = await api(statsIsYoutube(account) ? "/api/stats/youtube/open" : "/api/stats/tiktok/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account: account.account }) });
    if (!sent.started) return;
  } catch (err) {
    toastError(`Relevé ${STATS_SERVICES[account.service] || "TikTok"} impossible`, err);
    return;
  }
  statsUi.at = 0;
  statsLoad();
}

/* Tant qu'un releve tourne sur le serveur : on relit les comptes ; a la fin le dernier releve est recharge. */
function statsSchedulePoll() {
  clearTimeout(statsUi.poll);
  statsUi.poll = null;
  const account = statsAccount();
  if (currentScreen !== "stats" || !account || !account.refreshing) return;
  statsUi.poll = setTimeout(() => { statsUi.poll = null; statsUi.at = 0; statsLoad(); }, STATS_POLL_MS);
}

/* Un releve en tache de fond qui vient de finir : dit s'il a reussi ou pourquoi il a echoue (jamais silencieux). */
function statsNotifyDone() {
  for (const a of statsUi.accounts || []) {
    const was = statsUi.wasRefreshing[a.account];
    statsUi.wasRefreshing[a.account] = Boolean(a.refreshing);
    if (!was || a.refreshing) continue;
    if (a.refresh_error) toast({ kind: "bad", title: `Relevé ${STATS_SERVICES[a.service] || "TikTok"} impossible`, body: `${a.label} : ${a.refresh_error}`, ms: 7000 });
    else toast({ kind: "ok", title: "Relevé terminé", body: `${a.label} : statistiques à jour.` });
  }
}

// Un evenement serveur (publication, releve) rend les chiffres obsoletes.
document.addEventListener("clipper:event", () => {
  statsUi.at = 0;
  if (currentScreen === "stats") statsLoad();
});
window.addEventListener("resize", () => { if (currentScreen === "stats") renderCurrent(); });

/* ---------- graphique SVG ---------- */

/* Trace d'une serie : un segment par suite de valeurs presentes, une valeur absente (jour sans releve) coupe la ligne. */
function statsLinePaths(values, x, y) {
  const paths = [];
  let run = [];
  values.forEach((v, i) => {
    if (v === null || v === undefined) { if (run.length) paths.push(run); run = []; } else run.push(`${x(i).toFixed(1)} ${y(v).toFixed(1)}`);
  });
  if (run.length) paths.push(run);
  return paths.map((r) => `M${r.join("L")}`).join("");
}

/* Plus haute graduation « ronde » au-dessus de la valeur max (4 graduations). */
function statsNiceMax(raw) {
  const max = Math.max(1, raw);
  const step = Math.pow(10, Math.floor(Math.log10(max / 4)));
  const nice = [1, 2, 2.5, 5, 10].map((k) => k * step).find((k) => max / k <= 4) || step * 10;
  return Math.ceil(max / nice) * nice;
}

const statsAxis = (v) => (v >= 1000 ? `${fr(Math.round(v / 100) / 10, 1)} k` : fr(Math.round(v * 10) / 10, v < 10 && v % 1 ? 1 : 0));

/* cfg : {labels, series: [{values, cls}], width, height, max, fy, tip(i), aria}. Les jours sans valeur sont des bandes grises. */
function statsChart(cfg) {
  const W = cfg.width, H = cfg.height || 240, L = 44, R = 12, T = 12, B = 26;
  const n = cfg.labels.length;
  const all = cfg.series.flatMap((s) => s.values).filter((v) => v !== null && v !== undefined);
  const top = cfg.max !== undefined ? cfg.max : statsNiceMax(Math.max(1, ...all));
  const x = (i) => L + (n === 1 ? 0 : (i / (n - 1)) * (W - L - R));
  const y = (v) => T + (1 - v / top) * (H - T - B);
  const band = n > 1 ? (W - L - R) / (n - 1) : 0;
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = (top / 4) * k;
    g += `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text x="${L - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end">${esc(cfg.fy ? cfg.fy(v) : statsAxis(v))}</text>`;
  }
  const first = cfg.series[0].values;
  first.forEach((v, i) => { if ((v === null || v === undefined) && band) g += `<rect class="gapband" x="${(x(i) - band / 2).toFixed(1)}" y="${T}" width="${band.toFixed(1)}" height="${H - T - B}"/>`; });
  const every = Math.max(1, Math.ceil(n / (W < 500 ? 5 : 8)));
  cfg.labels.forEach((label, i) => {
    if ((i % every === 0 && (n - 1 - i >= every * 0.7 || i === 0)) || i === n - 1) {
      g += `<text x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(label)}</text>`;
    }
  });
  cfg.series.slice().reverse().forEach((s) => {
    const d = statsLinePaths(s.values, x, y);
    if (d) g += `<path class="ln ${s.cls || ""}" d="${d}"/>`;
  });
  first.forEach((v, i) => {
    g += `<circle class="hit" cx="${x(i).toFixed(1)}" cy="${(v === null || v === undefined ? T + (H - T - B) / 2 : y(v)).toFixed(1)}" r="${v === null || v === undefined ? 4 : 6}"><title>${esc(cfg.tip(i))}</title></circle>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(cfg.aria)}">${g}</svg>`;
}

/* Largeur du graphique : celle de l'ecran, bornee (rendu repris au redimensionnement). */
function statsChartWidth() {
  const body = $("#screen-stats [data-body]");
  return Math.max(280, Math.min(1100, ((body && body.clientWidth) || 800) - 48));
}

/* ---------- blocs ---------- */

function statsEmpty(iconName, title, text, action) { return `<div class="card s3-card">${emptyState(iconName, title, text, action)}</div>`; }

const statsScanButton = (account) => `<button class="btn" type="button" data-stats-scan${statsRefreshing(account) || !account.ready ? " disabled" : ""}>${icon("refresh-cw", statsRefreshing(account) ? "spin" : "")}${statsRefreshing(account) ? "Relevé en cours" : "Relever maintenant"}</button>`;

const statsFullButton = (account) => `<button class="btn" data-variant="outline" type="button" data-stats-full${statsRefreshing(account) || !account.ready ? " disabled" : ""}>${icon("layers", "")}Relevé complet</button>`;

/* « Relevé complet » : detail de TOUTES les videos (le releve normal est plafonne aux plus recentes) ; la confirmation
   dit combien de pages TikTok Studio seront ouvertes (2 + 3 par video connue, jamais devine quand rien n'est connu). */
function statsFullConfirmBody(account) {
  if (!account.full_pages) return "Clipper va ouvrir TikTok Studio et relever le détail de toutes les vidéos, sans plafond. Le nombre de pages à ouvrir est inconnu : aucun relevé n'est encore enregistré pour ce compte. Cela peut durer longtemps.";
  return `Clipper va ouvrir environ ${fr(account.full_pages)} pages dans TikTok Studio (${fr(account.known_posts)} vidéo${account.known_posts > 1 ? "s" : ""} connue${account.known_posts > 1 ? "s" : ""}, 3 pages par vidéo + 2 pages d'ensemble) et relever le détail de chacune, sans plafond. Cela peut durer longtemps ; le relevé normal, lui, reste plafonné aux vidéos les plus récentes.`;
}

function statsControls(account) {
  const accounts = statsUi.accounts;
  const options = accounts.map((a) => `<option value="${esc(a.account)}"${a.account === account.account ? " selected" : ""}>${esc(STATS_SERVICES[a.service] || "TikTok")} · ${esc(a.label)} · ${esc(a.account)}</option>`).join("");
  const youtube = statsIsYoutube(account);
  const periods = `${STATS_PERIODS.map((n) => `<button type="button" data-stats-period="${n}" class="${n === statsUi.period ? "on" : ""}" aria-pressed="${n === statsUi.period}">${n} jours</button>`).join("")}`;
  const when = account.fetched_at
    ? `Dernier relevé : <b>${esc(statsWhen(account.fetched_at))}</b><br><span class="muted">${esc(fr(account.snapshots))} relevé${account.snapshots > 1 ? "s" : ""} enregistré${account.snapshots > 1 ? "s" : ""}</span>`
    : "Dernier relevé : <b>jamais</b>";
  const notReady = account.ready ? "" : `<div class="alert" role="status" data-stats-notready>${icon("lock")}<h2>Compte non prêt à publier : aucun relevé n'est lancé.</h2><section>${esc(account.not_ready_reason)}</section></div>`;
  const running = statsRefreshing(account) ? `<div class="alert" role="status" data-stats-running>${icon("refresh-cw", "spin")}<h2>Relevé en cours.</h2><section>${account.fetched_at ? "Les chiffres affichés sont ceux du dernier relevé ; ils se mettent à jour à la fin." : "Aucun relevé enregistré pour l'instant ; les chiffres apparaissent à la fin."}</section></div>` : "";
  const failed = account.error ? `<div class="alert" data-variant="destructive" role="alert" data-stats-error>${icon("circle-alert")}<h2>Dernier relevé arrêté (${esc(account.error.code)}).</h2><section>${esc(account.error.reason)}</section></div>` : "";
  return `<section class="ctl" aria-label="Compte et période">
      <div class="acct field"><label for="stats-account">Compte TikTok ou YouTube</label><select class="input" id="stats-account" data-stats-account>${options}</select></div>
      ${youtube ? "" : `<div class="field"><span class="field-label" id="stats-lbl-period">Période</span><div class="seg" data-stats-periods role="group" aria-labelledby="stats-lbl-period">${periods}</div></div>`}
      <div class="scan"><div class="scan-when">${when}</div>${statsScanButton(account)}${youtube ? "" : statsFullButton(account)}</div>
    </section>${notReady}${running}${failed}`;
}

function statsTabs(account, tab, count) {
  const tabs = [["overview", "Vue d'ensemble", null], ["videos", "Vidéos", count]];
  return `<div class="tabs" role="tablist" aria-label="Sections">${tabs.map(([id, label, n]) =>
    `<a role="tab" class="${tab === id ? "on" : ""}" aria-selected="${tab === id}" href="${statsHref(account.account, id)}" data-stats-tab="${id}">${esc(label)}${n === null ? "" : ` <span class="n">${esc(fr(n))}</span>`}</a>`).join("")}</div>`;
}

function statsOverview(account) {
  const data = statsUi.overview;
  if (!data || data.account !== account.account) return `<div class="skeleton sk-card"></div>`;
  if (!data.tiles) return statsEmpty("inbox", `Aucun relevé pour ${account.label}`, "Clipper n'a encore rien relevé sur TikTok Studio pour ce compte. Le premier relevé remplit les tuiles et démarre les courbes par jour.");
  const period = data.period;
  const metric = STATS_METRICS.find((m) => m.id === statsUi.metric) || STATS_METRICS[0];
  const tiles = STATS_METRICS.map((m) => {
    const tile = data.tiles[m.id];
    const given = statsDelta(tile.change_pct);
    const hist = tile.history_change_pct === null || tile.history_change_pct === undefined ? "" : `<small title="Calculée sur nos relevés : valeur il y a ${period} jours">relevés : ${esc(statsDelta(tile.history_change_pct).text)}</small>`;
    return `<button class="kpi kpi-btn" type="button" data-stats-metric="${m.id}" aria-pressed="${m.id === metric.id}">
      <div class="kpi-label">${icon(m.icon)}${esc(m.label)}</div><div class="kpi-value${tile.value === null || tile.value === undefined ? " none" : ""}">${esc(tileValue(tile.value))}</div>
      ${tile.change_pct === null || tile.change_pct === undefined ? "" : `<span class="delta ${given.cls}">${esc(given.text)} <small>vs ${period} j précédents (TikTok)</small></span>`}${hist}</button>`;
  }).join("");
  const s = data.series[metric.id];
  const chart = statsChart({
    labels: s.labels.map((d) => `${d.slice(8, 10)}/${d.slice(5, 7)}`), width: statsChartWidth(),
    series: [{ values: s.values }, { values: s.previous, cls: "prev" }], aria: `${metric.label} par jour sur ${period} jours`,
    tip: (i) => `${s.labels[i].slice(8, 10)}/${s.labels[i].slice(5, 7)} : ${s.values[i] === null ? "pas de relevé" : fr(s.values[i])}${s.previous[i] === null ? "" : ` · période précédente : ${fr(s.previous[i])}`}`,
  });
  const noPoint = s.values.every((v) => v === null);
  return `<div class="kpis" data-stats-tiles>${tiles}</div>
    <div class="card s3-card" data-stats-curve>
      <div class="s3-head"><h2>${esc(metric.label)} par jour</h2><div class="s3-right"><span class="muted" style="font-size:13px">${period} derniers jours · ${esc(tileValue(data.tiles[metric.id].value))} sur la période</span></div></div>
      <div class="toolbar" style="margin:16px 16px 0"><div class="seg" role="group" aria-label="Métrique de la courbe">${STATS_METRICS.map((m) => `<button type="button" data-stats-metric="${m.id}" class="${m.id === metric.id ? "on" : ""}" aria-pressed="${m.id === metric.id}">${esc(m.label)}</button>`).join("")}</div></div>
      <div class="chart-wrap"><div class="chart">${noPoint ? `<p class="muted stats-empty">TikTok n'affichait aucune valeur pour « ${esc(metric.label)} » sur les relevés.</p>` : chart}</div></div>
      <div class="legend"><span><i></i>${period} derniers jours</span><span><i class="prev"></i>${period} jours précédents</span><span><i class="gap"></i>Jour sans relevé</span></div>
      <div class="pager-note"><span class="note">${icon("info", "i-sm")}<span>Les tuiles viennent du dernier relevé de la page Données analytiques. Un point de la courbe est la valeur de la tuile ${period} jours au dernier relevé du jour : un jour sans relevé reste vide au lieu d'être inventé.</span></span></div>
    </div>`;
}

/* ---------- compte YouTube : tuiles et liste des Shorts relevés dans YouTube Studio ---------- */

const statsYtValue = (n) => (n === null || n === undefined ? "non fourni par YouTube Studio" : fr(n));
const statsYtCell = (key, v) => {
  const value = v[key];
  if (key === "title") return null;
  if (key === "posted_at") return statsPostedAt(value, v.posted_at_text);
  if (key === "avg_view_s") return statsDuration(value);
  if (key === "watched_share" || key === "swiped_share") return statsPctValue(value, 1);
  return statsNum(value);
};

function statsYoutubeOverview(account) {
  const data = statsUi.overview;
  if (!data || data.account !== account.account) return `<div class="skeleton sk-card"></div>`;
  if (!data.fetched_at) return statsEmpty("inbox", `Aucun relevé pour ${account.label}`, "Clipper n'a encore rien relevé sur YouTube Studio pour ce compte. Le premier relevé remplit les tuiles et la liste des Shorts.");
  const tiles = STATS_YT_TILES.map(([id, label]) => `<div class="kpi"><div class="kpi-label">${esc(label)}</div><div class="kpi-value${data.tiles[id] === null || data.tiles[id] === undefined ? " none" : ""}">${esc(statsYtValue(data.tiles[id]))}</div></div>`).join("");
  return `<div class="kpis" data-stats-tiles>${tiles}</div>
    <div class="card s3-card"><div class="pager-note"><span class="note">${icon("info", "i-sm")}<span>Totaux des dernières valeurs relevées dans YouTube Studio pour les Shorts publiés. « Restés à regarder » et « Balayés » sont la part des vues qui ont continué ou balayé (vues vs balayées) ; une valeur que Studio n'affiche pas reste vide, jamais 0.</span></span></div></div>`;
}

function statsYoutubeVideos(account) {
  const videos = statsUi.videos;
  if (!videos || statsUi.videosAccount !== account.account) return `<div class="skeleton sk-card"></div>`;
  if (!account.fetched_at) return statsEmpty("inbox", `Aucun Short relevé pour ${account.label}`, "Les Shorts apparaissent après le premier relevé de Contenus > Shorts dans YouTube Studio.");
  const sort = STATS_YT_COLUMNS.some(([key]) => key === statsUi.sort.key) ? statsUi.sort : { key: "posted_at", dir: "desc" };
  const head = STATS_YT_COLUMNS.map(([key, label, right]) =>
    `<th class="${right ? "r" : ""}" ${sort.key === key ? `aria-sort="${sort.dir === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" data-stats-sort="${key}">${esc(label)}<span class="arr" aria-hidden="true">${sort.key === key ? (sort.dir === "asc" ? "▲" : "▼") : ""}</span></button></th>`).join("");
  const rows = videos.map((v) => `<tr data-stats-yt="${esc(v.post_id)}">
    <td class="c-cap"><div class="cap"><a href="${esc(v.post_url)}" target="_blank" rel="noopener">${esc(v.title || "(sans titre)")}</a></div><div class="vis"><span class="chip plain ${v.state === "published" ? "ok" : ""}">${esc(v.status || v.visibility || "—")}</span>${v.detail_not_read ? `<span class="chip plain" title="Le détail n'a pas été relu à ce relevé (plafond stats_detail_max)">détail non relu</span>` : ""}</div></td>
    ${STATS_YT_COLUMNS.slice(1).map(([key, label]) => { const cell = statsYtCell(key, v); return `<td class="${key === "posted_at" ? "c-date" : "r"} ${cell === "—" ? "dim" : ""}" data-l="${esc(label)}">${esc(cell)}</td>`; }).join("")}</tr>`).join("")
    || `<tr><td colspan="${STATS_YT_COLUMNS.length}" class="muted" style="text-align:center;padding:32px">Aucun Short ne contient « ${esc(statsUi.q)} ».</td></tr>`;
  return `<div class="card s3-card" data-stats-videos>
    <div class="s3-head"><h2>Shorts de ${esc(account.label)}</h2><div class="s3-right"><span class="input-ico">${icon("search")}<input class="input" id="stats-q" data-stats-q type="search" placeholder="Filtrer par titre" value="${esc(statsUi.q)}" aria-label="Filtrer par titre" style="width:220px"></span></div></div>
    <div class="sort-m"><select class="input" data-stats-sort-select aria-label="Trier par">${STATS_YT_COLUMNS.map(([key, label]) => `<option value="${key}"${sort.key === key ? " selected" : ""}>Trier : ${esc(label)}</option>`).join("")}</select><button class="btn" data-variant="outline" type="button" data-stats-sort-dir aria-label="Inverser le tri">${sort.dir === "asc" ? "▲" : "▼"}</button></div>
    <div class="s3-scroll"><table class="table s3-table ptable"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>
    <div class="pager-note">${icon("info", "i-sm")}<span>${esc(fr(videos.length))} Short${videos.length > 1 ? "s" : ""} relevé${videos.length > 1 ? "s" : ""} dans YouTube Studio (Contenus &gt; Shorts). Un Short programmé n'a pas encore de chiffres.</span></div></div>`;
}

/* Restriction « Pour toi » (bandeau de TikTok Studio, relevé par vidéo) : false = restreinte, true = éligible,
   null/absent = programmée ou jamais relevée : jamais comptée, jamais devinée. */
function statsRestriction(videos) {
  const known = videos.filter((v) => v.fyf_eligible === true || v.fyf_eligible === false);
  return { restricted: known.filter((v) => v.fyf_eligible === false).length, online: known.length };
}

function statsRestrictedChip(v) {
  return v.fyf_eligible === false
    ? `<span class="chip warn" data-stats-restricted title="${esc(v.fyf_notice || "")}">${icon("triangle-alert", "i-xs")}Restreinte : pas dans Pour toi</span>` : "";
}

function statsRestrictionSummary(videos) {
  const r = statsRestriction(videos);
  if (!r.online) return "";
  return `<span class="chip ${r.restricted ? "warn" : "plain ok"}" data-stats-restriction-summary>${esc(fr(r.restricted))} vidéo${r.restricted > 1 ? "s" : ""} restreinte${r.restricted > 1 ? "s" : ""} sur ${esc(fr(r.online))} en ligne</span>`;
}

function statsVideoRow(v) {
  const vis = STATS_VISIBILITY[v.visibility] || (v.visibility ? esc(v.visibility) : "—");
  const proc = v.processing;
  const nums = [statsNum(v.views), statsNum(v.likes), statsNum(v.comments), statsNum(v.shares), statsDuration(v.avg_watch_s), statsPctValue(v.watched_full, 0)];
  const labels = ["Vues", "J'aime", "Commentaires", "Partages", "Temps moyen", "% vu en entier"];
  const origin = v.outside_clipper ? `<span class="chip plain">publié hors Clipper</span>` : `<span class="chip plain ok">clip Clipper</span>`;
  return `<tr class="clickable" tabindex="0" role="link" data-stats-open="${esc(v.post_id)}" aria-label="Fiche : ${esc(v.caption || v.post_id)}">
    <td class="c-thumb"><div class="thumb-p ${proc ? "proc" : ""}" ${proc ? "" : `style="${statsPoster(v.post_id)}"`}><i></i></div></td>
    <td class="c-cap"><div class="cap">${esc(v.caption || "(sans légende)")}</div><div class="vis">${proc ? `<span class="chip warn">En cours de traitement</span>` : `<span class="chip plain ${v.visibility === "public" ? "ok" : ""}">${vis}</span>`}${statsRestrictedChip(v)}${origin}</div></td>
    <td class="c-date">${esc(statsPostedAt(v.posted_at, v.posted_at_text))}</td>
    ${nums.map((n, i) => `<td class="r ${proc || n === "—" ? "dim" : ""}" data-l="${labels[i]}">${esc(n)}</td>`).join("")}</tr>`;
}

function statsVideosList(account) {
  const videos = statsUi.videos;
  if (!videos || statsUi.videosAccount !== account.account) return `<div class="skeleton sk-card"></div>`;
  if (!account.fetched_at) return statsEmpty("inbox", `Aucune publication relevée pour ${account.label}`, "Les publications apparaissent après le premier relevé de la page Contenu de TikTok Studio.");
  if (!videos.length && !statsUi.q) return statsEmpty("film", "Aucune publication sur ce compte", "Le relevé est à jour, mais TikTok Studio ne liste aucune vidéo pour ce compte. Publie un clip depuis Clipper : il apparaîtra ici au relevé suivant.", `<a class="btn" data-variant="outline" href="#/publish">${icon("send")}Aller à Publication</a>`);
  const sort = statsUi.sort;
  const head = `<th aria-label="Vignette" style="width:56px"></th>` + STATS_COLUMNS.map(([key, label, right]) =>
    `<th class="${right ? "r" : ""}" ${sort.key === key ? `aria-sort="${sort.dir === "asc" ? "ascending" : "descending"}"` : ""}><button type="button" data-stats-sort="${key}">${esc(label)}<span class="arr" aria-hidden="true">${sort.key === key ? (sort.dir === "asc" ? "▲" : "▼") : ""}</span></button></th>`).join("");
  const body = videos.length ? videos.map(statsVideoRow).join("")
    : `<tr><td colspan="9" class="muted" style="text-align:center;padding:32px">Aucune légende ne contient « ${esc(statsUi.q)} ».</td></tr>`;
  return `<div class="card s3-card" data-stats-videos>
    <div class="s3-head"><h2>Vidéos de ${esc(account.label)}</h2><div class="s3-right">${statsRestrictionSummary(statsUi.videos)}<span class="input-ico">${icon("search")}<input class="input" id="stats-q" data-stats-q type="search" placeholder="Filtrer par légende" value="${esc(statsUi.q)}" aria-label="Filtrer par légende" style="width:220px"></span></div></div>
    <div class="sort-m"><select class="input" data-stats-sort-select aria-label="Trier par">${STATS_COLUMNS.map(([key, label]) => `<option value="${key}"${sort.key === key ? " selected" : ""}>Trier : ${esc(label)}</option>`).join("")}</select><button class="btn" data-variant="outline" type="button" data-stats-sort-dir aria-label="Inverser le tri">${sort.dir === "asc" ? "▲" : "▼"}</button></div>
    <div class="s3-scroll"><table class="table s3-table ptable"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>
    <div class="pager-note">${icon("info", "i-sm")}<span>${esc(fr(videos.length))} vidéo${videos.length > 1 ? "s" : ""} · cliquer une ligne ouvre sa fiche. Les vidéos en cours de traitement sont relevées à nouveau au prochain passage.</span></div></div>`;
}

/* Barres « libelle · part » d'un onglet Spectateurs / Engagement ; une part est une fraction ou un nombre. */
function statsEntryBars(entries, asPercent) {
  if (!entries || !entries.length) return `<p class="muted stats-empty">TikTok n'affiche rien ici pour l'instant.</p>`;
  const max = Math.max(...entries.map((e) => e.value || 0)) || 1;
  return entries.map((e) => `<div class="src-row"><span>${esc(e.label)}</span><div class="bar"><i style="width:${Math.max(2, Math.round(((e.value || 0) / (asPercent ? 1 : max)) * 100))}%"></i></div><b>${esc(e.value === null ? "—" : asPercent ? statsPctValue(e.value, 0) : fr(e.value))}</b></div>`).join("");
}

const statsFig = (label, value) => `<div class="fig"><div class="l">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;

function statsVideoOverviewTab(v) {
  const hours = v.watch_total_s === null || v.watch_total_s === undefined ? null : v.watch_total_s / 3600;
  const figs = [["Vues de vidéo", statsNum(v.views)], ["Temps de lecture total", hours === null ? "—" : hours >= 1 ? `${fr(hours, 1)} h` : statsDuration(v.watch_total_s)],
    ["Temps de visionnage moyen", statsDuration(v.avg_watch_s)], ["A regardé toute la vidéo", statsPctValue(v.watched_full, 1)],
    ["Nouveaux followers", statsNum(v.new_followers)], ["J'aime", statsNum(v.likes)], ["Commentaires", statsNum(v.comments)], ["Partages", statsNum(v.shares)]];
  const curve = v.retention_curve;
  const retention = curve && curve.length
    ? `<div class="chart">${statsChart({ labels: curve.map((p) => statsDuration(p.t_s)), width: statsChartWidth() > 640 ? 640 : statsChartWidth(), height: 200, max: 100,
      series: [{ values: curve.map((p) => (p.share === null ? null : p.share * 100)) }], fy: (n) => `${fr(Math.round(n))} %`, aria: "Taux de rétention",
      tip: (i) => `à ${statsDuration(curve[i].t_s)} : ${statsPctValue(curve[i].share, 0)} des spectateurs` })}</div>`
    : `<p class="muted stats-empty">${v.retention === null || v.retention === undefined ? "Courbe de rétention non relevée (TikTok ne l'affiche pas encore)." : `Taux de rétention : ${esc(statsPctValue(v.retention, 1))} (courbe non relevée).`}</p>`;
  const sources = v.traffic_sources
    ? `<p>${esc(v.traffic_sources)}</p>`
    : `<div class="reason">${icon("lock", "i-sm")}<span>Sources de trafic <b>disponibles dès 100 vues</b> : TikTok n'affiche pas encore la répartition.</span></div>`;
  const track = (v.history || []).map((h) => ({ label: statsWhen(h.fetched_at), views: h.views }));
  const history = track.length > 1 ? `<div class="card s3-card s3-pad"><div class="section-title">${icon("trending-up")}Vues à chaque relevé</div><div class="chart">${statsChart({
    labels: track.map((t) => t.label.slice(0, 5)), width: statsChartWidth() > 640 ? 640 : statsChartWidth(), height: 180, series: [{ values: track.map((t) => t.views) }],
    aria: "Vues à chaque relevé", tip: (i) => `${track[i].label} : ${track[i].views === null ? "—" : fr(track[i].views)} vues` })}</div></div>` : "";
  return `<div class="stack"><div class="figs">${figs.map(([l, val]) => statsFig(l, val)).join("")}</div>
    <div class="card s3-card"><div class="s3-head"><h2>Taux de rétention</h2><div class="s3-right muted" style="font-size:13px">Part des spectateurs encore présents</div></div><div class="chart-wrap">${retention}</div></div>
    ${history}<div class="card s3-card s3-pad"><div class="section-title">${icon("trending-up")}Sources de trafic</div>${sources}</div></div>`;
}

function statsVideoViewersTab(v) {
  if (!v.viewers) return `<div class="card s3-card s3-pad"><div class="reason">${icon("lock", "i-sm")}<span>Données <b>disponibles dès 100 vues</b> : TikTok n'affiche pas encore les spectateurs de cette vidéo.</span></div></div>`;
  return `<div class="stack"><div class="figs">${statsFig("Total des spectateurs", statsNum(v.viewers.total))}</div>${STATS_VIEWER_SECTIONS.map(([key, label]) =>
    `<div class="card s3-card s3-pad" data-stats-section="${key}"><div class="section-title">${esc(label)}</div>${statsEntryBars(v.viewers[key], true)}</div>`).join("")}</div>`;
}

function statsVideoEngagementTab(v) {
  if (!v.engagement) return `<div class="card s3-card s3-pad"><div class="reason">${icon("lock", "i-sm")}<span>Données <b>disponibles dès 100 vues</b> : TikTok n'affiche pas encore l'engagement de cette vidéo.</span></div></div>`;
  return `<div class="stack"><div class="figs">${statsFig("J'aime", statsNum(v.likes))}${statsFig("Commentaires", statsNum(v.comments))}${statsFig("Partages", statsNum(v.engagement.shares))}</div>
    <div class="card s3-card s3-pad" data-stats-section="likes_over_time"><div class="section-title">J'aime dans le temps</div>${statsEntryBars(v.engagement.likes_over_time, false)}</div>
    <div class="card s3-card s3-pad" data-stats-section="comment_words"><div class="section-title">Mots les plus utilisés dans les commentaires</div>${statsEntryBars(v.engagement.comment_words, false)}</div></div>`;
}

function statsVideoSheet(account) {
  const v = statsUi.video;
  const back = `<a class="btn" data-variant="ghost" href="${statsHref(account.account, "videos")}" data-stats-back>${icon("chevron-left")}Toutes les vidéos</a>`;
  if (!v || v.post_id !== statsRoute().post || statsUi.videoAccount !== account.account) return `${back}<div class="skeleton sk-card"></div>`;
  const links = [`<a class="btn" data-variant="outline" href="${esc(v.post_url)}" target="_blank" rel="noopener noreferrer">${icon("external-link")}<span>Ouvrir sur TikTok</span></a>`];
  if (v.clip) {
    links.push(`<a class="btn" data-variant="outline" href="#/clips/${encodeURIComponent(v.clip.video_id)}">${icon("clapperboard")}<span>Voir le clip dans Clipper</span></a>`,
      `<a class="btn" data-variant="outline" href="#/videos/${encodeURIComponent(v.clip.video_id)}">${icon("film")}<span>Vidéo source : ${esc(v.clip.video_id)}</span></a>`);
  }
  const origin = v.outside_clipper ? `<p class="muted" style="font-size:13px;margin-top:12px" data-stats-outside>${icon("info", "i-sm")} Publié hors Clipper : aucun clip ni vidéo source ne correspond à ce post.</p>` : "";
  const vis = STATS_VISIBILITY[v.visibility] || v.visibility || "—";
  const tab = STATS_VIDEO_TABS.some(([id]) => id === statsUi.vtab) ? statsUi.vtab : "overview";
  const content = { overview: statsVideoOverviewTab, viewers: statsVideoViewersTab, engagement: statsVideoEngagementTab }[tab](v);
  return `${back}<div class="video-sheet" data-stats-sheet>
    <div><div class="poster-big" style="${v.processing ? "background:var(--muted)" : statsPoster(v.post_id)}"><span>${esc((v.caption || "").split("#")[0].trim() || v.post_id)}</span></div>
      <h2 style="font-size:17px;margin-top:16px">${esc(v.caption || "(sans légende)")}</h2>
      <div class="muted" style="font-size:13px;margin-top:8px">Publié le ${esc(statsPostedAt(v.posted_at, v.posted_at_text))} · <span class="chip plain ${v.visibility === "public" ? "ok" : ""}">${esc(vis)}</span> ${statsRestrictedChip(v)}</div>
      ${v.fyf_eligible === false ? `<div class="reason" data-stats-fyf-notice>${icon("triangle-alert", "i-sm")}<span><b>TikTok ne recommande pas cette vidéo dans le fil Pour toi.</b> ${esc(v.fyf_notice || "")}</span></div>` : ""}
      <div class="dlinks">${links.join("")}</div>${origin}</div>
    <div><div class="tabs" role="tablist" aria-label="Sections de la fiche">${STATS_VIDEO_TABS.map(([id, label]) => `<button type="button" role="tab" class="${tab === id ? "on" : ""}" aria-selected="${tab === id}" data-stats-vtab="${id}">${esc(label)}</button>`).join("")}</div>
      ${v.processing ? `<div class="reason">${icon("hourglass", "i-sm")}<span><b>TikTok traite encore cette vidéo.</b> Les chiffres arrivent en général dans l'heure qui suit la publication ; Clipper la relèvera au prochain passage.</span></div>` : ""}
      ${v.detail_not_read ? `<div class="reason">${icon("info", "i-sm")}<span><b>Détail non relevé.</b> Cette vidéo est plus ancienne que les dernières relevées : seuls les chiffres de la liste sont connus. Le bouton « Relevé complet » relève aussi son détail.</span></div>` : ""}
      ${content}</div></div>`;
}

/* ---------- section « Coûts et durées » (mesures internes de Clipper, GET /api/measures) ---------- */

const statsMoney = (usd) => `${fr(usd, 2)} $`;

function statsMeasuresCost(cost) {
  const videos = Object.entries(cost.by_video || {}).sort((a, b) => b[1].cost - a[1].cost);
  if (!videos.length) return `<p class="muted stats-empty">Aucun appel au modèle de langage sur la période.</p>`;
  const rows = videos.map(([id, v]) => `<tr><td class="wrap"><a href="#/videos/${encodeURIComponent(id)}">${esc(id)}</a></td><td class="r">${esc(fr(v.calls))}</td><td class="r">${esc(statsMoney(v.cost))}</td><td class="wrap">${v.unreported_calls ? `${esc(fr(v.unreported_calls))} appel${v.unreported_calls > 1 ? "s" : ""} sans coût rapporté` : ""}</td></tr>`).join("");
  return `<div class="s3-scroll"><table class="table s3-table"><thead><tr><th>Vidéo</th><th class="r">Appels</th><th class="r">Coût</th><th>Remarque</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function statsMeasuresSteps(steps) {
  const names = Object.keys(steps || {});
  if (!names.length) return `<p class="muted stats-empty">Aucune étape terminée sur la période.</p>`;
  const rows = names.map((name) => `<tr><td>${esc(STEP_LABELS[name] || name)}</td><td class="r">${esc(statsDuration(steps[name].mean_s))}</td><td class="r">${esc(statsDuration(steps[name].last_s))}</td><td class="r">${esc(fr(steps[name].count))}</td></tr>`).join("");
  return `<div class="s3-scroll"><table class="table s3-table"><thead><tr><th>Étape</th><th class="r">Durée moyenne</th><th class="r">Dernière</th><th class="r">Vidéos</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function statsMeasuresSection() {
  if (statsUi.measuresError) {
    return `<section class="learning"><h2>Coûts et durées</h2><p class="reason bad" role="alert">Lecture impossible : ${esc(statsUi.measuresError.message || statsUi.measuresError)}</p></section>`;
  }
  const m = statsUi.measures;
  if (!m) return "";
  const total = m.llm_cost.unreported_calls ? `${statsMoney(m.llm_cost.total)} · ${fr(m.llm_cost.unreported_calls)} appel(s) sans coût rapporté, non comptés` : statsMoney(m.llm_cost.total);
  return `<section class="learning"><h2>Coûts et durées</h2>
    <div class="card s3-card s3-pad" data-measures-cost><h3>Coût par vidéo</h3><p class="muted">Modèle de langage, total sur la période : <strong>${esc(total)}</strong></p>${statsMeasuresCost(m.llm_cost)}</div>
    <div class="card s3-card s3-pad" data-measures-steps><h3>Durée par étape</h3><p class="muted">Vidéos terminées sur la période.</p>${statsMeasuresSteps(m.steps)}</div></section>`;
}

/* ---------- section « Apprentissage » (ADR-c260, SPEC-00db R6-R7) ---------- */

/* Lecture seule de GET /api/learning (le worker verse, recalibre et coache : aucun calcul ici). Trois blocs : l'etat
   de la boucle, les poids par juge, les propositions du coach que l'humain adopte ou refuse. */
const LEARNING_REASONS = {
  none: "aucun post du relevé ne correspond", ambiguous: "plusieurs posts correspondent",
  immature: "trop récent pour compter", account_below_min: "compte sous le minimum de posts à vues",
  not_in_stats: "absent du dernier relevé", service_without_stats: "service sans statistiques",
};
const LEARNING_WEIGHT_REASONS = { fixed: "poids fixe (jamais recalibré)", min_clips: "pas assez de clips", undefined_agreement: "accord indéfini" };
const LEARNING_STATUS = { proposed: "À décider", adopted: "Adoptée", refused: "Refusée" };

const learningWhen = (iso) => (iso ? statsWhen(iso) : "jamais");

/* { raison: n } a partir d'une liste d'objets portant `reason`. */
function learningCount(rows) {
  const counts = {};
  for (const row of rows || []) counts[row.reason] = (counts[row.reason] || 0) + 1;
  return counts;
}

function learningReasonList(counts) {
  const keys = Object.keys(counts);
  if (!keys.length) return "<li>aucun</li>";
  return keys.map((k) => `<li><strong>${fr(counts[k])}</strong> · ${esc(LEARNING_REASONS[k] || k)}</li>`).join("");
}

function learningStateBlock(data) {
  const sync = data.sync || {}, links = data.links || {};
  const perAccount = links["counts"] || {};
  const linked = Object.values(perAccount).reduce((t, c) => t + (c.linked || 0), 0);
  const error = sync.last_error
    ? `<p class="reason bad" data-learning-error>Dernière erreur (${esc(sync.last_error.where)}) · ${esc(learningWhen(sync.last_error.at))} : ${esc(sync.last_error.message)}</p>` : "";
  const excluded = {};
  for (const row of sync.excluded || []) {
    const key = `${row.account || "—"} · ${LEARNING_REASONS[row.reason] || row.reason}`;
    excluded[key] = (excluded[key] || 0) + 1;
  }
  const excludedRows = Object.keys(excluded).map((k) => `<li><strong>${fr(excluded[k])}</strong> · ${esc(k)}</li>`).join("") || "<li>aucun</li>";
  return `<div class="card s3-card s3-pad" data-learning-state>
    <h3>État de la boucle</h3>
    ${data.enabled ? "" : `<p class="reason">Apprentissage désactivé ([learning] enabled = false) : le worker ne rattache ni ne verse rien.</p>`}
    <p>Dernier versement : <strong>${esc(learningWhen(sync.last_sync))}</strong></p>
    ${error}
    <p>Clips reliés à leur post : <strong>${fr(linked)}</strong></p>
    <p>Clips non reliés :</p><ul>${learningReasonList(learningCount(links.unlinked))}</ul>
    <p>Clips exclus de l'apprentissage :</p><ul>${excludedRows}</ul>
  </div>`;
}

function learningWeightsBlock(data) {
  const judges = data.weights && data.weights.judges ? Object.keys(data.weights.judges) : [];
  if (!judges.length) {
    return `<div class="card s3-card s3-pad" data-learning-weights><h3>Poids par juge</h3><p class="reason">Pas encore de poids calculés : il faut des clips mûrs sur un compte éligible.</p></div>`;
  }
  const rows = judges.map((name) => {
    const j = data.weights.judges[name];
    return `<tr><td>${esc(name)}</td><td>${fr(j.weight, 2)}</td><td>${j.agreement === null || j.agreement === undefined ? "—" : fr(j.agreement, 2)}</td><td>${fr(j.clips || 0)}</td><td>${esc(LEARNING_WEIGHT_REASONS[j.reason] || j.reason || "—")}</td></tr>`;
  }).join("");
  return `<div class="card s3-card s3-pad" data-learning-weights><h3>Poids par juge</h3>
    <div class="s3-scroll"><table class="table s3-table"><thead><tr><th>Juge</th><th>Poids</th><th>Accord</th><th>Cas</th><th>Raison</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
}

function learningMetric(metric) {
  if (!metric) return "—";
  return `erreur ${fr(metric.before, 3)} → ${fr(metric.after, 3)} sur ${fr(metric.cases)} cas`;
}

function learningProposal(p) {
  const key = `${esc(p.judge)}/${p.version}`;
  const decided = p.status === "proposed" ? "" : ` · ${esc(learningWhen(p.decided_at))}`;
  const buttons = p.status === "proposed"
    ? `<div class="s3-right"><button class="btn" data-size="sm" type="button" data-learning-adopt="${key}">Adopter</button><button class="btn" data-variant="outline" data-size="sm" type="button" data-learning-refuse="${key}">Refuser</button></div>` : "";
  return `<div class="card s3-card s3-pad" data-learning-proposal="${key}">
    <h4>${esc(p.judge)} · v${fr(p.version)} <span class="chip plain">${esc(LEARNING_STATUS[p.status] || p.status)}${decided}</span></h4>
    <p>Métrique : ${esc(learningMetric(p.metric))} (plus bas est mieux)</p>
    ${p.error ? `<p class="reason bad">${esc(p.error)}</p>` : ""}
    <p><strong>Perspective en place</strong></p><pre style="white-space:pre-wrap">${esc(p.perspective_current || "—")}</pre>
    <p><strong>Perspective proposée</strong></p><pre style="white-space:pre-wrap">${esc(p.perspective_proposed || "—")}</pre>
    ${buttons}
  </div>`;
}

function learningCoachBlock(data) {
  const items = data.coach || [];
  const body = items.length ? items.map(learningProposal).join("")
    : `<p class="reason">Aucune proposition : le coach passe seulement avec assez de clips mûrs nouveaux, et rien ne s'applique sans ton accord.</p>`;
  return `<div data-learning-coach><h3>Coach des prompts</h3>${body}</div>`;
}

const LEARNING_MOMENT_SOURCES = { transcript: "Transcription", action: "Action" };

/* Tableau de retention a maturite : les lignes et le message viennent du serveur (triees, seuil retention_min_n) ; aucun calcul ici. */
function learningRetentionBlock(data) {
  const r = data.retention;
  if (!r) return "";
  const note = r.message ? `<p class="reason" data-learning-retention-note>${esc(r.message)}</p>` : "";
  const body = r.rows.length
    ? r.rows.map((row) => `<tr><td>${esc(row.video_id)} · ${esc(row.clip_id)}</td><td>${statsDuration(row.duration)}</td><td>${statsPctValue(row.pct_watched, 1)}</td><td>${statsPctValue(row.watched_full, 0)}</td><td>${statsPctValue(row.views_percentile, 0)}</td><td>${esc(LEARNING_MOMENT_SOURCES[row.moment_source] || "—")}</td></tr>`).join("")
    : `<tr><td colspan="6" class="muted">Aucun clip mûr relevé.</td></tr>`;
  return `<div class="card s3-card s3-pad" data-learning-retention><h3>Rétention à maturité</h3>
    <p>Clips mûrs : <strong>${fr(r.n)}</strong></p>${note}
    <div class="s3-scroll"><table class="table s3-table"><thead><tr><th>Clip</th><th>Durée</th><th>% vu</th><th>% vu en entier</th><th>Rang des vues</th><th>Source du moment</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
}

/* « Ce qui marche » : quatre tableaux (jeu, streamer, heure, compte) ; groupes, medianes et `few` viennent du serveur, aucun calcul ici (ADR-49cd). */
const LEARNING_BREAKDOWN_TABLES = [["game", "Jeu"], ["streamer", "Streamer"], ["hour", "Heure"], ["account", "Compte"]];

function learningBreakdownTable(name, title, groups) {
  const body = groups.map((g) => `<tr><td>${esc(g.label)}${g.few ? ` <span class="muted" data-breakdown-few>trop peu pour conclure</span>` : ""}</td><td>${fr(g.n)}</td><td>${fr(g.median_views)}</td><td>${statsPctValue(g.median_pct_watched, 1)}</td><td><a href="#/clips/${encodeURIComponent(g.best.video_id)}">${esc(g.best.video_id)} · ${esc(g.best.clip_id)}</a> (${fr(g.best.views)} vues)</td></tr>`).join("");
  return `<div data-breakdown="${name}"><h4>${esc(title)}</h4><div class="s3-scroll"><table class="table s3-table"><thead><tr><th>${esc(title)}</th><th>n</th><th>Vues médianes</th><th>Part vue médiane</th><th>Meilleur clip</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
}

function learningBreakdownBlock(data) {
  const b = data.breakdown;
  if (!b) return "";
  const body = b.n
    ? LEARNING_BREAKDOWN_TABLES.map(([name, title]) => learningBreakdownTable(name, title, b.groups[name])).join("")
    : `<p class="muted" data-breakdown-empty>aucun relevé mûr</p>`;
  const skipped = b.skipped ? `<p class="muted">${fr(b.skipped)} relevé(s) sans vues ignoré(s).</p>` : "";
  return `<div class="card s3-card s3-pad" data-learning-breakdown><h3>Ce qui marche</h3>
    <p class="muted">Vues médianes à maturité, relevés réels seulement (${fr(b.n)} clips).</p>${skipped}${body}</div>`;
}

function statsLearningSection() {
  if (statsUi.learningError) {
    return `<section class="learning"><h2>Apprentissage</h2><p class="reason bad">Lecture impossible : ${esc(statsUi.learningError.message || statsUi.learningError)}</p></section>`;
  }
  if (!statsUi.learning) return "";
  return `<section class="learning"><h2>Apprentissage</h2>${learningStateBlock(statsUi.learning)}${learningWeightsBlock(statsUi.learning)}${learningRetentionBlock(statsUi.learning)}${learningBreakdownBlock(statsUi.learning)}${learningCoachBlock(statsUi.learning)}</section>`;
}

/* Adopter ecrit la perspective dans config.toml (le serveur), Refuser ne touche a rien ; l'humain decide, jamais le coach. */
async function learningDecide(key, verb) {
  const [judge, version] = key.split("/");
  const adopt = verb === "adopt";
  if (adopt && !(await confirmDialog({ title: "Adopter cette perspective ?", body: `La perspective proposée pour ${judge} remplace celle en place dans config.toml ([jury.judges.${judge}] perspective).`, confirmLabel: "Adopter", danger: false }))) return;
  try {
    const j = encodeURIComponent(judge), v = encodeURIComponent(version);
    const done = adopt ? await api(`/api/learning/coach/${j}/${v}/adopt`, { method: "POST" }) : await api(`/api/learning/coach/${j}/${v}/refuse`, { method: "POST" });
    toast({ kind: "ok", title: adopt ? "Perspective adoptée" : "Proposition refusée", body: `${judge} v${version}${adopt && done.comments_lost ? " · les commentaires de config.toml ont été perdus" : ""}.` });
  } catch (err) {
    toastError(adopt ? "Adoption impossible" : "Refus impossible", err);
  }
  statsUi.at = 0;
  await statsLoad();
}

/* ---------- relevé a la demande ---------- */

/* Ouvre le Chrome du profil sur ce PC (visible) ; un arret sur (captcha, connexion expiree, compte non pret) revient
   en 409 avec sa raison, affichee telle quelle. */
async function statsScan(account, full) {
  if (full && !(await confirmDialog({ title: "Lancer un relevé complet ?", body: statsFullConfirmBody(account), confirmLabel: "Relevé complet", danger: false }))) return;
  statsUi.refreshing = true;
  renderCurrent();
  try {
    const sent = await api(statsIsYoutube(account) ? "/api/stats/youtube/refresh" : "/api/stats/tiktok/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(full ? { account: account.account, full: true } : { account: account.account }) });
    const posts = Object.values(sent.accounts).reduce((t, a) => t + (a.posts || 0), 0);
    if (Object.values(sent.accounts).some((a) => a.running)) toast({ kind: "info", title: "Relevé déjà en cours", body: `${account.label} : un relevé tourne déjà, rien n'est relancé.` });
    else toast({ kind: "ok", title: "Relevé terminé", body: `${account.label} : ${posts} publication${posts > 1 ? "s" : ""} relevée${posts > 1 ? "s" : ""}.` });
  } catch (err) {
    toastError(`Relevé ${STATS_SERVICES[account.service] || "TikTok"} impossible`, err);
  } finally {
    statsUi.refreshing = false;
    statsUi.at = 0;
    await statsLoad();
  }
}

function statsWire(body, account) {
  const select = $("[data-stats-account]", body);
  if (select) select.onchange = () => { location.hash = statsHref(select.value, statsRoute().tab); };
  $$("[data-stats-period]", body).forEach((b) => (b.onclick = () => { statsUi.period = Number(b.dataset.statsPeriod); renderCurrent(); statsLoad(); }));
  $$("[data-stats-metric]", body).forEach((b) => (b.onclick = () => { statsUi.metric = b.dataset.statsMetric; renderCurrent(); }));
  const scan = $("[data-stats-scan]", body);
  if (scan) scan.onclick = () => statsScan(account, false);
  const fullScan = $("[data-stats-full]", body);
  if (fullScan) fullScan.onclick = () => statsScan(account, true);
  $$("[data-stats-sort]", body).forEach((b) => (b.onclick = () => {
    const key = b.dataset.statsSort;
    statsUi.sort = statsUi.sort.key === key ? { key, dir: statsUi.sort.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "caption" || key === "title" ? "asc" : "desc" };
    renderCurrent();
    statsLoad();
  }));
  const sortSelect = $("[data-stats-sort-select]", body);
  if (sortSelect) sortSelect.onchange = () => { statsUi.sort = { key: sortSelect.value, dir: sortSelect.value === "caption" || sortSelect.value === "title" ? "asc" : "desc" }; renderCurrent(); statsLoad(); };
  const sortDir = $("[data-stats-sort-dir]", body);
  if (sortDir) sortDir.onclick = () => { statsUi.sort = { key: statsUi.sort.key, dir: statsUi.sort.dir === "asc" ? "desc" : "asc" }; renderCurrent(); statsLoad(); };
  const search = $("[data-stats-q]", body);
  if (search) {
    let timer = null;
    search.oninput = () => {
      statsUi.q = search.value;
      clearTimeout(timer);
      timer = setTimeout(async () => {
        await statsLoad();
        const again = $("[data-stats-q]");
        if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
      }, 250);
    };
  }
  $$("[data-stats-open]", body).forEach((tr) => {
    const open = () => { statsUi.vtab = "overview"; location.hash = statsHref(account.account, "videos", tr.dataset.statsOpen); };
    tr.onclick = open;
    tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  $$("[data-learning-adopt]", body).forEach((b) => (b.onclick = () => learningDecide(b.dataset.learningAdopt, "adopt")));
  $$("[data-learning-refuse]", body).forEach((b) => (b.onclick = () => learningDecide(b.dataset.learningRefuse, "refuse")));
  $$("[data-stats-vtab]", body).forEach((b) => (b.onclick = () => { statsUi.vtab = b.dataset.statsVtab; renderCurrent(); }));
}

Screens.stats = {
  render(body) {
    if (Date.now() - statsUi.lastRender > STATS_VISIT_GAP_MS) statsUi.opened = {}; // ecran rouvert : nouvelle ouverture
    statsUi.lastRender = Date.now();
    if (!statsUi.accounts || statsUi.key !== statsKey() || Date.now() - statsUi.at > STATS_STALE_MS) statsLoad();
    if (!statsUi.accounts) {
      morph(body, `<div class="s3">${statsUi.error
        ? emptyState("circle-alert", "Chargement impossible", String(statsUi.error.message || statsUi.error))
        : `<div class="skeleton sk-line"></div><div class="skeleton sk-card"></div><div class="skeleton sk-card"></div>`}</div>`);
      return;
    }
    const account = statsAccount();
    if (!account) {
      morph(body, `<div class="s3">${emptyState("users", "Aucun compte TikTok ou YouTube", "Ajoute un compte TikTok ou YouTube dans l'écran Comptes : les statistiques sont relevées compte par compte.", `<a class="btn" data-variant="outline" href="#/accounts">${icon("users")}Aller à Comptes</a>`)}</div>`);
      return;
    }
    const route = statsRoute();
    let pane;
    const youtube = statsIsYoutube(account);
    if (route.tab === "videos" && route.post && !youtube) pane = statsVideoSheet(account);
    else {
      const count = statsUi.videos && route.tab === "videos" ? statsUi.videos.length : null;
      const list = youtube ? statsYoutubeVideos : statsVideosList;
      pane = statsTabs(account, route.tab, count) + (route.tab === "videos" ? list(account) : (youtube ? statsYoutubeOverview : statsOverview)(account));
    }
    const sheet = route.tab === "videos" && route.post && !youtube;
    morph(body, `<div class="s3">${statsUi.error ? `<p class="reason bad">Actualisation impossible : ${esc(statsUi.error.message || statsUi.error)}</p>` : ""}${statsUi.youtubeError ? `<p class="reason bad" data-stats-youtube-error>Comptes YouTube indisponibles : ${esc(statsUi.youtubeError.message || statsUi.youtubeError)}</p>` : ""}${statsControls(account)}${pane}${sheet ? "" : statsMeasuresSection() + statsLearningSection()}</div>`);
    statsWire(body, account);
  },
};
