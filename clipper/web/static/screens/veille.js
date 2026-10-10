/* Écran « Veille » (SPEC-bdd9 R10, ADR-ca9a), d'après research/maquettes/veille.html. Lit GET /api/veille (état du
   jour ou du dernier relevé, sélection, clés présentes en booléens) et GET /api/clips?archived=1 (titres et scores
   des clips de la sélection). Aucune logique réseau ni LLM ici : « Rafraîchir » dépose une demande
   (POST /api/veille/refresh), « Clipper » / « Ignorer » / « Restaurer » appellent les routes de la veille ; le
   worker fait le reste. Une donnée absente (null) s'affiche avec sa raison, jamais un 0. Charge après screens.js
   dont il remplace l'entrée Screens.veille. */
"use strict";

const VEILLE_STALE_MS = 4000;
const SOURCE_LABELS = { twitch: "Twitch", youtube: "YouTube", steam: "Steam", steam_fr: "Ventes Steam FR", igdb: "IGDB (sorties)", steam_players: "Steam (joueurs hors top)", steam_followers: "Steam (abonnés)", steam_reviews: "Steam (avis 30 j)", twitch_vods_30d: "Twitch (VOD 30 j)" };
const COUNT_LABELS = { rows: "lignes", recent: "récentes", upcoming: "à venir", skipped_rows: "lignes ignorées", games: "jeux", vods: "VOD", videos: "vidéos", private: "VOD réservées écartées", restricted: "VOD abonnés écartées", requested: "demandés", found: "trouvés", unknown: "inconnus", skipped: "coupés au plafond", rate_limited: "non relevés (limite)", unreachable: "VOD injoignables écartées", untested: "VOD non testées (jeu déjà servi)", deadline: "VOD non testées (échéance)" };
/* Le compteur « échéance » d'une source de tendance compte des jeux ou des appid, pas des VOD. */
const veilleCountLabel = (source, key) => (key === "deadline" && source !== "twitch" ? "non relevés (échéance)" : COUNT_LABELS[key] || key);

const veilleUi = { data: null, clips: [], error: null, loading: null, dirty: false, at: 0, style: {}, busy: false, html: "", sheet: null };

function loadVeille() {
  if (veilleUi.loading) { veilleUi.dirty = true; return veilleUi.loading; }
  veilleUi.loading = (async () => {
    try {
      veilleUi.data = await api("/api/veille");
      veilleUi.clips = await api("/api/clips?archived=1");
      veilleUi.error = null;
      store.veille = veilleUi.data;
    } catch (err) {
      veilleUi.error = err;
    } finally {
      veilleUi.loading = null;
      veilleUi.at = Date.now();
    }
    if (currentScreen === "veille") renderCurrent();
    updateCounts();
    if (veilleUi.dirty) { veilleUi.dirty = false; loadVeille(); }
  })();
  return veilleUi.loading;
}

// Un changement sous state/veille/ (relevé, Clipper, sélection) ou un clip qui bouge rend l'écran obsolète.
document.addEventListener("clipper:event", (e) => {
  const kind = e.detail && e.detail.kind;
  if (kind === "veille" || kind === "publish" || kind === "video") {
    veilleUi.at = 0;
    if (currentScreen === "veille" || kind === "veille") loadVeille();
  }
});

/* ---------- formats ---------- */

const veilleWhen = (iso, withDay) => {
  if (!iso) return "";
  const opts = { hour: "2-digit", minute: "2-digit" };
  if (withDay) Object.assign(opts, { weekday: "short", day: "numeric", month: "short" });
  return new Date(iso).toLocaleString("fr-FR", Object.assign({ timeZone: CLIPPER_TZ }, opts));
};
const veilleDay = (iso) => new Date(iso).toLocaleDateString("fr-FR", { timeZone: CLIPPER_TZ, day: "numeric", month: "short" });
const veilleDuration = (s) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(sec)}` : `${m}:${p(sec)}`;
};
const veilleSrcIcon = (source) => `<span class="src-ico src-${esc(source)}" title="${esc(SOURCE_LABELS[source] || source)}">${icon(source === "twitch" ? "tv" : source === "youtube" ? "play" : "users")}</span>`;

/* Δ 7 j d'un jeu : « +80 % », ou la raison de l'absence (jamais un chiffre inventé). */
function veilleDelta(game, kind) {
  if (!game) return `<span class="muted">jeu non relevé</span>`;
  if (kind === "steam" && !game.steam_match && game.steam_players_now == null) return game.steam_sellers_rank != null ? veilleSellers(game) : `<span class="muted">hors Steam</span>`;
  if (kind === "twitch" && game.twitch_match === false) return `<span class="muted">hors Twitch FR</span>`;
  const value = game[`${kind}_delta_pct`];
  if (value == null && kind === "steam" && game.steam_now_delta_pct != null) {
    const now = game.steam_now_delta_pct;
    return `<b class="${now >= 0 ? "ok" : "bad"}">${now > 0 ? "+" : ""}${esc(fr(now))} % (à l'instant)</b>`;
  }
  if (value == null && kind === "steam" && game.steam_new_in_top) return `<b class="ok">Nouveau dans le top Steam</b>`;
  if (value == null && kind === "steam" && game.steam_rank_gain != null) return `<b class="${game.steam_rank_gain >= 0 ? "ok" : "bad"}">${game.steam_rank_gain > 0 ? "+" : ""}${esc(fr(game.steam_rank_gain))} places</b>`;
  if (value == null) return `<span class="muted">pas assez d'historique (${esc(game.baseline_days_available)} j)</span>`;
  return `<b class="${value >= 0 ? "ok" : "bad"}">${value > 0 ? "+" : ""}${esc(fr(value))} %</b>`;
}

/* Un jeu monte : hausse vs 7 jours (Twitch, Steam) ≥ min %, OU nouveau dans un top Steam (mondial ou ventes FR),
   OU gain de places ≥ gainMin dans l'un de ces tops. */
const veilleRises = (game, min, gainMin = 5) => [game.twitch_delta_pct, game.steam_delta_pct].some((v) => v != null && v >= min)
  || game.steam_new_in_top === true || game.steam_sellers_new === true
  || [game.steam_rank_gain, game.steam_sellers_gain].some((v) => v != null && v >= gainMin);

/* Top des ventes Steam du pays : « #5 (+107 places) », « #3 (nouveau dans le top ventes FR) », ou la raison de l'absence. */
function veilleSellers(game) {
  if (game.steam_sellers_rank == null) return `<span class="muted">hors top ventes FR</span>`;
  const gain = game.steam_sellers_new ? `<b class="ok">Nouveau dans le top ventes FR</b>`
    : game.steam_sellers_gain != null ? `<b class="${game.steam_sellers_gain >= 0 ? "ok" : "bad"}">${game.steam_sellers_gain > 0 ? "+" : ""}${esc(fr(game.steam_sellers_gain))} places</b>` : "";
  return `<span class="mono">#${esc(game.steam_sellers_rank)}</span> ${gain}`;
}

/* Repère de sortie : « Sortie J+3 » (J0 = le jour même). Absent (null) = pas de badge. */
const veilleReleaseBadge = (days) => (days == null ? "" : `<span class="chip accent plain" data-veille-release-badge>Sortie J+${esc(days)}</span>`);

/* ---------- sections ---------- */

/* « Relevé incomplet » : seulement quand l'échéance globale a coupé quelque chose (deadline_hit écrit par la veille). */
function veilleIncomplete(data) {
  const day = data.day || {};
  if (!day.deadline_hit) return "";
  const cfg = data.settings || {};
  return `<p class="reason warn" data-veille-incomplete role="status">Relevé incomplet : échéance de ${esc(cfg.veille_deadline_s ?? "?")} s atteinte à ${esc(veilleWhen(day.deadline_at))}</p>`;
}

function veilleSources(data) {
  const day = data.day;
  const sources = day.sources || {};
  const pills = Object.keys(SOURCE_LABELS).map((name) => {
    const s = sources[name];
    if (!s) return `<span class="src-pill">${veilleSrcIcon(name)}${esc(SOURCE_LABELS[name])} : pas relevée</span>`;
    const bad = s.status === "error";
    const warn = s.status === "partial" || s.status === "skipped";
    const counts = Object.entries(s.counts || {}).map(([k, v]) => `${v} ${veilleCountLabel(name, k)}`).join(", ");
    const style = warn ? ` style="background:var(--warn-soft);color:var(--warn)"` : "";
    return `<span class="src-pill${bad ? " bad" : warn ? " warn" : ""}"${style}>${veilleSrcIcon(name)}${esc(SOURCE_LABELS[name])}${bad ? " : erreur" : warn ? " : incomplète" : ""}${!bad && counts ? ` : ${esc(counts)}` : ""}</span>`;
  }).join("");
  const named = Object.keys(SOURCE_LABELS).filter((n) => sources[n]);
  const errors = named.filter((n) => sources[n].status === "error")
    .map((n) => `<p class="reason bad" role="alert"><b>${esc(SOURCE_LABELS[n])} :</b> ${esc(sources[n].error)}</p>`).join("");
  const warnings = named.filter((n) => (sources[n].status === "partial" || sources[n].status === "skipped") && sources[n].error)
    .map((n) => `<p class="reason warn" role="status"><b>${esc(SOURCE_LABELS[n])} :</b> ${esc(sources[n].error)}</p>`).join("");
  const llm = day.llm || {};
  const llmChip = llm.status === "ok" ? `<span class="chip accent plain">Claude : ${esc(day.proposals.length)} proposition${day.proposals.length > 1 ? "s" : ""}</span>`
    : llm.status === "error" ? `<span class="chip bad plain">Claude : erreur</span>`
    : llm.status === "retry" ? `<span class="chip warn plain">Claude : choix reporté${llm.retry_at ? ` à ${esc(veilleWhen(llm.retry_at, true))}` : ""} (tentative ${esc(llm.attempts ?? "?")})</span>`
    : `<span class="chip plain">Claude : pas appelé</span>`;
  const llmError = llm.status === "error" ? `<p class="reason bad" role="alert"><b>Choix de Claude :</b> ${esc(llm.error)}</p>`
    : llm.status === "retry" ? `<p class="reason warn" role="status"><b>Choix de Claude reporté :</b> ${esc(llm.error)}</p>` : "";
  const when = data.running ? `<span class="chip running plain">Relevé en cours…</span>`
    : `<span>Relevé du <b class="mono">${esc(veilleWhen(day.finished_at || day.started_at, true))}</b></span>`;
  return `<div data-veille-sources><div class="sources">${when}${pills}${llmChip}</div>${veilleIncomplete(data)}${errors}${warnings}${llmError}</div>`;
}

/* Pied du KPI « VOD proposées » : ce qui a été écarté et pourquoi, chaque nombre tel qu'écrit par la veille. */
function veilleExcludedFoot(ex, cfg) {
  if (!ex) return "";
  const parts = [`${ex.already_known || 0} déjà connues`, `${ex.too_short || 0} trop courtes`];
  if (ex.no_community != null) parts.push(`${ex.no_community} VOD écartées : communauté insuffisante ou jeu inconnu`);
  if (ex.access_restricted != null) parts.push(`${ex.access_restricted} VOD écartées : réservées aux abonnés`);
  if (ex.access_unreachable != null) parts.push(`${ex.access_unreachable} VOD écartées : injoignables après ${cfg.twitch_access_attempts ?? "?"} essais`);
  if (ex.access_untested != null) parts.push(`${ex.access_untested} VOD non testées : jeu déjà servi (${cfg.max_vods_per_game ?? "?"} VOD accessibles par jeu)`);
  if (ex.access_deadline != null) parts.push(`${ex.access_deadline} VOD non testées : échéance`);
  return parts.join(", ");
}

function veilleKpis(data) {
  const day = data.day, cfg = data.settings || {};
  const rising = (day.games || []).filter((g) => veilleRises(g, cfg.rise_min_pct ?? 50, cfg.steam_rank_gain_min ?? 5)).length;
  const proposed = day.proposals.filter((p) => p.status !== "ignored").length;
  const sel = data.selection || { kept: [], archived: [] };
  const clipKeys = (rows) => new Set(rows.map((r) => `${r.video_id}/${r.clip_id}`)).size;
  const kept = clipKeys(sel.kept), rendered = kept + clipKeys(sel.archived);
  const kpi = (label, value, foot, cls) => `<div class="kpi${cls ? ` ${cls}` : ""}"><div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div><div class="kpi-foot">${esc(foot)}</div></div>`;
  return `<div class="kpis kpis-4" data-veille-kpi>
    ${kpi("Jeux qui montent", esc(rising), `sur ${(day.games || []).length} relevés (≥ ${cfg.rise_min_pct ?? 50} % ou ≥ ${cfg.steam_rank_gain_min ?? 5} places Steam)`, "accent")}
    ${kpi("VOD proposées", `${esc(proposed)}<small>/ ${esc(cfg.max_vods_per_day ?? "?")}</small>`, veilleExcludedFoot(day.excluded, cfg))}
    ${kpi("Clips gardés", `${esc(kept)}<small>/ ${esc(rendered)} rendus</small>`, `${cfg.best_clips_per_day ?? "?"} meilleurs par jour`)}
    ${kpi("Prochain relevé", data.next_run_at ? esc(veilleWhen(data.next_run_at, true)) : "—", data.enabled ? `à ${cfg.run_at || "?"} (${cfg.timezone || "Europe/Paris"})` : "veille désactivée")}
  </div>`;
}

/* Courbe 30 j du jeu de la proposition et résumé « s4 → s1 » (lu de summary, jamais recalculé ici). */
function veilleProposalTrend(game) {
  if (!game || !game.trend_30d) return "";
  const pick = ["steam_reviews", "twitch_vods_fr"].map((name) => [name, game.trend_30d.series[name]])
    .find(([, serie]) => serie && serie.summary && serie.summary.measured_days > 0);
  const week = (v) => (v == null ? "?" : fr(v));
  const line = pick ? `<p class="muted" data-veille-weeks>s4 → s1 : ${esc(week(pick[1].summary.weeks[0]))} → ${esc(week(pick[1].summary.weeks[3]))} ${esc(TREND_SERIES[pick[0]].unit)}</p>` : "";
  return `<div data-veille-trend>${veilleTrendChartWithLegend(game.trend_30d)}</div>${line}`;
}

/* État réel d'une proposition mise en file, lu par GET /api/veille (p.live) : jamais « en file » par défaut. */
function veilleLiveChip(live) {
  if (!live) return `<span class="chip" data-live-state="unknown">état inconnu</span>`;
  return `<span class="chip${live.state === "queued" ? " queued" : ""}" data-live-state="${esc(live.state)}">${esc(live.label)}</span>`;
}

function veilleProposal(p, game, channels) {
  const c = p.candidate;
  const queued = p.status === "queued";
  const sig = (source, label, html) => `<span class="sig">${veilleSrcIcon(source)}${esc(label)} ${html}</span>`;
  const signals = [
    sig("twitch", "Twitch FR", veilleDelta(game, "twitch")),
    sig("steam", "Steam", veilleDelta(game, "steam")),
    game && game.steam_sellers_rank != null && game.steam_match ? sig("steam", "Ventes FR", veilleSellers(game)) : "",
    c.source === "youtube" && c.views_per_hour != null ? sig("youtube", "YouTube", `<b>${esc(fr(Math.round(c.views_per_hour)))} vues/h</b>`) : "",
    (c.signals || {}).release_days_since != null ? veilleReleaseBadge(c.signals.release_days_since) : "",
  ].join("");
  const chosen = veilleUi.style[p.candidate_id] || "";
  const styleSelect = `<select class="input" data-veille-style aria-label="Style"><option value="">Sans style (config.toml)</option>${channels.map((n) => `<option value="${esc(n)}"${n === chosen ? " selected" : ""}>Style : ${esc(n)}</option>`).join("")}</select>`;
  const actions = queued
    ? `${veilleLiveChip(p.live)}<span class="note">Mise en file à ${esc(veilleWhen(p.decided_at))}${p.channel ? ` avec le style <b class="mono">${esc(p.channel)}</b>` : ""} · ne sera plus proposée.</span><span class="spacer"></span><a class="btn btn-sm btn-ghost" href="#/videos">Voir dans Vidéos</a>`
    : `<a class="btn btn-sm btn-ghost" href="${esc(c.url)}" target="_blank" rel="noopener">Voir la VOD</a><span class="spacer"></span>${styleSelect}<button class="btn btn-sm btn-primary" type="button" data-veille-clip>Clipper</button><button class="btn btn-sm btn-ghost" type="button" data-veille-ignore>Ignorer</button>`;
  const meta = [c.channel_name, c.game_name ? (c.game_source === "titre" ? `${c.game_name} (jeu déduit du titre)` : c.game_name) : "", c.published_at ? `publié le ${veilleDay(c.published_at)}` : "", c.view_count != null ? `${fr(c.view_count)} vues` : ""].filter(Boolean).map(esc).join(" · ");
  return `<article class="prop${queued ? " queued" : ""}" data-veille-prop="${esc(p.candidate_id)}">
    <div class="prop-thumb"><span class="rank">${esc(p.rank)}</span><div class="art">${c.thumbnail_url ? `<img loading="lazy" alt="" src="${esc(c.thumbnail_url)}" style="width:100%;height:100%;object-fit:cover;display:block" onerror="this.remove()">` : ""}</div><span class="dur">${esc(veilleDuration(c.duration_s))}</span></div>
    <div class="prop-main">
      <div class="prop-title">${esc(c.title)}</div>
      <div class="prop-meta">${veilleSrcIcon(c.source)}<span>${meta}</span></div>
      <div class="signals">${signals}</div>
      ${veilleProposalTrend(game)}
      <p class="reason"><b>Pourquoi :</b> ${esc(p.reason)}</p>
      <div class="prop-actions">${actions}</div>
    </div>
  </article>`;
}

/* À décider (proposed) d'abord, dans l'ordre de Claude ; les mises en file (queued) dans une section repliée ;
   une ignorée disparaît (SPEC-bdd9 R10), seul leur nombre reste affiché. */
function veilleProposalGroups(proposals) {
  const byRank = (a, b) => a.rank - b.rank;
  return {
    pending: proposals.filter((p) => p.status === "proposed").sort(byRank),
    // Mises en file : la décision la plus récente d'abord, à la minute près (une liste mise en file d'un coup reste
    // dans l'ordre de Claude) ; un relevé rejoué efface toute la liste du jour, décidées comprises : « Déjà décidées » = le relevé courant (SPEC-8a45).
    decided: proposals.filter((p) => p.status === "queued")
      .sort((a, b) => String(b.decided_at || "").slice(0, 16).localeCompare(String(a.decided_at || "").slice(0, 16)) || byRank(a, b)),
    ignored: proposals.filter((p) => p.status === "ignored").length,
  };
}

function veilleProposals(data, channels) {
  const day = data.day;
  const games = Object.fromEntries((day.games || []).map((g) => [g.key, g]));
  const { pending, decided, ignored } = veilleProposalGroups(day.proposals);
  const card = (p) => veilleProposal(p, games[p.candidate.game_key], channels);
  const empty = day.llm && day.llm.status === "error" ? "Claude n'a rien proposé : voir l'erreur ci-dessus."
    : day.llm && day.llm.status === "retry" ? "Choix de Claude en attente (limite de session) : il sera repris automatiquement."
    : decided.length ? "Plus rien à décider : les propositions du jour sont dans « Déjà décidées » ci-dessous."
    : "Aucune VOD proposée aujourd'hui.";
  const note = day.skipped_note ? `<div class="arch-row"><span class="t muted">${esc(day.skipped_note)}</span></div>` : "";
  const dropped = ignored ? `<div class="arch-row" data-veille-ignored><span class="t muted">${esc(ignored)} proposition${ignored > 1 ? "s" : ""} ignorée${ignored > 1 ? "s" : ""} aujourd'hui : elle${ignored > 1 ? "s ne seront" : " ne sera"} plus proposée${ignored > 1 ? "s" : ""}.</span></div>` : "";
  const folded = decided.length ? `<details class="archived" data-veille-decided><summary>${icon("check", "i-xs")}Déjà décidées (${esc(decided.length)})</summary>${decided.map(card).join("")}</details>` : "";
  return `<section data-veille-proposals><div class="panel">
    ${pending.length ? pending.map(card).join("") : `<div class="list-item muted">${esc(empty)}</div>`}
    ${note}${dropped}${folded}</div></section>`;
}

function veilleClipTitle(c) {
  return `${c.screen_title || c.title || c.clip_id}${c.parts_total > 1 ? ` (${c.part}/${c.parts_total})` : ""}`;
}

function veilleBest(data) {
  const sel = data.selection;
  if (!sel) return `<section data-veille-best><div class="panel"><div class="list-item muted">Aucun clip de veille terminé pour l'instant : la sélection des meilleurs clips du jour apparaît ici.</div></div></section>`;
  const byKey = Object.fromEntries(veilleUi.clips.map((c) => [`${c.video_id}/${c.clip_id}`, c]));
  const clipOf = (row) => byKey[`${row.video_id}/${row.clip_id}`] || { video_id: row.video_id, clip_id: row.clip_id };
  const keptCards = sel.kept.map((row) => {
    const c = clipOf(row);
    return `<div class="clip veille-clip"><div class="mini-clip"><img loading="lazy" alt="" src="/media/clip/${encodeURIComponent(row.video_id)}/${encodeURIComponent(row.clip_id)}/thumbnail"><span class="score">${esc(fr(row.score, 1))}</span></div>
      <div class="clip-title">${esc(veilleClipTitle(c))}</div>
      <div class="veille-clip-actions"><a class="btn btn-xs btn-ghost" href="#/clips/${encodeURIComponent(row.video_id)}">Voir</a><a class="btn btn-xs" href="#/clips/${encodeURIComponent(row.video_id)}">Approuver</a></div></div>`;
  }).join("");
  const archivedRows = sel.archived.map((row) => {
    const c = clipOf(row);
    return `<div class="arch-row" data-veille-archived="${esc(row.video_id)}/${esc(row.clip_id)}"><span class="score">${esc(fr(row.score, 1))}</span><span class="t">${esc(veilleClipTitle(c))}</span><span class="why">rang ${esc(row.rank)}</span><button class="btn btn-xs btn-ghost" type="button" data-veille-restore>${icon("undo-2", "i-xs")}Restaurer</button></div>`;
  }).join("");
  return `<section data-veille-best><div class="panel">
    <div class="panel-head"><h2>Sélection du ${esc(veilleDay(`${sel.date}T12:00:00Z`))}</h2><div class="right"><span class="chip ok plain">${sel.kept.length} gardé${sel.kept.length > 1 ? "s" : ""}</span><span class="chip plain">${sel.archived.length} archivé${sel.archived.length > 1 ? "s" : ""}</span></div></div>
    ${keptCards ? `<div class="clips-grid">${keptCards}</div>` : `<div class="list-item muted">Aucun clip gardé.</div>`}
    ${archivedRows ? `<details class="archived"><summary>${icon("check", "i-xs")}Archivés (${sel.archived.length}) : sous le meilleur du jour, masqués de l'écran Clips, jamais supprimés</summary>${archivedRows}</details>` : ""}
  </div></section>`;
}

/* « Sorties de jeux » (SPEC-df51 R17, maquette research/maquettes/calendrier-sorties.html) : tel qu'écrit dans
   days/<date>.json, aucun recalcul (l'ordre des tableaux est celui de la collecte). Les jaquettes sont chargées par le
   navigateur depuis images.igdb.com (ADR-0944) : le serveur ne les télécharge, ne les relaie ni ne les stocke. */
const IGDB_COVER_URL = "https://images.igdb.com/igdb/image/upload/t_cover_big/";
const VEILLE_DATE_LONG = { weekday: "long", day: "numeric", month: "long", year: "numeric" };
const VEILLE_FRISE_MAX = 5;
const VEILLE_PLATFORMS_CARD = 3;

const veilleDate = (iso, opts) => new Date(`${iso}T12:00:00Z`).toLocaleDateString("fr-FR", Object.assign({ timeZone: CLIPPER_TZ }, opts));
const veilleAddDays = (iso, n) => new Date(Date.parse(`${iso}T12:00:00Z`) + n * 864e5).toISOString().slice(0, 10);
const veilleSigned = (n) => `${n > 0 ? "+" : ""}${fr(n)}`;
const veilleHypes = (e) => (e.hypes != null ? `${fr(e.hypes)} hypes` : "hypes inconnues");

/* Jaquette : image IGDB, ou vignette portant le nom (cover_image_id nul ou image en erreur, aucune autre adresse). */
function veilleCover(e, extra) {
  const img = e.cover_image_id
    ? `<img loading="lazy" src="${IGDB_COVER_URL}${esc(e.cover_image_id)}.jpg" alt="Jaquette de ${esc(e.name)}" onerror="this.parentNode.classList.add('err')">` : "";
  return `<span class="cover${e.cover_image_id ? "" : " err"}">${img}<span class="ph">${esc(e.name)}</span>${extra || ""}</span>`;
}

function veillePlatforms(e, max) {
  const all = e.platforms || [];
  const shown = max ? all.slice(0, max) : all;
  const more = max && all.length > max ? `<span class="tag">+${all.length - max}</span>` : "";
  return shown.map((p) => `<span class="tag">${esc(p)}</span>`).join("") + more;
}

/* « Sortie J+3 », « Aujourd'hui » (jour même) ou « J-5 » (à venir). */
const veilleDaysLabel = (days) => (days === 0 ? "Aujourd'hui" : days < 0 ? `Sortie J+${-days}` : `J-${days}`);

/* La ligne tendance : seulement les champs non nuls de trend, chacun avec sa provenance. */
function veilleTrendParts(t) {
  if (!t) return [];
  const parts = [];
  if (t.steam_sellers_new) parts.push("nouveau dans le top ventes Steam FR");
  else if (t.steam_sellers_rank != null) parts.push(`n°${fr(t.steam_sellers_rank)} des ventes Steam FR${t.steam_sellers_gain != null ? ` (${veilleSigned(t.steam_sellers_gain)} places)` : ""}`);
  if (t.steam_new_in_top) parts.push("nouveau dans le top Steam");
  else if (t.steam_rank != null) parts.push(`n°${fr(t.steam_rank)} du top Steam${t.steam_rank_gain != null ? ` (${veilleSigned(t.steam_rank_gain)} places)` : ""}`);
  if (t.twitch_fr_viewers != null) parts.push(`${fr(t.twitch_fr_viewers)} viewers Twitch FR`);
  if (t.steam_players != null) parts.push(`${fr(t.steam_players)} joueurs Steam (pic du jour)`);
  else if (t.steam_players_now != null) parts.push(`${fr(t.steam_players_now)} joueurs Steam (à l'instant du relevé)`);
  if (t.steam_followers != null) parts.push(`${fr(t.steam_followers)} abonnés Steam${t.steam_followers_gain_7d != null ? ` (${veilleSigned(t.steam_followers_gain_7d)} en 7 j)` : ""}`);
  return parts;
}

const veilleCommunityChip = (t) => (!t ? "" : t.community && t.community.ok ? `<span class="chip ok plain">Communauté</span>` : `<span class="chip plain">Peu de monde</span>`);

/* Mini-courbe SVG en ligne (sans bibliothèque) des joueurs Steam : un point par mesure, « pic du jour » (peak) et
   « à l'instant » (now) de couleurs distinctes, reliés par type. Moins de 2 points : pas de courbe. */
function veilleSpark(history) {
  const pts = (history || []).filter((p) => p && p.players != null);
  if (!pts.length) return `<span class="muted">aucune mesure</span>`;
  if (pts.length < 2) return `<span class="muted">1 jour de mesure</span>`;
  const dates = [...new Set(pts.map((p) => p.date))].sort();
  const values = pts.map((p) => p.players);
  const lo = Math.min(...values), hi = Math.max(...values);
  const W = 96, H = 28, PAD = 4;
  const x = (d) => (dates.length === 1 ? W / 2 : PAD + (dates.indexOf(d) * (W - 2 * PAD)) / (dates.length - 1));
  const y = (v) => (hi === lo ? H / 2 : H - PAD - ((v - lo) * (H - 2 * PAD)) / (hi - lo));
  const kinds = [["peak", "pk"], ["now", "nw"]];
  const lines = kinds.map(([kind, cls]) => {
    const own = pts.filter((p) => p.kind === kind).sort((a, b) => (a.date < b.date ? -1 : 1));
    return own.length > 1 ? `<polyline class="ln ${cls}" fill="none" points="${own.map((p) => `${x(p.date).toFixed(1)},${y(p.players).toFixed(1)}`).join(" ")}"/>` : "";
  }).join("");
  const dots = pts.map((p) => `<circle class="${p.kind === "peak" ? "pk" : "nw"}" cx="${x(p.date).toFixed(1)}" cy="${y(p.players).toFixed(1)}" r="2.4"><title>${esc(p.kind === "peak" ? "pic du jour" : "à l'instant")} ${esc(p.date)} : ${esc(fr(p.players))}</title></circle>`).join("");
  return `<span class="spark-wrap" data-veille-spark><svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Joueurs Steam du ${esc(dates[0])} au ${esc(dates[dates.length - 1])}, de ${esc(fr(lo))} à ${esc(fr(hi))}">${lines}${dots}</svg><span class="spark-key"><i class="pk"></i>pic du jour <i class="nw"></i>à l'instant</span></span>`;
}

function veilleRecentCard(e) {
  const hot = e.trend ? `<span class="hot">${icon("trending-up", "i-xs")}Tendance</span>` : "";
  const trend = veilleTrendParts(e.trend);
  return `<article class="rc"><button type="button" class="pick" data-veille-open="${esc(e.igdb_id)}" aria-label="Détail de ${esc(e.name)}">${veilleCover(e, `<span class="jn">${esc(veilleDaysLabel(e.days))}</span>${hot}`)}</button>
    <div class="nm">${esc(e.name)}</div>
    <div class="meta">${veillePlatforms(e, VEILLE_PLATFORMS_CARD)}${e.portage ? `<span class="chip plain">Portage</span>` : ""}</div>
    <div class="stat">${esc(veilleHypes(e))}</div>
    ${trend.length ? `<div class="trend">${icon("trending-up", "i-xs")}<span>${esc(trend.join(" · "))}</span></div>` : ""}
    ${veilleCommunityChip(e.trend)}</article>`;
}

function veilleDayHead(iso, offset) {
  const weekday = veilleDate(iso, { weekday: "long" });
  return `<div class="dh"><span class="wd">${offset === 0 ? "Aujourd'hui" : esc(weekday)}</span><span class="dt">${esc(veilleDate(iso, { day: "numeric", month: "short" }))}<span class="jn">${offset === 0 ? esc(weekday) : `J-${offset}`}</span></span></div>`;
}

/* Les sorties d'un jour de la frise : aujourd'hui = recent à days = 0, les autres = upcoming du jour, dans l'ordre du tableau. */
const veilleDayEntries = (rel, offset) => (offset === 0 ? rel.recent.filter((e) => e.days === 0) : rel.upcoming.filter((e) => e.days === offset));

function veilleFriseColumn(rel, iso, offset) {
  const list = veilleDayEntries(rel, offset);
  const shown = list.slice(0, VEILLE_FRISE_MAX), rest = list.length - shown.length;
  const weekend = [0, 6].includes(new Date(`${iso}T12:00:00Z`).getUTCDay());
  const cls = ["day", offset === 0 ? "today" : "", list.length ? "" : "empty", weekend ? "we" : ""].filter(Boolean).join(" ");
  const cells = shown.map((e, k) => `<button type="button" class="pick${k === 0 ? " big" : ""}" data-veille-open="${esc(e.igdb_id)}" aria-label="Détail de ${esc(e.name)}">${veilleCover(e, e.trend ? `<span class="hot">${icon("trending-up", "i-xs")}</span>` : "")}<span class="t">${esc(e.name)}${k === 0 ? `<br><span class="mono hy">${esc(veilleHypes(e))}</span>` : ""}</span></button>`).join("");
  return `<div class="${cls}">${veilleDayHead(iso, offset)}${list.length
    ? `<div class="dstack">${cells}${rest > 0 ? `<span class="cal-more">+${rest} autre${rest > 1 ? "s" : ""}</span>` : ""}</div>`
    : `<span class="none">Aucune sortie notable</span>`}</div>`;
}

/* La même chose pour le téléphone : seulement les jours avec sortie, et « N jours sans sortie notable » entre deux. */
function veilleMobileList(rel, today, span) {
  let html = "", gap = 0, started = false;
  for (let offset = 0; offset <= span; offset += 1) {
    const list = veilleDayEntries(rel, offset);
    if (!list.length) { gap += 1; continue; }
    if (started && gap) html += `<div class="mgap">${gap} jour${gap > 1 ? "s" : ""} sans sortie notable</div>`;
    gap = 0;
    started = true;
    const iso = veilleAddDays(today, offset);
    const rows = list.slice(0, VEILLE_FRISE_MAX).map((e, k) => `<button type="button" class="mrow${k === 0 ? " bigr" : ""}" data-veille-open="${esc(e.igdb_id)}">${veilleCover(e)}<span><span class="nm${k ? " small" : ""}">${esc(e.name)}</span><span class="sub2">${veillePlatforms(e, VEILLE_PLATFORMS_CARD)}${e.trend ? `<span class="chip info plain">Tendance</span>` : ""}</span></span><span class="mono muted hyp">${esc(veilleHypes(e))}</span></button>`).join("");
    html += `<div class="mday${offset === 0 ? " today" : ""}">${veilleDayHead(iso, offset)}${rows}${list.length > VEILLE_FRISE_MAX ? `<div class="mgap more-gap">+${list.length - VEILLE_FRISE_MAX} autres</div>` : ""}</div>`;
  }
  return html;
}

/* Panneau détail d'une sortie : jaquette, nom, date, plateformes, hypes, tendance, portage, lien IGDB. */
function veilleSheet(rel, id) {
  if (id == null) return "";
  const e = [...rel.recent, ...rel.upcoming].find((x) => String(x.igdb_id) === String(id));
  if (!e) return "";
  const trend = veilleTrendParts(e.trend);
  const history = e.trend && e.trend.steam_players_history;
  const rows = [
    `<dt>Date</dt><dd>${esc(veilleDate(e.date, VEILLE_DATE_LONG))} <span class="chip${e.days <= 0 ? " ok" : ""} plain">${esc(veilleDaysLabel(e.days))}</span></dd>`,
    `<dt>Plateformes</dt><dd class="plats">${veillePlatforms(e) || `<span class="muted">plateformes inconnues</span>`}</dd>`,
    `<dt>Hypes</dt><dd>${e.hypes != null ? esc(fr(e.hypes)) : `<span class="muted">inconnues</span>`}</dd>`,
    e.trend ? `<dt>Tendance</dt><dd class="trend-dd">${trend.length ? esc(trend.join(" · ")) : `<span class="muted">aucun chiffre relevé</span>`} ${veilleCommunityChip(e.trend)}${history && history.length ? `<div>${veilleSpark(history)}</div>` : ""}</dd>` : "",
    e.portage ? `<dt>Type</dt><dd>Portage sur nouvelle plateforme</dd>` : "",
  ].join("");
  const link = e.url ? `<a class="btn" href="${esc(e.url)}" target="_blank" rel="noopener">Voir sur IGDB</a>` : "";
  return `<div class="scrim open" data-veille-scrim role="dialog" aria-modal="true" aria-labelledby="veille-sheet-name"><div class="sheet">
    <button type="button" class="x" aria-label="Fermer" data-veille-close>${icon("x")}</button>${veilleCover(e)}
    <div><h3 id="veille-sheet-name">${esc(e.name)}</h3><dl>${rows}</dl><div class="acts">${link}</div></div></div></div>`;
}

function veilleReleases(data) {
  const day = data.day, igdb = (day.sources || {}).igdb, cfg = data.settings || {};
  const head = `<div class="section-title">${icon("calendar")}Sorties de jeux</div>`;
  if (igdb && igdb.status === "error") {
    return `<section data-veille-releases>${head}<div class="panel"><p class="reason bad" role="alert"><b>${esc(SOURCE_LABELS.igdb)} :</b> ${esc(igdb.error)}</p></div></section>`;
  }
  const rel = day.releases || { recent: [], upcoming: [], excluded_low_hypes: 0, truncated: { recent: 0, upcoming: 0 } };
  const span = Number(cfg.upcoming_days) || 14;
  const more = (n) => (n > 0 ? `<div class="more-line muted">+${esc(n)} autres</div>` : "");
  const excluded = rel.excluded_low_hypes > 0 ? `<p class="cal-note">${esc(rel.excluded_low_hypes)} sortie${rel.excluded_low_hypes > 1 ? "s" : ""} écartée${rel.excluded_low_hypes > 1 ? "s" : ""} (moins de ${esc(cfg.igdb_min_hypes ?? 0)} hypes)</p>` : "";
  const chips = `<span class="chip plain">${rel.recent.length} récentes</span><span class="chip plain">${rel.upcoming.length} à venir</span><span class="chip info plain">Source : IGDB</span>`;
  const title = `<div class="panel-head"><h2>Calendrier du ${esc(veilleDate(day.date, VEILLE_DATE_LONG))}</h2><div class="right">${chips}</div></div>`;
  if (!rel.recent.length && !rel.upcoming.length) {
    return `<section data-veille-releases>${head}<div class="panel">${title}<div class="panel-body"><div class="list-item muted">Aucune sortie dans la fenêtre</div>${excluded}</div></div></section>`;
  }
  const recent = rel.recent.length ? `<div class="recent">${rel.recent.map(veilleRecentCard).join("")}</div>` : `<div class="list-item muted">Aucune sortie récente</div>`;
  const columns = Array.from({ length: span + 1 }, (_, offset) => veilleFriseColumn(rel, veilleAddDays(day.date, offset), offset)).join("");
  return `<section data-veille-releases>${head}<div class="panel">${title}<div class="panel-body">
    <div class="cal-sub"><b>Sorties récentes</b>les plus attendues d'abord</div>${recent}${more(rel.truncated.recent)}
    <div class="cal-sub cal-sub-next"><b>À venir (${esc(span)} j)</b>une colonne par jour, la plus attendue en grand</div>
    <div class="cal-frise">${columns}</div><div class="mlist">${veilleMobileList(rel, day.date, span)}</div>${more(rel.truncated.upcoming)}
    <div class="legend"><span><i class="lg-today"></i>Aujourd'hui</span><span><i class="lg-trend"></i>En tendance dans le relevé du jour</span></div>
    ${excluded}<p class="cal-note">Dates en Europe/Paris. Jaquettes chargées par ton navigateur depuis images.igdb.com.</p></div></div>${veilleSheet(rel, veilleUi.sheet)}</section>`;
}

/* Cellule Steam de « Ce qui monte » : pic du jour, à défaut instantané nommé, puis la courbe construite par la veille. */
function veilleSteamCell(g) {
  let value;
  if (g.steam_players != null) value = esc(fr(g.steam_players));
  else if (g.steam_players_now != null) value = `${esc(fr(g.steam_players_now))} à l'instant`;
  else if (g.steam_match) value = `<span class="muted">pas relevé</span>`;
  else value = g.steam_sellers_rank != null ? `<span class="muted">ventes FR #${esc(g.steam_sellers_rank)}</span>` : `<span class="muted">hors Steam</span>`;
  const curve = g.steam_players_history && (g.steam_match || g.steam_players_now != null || g.steam_players_history.length) ? `<div>${veilleSpark(g.steam_players_history)}</div>` : "";
  return `${value}${curve}`;
}

const COMMUNITY_LABELS = { steam: "joueurs Steam", followers: "abonnés Steam", twitch: "viewers Twitch FR", hypes: "hypes IGDB" };
function veilleCommunityCell(g) {
  if (!g.community) return `<span class="muted">non relevée</span>`;
  if (!g.community.ok) return `<span class="muted">insuffisante</span>`;
  return `<b class="ok">ok</b> <span class="muted">(${esc((g.community.met || []).map((m) => COMMUNITY_LABELS[m] || m).join(", "))})</span>`;
}

function veilleFollowersCell(g) {
  const gain = g.steam_followers_gain_7d != null ? `${veilleSigned(g.steam_followers_gain_7d)} (7 j)` : "historique insuffisant";
  return `${g.steam_followers != null ? esc(fr(g.steam_followers)) : `<span class="muted">inconnu</span>`}<div class="muted">${esc(gain)}</div>`;
}

/* Courbes 30 j (SPEC-85a0 R27) : une ligne SVG en ligne, sans bibliothèque, par série disponible. Chaque série a sa
   propre échelle (unités différentes). Un point par jour mesuré ; deux jours consécutifs sont reliés, un jour sans
   mesure fait un trou (jamais zéro ni interpolé). Résumés, pics et nombres de jours viennent du serveur (summary). */
const TREND_SERIES = {
  steam_reviews: { label: "avis Steam", unit: "avis Steam/jour", field: "value", color: "var(--accent)" },
  twitch_vods_fr: { label: "VOD Twitch FR", unit: "VOD Twitch FR/jour", field: "vods", color: "var(--info)" },
  twitch_viewers_fr: { label: "viewers Twitch FR", unit: "viewers Twitch FR", field: "value", color: "var(--ok)" },
};
const VEILLE_DAY_MS = 864e5;

function veilleTrendChart(trend) {
  const W = 160, H = 44, PAD = 4;
  const since = Date.parse(`${trend.since}T12:00:00Z`);
  const dayIndex = (date) => Math.round((Date.parse(`${date}T12:00:00Z`) - since) / VEILLE_DAY_MS);
  const x = (date) => PAD + (dayIndex(date) * (W - 2 * PAD)) / Math.max(1, trend.days - 1);
  const groups = Object.entries(TREND_SERIES).map(([name, meta]) => {
    const serie = trend.series[name];
    const pts = serie && serie.status !== "unavailable" ? serie.points || [] : [];
    if (pts.length < 2) return "";
    const values = pts.map((p) => p[meta.field]);
    const lo = Math.min(...values), hi = Math.max(...values);
    const y = (v) => (hi === lo ? H / 2 : H - PAD - ((v - lo) * (H - 2 * PAD)) / (hi - lo));
    const at = (p) => `${x(p.date).toFixed(1)},${y(p[meta.field]).toFixed(1)}`;
    const runs = [];
    pts.forEach((p, i) => {
      if (i && dayIndex(p.date) - dayIndex(pts[i - 1].date) === 1) runs[runs.length - 1].push(p);
      else runs.push([p]);
    });
    const lines = runs.filter((run) => run.length > 1)
      .map((run) => `<polyline fill="none" stroke-width="1.5" stroke="${meta.color}" style="stroke:${meta.color}" points="${run.map(at).join(" ")}"/>`).join("");
    const dots = pts.map((p) => `<circle r="1.8" fill="${meta.color}" style="fill:${meta.color}" cx="${x(p.date).toFixed(1)}" cy="${y(p[meta.field]).toFixed(1)}"><title>${esc(meta.label)} ${esc(p.date)} : ${esc(fr(p[meta.field]))}</title></circle>`).join("");
    return `<g data-series="${name}">${lines}${dots}</g>`;
  }).join("");
  return groups ? `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Courbes sur ${esc(trend.days)} jours depuis le ${esc(trend.since)}">${groups}</svg>` : "";
}

/* La légende : une ligne par série, sa couleur, ses jours mesurés (« n j mesurés / 30 »), son pic ; la raison quand elle est indisponible. */
function veilleTrendLegend(trend) {
  return Object.entries(TREND_SERIES).map(([name, meta]) => {
    const serie = trend.series[name];
    const swatch = `<i style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;background:${meta.color}"></i>`;
    if (!serie) return "";
    if (serie.status === "unavailable") return `<div class="muted">${swatch}${esc(meta.label)} : indisponible (${esc(serie.reason)})</div>`;
    const sum = serie.summary;
    if (!sum || sum.measured_days < 2) return `<div class="muted">${swatch}${esc(meta.label)} : ${sum && sum.measured_days === 1 ? "1 jour de mesure" : "aucune mesure"}</div>`;
    const note = serie.status === "partial" && serie.reason ? ` <span title="${esc(serie.reason)}">(${esc(serie.reason)})</span>` : "";
    return `<div class="muted">${swatch}${esc(meta.label)} : ${esc(sum.measured_days)} j mesurés / ${esc(sum.window_days)}${sum.peak ? ` · pic ${esc(sum.peak.date)}` : ""}${note}</div>`;
  }).join("");
}

const veilleTrendChartWithLegend = (trend) => `${veilleTrendChart(trend)}<div class="trend-legend">${veilleTrendLegend(trend)}</div>`;

function veilleTrend(game) {
  if (!game.trend_30d) return `<span class="muted">jeu non suivi</span>`;
  return veilleTrendChartWithLegend(game.trend_30d);
}

function veilleRising(data) {
  const games = [...(data.day.games || [])].sort((a, b) => Math.max(b.twitch_delta_pct ?? -1e9, b.steam_delta_pct ?? -1e9) - Math.max(a.twitch_delta_pct ?? -1e9, a.steam_delta_pct ?? -1e9));
  const num = (v, reason) => (v == null ? `<span class="muted">${esc(reason)}</span>` : esc(fr(v)));
  const rows = games.map((g) => `<tr>
    <td>${esc(g.name)} ${g.release ? veilleReleaseBadge(g.release.days_since) : ""}</td>
    <td class="r">${g.twitch_match === false ? `<span class="muted">hors Twitch FR</span>` : num(g.twitch_fr_viewers, "pas relevé")}</td><td class="r">${veilleDelta(g, "twitch")}</td>
    <td class="r">${veilleSteamCell(g)}</td><td class="r">${veilleDelta(g, "steam")}</td><td class="r">${veilleFollowersCell(g)}</td><td class="r">${veilleSellers(g)}</td>
    <td class="r">${veilleCommunityCell(g)}</td>
    <td class="r">${num(g.youtube_views_per_hour == null ? null : Math.round(g.youtube_views_per_hour), "clé absente ou pas de vidéo")}</td>
    <td class="r">${esc(g.vod_count)}</td><td class="r" data-veille-trend>${veilleTrend(g)}</td></tr>`).join("");
  return `<section data-veille-rising><div class="section-title">${icon("trending-up")}Ce qui monte</div><div class="panel">
    ${rows ? `<div class="table-wrap"><table class="table"><thead><tr><th>Jeu</th><th class="r">Twitch FR (viewers)</th><th class="r">Δ 7 j</th><th class="r">Steam (pic du jour / à l'instant)</th><th class="r">Δ 7 j</th><th class="r">Abonnés Steam</th><th class="r">Ventes FR</th><th class="r">Communauté</th><th class="r">YouTube FR (vues/h)</th><th class="r">VOD FR</th><th class="r">30 j</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="list-item muted">Aucun jeu relevé : vérifie les sources ci-dessus.</div>`}
    <div class="arch-row"><span class="t muted">Un jeu sans correspondance Steam ou hors du top ventes FR l'indique ; une donnée absente est expliquée, aucune valeur n'est inventée.</span></div>
  </div></section>`;
}

function veilleSettings(data) {
  const cfg = data.settings || {};
  const field = (label, value, key) => `<div class="field"${key ? ` data-veille-setting="${key}"` : ""}><label>${esc(label)}</label><input class="input" value="${esc(value)}" readonly></div>`;
  const key = (label, set, source) => `<div class="key">${veilleSrcIcon(source)}<span>${esc(label)}</span><span class="chip ${set ? "ok" : "bad"} plain">${set ? "saisie" : "absente"}</span></div>`;
  return `<section data-veille-settings><div class="panel panel-pad stack">
    <div class="form-grid">
      <div class="field full"><label>Mes goûts (texte libre, lu par Claude)</label><textarea class="input" rows="2" readonly>${esc(cfg.taste || "")}</textarea></div>
      ${field("VOD proposées par jour (max)", cfg.max_vods_per_day)}${field("Meilleurs clips gardés par jour", cfg.best_clips_per_day)}
      ${field(`Heure du relevé (${cfg.timezone || "Europe/Paris"})`, cfg.run_at)}${field("Langue des streams / région", `${cfg.language} / ${cfg.region}`)}
      ${field("Sorties à venir (jours)", cfg.upcoming_days, "upcoming_days")}${field("Fenêtre de sortie (jours)", cfg.release_window_days, "release_window_days")}${field("Hypes IGDB minimum", cfg.igdb_min_hypes, "igdb_min_hypes")}
      ${field("Sorties récentes affichées (max)", cfg.igdb_recent_max, "igdb_recent_max")}${field("Sorties à venir affichées (max)", cfg.igdb_upcoming_max, "igdb_upcoming_max")}
      ${field("Joueurs Steam hors top : appels (max)", cfg.steam_players_lookups_max, "steam_players_lookups_max")}${field("Abonnés Steam : appels (max)", cfg.steam_followers_lookups_max, "steam_followers_lookups_max")}${field("Abonnés Steam : pause entre appels (s)", cfg.steam_followers_pause_s, "steam_followers_pause_s")}
      ${field("Communauté : joueurs Steam min.", cfg.community_min_steam_players, "community_min_steam_players")}${field("Communauté : abonnés Steam min.", cfg.community_min_steam_followers, "community_min_steam_followers")}${field("Communauté : viewers Twitch FR min.", cfg.community_min_twitch_viewers, "community_min_twitch_viewers")}${field("Communauté : hypes IGDB min.", cfg.community_min_hypes, "community_min_hypes")}
      ${field("VOD par jeu (max)", cfg.max_vods_per_game, "max_vods_per_game")}
      ${field("Courbe de tendance (jours)", cfg.trend_days, "trend_days")}${field("Jeux suivis (max)", cfg.trend_games_max, "trend_games_max")}${field("Échéance du relevé (s)", cfg.veille_deadline_s, "veille_deadline_s")}
      ${field("Test d'accès : essais par VOD", cfg.twitch_access_attempts, "twitch_access_attempts")}${field("Test d'accès : pause entre essais (s)", cfg.twitch_access_retry_pause_s, "twitch_access_retry_pause_s")}
      ${field("Avis Steam : pause entre appels (s)", cfg.steam_reviews_pause_s, "steam_reviews_pause_s")}${field("VOD Twitch : pages d'historique (max)", cfg.twitch_history_pages_max, "twitch_history_pages_max")}
    </div>
    <div class="keys">${key("Twitch (client id + secret)", data.twitch_client_id_set && data.twitch_client_secret_set, "twitch")}${key("YouTube (clé API)", data.youtube_api_key_set, "youtube")}
      <div class="key">${veilleSrcIcon("steam")}<span>Steam</span><span class="chip ok plain">sans clé</span></div></div>
    <a class="btn btn-sm" href="#set-veille">Modifier dans Réglages › Veille</a>
  </div></section>`;
}

function veilleView(body) {
  const data = veilleUi.data;
  if (!data.enabled) {
    veilleUi.html = "";
    body.innerHTML = emptyState("trending-up", "Veille désactivée", "La veille propose chaque jour des VOD à clipper d'après ce qui monte sur Twitch, YouTube et Steam. Active-la et saisis tes clés dans Réglages › Veille.", `<a class="btn btn-primary" href="#set-veille">Ouvrir Réglages › Veille</a>`);
    return;
  }
  const refresh = `<button type="button" class="btn" data-veille-refresh${data.running || veilleUi.busy ? " disabled" : ""}${data.day && data.day.llm && data.day.llm.status === "retry" ? ` title="Rejoue tout le relevé (collecte réseau comprise), pas seulement le choix de Claude"` : ""}>${icon("rotate-ccw", "i-xs")}${data.running ? "Relevé en cours…" : "Rafraîchir"}</button>`;
  if (!data.day) {
    body.innerHTML = `<div class="toolbar"><span class="grow"></span>${refresh}</div>${emptyState("trending-up", "Aucun relevé pour l'instant", `Le premier relevé aura lieu ${data.next_run_at ? `le ${veilleWhen(data.next_run_at, true)}` : "bientôt"} ; « Rafraîchir » le lance maintenant.`)}`;
    return;
  }
  const channels = store.channels || [];
  const html = `<div class="stack">
    <div class="toolbar"><span class="muted">Relevé quotidien à ${esc((data.settings || {}).run_at || "?")} · les VOD déjà clippées ne sont plus proposées.</span><span class="grow"></span>${refresh}</div>
    ${veilleSources(data)}${veilleKpis(data)}${veilleProposals(data, channels)}${veilleReleases(data)}${veilleBest(data)}${veilleRising(data)}${veilleSettings(data)}
  </div>`;
  if (veilleUi.html === html && body.childElementCount) return;
  veilleUi.html = html;
  body.innerHTML = html;
}

/* ---------- actions ---------- */

async function veilleCall(path, method, payload, okTitle, errTitle) {
  try {
    const result = await api(path, payload === undefined ? { method } : jsonBody(method, payload));
    if (okTitle) toast({ kind: "ok", title: okTitle });
    return result;
  } catch (err) {
    toastError(errTitle, err);
    return null;
  } finally {
    veilleUi.at = 0;
    await loadVeille();
  }
}

function veilleWire(body) {
  const dayDate = () => veilleUi.data.day.date;
  const cand = (el) => el.closest("[data-veille-prop]").dataset.veilleProp;
  $$("[data-veille-style]", body).forEach((s) => (s.onchange = () => { veilleUi.style[cand(s)] = s.value; }));
  $$("[data-veille-refresh]", body).forEach((b) => (b.onclick = async () => {
    veilleUi.busy = true;
    b.disabled = true;
    await veilleCall("/api/veille/refresh", "POST", undefined, "Relevé demandé : le worker le lance", "Impossible de rafraîchir la veille");
    veilleUi.busy = false;
    renderCurrent();
  }));
  $$("[data-veille-clip]", body).forEach((b) => (b.onclick = async () => {
    const id = cand(b);
    b.disabled = true;
    const channel = veilleUi.style[id] || null;
    const entry = await veilleCall(`/api/veille/${encodeURIComponent(dayDate())}/${encodeURIComponent(id)}/clip`, "POST", { channel, short_clips: null }, "VOD mise en file", "Impossible de clipper cette VOD");
    if (entry) { try { await loadQueue(); } catch (err) { toastError("File indisponible", err); } }
  }));
  $$("[data-veille-ignore]", body).forEach((b) => (b.onclick = async () => {
    const id = cand(b);
    if (!(await confirmDialog({ title: "Ignorer cette VOD ?", body: "Elle ne sera plus jamais proposée.", confirmLabel: "Ignorer" }))) return;
    await veilleCall(`/api/veille/${encodeURIComponent(dayDate())}/${encodeURIComponent(id)}/ignore`, "POST", undefined, null, "Impossible d'ignorer cette VOD");
  }));
  $$("[data-veille-open]", body).forEach((b) => (b.onclick = () => { veilleUi.sheet = b.dataset.veilleOpen; veilleUi.focusSheet = true; renderCurrent(); }));
  $$("[data-veille-close]", body).forEach((b) => (b.onclick = veilleCloseSheet));
  const scrim = body.querySelector("[data-veille-scrim]");
  if (scrim) {
    scrim.onclick = (e) => { if (e.target === scrim) veilleCloseSheet(); };
    if (veilleUi.focusSheet) { veilleUi.focusSheet = false; const x = scrim.querySelector("[data-veille-close]"); if (x) x.focus(); }
  }
  $$("[data-veille-restore]", body).forEach((b) => (b.onclick = async () => {
    const [video, clip] = b.closest("[data-veille-archived]").dataset.veilleArchived.split("/");
    b.disabled = true;
    await veilleCall(`/api/veille/clips/${encodeURIComponent(video)}/${encodeURIComponent(clip)}/restore`, "POST", undefined, "Clip restauré", "Impossible de restaurer le clip");
  }));
}

/* Ferme le panneau détail et rend le focus à la jaquette qui l'avait ouvert. */
function veilleCloseSheet() {
  const opened = veilleUi.sheet;
  veilleUi.sheet = null;
  renderCurrent();
  const opener = opened == null ? null : document.querySelector(`[data-veille-open="${opened}"]`);
  if (opener) opener.focus();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && veilleUi.sheet != null && currentScreen === "veille") veilleCloseSheet();
});

Screens.veille = {
  render(body) {
    if (Date.now() - veilleUi.at > VEILLE_STALE_MS) loadVeille();
    if (!veilleUi.data) {
      body.innerHTML = veilleUi.error
        ? emptyState("circle-alert", "Chargement impossible", String(veilleUi.error.message || veilleUi.error))
        : `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>`;
      return;
    }
    veilleView(body);
    veilleWire(body);
  },
};
