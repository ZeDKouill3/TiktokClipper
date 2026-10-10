/* Ecran « Publication » (SPEC-c100 E6, SPEC-74e9 §4, SPEC-1ed3), agencement de la maquette
   docs/maquette-web-v2 : le CALENDRIER de la semaine est la vue principale (colonne de droite) ;
   a gauche, « Nouvelle publication », la liste des publications en cours et les clips approuves
   a publier. Lit GET /api/publish?account=&week= (creneaux de la semaine, publications entre les
   creneaux, publies / echecs du COMPTE TikTok choisi, ou de tous) et GET /api/publications. Publier =
   « Nouvelle publication » (clip, compte, Maintenant ou une date) ; les creneaux du style sont facultatifs : un clip approuve se
   glisse (souris ou appui long au toucher) vers un creneau libre : POST .../move. Toutes les
   heures affichees sont celles de Paris (Europe/Paris). Aucune logique serveur ici : tout passe
   par l'API. Charge apres screens.js dont il remplace l'entree Screens.publish. */
"use strict";

const PUB_STATUS = {
  approved: { label: "Approuvé", cls: "ok" }, scheduled: { label: "Planifié", cls: "info" },
  published: { label: "Publié", cls: "ok" }, failed: { label: "Échec", cls: "bad" },
};
// Statut TikTok d'une publication (SPEC-9225 R3, R4), calcule par l'API : prime sur le statut de la file.
const PUB_TIKTOK = {
  pending: { label: "En attente", cls: "pending" }, in_progress: { label: "En cours", cls: "info" }, scheduled_on_tiktok: { label: "Programmée sur TikTok", cls: "info" },
  scheduled_on_youtube: { label: "Programmée sur YouTube", cls: "info" },
  published: { label: "Publiée", cls: "ok" }, failed: { label: "Échec", cls: "bad" },
  refused_by_platform: { label: "Refusé par TikTok", cls: "bad" },
  removed_from_platform: { label: "Supprimé de la plateforme", cls: "pending" },
};
const PUB_STALE_MS = 4000;
const PUB_HOLD_MS = 250;      // appui long avant de saisir un clip au toucher
const PUB_SLOP_PX = 8;        // mouvement tolere pendant l'appui long (sinon : defilement)
const pubEnc = encodeURIComponent;

// ``range`` (Jour / Semaine / Mois, TASK-ad4d) : choix retenu pendant la session (pas rechargé depuis le
// serveur), Semaine par défaut. ``week`` ancre la plage demandée (son nom garde le sens historique de
// l'appel ``week=`` ; il ancre tout autant un jour ou un mois).
const pubUi = { account: "", range: "week", week: "", key: "", data: null, error: null, loading: null, dirty: false, at: 0, html: "", dragKey: null, touching: false, landed: null };

// Publications pilotees (SPEC-1ed3) : GET /api/publications, independantes du compte choisi.
const pubPosts = { data: null, error: null, loading: null, dirty: false, at: 0 };

/* Libelle d'un compte avec son service (« Ma chaîne (YouTube) ») : les comptes des deux services sont proposes (SPEC-5e50 R2). */
const pubAccountText = (a) => {
  const service = a.service_label || (a.service === "youtube" ? "YouTube" : a.service === "tiktok" ? "TikTok" : "");
  return `${a.label || a.id}${service ? ` (${service})` : ""}`;
};
const pubAccountLabel = (id) => {
  const accounts = [...((pubUi.data && pubUi.data.accounts) || []), ...((pubPosts.data && pubPosts.data.accounts) || [])];
  const found = accounts.find((a) => a.id === id);
  return found ? (found.label || id) : id;
};
const pubKey = (c) => `${c.video_id}/${c.clip_id}`;
const pubStatus = (c) => PUB_STATUS[c.publish_status] || { label: c.publish_status, cls: "pending" };
const pubTitle = (c) => c.screen_title || c.title || c.clip_id;
/* Libelle et classe du statut d'une publication. Programmee sur TikTok = pas encore en ligne : « Publiée » seulement
   une fois l'heure passee (`tiktok_live`, calcule par le serveur). */
function pubStatusOf(c) {
  if (c.waiting_reason) return { label: "En attente du compte", cls: "warn" };
  if (pubIsScheduledOnService(c) && c.tiktok_live) return PUB_TIKTOK.published;
  return PUB_TIKTOK[c.tiktok_status] || pubStatus(c);
}
const pubChip = (c) => { const s = pubStatusOf(c); return `<span class="chip ${s.cls}">${esc(s.label)}</span>`; };
const pubIsScheduledOnService = (c) => c.tiktok_status === "scheduled_on_tiktok" || c.tiktok_status === "scheduled_on_youtube";
const pubServiceName = (c) => (c.service === "youtube" || c.tiktok_status === "scheduled_on_youtube" ? "YouTube" : "TikTok");
const pubCaptionText = (c) => [c.description || "", (c.hashtags || []).join(" ")].filter(Boolean).join("\n\n");
// Toutes les heures affichees sont celles de Paris (Europe/Paris, ete +02:00 / hiver +01:00), jamais un
// decalage fixe : le serveur donne les champs *_paris (zoneinfo) que l'on lit tels quels, et tout autre
// instant passe par Intl avec le fuseau Europe/Paris.
const PUB_TZ = "Europe/Paris";
const pubDate = (iso) => iso.slice(0, 10);
const pubTime = (iso) => iso.slice(11, 16);
const pubUtc = (ymd) => { const [y, m, d] = ymd.split("-").map(Number); return new Date(Date.UTC(y, m - 1, d)); };
const pubFmt = (ymd, opts) => pubUtc(ymd).toLocaleDateString("fr-FR", Object.assign({ timeZone: "UTC" }, opts));
const pubShift = (ymd, days) => { const d = pubUtc(ymd); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
// Décale d'un nombre de mois en se calant au 1er (sinon le 31 janvier + 1 mois déborde sur mars) : seulement
// pour choisir la plage à demander au serveur (navigation), jamais pour grouper des publications par jour.
const pubShiftMonth = (ymd, months) => { const d = pubUtc(ymd); d.setUTCMonth(d.getUTCMonth() + months, 1); return d.toISOString().slice(0, 10); };
// Palier de densité d'une case jour de la Semaine (TASK-460904227d2f, remplace TASK-ad4d) : normal <= 4
// (case pleine), compact <= 10 (boîtes réduites), mini au-delà (une ligne heure + titre + pastille). Jamais de
// coupure (TASK-39bbe20b3284) : la case s'agrandit, toutes les publications du jour restent visibles.
function pubBoxClass(count) {
  if (count > 10) return "mini";
  if (count > 4) return "compact";
  return "";
}
const pubSlotLabel = (iso) => `${pubFmt(pubDate(iso), { weekday: "long", day: "numeric", month: "long" })} à ${pubTime(iso)}`;
const pubWhen = (iso) => new Date(iso).toLocaleString("fr-FR", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: PUB_TZ });

/* Heure murale de Paris d'un instant, au format du champ datetime-local (AAAA-MM-JJTHH:MM). */
const pubLocalInput = (iso) => new Date(iso).toLocaleString("sv-SE", { timeZone: PUB_TZ, hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).replace(" ", "T");

/* Instant (Date) d'une heure murale de Paris saisie dans un champ datetime-local : le decalage d'ete ou d'hiver
   est celui de Paris a cette date, pas celui du navigateur. */
function pubParisInstant(local) {
  const [ymd, hm] = local.split("T");
  const [y, m, d] = ymd.split("-").map(Number);
  const [h, mi] = hm.split(":").map(Number);
  const wanted = Date.UTC(y, m - 1, d, h, mi);
  let guess = wanted;
  for (let i = 0; i < 2; i++) {
    const [gy, gm, gd] = pubLocalInput(new Date(guess).toISOString()).split("T")[0].split("-").map(Number);
    const [gh, gmi] = pubLocalInput(new Date(guess).toISOString()).split("T")[1].split(":").map(Number);
    guess += wanted - Date.UTC(gy, gm - 1, gd, gh, gmi);
  }
  return new Date(guess);
}

function pubClips() {
  const d = pubUi.data;
  if (!d) return [];
  return [...d.unscheduled, ...d.slots.map((s) => s.clip).filter(Boolean), ...(d.off_slot || []), ...d.done,
    ...(d.days || []).flatMap((day) => day.posts)];
}
const pubFind = (key) => pubClips().find((c) => pubKey(c) === key) || null;

/* ---------- Chargement ---------- */

function pubWantedKey() { return `${pubUi.account}|${pubUi.range}|${pubUi.week}`; }

function pubLoad() {
  if (pubUi.loading) { pubUi.dirty = true; return pubUi.loading; }
  const key = pubWantedKey();
  pubUi.loading = (async () => {
    try {
      const data = await api(`/api/publish?account=${pubEnc(pubUi.account)}&week=${pubEnc(pubUi.week)}&range=${pubEnc(pubUi.range)}`);
      if (key === pubWantedKey()) {
        pubUi.data = data; pubUi.error = null;
        if (!pubUi.week) pubUi.week = data.week_start; // « cette semaine » : on retient le lundi renvoye
        pubUi.key = pubWantedKey();
      }
    } catch (err) {
      if (key === pubWantedKey()) { pubUi.data = null; pubUi.error = err; pubUi.key = key; }
    } finally {
      pubUi.loading = null;
      pubUi.at = Date.now();
    }
    if (currentScreen === "publish") renderCurrent();
    if (pubUi.dirty) { pubUi.dirty = false; pubLoad(); }
  })();
  return pubUi.loading;
}

function pubPostsLoad() {
  if (pubPosts.loading) { pubPosts.dirty = true; return pubPosts.loading; }
  pubPosts.loading = (async () => {
    try {
      pubPosts.data = await api("/api/publications");
      pubPosts.error = null;
    } catch (err) {
      pubPosts.error = err;
    } finally {
      pubPosts.loading = null;
      pubPosts.at = Date.now();
    }
    if (currentScreen === "publish") renderCurrent();
    if (pubPosts.dirty) { pubPosts.dirty = false; pubPostsLoad(); }
  })();
  return pubPosts.loading;
}

// Un evenement publish / video / queue rend la semaine et la liste obsoletes (le serveur reste la source).
document.addEventListener("clipper:event", () => {
  pubUi.at = 0;
  pubPosts.at = 0;
  pubRep.at = 0;
  if (currentScreen === "publish" && !pubUi.dragKey) {
    pubLoad();
    pubPostsLoad();
    repLoad();
  }
});

/* ---------- Rendu ---------- */

function pubPost(c, extra) {
  // Une programmation « à vérifier » (post peut-être déjà sur TikTok, publication-I1) ne se replanifie pas en la glissant.
  const draggable = (c.publish_status === "approved" || c.publish_status === "scheduled" || c.publish_status === "failed") && !c.missing && !c.to_verify;
  const icoName = c.publish_status === "published" ? "circle-check" : c.publish_status === "failed" ? "circle-alert" : "";
  return `<div class="post ${esc(c.publish_status)}${c.missing ? " missing" : ""}" data-post="${esc(pubKey(c))}" draggable="${draggable}" tabindex="0" role="button" aria-label="Ouvrir le clip ${esc(pubTitle(c))}" title="${esc(pubTitle(c))}">
    <div class="mini-clip">${c.video_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(c.thumbnail_url)}" alt="" tabindex="-1">` : ""}</div>
    <span class="pt">${esc(c.missing ? `Clip introuvable (${c.clip_id})` : pubTitle(c))}</span>
    ${c.account ? `<span class="pub-acct${c.waiting_reason ? " waiting" : ""}" data-post-account title="${esc(c.waiting_reason || `Compte : ${pubAccountLabel(c.account)}`)}">${esc(pubAccountLabel(c.account))}</span>` : ""}
    ${c.missing ? "" : `<span class="pub-service" data-post-service>${esc(pubServiceName(c))}</span>`}
    ${extra || ""}${icoName ? icon(icoName, "i-xs") : ""}</div>`;
}

/* Calendrier (TASK-ad4d) : Jour / Semaine / Mois. Le serveur regroupe déjà les publications par jour
   (`d.days`, triées à l'heure de Paris, jamais deux au même instant ne s'écrasent : c'est une liste, pas une
   case par heure) ; le JS ne fait que choisir le gabarit et poser les créneaux libres restants comme cibles
   de dépôt (Jour et Semaine seulement, SPEC-c100). Aucune date n'est recalculée ici au-delà de l'affichage. */
const pubToday = () => new Date().toLocaleDateString("sv-SE", { timeZone: PUB_TZ });

/* Créneau libre d'un jour donné, rendu comme avant (TASK-5c00) : une case vide ciblable au glisser-déposer. */
function pubFreeSlot(s, inner) {
  const past = Date.parse(s.slot_at) < Date.now();
  return `<div class="cal-c slot free${past ? " past" : ""}" data-slot-at="${esc(s.slot_at)}" data-slot="${esc(pubTime(s.slot_at_paris))}" aria-label="Créneau ${esc(pubSlotLabel(s.slot_at_paris))}, libre">${inner || ""}</div>`;
}

/* Tout ce qui occupe un jour (ymd) : ses publications (`d.days`) et, Jour/Semaine seulement, ses créneaux
   encore libres (`d.slots`) comme cibles de dépôt ; un créneau déjà occupé n'y figure qu'une fois, via la
   publication (jamais dupliqué avec le créneau). Trié à l'heure de Paris. */
function pubDayItems(d, ymd) {
  const box = (d.days || []).find((x) => x.date === ymd);
  const posts = box ? box.posts : [];
  const frees = (d.slots || []).filter((s) => s.free && pubDate(s.slot_at_paris) === ymd);
  const at = (item) => (item.__free ? item.slot_at_paris : (item.slot_at_paris || item.published_at_paris || ""));
  return [...posts, ...frees.map((s) => ({ __free: true, ...s }))].sort((a, b) => at(a).localeCompare(at(b)));
}

/* Ligne minuscule d'une publication (palier mini Semaine) : heure + titre tronque + pastille de statut, pas de
   vignette ni de details (ADR-ad2e : rien n'est cache, juste reduit au minimum lisible). Les creneaux libres
   restent des cibles de depot normales (pubFreeSlot). */
function pubMiniRow(it) {
  const at = it.slot_at_paris || it.published_at_paris || "";
  return `<div class="cal-mini-row" data-post="${esc(pubKey(it))}" tabindex="0" role="button" aria-label="${esc(pubTitle(it))}, ${esc(pubStatusOf(it).label)}">
    <span class="cal-mini-time">${esc(pubTime(at))}</span><span class="cal-mini-title">${esc(pubTitle(it))}</span>
    <i class="cal-mini-dot ${esc(it.publish_status || "")}" aria-hidden="true"></i></div>`;
}

/* Mois (TASK-39bbe20b3284) : pas de boite de publication, seulement le numero du jour et le nombre de videos
   (« 17 vidéos », « 1 vidéo », rien si 0) ; un clic sur la case ouvre la vue Jour de ce jour (data-pub-day). */
function pubMonthBoxHtml(d, ymd) {
  const box = (d.days || []).find((x) => x.date === ymd);
  const count = box ? box.posts.length : 0;
  const cls = ["small", ymd === pubToday() ? "today" : ""].filter(Boolean).join(" ");
  const label = count ? `${count} ${count > 1 ? "vidéos" : "vidéo"}` : "";
  return `<div class="cal-day ${cls}" data-date="${esc(ymd)}" data-pub-day="${esc(ymd)}" tabindex="0" role="button" aria-label="${esc(pubFmt(ymd, { weekday: "long", day: "numeric", month: "long" }))}${label ? `, ${label}` : ""}">
    <div class="cal-day-h"><span class="n">${esc(pubFmt(ymd, { day: "numeric" }))}</span></div>
    ${label ? `<div class="cal-month-count">${label}</div>` : ""}
  </div>`;
}

function pubDayBoxHtml(d, ymd) {
  const items = pubDayItems(d, ymd);
  const count = items.filter((it) => !it.__free).length;
  const tier = pubBoxClass(count);
  const body = items.map((it) => (it.__free ? pubFreeSlot(it)
    : tier === "mini" ? pubMiniRow(it)
    : pubPost(it, `<span class="pub-day-time">${esc(pubTime(it.slot_at_paris || it.published_at_paris))}</span>`))).join("");
  const cls = [tier, ymd === pubToday() ? "today" : ""].filter(Boolean).join(" ");
  return `<div class="cal-day${cls ? ` ${cls}` : ""}" data-date="${esc(ymd)}">
    <div class="cal-day-h"><span class="n">${esc(pubFmt(ymd, { day: "numeric" }))}</span>${count ? `<span class="cal-day-count">${count}</span>` : ""}</div>
    <div class="cal-day-body">${body}</div>
  </div>`;
}

/* Semaine : une case par jour (toutes ses publications, mises à l'échelle avec leur nombre) plutôt qu'une
   case par heure (TASK-ad4d, remplace la grille horaire de TASK-5c00). Pas de colonne d'heure (TASK-460904227d2f,
   reste de l'ancienne grille horaire) : sept colonnes, une par jour. */
function pubCalendarWeek(d) {
  const days = Array.from({ length: 7 }, (_, i) => pubShift(d.week_start, i));
  const head = days.map((ymd) => `<div class="cal-h${ymd === pubToday() ? " today" : ""}"><div class="d">${esc(pubFmt(ymd, { weekday: "short" }))}</div><div class="n">${esc(pubFmt(ymd, { day: "numeric" }))}</div></div>`).join("");
  const body = days.map((ymd) => pubDayBoxHtml(d, ymd)).join("");
  return `<div class="cal-wrap"><div class="cal cal-boxes" id="pub-cal">${head}${body}</div></div>`;
}

/* Mois : grille classique (lundi en tête), pas de cible de dépôt (le serveur ne renvoie aucun créneau pour
   cette plage, SPEC-c100). Les cases hors mois ne sont que du remplissage visuel, jamais une date calculée
   pour y ranger une publication. */
function pubCalendarMonth(d) {
  const first = d.days[0].date;
  const lead = (pubUtc(first).getUTCDay() + 6) % 7; // lundi = 0
  const cells = lead + d.days.length;
  const trailing = (7 - (cells % 7)) % 7;
  const weekdays = Array.from({ length: 7 }, (_, i) => pubShift(first, i - lead));
  const head = weekdays.map((ymd) => `<div class="cal-h"><div class="d">${esc(pubFmt(ymd, { weekday: "short" }))}</div></div>`).join("");
  const blanks = (n) => Array.from({ length: n }, () => `<div class="cal-day blank"></div>`).join("");
  const body = `${blanks(lead)}${d.days.map((day) => pubMonthBoxHtml(d, day.date)).join("")}${blanks(trailing)}`;
  return `<div class="cal-wrap"><div class="cal-month" id="pub-cal">${head}${body}</div></div>`;
}

/* Jour : liste horaire détaillée (heure, compte, service, titre, statut), pas une case. */
function pubCalendarDay(d) {
  const ymd = d.range_start;
  const items = pubDayItems(d, ymd);
  if (!items.length) return `<p class="reason">Aucune publication ce jour.</p>`;
  const row = (it) => it.__free
    ? pubFreeSlot(it, `<span class="pub-day-time">${esc(pubTime(it.slot_at_paris))}</span><span class="muted">Créneau libre</span>`)
    : pubPost(it, `<span class="pub-day-time">${esc(pubTime(it.slot_at_paris || it.published_at_paris))}</span>${pubChip(it)}`);
  return `<div class="pub-day-list" id="pub-cal">${items.map(row).join("")}</div>`;
}

function pubCalendar(d) {
  if (d.range === "day") return pubCalendarDay(d);
  if (d.range === "month") return pubCalendarMonth(d);
  return pubCalendarWeek(d);
}

/* Ligne de detail d'une publication terminee : « programmee » tant que l'heure n'est pas passee, « publie » ensuite. */
function pubDoneLine(c) {
  const live = c.publish_status !== "published" || !pubIsScheduledOnService(c) || c.tiktok_live;
  const state = c.publish_status === "failed" ? "échec"
    : live ? "publié" : `programmée sur ${pubServiceName(c)}${c.tiktok_publish_at ? `, en ligne le ${pubWhen(c.tiktok_publish_at)}` : ""}`;
  const at = c.publish_status === "published" && !live ? "" : (c.publish_status === "published" ? (c.published_at_paris || c.slot_at_paris) : c.slot_at_paris);
  return `${state}${at ? ` · ${pubSlotLabel(at)}` : ""}`;
}

/* Comptes TikTok proposes au selecteur : ceux de l'ecran Comptes (le serveur les rend avec chaque reponse). */
function pubAccounts(d) {
  return (d && d.accounts) || (pubPosts.data && pubPosts.data.accounts) || [];
}

// Libellés de navigation par plage (TASK-ad4d) : les trois jeux de textes vivent tous dans la source, quelle
// que soit la vue active, pour que « Semaine précédente »/« Semaine suivante » restent toujours trouvables.
const PUB_RANGE_LABELS = {
  day: { prev: "Jour précédent", next: "Jour suivant", today: "Aujourd'hui" },
  week: { prev: "Semaine précédente", next: "Semaine suivante", today: "Cette semaine" },
  month: { prev: "Mois précédent", next: "Mois suivant", today: "Ce mois" },
};
const PUB_RANGE_OPTS = [["day", "Jour"], ["week", "Semaine"], ["month", "Mois"]];

/* Libellé de la plage affichée, calculé depuis les bornes renvoyées par le serveur (jamais recalculé ici). */
function pubRangeLabel(d) {
  if (!d) return "";
  if (d.range === "day") return pubFmt(d.range_start, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  if (d.range === "month") return pubFmt(d.range_start, { month: "long", year: "numeric" });
  return `${pubFmt(d.week_start, { day: "numeric", month: "short" })} au ${pubFmt(d.week_end, { day: "numeric", month: "short", year: "numeric" })}`;
}

function pubToolbar(d) {
  const labels = PUB_RANGE_LABELS[pubUi.range];
  const style = d && d.account ? `<span class="muted">Créneaux du compte (écran Comptes)</span>` : "";
  return `<div class="toolbar pub-toolbar">
    <select class="input" id="pub-account" aria-label="Compte de publication"><option value="">Tous les comptes</option>${pubAccounts(d).map((a) => `<option value="${esc(a.id)}"${a.id === pubUi.account ? " selected" : ""}>${esc(pubAccountText(a))}</option>`).join("")}</select>
    <div class="seg" role="group" aria-label="Vue du calendrier">${PUB_RANGE_OPTS.map(([k, label]) => `<button type="button" class="${k === pubUi.range ? "on" : ""}" data-range="${k}" aria-pressed="${k === pubUi.range}">${label}</button>`).join("")}</div>
    <div class="row" style="gap:4px"><button type="button" class="icon-btn" data-week="-1" aria-label="${esc(labels.prev)}"${d ? "" : " disabled"}>${icon("chevron-left")}</button>
      <h2 style="font-size:16px;min-width:11ch;text-align:center">${esc(pubRangeLabel(d))}</h2>
      <button type="button" class="icon-btn" data-week="1" aria-label="${esc(labels.next)}"${d ? "" : " disabled"}>${icon("chevron-right")}</button>
      <button type="button" class="btn btn-xs btn-ghost" data-week="0">${esc(labels.today)}</button></div>
    <span class="grow"></span>
    <span class="pub-account-style">${style}</span>
    <div class="legend"><span><i class="pub-leg info"></i>planifié / programmé</span><span><i class="pub-leg ok"></i>publié</span><span><i class="pub-leg bad"></i>échec</span></div>
  </div>`;
}

const PUB_HELP = `<div class="banner">${icon("info", "i-lg")}<p><b>Publier :</b> « Nouvelle publication » choisit un clip, un compte, puis Maintenant ou une date ; le worker publie sur TikTok (Chrome visible). Un arrêt (captcha, connexion expirée...) met la publication en échec avec une capture : « Réessayer » la relance. Le sélecteur en haut choisit le compte TikTok (ou tous les comptes). Les créneaux du style lié au compte sont facultatifs : ils servent à planifier un clip approuvé en le glissant sur le calendrier.</p></div>`;

/* Zone principale : le calendrier de la semaine, toujours affiché (TASK-5c00), même sans compte choisi ni
   créneaux réguliers. Le message « aucun creneau » n'y apparait qu'une fois, en indication au-dessus du calendrier
   (jamais à sa place : la semaine garde ses publications passées et à venir). */
function pubCalendarZone(d) {
  if (pubUi.error) return `<p class="reason bad">Chargement impossible : ${esc(pubUi.error.message || pubUi.error)}</p>`;
  if (!d) return `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`;
  const hint = d.reason ? `<p class="reason" data-no-slot>${esc(d.reason)} (facultatif : « Nouvelle publication » publie sans créneau.)</p>` : "";
  return `<section>${hint}${pubCalendar(d)}</section>`;
}

/* Agencement de la maquette : a gauche « Nouvelle publication » et la liste unique « En attente » (TASK-5c00,
   validés sans date + publications en cours/échecs) ; a droite le calendrier de la semaine (toujours affiché,
   passé et futur : créneaux réguliers, programmations hors créneau, publiés et échecs). */
function pubLayoutHtml(d, postsHtml) {
  const main = `${pubToolbar(d)}${pubCalendarZone(d)}`;
  return `<div class="pub-pad">${PUB_HELP}<div class="grid g-side pub-grid" style="align-items:start"><div class="stack pub-side">${postsHtml}</div><div class="stack pub-main">${main}</div></div></div>`;
}

function pubView(body) {
  const html = pubLayoutHtml(pubUi.data, repHtml() + pubPostsSection());
  if (pubUi.html === html && body.childElementCount) return false; // rien de change : on garde les vignettes et le survol
  pubUi.html = html;
  body.innerHTML = html;
  return true;
}


/* ---------- Plan de demain (SPEC-78dc R9) ----------
   Répartition automatique : le plan vient de l'API (/api/repartition*) et la page l'affiche, rien d'autre. Aucun
   score, plafond ni créneau n'est calculé ici (ADR-49cd) : une modification renvoie les lignes au serveur, qui
   les redécrit, les prévisualise (refusal) et les valide. */

const pubRep = { data: null, error: null, loading: null, busy: null, busyPromise: null, at: 0 };

function repLoad() {
  if (pubRep.loading) return pubRep.loading;
  pubRep.loading = (async () => {
    try {
      pubRep.data = await api("/api/repartition");
      pubRep.error = null;
    } catch (err) {
      pubRep.error = err;
    } finally {
      pubRep.loading = null;
      pubRep.at = Date.now();
    }
    if (currentScreen === "publish") renderCurrent();
  })();
  return pubRep.loading;
}

const repClock = (iso) => new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone: PUB_TZ });
const repInstant = (iso) => new Date(iso).getTime();
const repSplit = (key) => { const i = key.lastIndexOf("|"); return [key.slice(0, i), key.slice(i + 1)]; };

/* Les lignes du compte et, entre elles, ses créneaux restés sans clip (« aucun clip disponible »), par heure. */
function repRows(account) {
  const taken = new Set(account.lines.map((l) => repInstant(l.slot_at)));
  const rows = account.lines.map((line, index) => ({ at: repInstant(line.slot_at), line, index }));
  (account.slots || []).forEach((slot) => { if (!taken.has(repInstant(slot.slot_at))) rows.push({ at: repInstant(slot.slot_at), slot }); });
  return rows.sort((a, b) => a.at - b.at);
}

function repScore(l) {
  if (typeof l.score !== "number") return `<span class="muted">sans score</span>`;
  const bonus = typeof l.bonus === "number" && l.bonus !== 0 ? ` <span class="rep-bonus">${l.bonus > 0 ? "+" : "−"}${fr(Math.abs(l.bonus), 1)}</span>` : "";
  return `<span class="rep-score"${l.bonus_reason ? ` title="${esc(l.bonus_reason)}"` : ""}>${fr(l.score, 1)}${bonus}</span>`;
}

function repClipSelect(d, account, key, usedKey) {
  const used = new Set(d.accounts.flatMap((a) => a.lines.map(pubKey)));
  const options = (account.pool || []).filter((u) => pubKey(u) === usedKey || !used.has(pubKey(u)))
    .map((u) => `<option value="${esc(pubKey(u))}">${esc(pubTitle(u))}${typeof u.score === "number" ? ` · ${fr(u.score, 1)}` : ""}</option>`).join("");
  return `<select class="input rep-clip" data-rep-clip="${esc(account.account)}|${esc(key)}" aria-label="Changer le clip" ${pubRep.busy ? "disabled" : ""}><option value="">Changer le clip</option>${options}</select>`;
}

function repLineRow(d, account, row, editable) {
  const l = row.line;
  const where = l.game_name ? esc(l.game_name) : "jeu inconnu : VOD";
  const badges = `${l.exploration ? `<span class="chip info">exploration</span>` : ""}${l.prime ? `<span class="chip">soir</span>` : ""}`;
  const tools = editable ? `<div class="row wrap rep-tools">
      ${repClipSelect(d, account, String(row.index), pubKey(l))}
      <input type="datetime-local" class="input rep-time" data-rep-time="${esc(account.account)}|${row.index}" min="${esc(d.day)}T00:00" max="${esc(d.day)}T23:59" value="${esc(pubLocalInput(l.slot_at))}" aria-label="Changer l'heure" ${pubRep.busy ? "disabled" : ""}>
      <button type="button" class="btn btn-xs btn-ghost" data-rep-remove="${esc(account.account)}|${row.index}" ${pubRep.busy ? "disabled" : ""}>Retirer</button></div>` : "";
  return `<div class="list-item rep-row" data-rep-row>
    <span class="rep-hour">${esc(repClock(l.slot_at))}</span>
    <div class="mini-clip">${l.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(l.thumbnail_url)}" alt="">` : ""}</div>
    <div class="li-main grow"><div class="li-title">${esc(l.screen_title || l.title || l.clip_id)}</div>
      <div class="li-sub muted">${repScore(l)} · ${where} ${badges}</div>
      ${l.refusal ? `<div class="li-sub bad">${esc(l.refusal)}</div>` : ""}
      ${l.warning ? `<div class="li-sub warn">${esc(l.warning)}</div>` : ""}
      ${tools}</div></div>`;
}

function repEmptyRow(d, account, row, editable) {
  const pick = editable ? repClipSelect(d, account, `@${row.slot.slot_at}`, "") : "";
  return `<div class="list-item rep-row rep-empty" data-rep-row>
    <span class="rep-hour">${esc(repClock(row.slot.slot_at))}</span>
    <div class="li-main grow"><div class="li-sub muted">aucun clip disponible</div>${pick}</div></div>`;
}

function repAccountHtml(d, account, editable) {
  const locked = (d.created || []).some((c) => c.account === account.account);
  const can = editable && !locked;
  const rows = repRows(account).map((row) => (row.line ? repLineRow(d, account, row, can) : repEmptyRow(d, account, row, can))).join("");
  const notes = (account.notes || []).map((n) => `<p class="li-sub muted">${esc(n)}</p>`).join("");
  return `<div class="rep-account"><div class="rep-account-title">${esc(account.label || account.account)}${locked ? ` <span class="chip ok">publications créées</span>` : ""}</div>
    <div class="panel">${rows || `<p class="muted" style="padding:10px">aucun créneau</p>`}</div>${notes}</div>`;
}

function repStateChip(d) {
  if (d.enabled === false) return `<span class="chip pending">désactivé</span>`;
  if (d.status === "validated") return `<span class="chip ok">validé le ${esc(d.validated_at ? pubWhen(d.validated_at) : "")}</span>`;
  if (d.status === "error") return `<span class="chip bad">erreur</span>`;
  if (d.status === "proposed") return `<span class="chip info">proposé</span>`;
  return `<span class="chip pending">aucun plan</span>`;
}

function repSection(body) {
  return `<section id="pub-plan" class="rep-plan"><div class="section-title">${icon("calendar-days")}Plan de demain</div>${body}</section>`;
}

function repHtml() {
  const d = pubRep.data;
  if (!d) {
    return repSection(pubRep.error ? `<p class="reason bad">Chargement impossible : ${esc(pubRep.error.message || pubRep.error)}</p>` : `<div class="skeleton skeleton-line"></div>`);
  }
  const head = `<div class="row wrap" style="gap:8px;align-items:center"><span class="muted">${esc(d.day ? pubFmt(d.day, { weekday: "long", day: "numeric", month: "long" }) : "")}</span>${repStateChip(d)}</div>`;
  if (d.enabled === false) {
    return repSection(`${head}<p class="muted" style="font-size:13px">La répartition automatique est désactivée (<code>[repartition] enabled = false</code>) : aucun plan n'est calculé.</p>`);
  }
  const accounts = d.accounts || [];
  const proposed = d.status === "proposed";
  const accepted = proposed && accounts.some((a) => a.lines.some((l) => !l.refusal));
  const computed = d.computed_at ? `<p class="li-sub muted">calculé le ${esc(pubWhen(d.computed_at))}${d.computed_by === "web" ? " (à la demande)" : ""}</p>` : "";
  const failure = d.status === "error" ? `<p class="reason bad">Calcul en erreur : ${esc((d.error && d.error.message) || "erreur inconnue")}</p>` : "";
  const absent = d.status === "absent" ? `<p class="muted" style="font-size:13px">Aucun plan calculé pour ce jour : « Calculer » le prépare tout de suite (le worker le fait chaque soir).</p>` : "";
  const last = d.last_error ? `<p class="reason bad">Dernière validation refusée : ${esc(d.last_error)}</p>` : "";
  const canCompute = !pubRep.busy && d.status !== "validated";
  const buttons = `<div class="row wrap" style="gap:8px;margin:8px 0">
    <button type="button" class="btn btn-xs" data-rep-compute ${canCompute ? "" : "disabled"}>${pubRep.busy === "compute" ? "Calcul en cours…" : (d.status === "absent" ? "Calculer" : "Recalculer")}</button>
    <button type="button" class="btn btn-xs btn-primary" data-rep-validate ${!pubRep.busy && accepted ? "" : "disabled"}>${pubRep.busy === "validate" ? "Validation en cours…" : "Valider le plan"}</button></div>`;
  const notes = (d.notes || []).map((n) => `<p class="li-sub muted">${esc(n)}</p>`).join("");
  return repSection(`${head}${computed}${failure}${absent}${last}${buttons}${accounts.map((a) => repAccountHtml(d, a, proposed)).join("")}${notes}`);
}

/* Une requête à la fois : pendant qu'elle court, les boutons sont désactivés et libellés « en cours ». */
function repRun(label, errorTitle, work) {
  if (pubRep.busy) return pubRep.busyPromise;
  pubRep.busy = label;
  renderCurrent();
  pubRep.busyPromise = (async () => {
    try {
      await work();
    } catch (err) {
      toastError(errorTitle, err);
      await repLoad();
    } finally {
      pubRep.busy = null;
    }
    renderCurrent();
  })();
  return pubRep.busyPromise;
}

const repCompute = () => repRun("compute", "Impossible de recalculer le plan", async () => {
  pubRep.data = await api("/api/repartition/compute", jsonBody("POST", { day: pubRep.data && pubRep.data.day }));
});

const repValidate = () => repRun("validate", "Impossible de valider le plan", async () => {
  if (!(await netGuard())) return;
  const res = await api(`/api/repartition/${pubEnc(pubRep.data.day)}/validate`, { method: "POST" });
  pubRep.data = res;
  const n = (res.created || []).length;
  toast({ kind: "ok", title: `${n} publication${n > 1 ? "s" : ""} créée${n > 1 ? "s" : ""}`, body: "Elles sont dans « En attente » et au calendrier.", ms: 3600 });
  pubPosts.at = 0; pubPostsLoad();
  pubUi.at = 0; pubLoad();
});

/* Remplace les lignes d'un compte (PUT) : le serveur redécrit chaque ligne et renvoie le plan à jour. */
function repSave(accountId, change) {
  const d = pubRep.data;
  const account = d.accounts.find((a) => a.account === accountId);
  const lines = change(account.lines.map((l) => ({ slot_at: l.slot_at, video_id: l.video_id, clip_id: l.clip_id })))
    .sort((a, b) => repInstant(a.slot_at) - repInstant(b.slot_at));
  return repRun("save", "Impossible de modifier le plan", async () => {
    pubRep.data = await api(`/api/repartition/${pubEnc(d.day)}`, jsonBody("PUT", { accounts: [{ account: accountId, lines }] }));
  });
}

const repRemove = (accountId, index) => repSave(accountId, (lines) => lines.filter((_, i) => i !== Number(index)));
const repSetTime = (accountId, index, local) => repSave(accountId, (lines) => lines.map((l, i) => (i === Number(index) ? { ...l, slot_at: pubParisInstant(local).toISOString() } : l)));
function repSetClip(accountId, where, clipKey) {
  const [video_id, clip_id] = clipKey.split("/");
  return repSave(accountId, (lines) => (where.startsWith("@")
    ? [...lines, { slot_at: where.slice(1), video_id, clip_id }]
    : lines.map((l, i) => (i === Number(where) ? { ...l, video_id, clip_id } : l))));
}

function repWire(body) {
  const compute = $("[data-rep-compute]", body), validate = $("[data-rep-validate]", body);
  if (compute) compute.onclick = () => repCompute();
  if (validate) validate.onclick = () => repValidate();
  $$("[data-rep-remove]", body).forEach((b) => (b.onclick = () => repRemove(...repSplit(b.dataset.repRemove))));
  $$("[data-rep-time]", body).forEach((input) => (input.onchange = () => { if (input.value) repSetTime(...repSplit(input.dataset.repTime), input.value); }));
  $$("[data-rep-clip]", body).forEach((select) => (select.onchange = () => { if (select.value) repSetClip(...repSplit(select.dataset.repClip), select.value); }));
}
/* ---------- fin Plan de demain ---------- */

/* ---------- Publications pilotees : liste, statuts, modifier / annuler (SPEC-1ed3 R5) ---------- */

/* Une publication « terminee » (publiee, ou programmee sur TikTok) quitte la liste en cours : elle reste au calendrier. */
const pubIsOngoing = (p) => p.publish_status !== "published";
/* Entree approuvee a l'ancienne, sans creneau ni Maintenant/Programmer : elle attend sans le dire. */
const pubNeedsMoment = (p) => p.publish_status === "approved" && !p.publish_mode && !p.slot_at;

function pubPostRow(p) {
  const mode = p.publish_mode === "scheduled" ? "programmée" : "maintenant";
  const when = p.tiktok_publish_at ? `en ligne le ${pubWhen(p.tiktok_publish_at)}` : (p.slot_at ? `${mode} · ${pubWhen(p.slot_at)}` : "");
  const detail = [when, p.account ? pubAccountLabel(p.account) : "", p.channel || "sans style"].filter(Boolean).join(" · ");
  return `<div class="list-item pub-post-row" data-pub-row="${esc(pubKey(p))}">
    <div class="mini-clip">${p.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(p.thumbnail_url)}" alt="">` : ""}</div>
    <div class="li-main grow"><div class="li-title">${esc(pubTitle(p))}</div>
      <div class="li-sub muted">${esc(detail)}</div>
      ${pubNeedsMoment(p) ? `<div class="li-sub warn" data-needs-moment>choisis Maintenant ou une date (Modifier)</div>` : ""}
      ${p.waiting_reason ? `<div class="li-sub warn" data-waiting-reason>${esc(p.waiting_reason)}</div>` : ""}
      ${p.publish_error ? `<div class="li-sub bad">Échec : ${esc(p.publish_error)}</div>` : ""}
      ${p.postponed_reason ? `<div class="li-sub muted">${esc(p.postponed_reason)}</div>` : ""}
      ${p.capture_url ? `<a href="${esc(p.capture_url)}" target="_blank" rel="noopener">voir la capture d'écran</a>` : ""}</div>
    ${pubChip(p)}
    <div class="row wrap" style="gap:6px">
      ${pubNeedsMoment(p) ? `<button type="button" class="btn btn-xs btn-primary" data-publish-now="${esc(pubKey(p))}">Publier maintenant</button>` : ""}
      ${p.publish_status === "failed" ? `<button type="button" class="btn btn-xs btn-primary" data-pub-retry>Réessayer</button>` : ""}
      ${p.editable ? `<button type="button" class="btn btn-xs" data-pub-edit>Modifier</button><button type="button" class="btn btn-xs btn-ghost" data-pub-cancel>Annuler</button>` : ""}
    </div></div>`;
}

/* Liste unique « En attente » (TASK-5c00, SPEC-1ed3) : les clips validés sans date et les publications en
   cours ou en échec ne sont listés qu'une fois ici (la section « À publier » faisait doublon avec celle-ci :
   un clip validé sans date y apparaissait aussi, cf. `pubNeedsMoment`, avec son propre « Publier maintenant »). */
function pubPostsSection() {
  const rows = pubPosts.data ? pubPosts.data.publications.filter((p) => pubIsOngoing(p) && (!pubUi.account || p.account === pubUi.account)) : [];
  // Boutons sur leur propre ligne : dans le titre, ils debordaient sur la colonne de droite (colonne etroite).
  const head = `<div class="section-title">${icon("inbox")}En attente <span class="more">${rows.length || ""}</span></div>
    <div class="row wrap" style="gap:8px;margin-bottom:10px">
    <button type="button" class="btn btn-primary btn-xs grow" data-pub-new>${icon("plus", "i-xs")}Nouvelle publication</button>
    <button type="button" class="btn btn-xs grow" data-pub-series>${icon("calendar-days", "i-xs")}Programmer une série</button></div>`;
  if (pubPosts.error) return `<section>${head}<p class="reason bad">Chargement impossible : ${esc(pubPosts.error.message || pubPosts.error)}</p></section>`;
  if (!pubPosts.data) return `<section>${head}<div class="skeleton skeleton-line"></div></section>`;
  return `<section>${head}${rows.length ? `<div class="panel" id="pub-posts">${rows.map(pubPostRow).join("")}</div>`
    : `<p class="muted" style="font-size:13px">Aucune publication en attente : « Nouvelle publication » choisit un clip, le compte, maintenant ou à une date. Les publications terminées sont dans le calendrier.</p>`}</section>${pubRefusedSection()}${pubRemovedSection()}`;
}

/* Clips refusés par TikTok à la vérification de contenu (TASK-7f582251f6c5) : retirés des clips disponibles, listés
   à part avec la raison et la capture, jamais republiés automatiquement ; le compte et la série continuent. */
function pubRefusedSection() {
  const rows = pubPosts.data ? (pubPosts.data.refused_by_platform || []).filter((p) => !pubUi.account || p.account === pubUi.account) : [];
  if (!rows.length) return "";
  const row = (p) => `<div class="list-item"><div class="li-main"><div class="li-title">${esc(pubTitle(p))}</div>
      <div class="li-sub bad">Refusé par TikTok : ${esc(p.error || "problème signalé à la vérification de contenu")}</div>
      ${p.capture_url ? `<a href="${esc(p.capture_url)}" target="_blank" rel="noopener">voir la capture d'écran</a>` : ""}</div></div>`;
  return `<section id="pub-refused"><div class="section-title">${icon("circle-x")}Refusés par TikTok <span class="more">${rows.length}</span></div>
    <div class="panel">${rows.map(row).join("")}</div></section>`;
}

/* Posts supprimés à la main de la plateforme (TASK-5a7b750462c4) : sortis de la file, du calendrier et des
   statistiques d'apprentissage, jamais republiés ; Clipper n'a rien effacé sur TikTok/YouTube. */
function pubRemovedSection() {
  const rows = pubPosts.data ? (pubPosts.data.removed_from_platform || []).filter((p) => !pubUi.account || p.account === pubUi.account) : [];
  if (!rows.length) return "";
  const row = (p) => `<div class="list-item"><div class="li-main"><div class="li-title">${esc(pubTitle(p))}</div>
      <div class="li-sub muted">Supprimé de la plateforme${p.removed_at ? ` le ${esc(pubWhen(p.removed_at))}` : ""}${p.removed_reason ? ` : ${esc(p.removed_reason)}` : ""}</div></div></div>`;
  return `<section id="pub-removed"><div class="section-title">${icon("trash-2")}Supprimés de la plateforme <span class="more">${rows.length}</span></div>
    <div class="panel">${rows.map(row).join("")}</div></section>`;
}

async function pubPostCancel(p) {
  const ok = await confirmDialog({ title: "Annuler cette publication ?", body: `« ${pubTitle(p)} » ne sera pas publié ; le clip redevient « à valider ».`, confirmLabel: "Annuler la publication" });
  if (!ok) return;
  try {
    await api(`/api/publications/${pubEnc(p.video_id)}/${pubEnc(p.clip_id)}`, { method: "DELETE" });
    toast({ kind: "warn", title: "Publication annulée", body: pubTitle(p), ms: 2600 });
  } catch (err) { toastError("Impossible d'annuler la publication", err); }
  pubPosts.at = 0;
  pubPostsLoad();
}

/* ---------- Formulaire « Nouvelle publication » (SPEC-1ed3 R1, R2) ---------- */

const PUB_VISIBILITIES = [["public", "Tout le monde"], ["friends", "Ami(e)s"], ["private", "Toi uniquement"]];
const PUB_YT_VISIBILITIES = [["public", "Publique"], ["unlisted", "Non répertoriée"], ["private", "Privée"]];
const PUB_CHECKS = [["off", "Désactivée (rapide)"], ["wait", "Attendre le résultat (~10 min)"]];

function pubFormClips(clips) {
  return clips.filter((c) => c.ready && (c.publish_status === "à valider" || c.publish_status === "approved"))
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")) || pubKey(b).localeCompare(pubKey(a)));
}

const pubWhenHint = (f, service) => (service === "youtube"
  ? `Programmer : YouTube programme la vidéo (heure de Paris, publiée comme publique) si la date est entre ${f.ytMinMinutes} min et ${f.ytMaxDays} jours ; au-delà, Clipper la garde et la programme le moment venu.`
  : `Programmer : TikTok programme la vidéo si la date est entre ${f.minMinutes} min et ${f.maxDays} jours ; au-delà, Clipper la garde et la programme le moment venu.`);

/* Service (tiktok | youtube) du compte choisi dans le formulaire : les reglages affiches sont ceux de ce service. */
function pubFormService(d) {
  const pick = $("#pub-form-account", d);
  const chosen = pick && pick.selectedOptions && pick.selectedOptions[0];
  return (chosen && chosen.dataset.service) || "tiktok";
}

function pubFormShowService(f, d) {
  const youtube = pubFormService(d) === "youtube";
  $("#pub-form-tiktok", d).hidden = youtube;
  $("#pub-form-youtube", d).hidden = !youtube;
  $("#pub-form-when-hint", d).textContent = pubWhenHint(f, youtube ? "youtube" : "tiktok");
}

function pubFormHtml(f) {
  const videos = Array.from(new Set(f.clips.map((c) => c.video_id)));
  const channels = Array.from(new Set(f.clips.map((c) => c.channel || ""))).sort();
  const ready = f.accounts.filter((a) => a.ready_to_publish);
  const o = f.options, y = f.ytOptions;
  const editing = Boolean(f.edit);
  return `
    <div class="modal-head"><h2>${editing ? "Modifier la publication" : "Nouvelle publication"}</h2>
      <p class="muted" style="margin-top:4px">Valider approuve le clip et le met en file : aucun créneau de style n'est nécessaire.</p></div>
    <div class="modal-body stack" style="gap:16px">
      ${editing ? "" : `<div class="field"><span class="field-label">Clip</span>
        <div class="row wrap" style="gap:8px">
          <input class="input" id="pub-form-search" type="search" placeholder="Rechercher (titre, description, identifiant)" aria-label="Rechercher un clip" style="flex:1 1 220px">
          <select class="input" id="pub-form-video" aria-label="Filtrer par vidéo"><option value="">Toutes les vidéos</option>${videos.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("")}</select>
          <select class="input" id="pub-form-channel" aria-label="Filtrer par style"><option value="">Tous les styles</option>${channels.map((ch) => `<option value="${esc(ch)}">${esc(ch || "sans style")}</option>`).join("")}</select></div>
        <div class="pubf-clips" id="pub-form-clips" role="listbox" aria-label="Clips à publier"></div></div>`}
      <div class="field"><label for="pub-form-account">Compte</label>
        <select class="input" id="pub-form-account">${ready.length ? ready.map((a) => `<option value="${esc(a.id)}" data-service="${esc(a.service || "tiktok")}">${esc(pubAccountText(a))}</option>`).join("") : `<option value="">Aucun compte prêt à publier</option>`}</select>
        <span class="hint">Seuls les comptes « prêts à publier » (écran Comptes) sont proposés ; prérempli avec le compte du style.</span></div>
      <div class="field"><span class="field-label">Quand</span>
        <div class="row wrap" style="gap:16px">
          <label class="row" style="gap:6px"><input type="radio" name="pub-form-when" value="immediate" id="pub-form-now"${f.mode === "immediate" ? " checked" : ""}> Maintenant</label>
          <label class="row" style="gap:6px"><input type="radio" name="pub-form-when" value="scheduled" id="pub-form-later"${f.mode === "scheduled" ? " checked" : ""}> Programmer</label>
          <input class="input" id="pub-form-at" type="datetime-local" aria-label="Date et heure"${f.mode === "scheduled" ? "" : " hidden"} value="${esc(f.at || "")}">
          <label class="row" style="gap:6px"><input type="radio" name="pub-form-when" value="after_last" id="pub-form-after"${f.mode === "after_last" ? " checked" : ""}> Après la dernière programmation</label>
          <input class="input" id="pub-form-interval" type="number" min="1" step="1" aria-label="Intervalle (heures)"${f.mode === "after_last" ? "" : " hidden"} value="${esc(String(f.intervalH || 4))}" style="width:8ch"></div>
        <span class="hint" id="pub-form-when-hint"${f.mode === "after_last" ? " hidden" : ""}>${esc(pubWhenHint(f, "tiktok"))}</span>
        <p class="hint" id="pub-form-after-hint"${f.mode === "after_last" ? "" : " hidden"}>${f.afterAt
          ? `Programmée ${esc(pubSlotLabel(f.afterAt))} (dernière programmation du compte + ${esc(String(f.intervalH || 4))} h).`
          : "Choisis un compte et un intervalle."}</p></div>
      <div class="field"><label for="pub-form-caption">Légende</label><textarea class="input" id="pub-form-caption" rows="3">${esc(f.caption)}</textarea></div>
      <div class="field"><label for="pub-form-tags">Hashtags</label><input class="input" id="pub-form-tags" value="${esc(f.tags)}"><span class="hint">Séparés par des espaces.</span></div>
      <div class="stack" id="pub-form-tiktok" style="gap:16px">
        <div class="field"><label for="pub-form-visibility">Visibilité</label>
          <select class="input" id="pub-form-visibility">${PUB_VISIBILITIES.map(([k, l]) => `<option value="${k}"${o.visibility === k ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
          <span class="hint">Une vidéo « Toi uniquement » ne peut pas être programmée (règle de TikTok).</span></div>
        <div class="field"><span class="field-label">Autoriser</span>
          <label class="row" style="gap:6px"><input type="checkbox" id="pub-form-comments"${o.allow_comments ? " checked" : ""}> Les commentaires</label>
          <label class="row" style="gap:6px"><input type="checkbox" id="pub-form-reuse"${o.allow_reuse ? " checked" : ""}> La réutilisation du contenu (duo, collage)</label>
          <label class="row" style="gap:6px"><input type="checkbox" id="pub-form-ai"${o.ai_generated ? " checked" : ""}> Contenu généré par IA (étiquette)</label></div>
        <div class="field"><label for="pub-form-check">Vérification de contenu</label>
          <select class="input" id="pub-form-check">${PUB_CHECKS.map(([k, l]) => `<option value="${k}"${o.content_check === k ? " selected" : ""}>${esc(l)}</option>`).join("")}</select></div>
      </div>
      <div class="stack" id="pub-form-youtube" style="gap:16px" hidden>
        <div class="field"><label for="pub-form-yt-title">Titre YouTube</label>
          <input class="input" id="pub-form-yt-title" maxlength="100" value="${esc(f.ytTitle || "")}">
          <span class="hint">100 caractères au plus ; par défaut le titre d'écran du clip. La description est la légende et les hashtags ci-dessus : #Shorts y est ajouté s'il manque.</span></div>
        <div class="field"><label for="pub-form-yt-visibility">Visibilité</label>
          <select class="input" id="pub-form-yt-visibility">${PUB_YT_VISIBILITIES.map(([k, l]) => `<option value="${k}"${y.visibility === k ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
          <span class="hint">Une vidéo programmée est publiée « comme publique » : « Publique » est exigée pour programmer.</span></div>
        <div class="field"><span class="field-label">Public</span>
          <label class="row" style="gap:6px"><input type="checkbox" id="pub-form-yt-kids"${y.made_for_kids ? " checked" : ""}> Conçue pour les enfants</label>
          <span class="hint">Non par défaut (réponse obligatoire de YouTube Studio).</span></div>
      </div>
      <p class="reason bad" id="pub-form-error" hidden role="alert"></p>
    </div>
    <div class="modal-foot"><button type="button" class="btn btn-ghost" data-dismiss>Fermer</button><span class="grow"></span>
      <button type="button" class="btn btn-primary" id="pub-form-submit">${editing ? "Enregistrer" : "Valider et publier"}</button></div>`;
}

function pubFormClipCards(f, d) {
  const q = ($("#pub-form-search", d).value || "").trim().toLowerCase();
  const video = $("#pub-form-video", d).value, channel = $("#pub-form-channel", d).value;
  const list = f.clips.filter((c) => (!video || c.video_id === video) && (channel === "" ? true : (c.channel || "") === channel)
    && (!q || [c.screen_title, c.description, c.clip_id, c.video_id].some((t) => String(t || "").toLowerCase().includes(q))));
  const box = $("#pub-form-clips", d);
  box.innerHTML = list.length ? list.map((c) => `<button type="button" class="pubf-clip${pubKey(c) === f.selected ? " on" : ""}" role="option" aria-selected="${pubKey(c) === f.selected}" data-pubf-clip="${esc(pubKey(c))}">
      <span class="mini-clip">${c.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(c.thumbnail_url)}" alt="">` : ""}</span>
      <span class="pubf-clip-main"><b>${esc(pubTitle(c))}</b><span class="muted">${esc(c.video_id)} · ${c.channel ? esc(c.channel) : "sans style"}</span></span>
      <span class="num" title="Score">${c.score != null ? esc(fr(c.score)) : ""}</span></button>`).join("")
    : `<p class="muted" style="padding:8px">Aucun clip à valider ou approuvé ne correspond.</p>`;
  $$("[data-pubf-clip]", box).forEach((b) => (b.onclick = () => pubFormSelect(f, d, b.dataset.pubfClip)));
}

async function pubFormSelect(f, d, key) {
  f.selected = key;
  const c = f.clips.find((x) => pubKey(x) === key);
  if (!c) return;
  $("#pub-form-caption", d).value = c.description || "";
  $("#pub-form-tags", d).value = (c.hashtags || []).join(" ");
  $("#pub-form-yt-title", d).value = c.screen_title || "";  // titre YouTube par defaut = titre d'ecran du clip
  pubFormClipCards(f, d);
}

function pubFormBody(f, d) {
  const mode = $("input[name='pub-form-when']:checked", d).value;
  const youtube = pubFormService(d) === "youtube";
  const title = $("#pub-form-yt-title", d).value.trim();
  const options = youtube
    ? Object.assign({ visibility: $("#pub-form-yt-visibility", d).value, made_for_kids: $("#pub-form-yt-kids", d).checked }, title ? { title } : {})
    : {
      visibility: $("#pub-form-visibility", d).value, allow_comments: $("#pub-form-comments", d).checked,
      allow_reuse: $("#pub-form-reuse", d).checked, ai_generated: $("#pub-form-ai", d).checked,
      content_check: $("#pub-form-check", d).value,
    };
  const body = {
    account: $("#pub-form-account", d).value, mode: mode === "after_last" ? "scheduled" : mode,
    description: $("#pub-form-caption", d).value, hashtags: parseHashtags($("#pub-form-tags", d).value),
    options,
  };
  if (mode === "scheduled") {
    const at = $("#pub-form-at", d).value;
    body.publish_at = at ? pubParisInstant(at).toISOString() : null; // l'heure saisie est celle de Paris
  } else if (mode === "after_last") {
    body.publish_at = f.afterAt || null; // calculée côté serveur (/api/publications/after-last), affichée avant validation
  }
  return body;
}

/* « Après la dernière programmation + N h » (SPEC-1ed3) : la date est calculée côté Python, affichée avant
   validation ; les refus habituels (fenêtre, avance minimale, plafonds) se vérifient à la création, comme
   toute publication programmée. */
async function pubFormRefreshAfterLast(f, d) {
  const hint = $("#pub-form-after-hint", d);
  const account = $("#pub-form-account", d).value;
  const interval = Number($("#pub-form-interval", d).value);
  f.intervalH = interval;
  if (!account || !(interval > 0)) {
    f.afterAt = null;
    hint.textContent = "Choisis un compte et un intervalle.";
    return;
  }
  try {
    const res = await api(`/api/publications/after-last?account=${pubEnc(account)}&interval_hours=${pubEnc(interval)}`);
    f.afterAt = res.publish_at;
    hint.textContent = `Programmée ${pubSlotLabel(res.publish_at_paris)} (dernière programmation du compte + ${interval} h).`;
  } catch (err) {
    f.afterAt = null;
    hint.textContent = err.message || String(err);
  }
}

async function pubFormSubmit(f, d) {
  const err = $("#pub-form-error", d);
  err.hidden = true;
  const afterLast = $("#pub-form-after", d).checked;
  if (afterLast && !f.afterAt) { err.textContent = "Choisis un compte et un intervalle valides avant de valider."; err.hidden = false; return; }
  const body = pubFormBody(f, d);
  if (!body.account) { err.textContent = "Choisis un compte prêt à publier (écran Comptes)."; err.hidden = false; return; }
  if (!f.edit && !f.selected) { err.textContent = "Choisis un clip."; err.hidden = false; return; }
  const [video_id, clip_id] = f.edit ? [f.edit.video_id, f.edit.clip_id] : f.selected.split("/");
  if (!(await netGuard())) return;
  try {
    if (f.edit) await api(`/api/publications/${pubEnc(video_id)}/${pubEnc(clip_id)}`, jsonBody("PATCH", body));
    else await api("/api/publications", jsonBody("POST", Object.assign({ video_id, clip_id }, body)));
  } catch (e) {
    err.textContent = e.message || String(e);
    if (e.body && e.body.next_at) { // plafond depasse : la raison est dite, la prochaine heure possible se prend en un clic
      const next = e.body.next_at;
      err.innerHTML = `${esc(e.message)} <button type="button" class="btn btn-xs" id="pub-form-use-next">Programmer à ${esc(pubWhen(next))}</button>`;
      $("#pub-form-use-next", err).onclick = () => { $("#pub-form-later", d).checked = true; $("#pub-form-at", d).hidden = false; $("#pub-form-at", d).value = pubLocalInput(next); err.hidden = true; };
    }
    err.hidden = false;
    return;
  }
  closeLayer();
  toast({ kind: "ok", title: f.edit ? "Publication modifiée" : (body.mode === "immediate" ? "Publication en file : le worker la publie dès qu'il est libre" : "Publication programmée"), body: f.edit ? pubTitle(f.edit) : "", ms: 3200 });
  pubPosts.at = 0;
  pubPostsLoad();
  if (typeof loadClips === "function") loadClips();
}

/* ouvre le formulaire ; `preset` { video_id, clip_id } prerempli depuis l'ecran Clips ; `edit` : une publication a modifier */
async function pubOpenForm(preset, edit) {
  let clips, data;
  try {
    [clips, data] = await Promise.all([api("/api/clips"), api("/api/publications")]);
  } catch (err) { toastError("Impossible d'ouvrir le formulaire", err); return null; }
  const base = edit || (preset && clips.find((c) => c.video_id === preset.video_id && c.clip_id === preset.clip_id)) || null;
  const f = {
    clips: pubFormClips(clips), accounts: data.accounts, edit: edit || null, selected: base && !edit ? pubKey(base) : "",
    options: Object.assign({}, data.defaults.options, edit && edit.service !== "youtube" ? edit.post_options : {}),
    ytOptions: Object.assign({}, data.defaults.youtube.options, edit && edit.service === "youtube" ? edit.post_options : {}),
    ytTitle: edit && edit.service === "youtube" && edit.post_options && edit.post_options.title ? edit.post_options.title : (base ? (base.screen_title || "") : ""),
    ytMinMinutes: data.defaults.youtube.schedule_min_minutes, ytMaxDays: data.defaults.youtube.schedule_max_days,
    mode: edit && edit.publish_mode === "scheduled" ? "scheduled" : "immediate",
    at: edit && edit.publish_mode === "scheduled" && edit.slot_at ? pubLocalInput(edit.slot_at) : "",
    caption: base ? (base.description || "") : "", tags: base ? (base.hashtags || []).join(" ") : "",
    minMinutes: data.defaults.schedule_min_minutes, maxDays: data.defaults.schedule_max_days,
    intervalH: 4, afterAt: null,
  };
  if (preset && !edit && base && !f.clips.some((c) => pubKey(c) === pubKey(base))) {
    toastError("Ce clip ne peut pas être publié", new Error("Il n'est ni à valider ni approuvé (refusé, déjà publié ou déjà en file)."));
    return null;
  }
  return openPanel("modal pub-modal pub-form", pubFormHtml(f), (d) => {
    if (!edit) {
      ["#pub-form-search"].forEach((s) => ($(s, d).oninput = () => pubFormClipCards(f, d)));
      ["#pub-form-video", "#pub-form-channel"].forEach((s) => ($(s, d).onchange = () => pubFormClipCards(f, d)));
      pubFormClipCards(f, d);
      if (f.selected) pubFormSelect(f, d, f.selected);
    }
    if (edit && edit.account) $("#pub-form-account", d).value = edit.account;
    $("#pub-form-account", d).onchange = () => { pubFormShowService(f, d); if ($("#pub-form-after", d).checked) pubFormRefreshAfterLast(f, d); };
    pubFormShowService(f, d);
    $$("input[name='pub-form-when']", d).forEach((r) => (r.onchange = () => {
      const afterLast = $("#pub-form-after", d).checked;
      $("#pub-form-at", d).hidden = !$("#pub-form-later", d).checked;
      $("#pub-form-interval", d).hidden = !afterLast;
      $("#pub-form-after-hint", d).hidden = !afterLast;
      $("#pub-form-when-hint", d).hidden = afterLast;
      if (afterLast) pubFormRefreshAfterLast(f, d);
    }));
    let afterTimer = null;
    $("#pub-form-interval", d).addEventListener("input", () => {
      clearTimeout(afterTimer);
      afterTimer = setTimeout(() => pubFormRefreshAfterLast(f, d), 400);
    });
    $("#pub-form-submit", d).onclick = () => pubFormSubmit(f, d);
  });
}

/* ---------- Formulaire « Programmer une série » (TASK-5bbf, SPEC-1ed3, SPEC-6076 R3/R6) : une
   publication toutes les X heures, N clips choisis automatiquement (meilleur score) ou a la main. Tout
   le calcul (dates, choix des clips, refus) vient de l'API ; ce fichier n'affiche que sa reponse. */

const PUBS_WHEN_HINT = "Une partie d'un clip découpé est toujours prise avec les autres, à la suite et dans l'ordre : si la série ne tient pas dans les places restantes, elle est sautée en entier (jamais coupée).";

/* Heure pleine de Paris suivante, puis +1h (reglage par defaut du debut de la serie). `nowIso` : pour les tests,
   sinon l'instant present. Rendu directement au format d'un champ datetime-local (heure murale de Paris). */
function pubSeriesDefaultStart(nowIso) {
  const parisLocal = pubLocalInput(nowIso || new Date().toISOString());
  const [ymd, hm] = parisLocal.split("T");
  const [y, m, d] = ymd.split("-").map(Number);
  const [h] = hm.split(":").map(Number);
  const rolled = new Date(Date.UTC(y, m - 1, d, h, 0));
  rolled.setUTCHours(rolled.getUTCHours() + 2); // heure pleine suivante (+1h), puis +1h de marge
  return rolled.toISOString().slice(0, 16);
}

const pubSeriesStyles = (units) => Array.from(new Set(units.map((u) => u.channel || ""))).sort();
const pubSeriesUnitKey = (u) => `${u.video_id}/${u.clip_ids.join(",")}`;
const pubSeriesUnitLabel = (u) => {
  const title = u.screen_title || u.title || u.clip_ids[0];
  return u.parts_total > 1 ? `${title} (${u.parts_total} parties)` : title;
};
const pubSeriesPostCount = (units) => units.reduce((n, u) => n + (u.clip_ids ? u.clip_ids.length : 1), 0);

/* Coche / decoche une unite entiere (toutes ses parties ensemble) : cocher une partie ajoute la serie
   complete, la decocher la retire entierement. L'ordre d'ajout est l'ordre de publication. */
function pubSeriesToggleUnit(selected, unit) {
  const key = pubSeriesUnitKey(unit);
  if (selected.some((u) => pubSeriesUnitKey(u) === key)) return selected.filter((u) => pubSeriesUnitKey(u) !== key);
  return [...selected, unit];
}

/* Deplace l'unite a `index` de `delta` positions (reordonnancement manuel « monter » / « descendre ») ;
   les parties d'une serie restent soudees puisqu'elles forment une seule entree. Sans effet en bord de liste. */
function pubSeriesMoveUnit(selected, index, delta) {
  const to = index + delta;
  if (to < 0 || to >= selected.length) return selected;
  const copy = selected.slice();
  [copy[index], copy[to]] = [copy[to], copy[index]];
  return copy;
}

function pubSeriesPoolCards(f, d) {
  const pool = f.units.filter((u) => !f.style || u.channel === f.style);
  const box = $("#pubs-pool", d);
  if (!box) return;
  const picked = new Set(f.selected.map(pubSeriesUnitKey));
  box.innerHTML = pool.length ? pool.map((u) => {
    const key = pubSeriesUnitKey(u), on = picked.has(key);
    return `<button type="button" class="pubf-clip${on ? " on" : ""}" role="option" aria-selected="${on}" data-pubs-pick="${esc(key)}">
      <span class="mini-clip">${u.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(u.thumbnail_url)}" alt="">` : ""}</span>
      <span class="pubf-clip-main"><b>${esc(pubSeriesUnitLabel(u))}</b><span class="muted">${esc(u.video_id)} · ${u.channel ? esc(u.channel) : "sans style"}</span></span>
      ${u.validated ? `<span class="chip ok">Validé</span>` : ""}
      <span class="num" title="Score">${u.score != null ? esc(fr(u.score)) : ""}</span></button>`;
  }).join("") : `<p class="muted" style="padding:8px">Aucun clip disponible pour ce style.</p>`;
  $$("[data-pubs-pick]", box).forEach((b) => (b.onclick = () => {
    const unit = pool.find((u) => pubSeriesUnitKey(u) === b.dataset.pubsPick);
    if (!unit) return;
    f.selected = pubSeriesToggleUnit(f.selected, unit);
    pubSeriesRenderManual(f, d);
  }));
}

/* Coche « Heure par clip » (TASK-fa00f90a735a) : chaque clip choisi (chaque partie d'un clip decoupe) a sa
   propre date. Le JS ne fait que PRE-REMPLIR avec le rythme actuel (debut + k x intervalle, ou a la suite de la
   derniere programmation) ; une date retouchee a la main (`edited`) n'est plus jamais recalculee. La validation
   (fenetre, avance, plafonds, creneau pris, meme heure, ordre des parties) reste cote Python. */
const pubSeriesClipKey = (videoId, clipId) => `${videoId}/${clipId}`;

function pubSeriesRhythmStart(f, d) {
  if ($("#pubs-after-last", d) && $("#pubs-after-last", d).checked) return f.afterAt ? new Date(f.afterAt) : null;
  const startLocal = $("#pubs-start", d) ? $("#pubs-start", d).value : "";
  return startLocal ? pubParisInstant(startLocal) : null;
}

/* Valeur datetime-local (heure de Paris) du k-ieme post au rythme actuel, ou "" si le rythme est incomplet. */
function pubSeriesPrefill(start, intervalH, k) {
  if (!start || !(intervalH > 0)) return "";
  return pubLocalInput(new Date(start.getTime() + k * intervalH * 3600000).toISOString());
}

/* Les clips a dater, dans l'ordre de publication, avec la date a afficher (saisie ou pre-remplie). */
function pubSeriesClipRows(f, d) {
  const start = pubSeriesRhythmStart(f, d);
  const intervalH = Number($("#pubs-interval", d) ? $("#pubs-interval", d).value : f.intervalH);
  const rows = [];
  f.selected.forEach((u) => u.clip_ids.forEach((clipId) => {
    const key = pubSeriesClipKey(u.video_id, clipId);
    const saved = f.clipDates[key];
    const value = saved && saved.edited ? saved.value : pubSeriesPrefill(start, intervalH, rows.length);
    if (!saved || !saved.edited) f.clipDates[key] = { value, edited: false };
    rows.push({ unit: u, video_id: u.video_id, clip_id: clipId, key, value });
  }));
  return rows;
}

/* Refus de l'apercu affiches sous le clip concerne, sans re-rendre les champs (le focus de saisie reste). */
function pubSeriesShowRefusals(f, d) {
  const byKey = {};
  if (f.preview) f.preview.items.forEach((it) => { byKey[pubSeriesClipKey(it.video_id, it.clip_id)] = it.refusal; });
  $$("[data-pubs-refusal]", d).forEach((el) => {
    const refusal = byKey[el.dataset.pubsRefusal];
    el.textContent = refusal || "";
    el.hidden = !refusal;
  });
}

function pubSeriesClipDateHtml(row, multi) {
  const partLabel = multi ? `Partie ${row.clip_id}` : "Date et heure";
  return `<div class="field" style="margin:6px 0 0"><label class="muted" style="font-size:12px">${esc(partLabel)} (heure de Paris)
      <input class="input" type="datetime-local" data-pubs-date="${esc(row.key)}" value="${esc(row.value)}"></label>
      <div class="li-sub bad" data-pubs-refusal="${esc(row.key)}" role="alert" hidden></div></div>`;
}

function pubSeriesSelectedRows(f, d) {
  const box = $("#pubs-selected", d);
  if (!box) return;
  const last = f.selected.length - 1;
  const clipRows = f.perClip ? pubSeriesClipRows(f, d) : [];
  box.innerHTML = f.selected.length ? f.selected.map((u, i) => `<div class="list-item">
      <span class="mini-clip">${u.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(u.thumbnail_url)}" alt="">` : ""}</span>
      <div class="li-main grow"><div class="li-title">${esc(pubSeriesUnitLabel(u))}</div><div class="li-sub muted">${esc(u.video_id)}</div>
        ${clipRows.filter((r) => r.unit === u).map((r) => pubSeriesClipDateHtml(r, u.clip_ids.length > 1)).join("")}</div>
      <div class="row" style="gap:4px">
        <button type="button" class="btn btn-xs" data-pubs-up="${i}" aria-label="Monter"${i === 0 ? " disabled" : ""}>↑</button>
        <button type="button" class="btn btn-xs" data-pubs-down="${i}" aria-label="Descendre"${i === last ? " disabled" : ""}>↓</button>
        <button type="button" class="btn btn-xs btn-ghost" data-pubs-remove="${i}" aria-label="Retirer">${icon("x", "i-xs")}</button>
      </div></div>`).join("") : `<p class="muted" style="padding:8px">Aucun clip sélectionné.</p>`;
  $$("[data-pubs-up]", box).forEach((b) => (b.onclick = () => { f.selected = pubSeriesMoveUnit(f.selected, Number(b.dataset.pubsUp), -1); pubSeriesRenderManual(f, d); }));
  $$("[data-pubs-down]", box).forEach((b) => (b.onclick = () => { f.selected = pubSeriesMoveUnit(f.selected, Number(b.dataset.pubsDown), 1); pubSeriesRenderManual(f, d); }));
  $$("[data-pubs-remove]", box).forEach((b) => (b.onclick = () => { f.selected = f.selected.filter((_, idx) => idx !== Number(b.dataset.pubsRemove)); pubSeriesRenderManual(f, d); }));
  $$("[data-pubs-date]", box).forEach((input) => (input.oninput = () => {
    f.clipDates[input.dataset.pubsDate] = { value: input.value, edited: true };
    if (f.onChange) f.onChange();
  }));
  pubSeriesShowRefusals(f, d);
}

function pubSeriesRenderManual(f, d) {
  pubSeriesPoolCards(f, d);
  pubSeriesSelectedRows(f, d);
  const count = $("#pubs-sel-count", d);
  if (count) count.textContent = String(pubSeriesPostCount(f.selected));
  f.preview = null;
  pubSeriesRenderPreview(f, d);
}

/* Texte sous la coche « À la suite de la dernière programmation » (TASK-8c4818a974fa) : la date vient de
   publish.after_last_schedule (reutilise /api/publications/after-last, deja servi pour « Nouvelle
   publication », TASK-5c00), jamais recalculee en JS ; `f.afterAtParis` est la chaine deja a l'heure de
   Paris renvoyee par l'API (``publish_at_paris``). */
function pubSeriesAfterLastHint(f) {
  if (!f.afterAt) return "Choisis un compte et un intervalle.";
  return `Programmée à partir de ${pubSlotLabel(f.afterAtParis || f.afterAt)} (dernière programmation du compte + ${f.intervalH} h).`;
}

function pubSeriesFormHtml(f) {
  const ready = f.accounts.filter((a) => a.ready_to_publish);
  const styles = pubSeriesStyles(f.units);
  const count = f.mode === "auto" ? (f.count || 0) : pubSeriesPostCount(f.selected);
  const available = f.available != null ? f.available : null;
  return `
    <div class="modal-head"><h2>Programmer une série</h2>
      <p class="muted" style="margin-top:4px">Publie plusieurs clips à un rythme régulier (une publication toutes les X heures), après un aperçu.</p></div>
    <div class="modal-body stack" style="gap:16px">
      <div class="field"><span class="field-label">Mode</span>
        <div class="row wrap" style="gap:16px">
          <label class="row" style="gap:6px"><input type="radio" name="pubs-mode" value="auto"${f.mode === "auto" ? " checked" : ""}> Automatique (les meilleurs clips)</label>
          <label class="row" style="gap:6px"><input type="radio" name="pubs-mode" value="manual"${f.mode === "manual" ? " checked" : ""}> Manuel (je choisis)</label></div></div>
      <div class="field"><label class="row" style="gap:6px"><input type="checkbox" id="pubs-together"${f.together !== false ? " checked" : ""}> Parties ensemble</label>
        <span class="hint">Les parties d'un clip découpé partent toujours groupées et dans l'ordre. Décoche pour les traiter comme des publications indépendantes.</span></div>
      <div class="field"><label for="pubs-style">Style</label>
        <select class="input" id="pubs-style"><option value="">Tous les styles</option>${styles.map((s) => `<option value="${esc(s)}"${s === f.style ? " selected" : ""}>${esc(s || "sans style")}</option>`).join("")}</select></div>
      <div class="field"><label for="pubs-account">Compte</label>
        <select class="input" id="pubs-account">${ready.length ? ready.map((a) => `<option value="${esc(a.id)}"${a.id === f.account ? " selected" : ""}>${esc(pubAccountText(a))}</option>`).join("") : `<option value="">Aucun compte prêt à publier</option>`}</select>
        <span class="hint">Seuls les comptes « prêts à publier » (écran Comptes) sont proposés.</span></div>
      <div class="row wrap" style="gap:16px">
        <div class="field"><label for="pubs-start">Début (heure de Paris)</label>
          <input class="input" id="pubs-start" type="datetime-local" value="${esc(f.afterLast && f.afterAt ? pubLocalInput(f.afterAt) : f.start)}"${f.afterLast ? " disabled" : ""}></div>
        <div class="field"><label for="pubs-interval">Toutes les (heures)</label>
          <input class="input" id="pubs-interval" type="number" min="1" step="1" value="${f.intervalH}" style="width:8ch"></div>
        ${f.mode === "auto" ? `<div class="field"><label for="pubs-count">Nombre de vidéos</label>
          <input class="input" id="pubs-count" type="number" min="1" step="1" value="${f.count}" style="width:8ch"${available != null ? ` max="${available}"` : ""}${available === 0 ? " disabled" : ""}></div>` : ""}
      </div>
      <div class="field"><label class="row" style="gap:6px"><input type="checkbox" id="pubs-after-last"${f.afterLast ? " checked" : ""}> À la suite de la dernière programmation</label>
        <span class="hint" id="pubs-after-last-hint"${f.afterLast ? "" : " hidden"}>${esc(pubSeriesAfterLastHint(f))}</span></div>
      ${f.mode === "manual" ? `
        <div class="field"><label class="row" style="gap:6px"><input type="checkbox" id="pubs-per-clip"${f.perClip ? " checked" : ""}> Heure par clip</label>
          <span class="hint">Choisis la date et l'heure de chaque clip sélectionné (pré-remplies avec le rythme ci-dessus, modifiables une par une).</span></div>
        <div class="field"><span class="field-label">Clips disponibles</span>
          <div class="pubf-clips" id="pubs-pool" role="listbox" aria-label="Clips disponibles"></div></div>
        <div class="field"><span class="field-label">Sélection (<span id="pubs-sel-count">${count}</span> vidéo(s), ordre de publication)</span>
          <div class="panel" id="pubs-selected"></div></div>`
        : `<p class="muted" style="font-size:13px" data-pubs-count-note>${count} vidéo${count > 1 ? "s" : ""} validée${count > 1 ? "s" : ""} seront choisies, par score décroissant.</p>`}
      <p class="hint">${PUBS_WHEN_HINT}</p>
      <p class="reason bad" id="pubs-error" hidden role="alert"></p>
      <div id="pubs-preview"></div>
    </div>
    <div class="modal-foot"><button type="button" class="btn btn-ghost" data-dismiss>Fermer</button><span class="grow"></span>
      <button type="button" class="btn" id="pubs-check">Aperçu</button>
      <button type="button" class="btn btn-primary" id="pubs-submit" disabled>Valider</button></div>`;
}

function pubSeriesBody(f, d) {
  const mode = $("input[name='pubs-mode']:checked", d).value;
  const style = $("#pubs-style", d).value;
  const account = $("#pubs-account", d).value;
  const interval_hours = Number($("#pubs-interval", d).value);
  const afterLast = $("#pubs-after-last", d).checked;
  const startLocal = $("#pubs-start", d).value;
  const parts_together = $("#pubs-together", d).checked;
  if (mode === "manual" && f.perClip) {
    // « Heure par clip » (TASK-fa00f90a735a) : la date de chaque clip part telle que saisie (heure de Paris),
    // l'API la valide clip par clip ; ni debut ni intervalle (seulement un pre-remplissage cote ecran).
    const rows = pubSeriesClipRows(f, d);
    return {
      mode, style: style || null, account, parts_together,
      selection: f.selected.map((u) => ({ video_id: u.video_id, clip_id: u.clip_ids[0] })),
      clip_dates: rows.map((r) => ({ video_id: r.video_id, clip_id: r.clip_id, publish_at: r.value ? pubParisInstant(r.value).toISOString() : "" })),
    };
  }
  const body = {
    mode, style: style || null, account, interval_hours, parts_together,
    // Coche cochee (TASK-8c4818a974fa) : la date vient de l'API (f.afterAt), jamais recalculee ici.
    start_at: afterLast ? (f.afterAt || "") : (startLocal ? pubParisInstant(startLocal).toISOString() : ""),
  };
  if (mode === "auto") body.count = Number($("#pubs-count", d).value);
  else body.selection = f.selected.map((u) => ({ video_id: u.video_id, clip_id: u.clip_ids[0] }));
  return body;
}

/* Borne la valeur du champ « Nombre de vidéos » au max disponible (TASK-fc561e4dc7e9) : au moins 1, au plus
   ``available`` (0 si aucun clip valide), inchangee si ``available`` est inconnu (aucun compte choisi). */
function pubSeriesClampCount(value, available) {
  if (available == null) return Number.isFinite(value) && value >= 1 ? value : 1;
  if (available <= 0) return 0;
  const v = Number.isFinite(value) && value >= 1 ? value : 1;
  return Math.min(v, available);
}

/* Message sous le champ « Nombre de vidéos » : explique pourquoi il est desactive, pourquoi il est au max,
   ou ce que l'automatique choisira sinon. */
function pubSeriesCountNote(count, available) {
  if (available === 0) return "Aucun clip validé disponible : valide d'abord des clips dans l'écran Clips.";
  if (available != null && count >= available) {
    return `Maximum disponible : ${available} vidéo${available > 1 ? "s" : ""}.`;
  }
  return `${count} vidéo${count > 1 ? "s" : ""} validée${count > 1 ? "s" : ""} seront choisies, par score décroissant.`;
}

/* Applique le max courant (``f.available``) au champ, le borne si besoin, et met a jour le message. */
function pubSeriesApplyCountMax(f, d) {
  const countEl = $("#pubs-count", d);
  if (!countEl) return;
  const clamped = pubSeriesClampCount(Number(countEl.value) || 0, f.available);
  if (String(clamped) !== countEl.value) countEl.value = clamped;
  f.count = clamped;
  if (f.available != null) countEl.max = String(f.available); else countEl.removeAttribute("max");
  countEl.disabled = f.available === 0;
  const note = $("[data-pubs-count-note]", d);
  if (note) note.textContent = pubSeriesCountNote(clamped, f.available);
}

/* Reinterroge le pool de clips (style, compte et coche « Parties ensemble » courants) : met a jour
   f.units (mode manuel) et f.available (max du mode auto pour le compte choisi, TASK-fc561e4dc7e9). */
async function pubSeriesFetchClips(f, d) {
  const style = $("#pubs-style", d).value;
  const accountEl = $("#pubs-account", d);
  const account = accountEl ? accountEl.value : "";
  const together = $("#pubs-together", d).checked;
  f.together = together;
  try {
    const q = `style=${pubEnc(style)}&together=${together}${account ? `&account=${pubEnc(account)}` : ""}`;
    const data = await api(`/api/publications/series/clips?${q}`);
    f.units = data.units;
    f.available = account ? data.available : null;
  } catch (e) {
    f.available = null;
  }
  if (f.mode === "manual") {
    f.selected = [];
    pubSeriesRenderManual(f, d);
  }
  pubSeriesApplyCountMax(f, d);
}

function pubSeriesRenderPreview(f, d) {
  const box = $("#pubs-preview", d);
  if (!box) return;
  const submit = $("#pubs-submit", d);
  if (!f.preview) { box.innerHTML = ""; if (submit) submit.disabled = true; return; }
  const p = f.preview;
  const rows = p.items.map((it) => `<div class="list-item${it.refusal ? " bad" : ""}">
      <span class="mini-clip">${it.thumbnail_url ? `<img loading="lazy" decoding="async" width="36" height="64" src="${esc(it.thumbnail_url)}" alt="">` : ""}</span>
      <div class="li-main grow"><div class="li-title">${esc(it.screen_title || it.title || it.clip_id)}</div>
        <div class="li-sub muted">${esc(pubSlotLabel(it.publish_at_paris))}</div>
        ${it.refusal ? `<div class="li-sub bad">${esc(it.refusal)}</div>` : ""}</div></div>`).join("");
  // Pourquoi « Programmer » reste grise : resume visible des dates refusees (sinon seulement en petit sous chaque ligne).
  const refused = p.items.filter((it) => it.refusal);
  const summary = refused.length
    ? `<p class="reason bad" style="margin-top:8px" data-series-refused>${refused.length} date${refused.length > 1 ? "s" : ""} refusée${refused.length > 1 ? "s" : ""} sur ${p.items.length} : ${esc(refused[0].refusal)}. ${f.mode === "manual" && f.perClip ? "Corrige la date du clip concerné." : "Change l'intervalle, la date de début ou le nombre de vidéos."}</p>`
    : "";
  box.innerHTML = `<div class="panel">${rows || `<p class="muted" style="padding:8px">Aucune publication.</p>`}</div>
    ${p.insufficient ? `<p class="reason bad" style="margin-top:8px">${esc(p.insufficient_reason)}</p>` : ""}${summary}`;
  if (submit) submit.disabled = !p.ok;
  pubSeriesShowRefusals(f, d);
}

async function pubSeriesPreview(f, d) {
  const err = $("#pubs-error", d);
  err.hidden = true;
  const body = pubSeriesBody(f, d);
  if (!body.account) { err.textContent = "Choisis un compte prêt à publier."; err.hidden = false; return; }
  if (body.mode === "manual" && !body.selection.length) { err.textContent = "Choisis au moins un clip."; err.hidden = false; return; }
  if (body.clip_dates) {
    if (body.clip_dates.some((c) => !c.publish_at)) { err.textContent = "Choisis la date et l'heure de chaque clip."; err.hidden = false; return; }
  } else if (!body.start_at) { err.textContent = "Choisis une date de début."; err.hidden = false; return; }
  try {
    f.preview = await api("/api/publications/series/preview", jsonBody("POST", body));
  } catch (e) {
    f.preview = null;
    err.textContent = e.message || String(e);
    err.hidden = false;
  }
  pubSeriesRenderPreview(f, d);
}

async function pubSeriesSubmit(f, d) {
  if (!f.preview || !f.preview.ok) return;
  const err = $("#pubs-error", d);
  err.hidden = true;
  if (!(await netGuard())) return;
  try {
    const res = await api("/api/publications/series", jsonBody("POST", pubSeriesBody(f, d)));
    closeLayer();
    toast({ kind: "ok", title: "Série programmée", body: `${res.created} publication${res.created > 1 ? "s" : ""}`, ms: 3200 });
    pubPosts.at = 0;
    pubPostsLoad();
  } catch (e) {
    err.textContent = e.message || String(e);
    err.hidden = false;
  }
}

/* Coche « À la suite de la dernière programmation » (TASK-8c4818a974fa) : reutilise l'endpoint deja servi
   pour « Nouvelle publication » (/api/publications/after-last, TASK-5c00 -> publish.after_last_schedule),
   met a jour le champ Debut (desactive) et le texte d'aide ; aucun calcul de date ici, seulement l'affichage
   de ce que l'API a renvoye. */
async function pubSeriesRefreshAfterLast(f, d) {
  const account = $("#pubs-account", d).value;
  const interval = Number($("#pubs-interval", d).value);
  f.intervalH = interval;
  const hint = $("#pubs-after-last-hint", d);
  if (!account || !(interval > 0)) {
    f.afterAt = null;
    f.afterAtParis = null;
    if (hint) hint.textContent = pubSeriesAfterLastHint(f);
    return;
  }
  try {
    const res = await api(`/api/publications/after-last?account=${pubEnc(account)}&interval_hours=${pubEnc(interval)}`);
    f.afterAt = res.publish_at;
    f.afterAtParis = res.publish_at_paris;
    const input = $("#pubs-start", d);
    if (input) input.value = pubLocalInput(res.publish_at);
    if (hint) hint.textContent = pubSeriesAfterLastHint(f);
  } catch (err) {
    f.afterAt = null;
    f.afterAtParis = null;
    if (hint) hint.textContent = err.message || String(err);
  }
}

function pubSeriesWire(f, d) {
  $$("input[name='pubs-mode']", d).forEach((r) => (r.onchange = () => { f.mode = r.value; pubSeriesRerender(f, d); }));
  $("#pubs-style", d).onchange = () => { f.style = $("#pubs-style", d).value; };
  $("#pubs-check", d).onclick = () => pubSeriesPreview(f, d);
  $("#pubs-submit", d).onclick = () => pubSeriesSubmit(f, d);
  // L'apercu se relance tout seul (500 ms apres la derniere saisie) : « Valider » ne reste plus grise
  // faute d'avoir clique « Apercu », et le nombre annonce suit le champ.
  let timer = null;
  const refresh = () => {
    f.preview = null;
    pubSeriesRenderPreview(f, d);
    pubSeriesApplyCountMax(f, d);
    clearTimeout(timer);
    if (f.mode === "auto" || f.selected.length) timer = setTimeout(() => pubSeriesPreview(f, d), 500);
  };
  f.onChange = refresh;
  // Coche « Heure par clip » (TASK-fa00f90a735a), decochee par defaut, mode manuel seulement.
  const perClipEl = $("#pubs-per-clip", d);
  if (perClipEl) {
    perClipEl.onchange = () => { f.perClip = perClipEl.checked; pubSeriesSelectedRows(f, d); refresh(); };
  }
  // Decochee par defaut (TASK-8c4818a974fa) : cochee, desactive Debut et affiche la date calculee cote
  // Python ; decochee, rend le champ au comportement actuel.
  const afterLastEl = $("#pubs-after-last", d);
  if (afterLastEl) {
    afterLastEl.onchange = () => {
      f.afterLast = afterLastEl.checked;
      const input = $("#pubs-start", d), hint = $("#pubs-after-last-hint", d);
      if (input) input.disabled = f.afterLast;
      if (hint) hint.hidden = !f.afterLast;
      if (f.afterLast) pubSeriesRefreshAfterLast(f, d).then(() => { if (f.perClip) pubSeriesSelectedRows(f, d); refresh(); });
      else { if (f.perClip) pubSeriesSelectedRows(f, d); refresh(); }
    };
  }
  ["#pubs-start", "#pubs-interval", "#pubs-count"].forEach((sel) => {
    const el = $(sel, d);
    if (el) el.addEventListener("input", () => {
      // L'intervalle recalcule la date tant que la coche est active (TASK-8c4818a974fa).
      if (sel === "#pubs-interval" && f.afterLast) { pubSeriesRefreshAfterLast(f, d).then(() => { if (f.perClip) pubSeriesSelectedRows(f, d); refresh(); }); return; }
      if (f.perClip && sel !== "#pubs-count") pubSeriesSelectedRows(f, d); // pre-remplissage : suit le rythme
      refresh();
    });
  });
  // Style, compte et coche « Parties ensemble » changent la composition du pool et le max du compte
  // (TASK-fc561e4dc7e9) : re-interroge le serveur avant de relancer l'apercu. Le compte recalcule aussi
  // la date « à la suite » tant que la coche est active (TASK-8c4818a974fa).
  ["#pubs-style", "#pubs-account", "#pubs-together"].forEach((sel) => {
    const el = $(sel, d);
    if (el) el.addEventListener("change", () => pubSeriesFetchClips(f, d).then(() => {
      if (sel === "#pubs-account" && f.afterLast) { pubSeriesRefreshAfterLast(f, d).then(() => { if (f.perClip) pubSeriesSelectedRows(f, d); refresh(); }); return; }
      refresh();
    }));
  });
  pubSeriesFetchClips(f, d).then(() => {
    if (f.mode === "auto") pubSeriesPreview(f, d);
  });
}

function pubSeriesRerender(f, d) {
  d.innerHTML = pubSeriesFormHtml(f);
  pubSeriesWire(f, d);
}

/* ouvre le formulaire « Programmer une série » */
async function pubOpenSeriesForm() {
  let clipsData, data;
  try {
    [clipsData, data] = await Promise.all([api("/api/publications/series/clips"), api("/api/publications")]);
  } catch (err) { toastError("Impossible d'ouvrir le formulaire", err); return null; }
  const f = {
    mode: "auto", style: "", units: clipsData.units, accounts: data.accounts,
    start: pubSeriesDefaultStart(), intervalH: clipsData.default_interval_h || 4,
    count: 1, selected: [], preview: null, together: true, available: null,
    afterLast: false, afterAt: null, afterAtParis: null,
    perClip: false, clipDates: {}, onChange: null,
  };
  return openPanel("modal pub-modal pub-form", pubSeriesFormHtml(f), (d) => pubSeriesWire(f, d));
}

/* ---------- Actions ---------- */

async function pubMove(c, slotAt) {
  const before = c.slot_at;
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/move`, jsonBody("POST", { slot_at: slotAt }));
    pubUi.landed = pubKey(c);
    await pubLoad();
    toast({
      kind: "ok", title: `Planifié ${pubSlotLabel(slotAt)}`, body: pubTitle(c),
      undo: () => pubRestore(c, before),
    });
  } catch (err) {
    toastError("Impossible de déplacer le clip", err);
    pubUi.at = 0;
    pubLoad();
  }
}

/* Annule une action : remet le clip sur `slotAt`, ou en attente sans creneau si `slotAt` est vide.
   Un clip publie ne se deplace pas : il repasse d'abord en attente (`viaUnschedule`). */
async function pubRestore(c, slotAt, viaUnschedule) {
  try {
    if (!slotAt || viaUnschedule) await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/unschedule`, { method: "POST" });
    if (slotAt) await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/move`, jsonBody("POST", { slot_at: slotAt }));
    toast({ kind: "info", title: "Annulé", body: pubTitle(c), ms: 2600 });
  } catch (err) {
    toastError("Impossible d'annuler", err);
  }
  pubUi.at = 0;
  pubLoad();
}

async function pubMarkPublished(c) {
  const account = c.account ? pubAccountLabel(c.account) : "";
  const ok = await confirmDialog({
    title: "Déclarer ce clip comme publié ?",
    body: `À utiliser seulement si « ${pubTitle(c)} » a déjà été publié hors de Clipper${account ? ` (sur ${account})` : ""}, par exemple depuis TikTok Studio ou l'appli : Clipper ne le publiera pas et le marque publié. Pour publier depuis Clipper, utilise « Nouvelle publication ».`,
    confirmLabel: "Déclarer publié", danger: false,
  });
  if (!ok) return false;
  const slotAt = c.slot_at;
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/published`, { method: "POST" });
    await pubLoad();
    toast({
      kind: "ok", title: "Clip déclaré publié (hors Clipper)", body: pubTitle(c),
      undo: () => pubRestore(c, slotAt, true),
    });
    return true;
  } catch (err) {
    toastError("Impossible de déclarer le clip publié", err);
    return false;
  }
}

async function pubRetry(c) {
  if (!(await netGuard())) return false;
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/retry`, { method: "POST" });
    await pubLoad();
    toast({ kind: "info", title: "Publication relancée", body: pubTitle(c), ms: 2600 });
    return true;
  } catch (err) {
    toastError("Impossible de réessayer la publication", err);
    return false;
  }
}

async function pubUnschedule(c) {
  const slotAt = c.slot_at;
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/unschedule`, { method: "POST" });
    await pubLoad();
    toast({
      kind: "warn", title: "Repassé en attente", body: pubTitle(c),
      undo: () => (slotAt ? pubRestore(c, slotAt) : pubLoad()),
    });
    return true;
  } catch (err) {
    toastError("Impossible de repasser le clip en attente", err);
    return false;
  }
}

/* ---------- Fiche d'un clip ---------- */

function pubAccountField(c) {
  const accounts = (pubUi.data && pubUi.data.accounts) || [];
  const locked = c.publish_status === "published" || c.missing;
  const options = accounts.filter((a) => a.ready_to_publish || a.id === c.account)
    .map((a) => `<option value="${esc(a.id)}"${a.id === c.account ? " selected" : ""}${a.ready_to_publish ? "" : " disabled"}>${esc(pubAccountText(a))}${a.ready_to_publish ? "" : a.paused_at ? " (en pause)" : " (non prêt à publier)"}</option>`);
  if (!c.account) options.unshift(`<option value="" selected>Aucun compte</option>`);
  return `<div class="field"><label for="pub-account">Compte de publication</label>
    <select class="input" id="pub-account" data-pub-account${locked ? " disabled" : ""}>${options.join("")}</select>
    <span class="hint">Seuls les comptes « prêts à publier » (écran Comptes) peuvent être choisis.</span></div>`;
}

async function pubSetAccount(c, account) {
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/account`, jsonBody("POST", { account }));
    await pubLoad();
    toast({ kind: "ok", title: "Compte de publication changé", body: `${pubTitle(c)} : ${pubAccountLabel(account)}`, ms: 2600 });
  } catch (err) {
    toastError("Impossible de changer le compte", err);
    pubUi.at = 0;
    pubLoad();
  }
}

async function pubMarkRemoved(c) {
  const ok = await confirmDialog({
    title: "Supprimé de la plateforme ?",
    body: `À utiliser si tu as supprimé « ${pubTitle(c)} » toi-même de TikTok ou YouTube : Clipper ne supprime rien là-bas, il le note, le sort des posts programmés et des statistiques d'apprentissage et ne le republiera jamais tout seul.`,
    confirmLabel: "Supprimé de la plateforme", danger: true,
  });
  if (!ok) return false;
  try {
    await api(`/api/publish/${pubEnc(c.video_id)}/${pubEnc(c.clip_id)}/removed`, jsonBody("POST", {}));
    await pubLoad();
    pubPosts.at = 0;
    pubPostsLoad();
    toast({ kind: "warn", title: "Post déclaré supprimé de la plateforme", body: pubTitle(c), ms: 2600 });
    return true;
  } catch (err) {
    toastError("Impossible de déclarer le post supprimé", err);
    return false;
  }
}

function pubDetailHtml(c) {
  const account = c.account;
  const status = c.publish_status;
  const inProgress = c.tiktok_status === "in_progress";  // le worker pilote : pas de « Déclarer publié » (fable-publication I2)
  const hint = status === "approved" ? "Clique « Publier maintenant » (formulaire Nouvelle publication), ou glisse ce clip sur un créneau libre du calendrier pour le planifier."
    : status === "failed" && c.to_verify ? "Programmation à vérifier : le post est peut-être déjà programmé sur TikTok (risque de doublon). Contrôle TikTok Studio : s'il y est, attends le prochain relevé (rapprochement automatique) ; s'il n'y est pas, « Réessayer »."
    : status === "failed" ? "La publication s'est arrêtée : regarde la capture, règle le problème dans le navigateur du compte, puis « Réessayer » (ou repasse le clip en attente pour le replanifier)." : "";
  return `
    <div class="modal-head"><div class="row wrap" style="gap:8px">${pubChip(c)}<h2>${esc(pubTitle(c))}</h2></div>
      <p class="muted" style="margin-top:4px">${c.slot_at ? esc(pubSlotLabel(c.slot_at)) : "Sans créneau"}${account ? ` · ${esc(pubAccountLabel(account))}` : ""}</p></div>
    <div class="modal-body stack" style="gap:16px">
      ${c.publish_error ? `<p class="reason bad">Publication en échec : ${esc(c.publish_error)}</p>` : ""}
      ${c.waiting_reason ? `<p class="reason warn" data-waiting-reason>En attente, non tentée : ${esc(c.waiting_reason)}</p>` : ""}
      ${pubAccountField(c)}
      ${c.capture_url ? `<a href="${esc(c.capture_url)}" target="_blank" rel="noopener" title="Capture d'écran de l'arrêt"><img class="pub-capture" loading="lazy" src="${esc(c.capture_url)}" alt="Capture d'écran de l'arrêt" style="max-width:100%;border-radius:8px"></a>` : ""}
      ${c.post_url ? `<p>Publiée : <a href="${esc(c.post_url)}" target="_blank" rel="noopener">${esc(c.post_url)}</a></p>` : ""}
      ${c.post_note ? `<p class="muted">${esc(c.post_note)}</p>` : ""}
      ${c.postponed_reason ? `<p class="muted">Reportée : ${esc(c.postponed_reason)}</p>` : ""}
      ${hint ? `<p class="muted">${esc(hint)}</p>` : ""}
      ${status === "scheduled" && !inProgress ? `<p class="muted">« Déclarer publié » sert à un clip que tu as déjà publié toi-même, hors de Clipper : il ne sera pas publié par le worker.</p>` : ""}
      <div class="field"><span class="field-label">Description</span><div class="pub-caption">${esc(c.description || "")}</div></div>
      <div class="hashtags">${(c.hashtags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    </div>
    <div class="modal-foot">
      <a class="btn btn-ghost" href="${esc(c.video_url)}" download="${esc(c.clip_id)}.mp4">${icon("download")}Télécharger</a>
      <button type="button" class="btn btn-ghost" data-copy>${icon("copy")}Copier la description</button>
      <span class="grow"></span>
      ${status === "failed" ? `<button type="button" class="btn btn-primary" data-retry>${icon("rotate-ccw")}Réessayer</button>` : ""}
      ${status === "scheduled" || (status === "failed" && !c.to_verify) ? `<button type="button" class="btn" data-unschedule>${icon("undo-2")}Repasser en attente</button>` : ""}
      ${status === "published" ? `<button type="button" class="btn btn-ghost" data-removed title="Tu as supprimé ce post de la plateforme à la main">${icon("trash-2")}Supprimé de la plateforme</button>` : ""}
      ${status === "approved" ? `<button type="button" class="btn btn-primary" data-publish-now>${icon("send")}Publier maintenant</button>` : ""}
      ${status === "scheduled" && !inProgress ? `<button type="button" class="btn" data-published title="Pour un clip déjà publié hors de Clipper">${icon("check")}Déclarer publié (hors Clipper)</button>` : ""}
    </div>`;
}

function pubOpenDetail(key) {
  const c = pubFind(key);
  if (!c) return;
  if (c.missing) { toastError("Clip introuvable", new Error(`Le sidecar de ${c.video_id}/${c.clip_id} n'existe plus dans output/.`)); return; }
  openPanel("modal pub-modal", pubDetailHtml(c), (d) => {
    $("[data-copy]", d).onclick = () => copyText(pubCaptionText(c), "Description et hashtags");
    const acc = $("[data-pub-account]", d);
    if (acc) acc.onchange = async () => { if (acc.value && acc.value !== c.account) { closeLayer(); await pubSetAccount(c, acc.value); } };
    const retry = $("[data-retry]", d);
    if (retry) retry.onclick = async () => { closeLayer(); await pubRetry(c); };
    const un = $("[data-unschedule]", d);
    if (un) un.onclick = async () => { closeLayer(); await pubUnschedule(c); };
    const removed = $("[data-removed]", d);
    if (removed) removed.onclick = async () => { closeLayer(); await pubMarkRemoved(c); };
    const pub = $("[data-published]", d);
    if (pub) pub.onclick = async () => { closeLayer(); await pubMarkPublished(c); };
    const now = $("[data-publish-now]", d);
    if (now) now.onclick = () => { closeLayer(); setTimeout(() => pubOpenForm({ video_id: c.video_id, clip_id: c.clip_id }), 340); };
  });
}

/* ---------- Glisser-deposer (souris : HTML5 drag ; toucher : appui long + deplacement) ---------- */

function pubDropOn(cell, key) {
  const c = pubFind(key);
  if (!c || !cell || !cell.dataset.slotAt) return;
  if (cell.classList.contains("past")) { toast({ kind: "warn", title: "Créneau passé", body: "Choisis un créneau à venir." }); return; }
  if (cell.querySelector("[data-post]") && cell.querySelector("[data-post]").dataset.post !== key) {
    toast({ kind: "warn", title: "Créneau déjà pris", body: "Choisis un créneau libre ou repasse l'autre clip en attente." });
    return;
  }
  if (c.slot_at === cell.dataset.slotAt) return;
  pubMove(c, cell.dataset.slotAt);
}

const pubCells = (root) => $$(".cal-c[data-slot-at]", root);
const pubClearDrop = (root) => $$(".cal-c.drop", root).forEach((x) => x.classList.remove("drop"));
const pubDroppable = (cell) => cell && !cell.classList.contains("past");

function pubWireMouse(body) {
  $$(".post[draggable='true']", body).forEach((el) => {
    el.addEventListener("dragstart", (e) => {
      if (pubUi.touching) { e.preventDefault(); return; }
      pubUi.dragKey = el.dataset.post;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", el.dataset.post);
    });
    el.addEventListener("dragend", () => { el.classList.remove("dragging"); pubUi.dragKey = null; pubClearDrop(body); });
  });
  pubCells(body).forEach((cell) => {
    cell.addEventListener("dragover", (e) => {
      if (!pubUi.dragKey || !pubDroppable(cell)) return;
      e.preventDefault();
      pubClearDrop(body);
      cell.classList.add("drop");
    });
    cell.addEventListener("dragleave", (e) => { if (!cell.contains(e.relatedTarget)) cell.classList.remove("drop"); });
    cell.addEventListener("drop", (e) => {
      e.preventDefault();
      const key = pubUi.dragKey || e.dataTransfer.getData("text/plain");
      pubUi.dragKey = null;
      pubClearDrop(body);
      pubDropOn(cell, key);
    });
  });
}

function pubWireTouch(body) {
  $$(".post[draggable='true']", body).forEach((el) => {
    let timer = null, ghost = null, origin = null, over = null;
    const stop = () => {
      clearTimeout(timer); timer = null;
      if (ghost) ghost.remove();
      ghost = null; over = null;
      el.classList.remove("dragging");
      pubClearDrop(body);
      pubUi.dragKey = null;
      setTimeout(() => { pubUi.touching = false; }, 400);
    };
    const place = (t) => { ghost.style.left = `${t.clientX}px`; ghost.style.top = `${t.clientY}px`; };
    el.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      origin = { x: t.clientX, y: t.clientY };
      timer = setTimeout(() => {
        timer = null;
        pubUi.touching = true;
        pubUi.dragKey = el.dataset.post;
        el.classList.add("dragging");
        ghost = el.cloneNode(true);
        ghost.classList.add("post-ghost");
        document.body.appendChild(ghost);
        place(t);
        if (navigator.vibrate) navigator.vibrate(15);
      }, PUB_HOLD_MS);
    }, { passive: true });
    el.addEventListener("touchmove", (e) => {
      const t = e.touches[0];
      if (timer) { if (Math.hypot(t.clientX - origin.x, t.clientY - origin.y) > PUB_SLOP_PX) { clearTimeout(timer); timer = null; } return; }
      if (!ghost) return;
      e.preventDefault(); // le doigt deplace le clip, la page ne defile plus
      place(t);
      const hit = document.elementFromPoint(t.clientX, t.clientY);
      const cell = hit ? hit.closest(".cal-c[data-slot-at]") : null;
      over = pubDroppable(cell) ? cell : null;
      pubClearDrop(body);
      if (over) over.classList.add("drop");
    }, { passive: false });
    el.addEventListener("touchend", (e) => {
      if (!ghost) { clearTimeout(timer); timer = null; return; }
      e.preventDefault();
      const target = over, key = pubUi.dragKey;
      stop();
      if (target) pubDropOn(target, key);
    });
    el.addEventListener("touchcancel", stop);
    el.addEventListener("contextmenu", (e) => { if (ghost || pubUi.touching) e.preventDefault(); });
  });
}

function pubWire(body) {
  repWire(body);
  const fresh = $("[data-pub-new]", body);
  if (fresh) fresh.onclick = () => pubOpenForm(null);
  const series = $("[data-pub-series]", body);
  if (series) series.onclick = () => pubOpenSeriesForm();
  $$("[data-pub-row]", body).forEach((row) => {
    const p = pubPosts.data.publications.find((x) => pubKey(x) === row.dataset.pubRow);
    if (!p) return;
    const edit = $("[data-pub-edit]", row), cancel = $("[data-pub-cancel]", row), retry = $("[data-pub-retry]", row);
    if (edit) edit.onclick = () => pubOpenForm(null, p);
    if (cancel) cancel.onclick = () => pubPostCancel(p);
    if (retry) retry.onclick = async () => { await pubRetry(p); pubPosts.at = 0; pubPostsLoad(); };
  });
  $$("[data-publish-now]", body).forEach((b) => (b.onclick = (e) => {
    e.stopPropagation();
    const [video_id, clip_id] = b.dataset.publishNow.split("/");
    pubOpenForm({ video_id, clip_id });
  }));
  const sel = $("#pub-account", body);
  if (sel) sel.onchange = () => { pubUi.account = sel.value; pubUi.week = ""; pubUi.data = null; pubUi.error = null; pubUi.html = ""; renderCurrent(); };
  $$("[data-week]", body).forEach((b) => (b.onclick = () => {
    const step = Number(b.dataset.week);
    if (step === 0) pubUi.week = "";
    else if (pubUi.range === "day") pubUi.week = pubShift(pubUi.data.range_start, step);
    else if (pubUi.range === "month") pubUi.week = pubShiftMonth(pubUi.data.range_start, step);
    else pubUi.week = pubShift(pubUi.data.week_start, 7 * step);
    pubUi.data = null; pubUi.html = "";
    renderCurrent();
  }));
  // Vue Jour / Semaine / Mois (TASK-ad4d) : retenue pendant la session, on repart d'aujourd'hui dans la
  // nouvelle vue plutôt que de traduire l'ancre affichée (plus simple et plus prévisible).
  $$("[data-range]", body).forEach((b) => (b.onclick = () => {
    if (pubUi.range === b.dataset.range) return;
    pubUi.range = b.dataset.range;
    pubUi.week = "";
    pubUi.data = null; pubUi.html = "";
    renderCurrent();
  }));
  $$("[data-post]", body).forEach((el) => {
    const open = () => pubOpenDetail(el.dataset.post);
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  // Mois (TASK-39bbe20b3284) : un clic (ou Entrée / Espace) sur une case ouvre la vue Jour de ce jour.
  $$("[data-pub-day]", body).forEach((el) => {
    const open = () => {
      pubUi.range = "day";
      pubUi.week = el.dataset.pubDay;
      pubUi.data = null; pubUi.html = "";
      renderCurrent();
    };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  pubWireMouse(body);
  pubWireTouch(body);
  if (pubUi.landed) {
    const landed = $(`.cal-c [data-post="${pubUi.landed}"]`, body);
    if (landed) landed.classList.add("landed");
    pubUi.landed = null;
  }
}

Screens.publish = {
  render(body) {
    if (!pubPosts.data && !pubPosts.loading) pubPostsLoad();
    else if (Date.now() - pubPosts.at > PUB_STALE_MS) pubPostsLoad();
    if (!pubRep.busy && Date.now() - pubRep.at > PUB_STALE_MS) repLoad();
    if (pubUi.dragKey) return; // pas de rendu pendant un glisser-deposer
    if (pubUi.key !== pubWantedKey() || Date.now() - pubUi.at > PUB_STALE_MS) pubLoad();
    if (pubView(body)) pubWire(body);
  },
};
