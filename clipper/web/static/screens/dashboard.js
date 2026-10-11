/* Ecran « Tableau de bord » v3 (SPEC-7715 E1, T2, T4, T9 ; ADR-a308). Lit GET /api/dashboard et se remet a jour
   sur chaque evenement SSE (clipper:event, emis par app.js) ; charge apres screens.js dont il remplace l'entree
   Screens.dashboard. La decision d'abord : « A faire » (echecs regroupes par cause, reprises, VOD a confirmer,
   clips a valider, posts a 0 vue), puis ce qui tourne, la file, les dernieres videos terminees ; a droite les
   prochaines publications, le cout par usage et l'espace disque.
   Une donnee introuvable arrive du serveur en null avec une cle <champ>_error : elle est affichee telle quelle
   (role="alert"), jamais remplacee par un 0 muet. Tout rendu passe par morph() (rapprochement du DOM). */
"use strict";

const DASH_STALE_MS = 4000; // repli polling : app.js re-rend toutes les 5 s sans evenement SSE
const DASH_DISK_STALE_MS = 60000; // /api/disk parcourt workspace/ et output/ : au plus une fois par minute
const DASH_RECENT = 6; // dernieres videos terminees montrees dans la table
const DASH_USAGES = 6; // usages du modele de langage detailles dans le rail, le reste est cumule
const dash = { loading: null, dirty: false, at: 0, error: null, groups: [], disk: null, diskError: null, diskAt: 0, diskLoading: null };

function loadDashboard() {
  if (dash.loading) { dash.dirty = true; return dash.loading; }
  dash.loading = (async () => {
    try {
      store.dashboard = await api("/api/dashboard");
      dash.error = null;
    } catch (err) {
      dash.error = err;
    } finally {
      dash.loading = null;
      dash.at = Date.now();
    }
    if (currentScreen === "dashboard") renderCurrent();
    if (dash.dirty) { dash.dirty = false; loadDashboard(); }
  })();
  return dash.loading;
}

function loadDisk() {
  if (dash.diskLoading) return dash.diskLoading;
  dash.diskAt = Date.now(); // meme en erreur : pas de nouvelle tentative avant la prochaine minute
  dash.diskLoading = (async () => {
    try {
      dash.disk = await api("/api/disk");
      dash.diskError = null;
    } catch (err) {
      dash.diskError = err;
    } finally {
      dash.diskLoading = null;
    }
    if (currentScreen === "dashboard") renderCurrent();
  })();
  return dash.diskLoading;
}

// Un evenement (video, queue, publish, watch, worker) rend les donnees obsoletes.
document.addEventListener("clipper:event", () => {
  dash.at = 0;
  if (currentScreen === "dashboard") loadDashboard();
});

/* Battement du worker : seule la tuile du voyant est relue et rapprochee, le reste de l'ecran n'est pas touche. */
document.addEventListener("clipper:worker", async () => {
  if (currentScreen !== "dashboard" || !store.dashboard) return;
  try {
    const part = await api("/api/dashboard/worker");
    Object.assign(store.dashboard, part);
    const tile = $('[data-section="worker"]', $("#screen-dashboard"));
    if (tile) morph(tile, dashWorkerTile(store.dashboard));
  } catch (err) {
    // voyant inchange : la prochaine mise a jour complete (ou le prochain battement) le corrigera.
  }
});

const dashDate = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : fmtParis(iso, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
};

const dashMoney = (usd) => `${fr(usd, 2)} $`;

const dashPlural = (n, one, many) => `${fr(n)} ${n > 1 ? many : one}`;

/* Octets -> « 375 Go » (unites francaises, base 1024 comme l'explorateur de fichiers). */
function dashBytes(bytes) {
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let value = Number(bytes);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${fr(value, unit >= 3 && value < 100 ? 1 : 0)} ${units[unit]}`;
}

/* Duree totale de calcul d'une video terminee : somme des etapes mesurees ; null si aucune n'a de duree. */
function dashCompute(video) {
  const seconds = Object.values(video.durations || {}).filter((s) => s != null);
  if (!seconds.length) return null;
  const total = seconds.reduce((t, s) => t + s, 0);
  if (total >= 3600) return `${Math.floor(total / 3600)} h ${String(Math.round((total % 3600) / 60)).padStart(2, "0")}`;
  if (total >= 90) return `${fr(total / 60, 0)} min`;
  return `${fr(total, 0)} s`;
}

/* ---------- Briques ---------- */

/* Message d'erreur d'un champ (<champ>_error), en place. */
function dashError(error) {
  return error ? `<p class="d3-error" role="alert">${icon("circle-alert", "i-sm")}<span>${esc(error)}</span></p>` : "";
}

const dashChip = (kind, text) => `<span class="d3-chip ${kind}">${esc(text)}</span>`;

/* Etat vide : une phrase qui dit pourquoi c'est vide et quoi faire. */
function dashEmpty(iconName, title, text, actionHtml) {
  return `<div class="d3-empty">${icon(iconName)}<h3>${esc(title)}</h3><p>${esc(text)}</p>${actionHtml || ""}</div>`;
}

function dashSection(key, title, content, count, right) {
  return `<section class="d3-section" data-section="${key}">
    <div class="d3-section-head"><h2>${esc(title)}</h2>${count != null ? `<span class="d3-count">${esc(count)}</span>` : ""}${right ? `<div class="d3-right">${right}</div>` : ""}</div>
    ${content}</section>`;
}

const dashCard = (content, extra) => `<div class="d3-card"${extra || ""}>${content}</div>`;

/* Contenu d'une liste : erreur serveur, sinon etat vide, sinon les lignes. */
function dashList(data, field, emptyText, rowFn) {
  if (data[field] === null || data[field] === undefined) return dashError(data[`${field}_error`] || `${field} : donnée absente de la réponse.`);
  return data[field].length ? data[field].map(rowFn).join("") : `<p class="d3-inline-empty">${esc(emptyText)}</p>`;
}

/* ---------- Echecs regroupes par cause (T9, principe 5 de DIRECTION.md) ---------- */

/* Famille d'une raison d'echec : quota du modele (statut 429), annulation a la main, sinon la raison elle-meme. */
function dashCauseKind(reason) {
  const text = String(reason || "");
  if (/\b(?:statut|status|HTTP)\s*429\b/i.test(text)) return "quota";
  if (/annul/i.test(text)) return "cancel";
  return "other";
}

function dashGroupText(group) {
  const n = group.videos.length;
  const plural = (one, many) => (n > 1 ? many : one);
  if (group.kind === "quota") return { title: `${fr(n)} ${plural("vidéo bloquée", "vidéos bloquées")} par le quota Claude`, cause: group.reason };
  if (group.kind === "cancel") return { title: `${fr(n)} ${plural("vidéo annulée", "vidéos annulées")} par l'utilisateur`, cause: group.reason };
  return { title: `${fr(n)} ${plural("vidéo en échec", "vidéos en échec")}`, cause: group.reason || "Aucune raison journalisée." };
}

/* Regroupe les videos en echec par cause commune, dans l'ordre d'apparition de leur premiere video : tous les 429
   ensemble, toutes les annulations ensemble, une raison inconnue ne se melange qu'avec la meme raison. */
function dashGroups(failed) {
  const groups = [];
  for (const video of failed) {
    const kind = dashCauseKind(video.reason);
    const reason = String(video.reason || "").trim();
    let group = groups.find((g) => g.kind === kind && (kind !== "other" || g.reason === reason));
    if (!group) { group = { kind, reason, videos: [] }; groups.push(group); }
    group.videos.push(video);
  }
  return groups.map((group) => Object.assign(group, dashGroupText(group)));
}

/* Relance groupee (T4) : une seule confirmation qui nomme le nombre, puis une relance par video depuis sa propre
   etape. Une panne n'arrete pas les autres et est rapportee par video, jamais avalee. */
async function dashRetryGroup(videos) {
  const n = videos.length;
  const ok = await confirmDialog({
    title: `Relancer ces ${fr(n)} vidéos ?`,
    body: "Chacune repart de l'étape où elle a échoué, dans l'ordre de la file. Les clips déjà rendus de ces vidéos seront remplacés.",
    confirmLabel: `Relancer les ${fr(n)}`, danger: false,
  });
  if (!ok) return;
  const done = [];
  const failed = [];
  for (const video of videos) {
    if (!video.step || !STEP_LABELS[video.step]) {
      failed.push(`${video.video_id} : étape inconnue, ouvre sa fiche et choisis l'étape à relancer.`);
      continue;
    }
    try {
      await api(`/api/videos/${encodeURIComponent(video.video_id)}/retry`, jsonBody("POST", { from_step: video.step }));
      done.push(video.video_id);
    } catch (err) {
      failed.push(`${video.video_id} : ${err && err.message ? err.message : err}`);
    }
  }
  if (done.length) toast({ kind: "ok", title: `${dashPlural(done.length, "relance mise", "relances mises")} en file`, body: "Le worker les reprend une par une." });
  if (failed.length) toastError(`${dashPlural(failed.length, "relance impossible", "relances impossibles")}`, new Error(failed.join("\n")));
  for (const id of done) await afterVideoAction(id);
}

/* ---------- Lignes ---------- */

function dashRunningRow(video) {
  const progress = video.progress;
  const pct = progress ? Math.round(progress.fraction * 100) : null;
  const eta = progress && progress.eta_s != null ? ` · reste ${fr(Math.ceil(progress.eta_s / 60))} min` : "";
  return `<div class="d3-row" data-video="${esc(video.video_id)}">
    ${videoThumb(video.video_id, video.platform_thumbnail)}
    <div class="d3-grow">
      <div class="d3-title">${esc(video.title || video.video_id)}</div>
      <div class="d3-sub">${video.channel ? `<span class="d3-tag">${esc(video.channel)}</span>` : ""}<span class="mono">${esc(video.source_url || "")}</span></div>
      <div class="d3-prog"><div class="d3-step"><b>${esc(STEP_LABELS[video.step] || video.step || "étape inconnue")}</b>${pct != null ? `<span class="num">${pct} %${eta}</span>` : ""}${progress && progress.message ? `<span class="muted">${esc(progress.message)}</span>` : ""}</div>
        ${progressBar(pct)}</div>
    </div>
    <button type="button" class="btn" data-variant="ghost" data-size="sm" data-cancel="${esc(video.video_id)}">Annuler le traitement</button>
  </div>`;
}

/* Une video en echec (dans la carte de son groupe) ou en attente de reprise (retry_at) : titre vers sa fiche,
   Relancer depuis son etape, Retirer. Les gestionnaires sont ceux d'app.js (wireActions). */
function dashProblemRow(video, kind) {
  const step = (STEP_LABELS[video.step] || video.step || "").toLowerCase();
  const id = esc(video.video_id);
  const retry = video.retry_at ? `<div class="d3-sub"><span>Reprise prévue le ${esc(dashDate(video.retry_at))}</span></div>` : "";
  const reason = kind === "queued" ? `<p class="d3-reason">${esc(video.reason || "Aucune raison journalisée.")}</p>` : "";
  return `<div class="d3-row" data-video="${id}">
    ${videoThumb(video.video_id, video.platform_thumbnail)}
    <div class="d3-grow"><a class="d3-title" href="#/videos/${encodeURIComponent(video.video_id)}">${esc(video.title || video.video_id)}</a>
      <div class="d3-sub">${video.title && video.title !== video.video_id ? `<span class="mono">${id}</span>` : ""}${video.channel ? `<span class="d3-tag">${esc(video.channel)}</span>` : ""}${step ? `<span>${kind === "queued" ? "en attente à" : "bloquée à"} <b>${esc(step)}</b></span>` : ""}</div>
      ${reason}${retry}</div>
    <button type="button" class="btn" data-variant="outline" data-size="sm" data-retry-video="${id}" data-from-step="${esc(video.step || "")}">Relancer${step ? ` depuis ${esc(step)}` : ""}</button>
    <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-dismiss-video="${id}" aria-label="Retirer des échecs" title="Retirer des échecs">${icon("x", "i-sm")}</button>
  </div>`;
}

function dashGroupCard(group, index) {
  return dashCard(`<header class="d3-card-head"><div class="d3-grow"><h3>${esc(group.title)}</h3><p class="d3-cause">${esc(group.cause)}</p></div>
      <button type="button" class="btn" data-size="sm" data-retry-group="${index}">${icon("rotate-ccw", "i-sm")}${group.videos.length > 1 ? `Relancer les ${fr(group.videos.length)}` : "Relancer"}</button></header>
    <div class="d3-rows">${group.videos.map((video) => dashProblemRow(video, "failed")).join("")}</div>`, ` data-group="${esc(group.kind)}"`);
}

/* VOD a confirmer : les gestionnaires Confirmer / Ignorer sont ceux de watch.js (wireWatch). */
function dashVodRow(vod) {
  const minutes = vod.duration_s != null ? `<span>${fr(Math.round(vod.duration_s / 60))} min</span>` : "";
  return `<div class="d3-row" data-vod="${esc(vod.video_id)}" data-channel="${esc(vod.channel)}">
    <div class="d3-grow"><div class="d3-title">${esc(vod.title || vod.video_id)}</div>
      <div class="d3-sub"><span class="d3-tag">${esc(vod.channel)}</span>${minutes}<span>repérée le ${esc(dashDate(vod.found_at))}</span></div></div>
    <a class="btn" data-variant="ghost" data-size="sm" href="${esc(vod.url)}" target="_blank" rel="noopener">Voir la VOD</a>
    <button type="button" class="btn" data-variant="ghost" data-size="sm" data-vod-ignore>Ignorer</button>
    <button type="button" class="btn" data-size="sm" data-vod-confirm>Confirmer</button>
  </div>`;
}

/* Posts sans vue depuis zero_view_alert_hours (TASK-974e) : compte en alerte d'un bloc, pas de relevé à part. */
function dashZeroViewRow(account, post) {
  const clip = post.video_id ? `${esc(post.video_id)} · clip ${esc(post.clip_id)}` : "post non relié à un clip";
  return `<div class="d3-row" data-post="${esc(post.post_id)}">
    ${dashChip("bad", "0 vue")}
    <div class="d3-grow"><div class="d3-title">Post sans vue sur ${esc(account)}</div>
      <div class="d3-sub"><span class="mono">${esc(post.post_id)}</span><span>${clip}</span><span>publié le ${esc(dashDate(post.posted_at))}</span><span>${esc(post.views)} vue(s) au relevé du ${esc(dashDate(post.read_at))}</span></div></div>
  </div>`;
}

function dashZeroViewsRows(data) {
  const { hours, accounts, no_reading: missing } = data.zero_views;
  const rows = accounts.map((group) => {
    const head = group.level === "account"
      ? `<p class="d3-error" role="alert">${icon("circle-alert", "i-sm")}<span>Le compte ${esc(group.account)} ne diffuse peut-être plus : ${group.posts.length} posts à 0 vue depuis ${esc(hours)} h.</span></p>`
      : "";
    return head + group.posts.map((post) => dashZeroViewRow(group.account, post)).join("");
  }).join("");
  const silent = missing.map((item) => `<div class="d3-row"><div class="d3-grow muted">Pas de relevé : ${esc(item.account)} · <span class="mono">${esc(item.post_id)}</span> · clip ${esc(item.clip_id)}</div></div>`).join("");
  return rows + silent;
}

function dashPublicationRow(entry) {
  return `<a class="d3-row" href="#/publish" data-clip="${esc(entry.clip_id)}">
    <div class="d3-when num">${esc(dashDate(entry.slot_at))}</div>
    <div class="d3-grow"><div class="d3-title">${esc(entry.screen_title || entry.clip_id)}</div>
      <div class="d3-sub"><span class="d3-tag">${esc(entry.channel)}</span><span class="mono">${esc(entry.video_id)}</span><span>clip ${esc(entry.clip_id)}</span></div></div>
  </a>`;
}

/* File d'attente : passer en tete et retirer sont ceux d'app.js (wireActions). */
function dashQueueRow(entry, index) {
  return `<div class="d3-row" data-queue="${esc(entry.video_id)}">
    <span class="d3-when num">${index + 1}</span>${videoThumb(entry.video_id, entry.platform_thumbnail)}
    <div class="d3-grow"><div class="d3-title mono">${esc(entry.video_id)}</div>
      <div class="d3-sub"><span>${esc(entry.action === "render" ? "rendu seul" : "traitement complet")}</span>${entry.channel ? `<span class="d3-tag">${esc(entry.channel)}</span>` : ""}</div></div>
    ${index > 0 ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-front="${esc(entry.video_id)}">Passer en tête</button>` : ""}
    <button type="button" class="btn" data-variant="ghost" data-size="sm" data-remove="${esc(entry.video_id)}">Retirer</button>
  </div>`;
}

/* ---------- Bandeau de chiffres ---------- */

function dashKpiTile(iconName, label, value, foot, extra) {
  return `<div class="d3-kpi"${extra || ""}><div class="d3-kpi-label">${icon(iconName)}${esc(label)}</div><div class="d3-kpi-value num">${value}</div>${foot || ""}</div>`;
}

/* Valeur d'un chiffre ; donnee introuvable : « — » ET la cause juste dessous (jamais un 0 muet). */
function dashKpiValue(data, field, compute) {
  if (data[field] === null || data[field] === undefined) {
    return { value: "—", foot: dashError(data[`${field}_error`] || `${field} : donnée absente de la réponse.`) };
  }
  return { value: esc(compute(data[field])), foot: "" };
}

/* Voyant du worker et materiel dans une seule tuile : « worker actif » quand le battement est recent, sinon
   « worker arrêté » ; la commande pour le lancer est dans « A faire » (une video mise en file attend sans lui). */
function dashWorkerTile(data) {
  const label = `<div class="d3-kpi-label">${icon("cpu")}Worker · GPU</div>`;
  const worker = data.worker;
  const hw = data.hardware;
  let machine = "";
  if (hw === null || hw === undefined) machine = dashError(data.hardware_error || "hardware : donnée absente de la réponse.");
  else {
    const device = hw.device === "cpu" ? "CPU" : String(hw.device).toUpperCase();
    const vram = hw.vram_used_mb != null ? ` · ${fr(hw.vram_used_mb)} Mo de VRAM utilisés` : "";
    machine = `<div class="d3-kpi-foot">${esc(device)}${esc(vram)}</div>${hw.vram_used_mb == null ? dashError(hw.vram_used_mb_error) : ""}`;
  }
  if (worker === null || worker === undefined) return label + dashError(data.worker_error || "worker : donnée absente de la réponse.") + machine;
  const state = worker.state === "active" ? dashChip("ok", "worker actif")
    : dashChip("bad", worker.state === "orphan" ? "worker orphelin" : "worker arrêté");
  const beat = worker.state === "active" ? `<div class="d3-kpi-foot">pid ${esc(worker.pid)} · dernier battement il y a ${esc(fr(Math.round(worker.age_s)))} s</div>` : "";
  return `${label}<div class="d3-kpi-state">${state}</div>${beat}${machine}`;
}

function dashKpis(data) {
  const waiting = data.queue === null || data.queue === undefined ? null : data.queue.filter((e) => e.status === "waiting").length;
  const blocked = data.failed === null || data.failed === undefined || data.queued === null || data.queued === undefined ? null : data.failed.length + data.queued.length;
  const running = dashKpiValue(data, "running", (list) => fr(list.length));
  const queue = waiting === null ? dashKpiValue(data, "queue", fr) : { value: esc(fr(waiting)), foot: "" };
  const block = blocked === null ? dashKpiValue(data, data.failed === null || data.failed === undefined ? "failed" : "queued", fr) : { value: esc(fr(blocked)), foot: "" };
  const clips = dashKpiValue(data, "clips_to_review", fr);
  const cost = dashKpiValue(data, "llm_cost", (c) => dashMoney(c.today));
  const costFoot = data.llm_cost ? `<div class="d3-kpi-foot">${esc(dashMoney(data.llm_cost.week))} sur 7 jours</div>` : cost.foot;
  return `<div class="d3-kpis">
    ${dashKpiTile("loader", "En cours", running.value, running.foot)}
    ${dashKpiTile("list-filter", "En file", queue.value, queue.foot)}
    <button type="button" class="d3-kpi d3-kpi-link warn" data-scroll="d3-todo"><div class="d3-kpi-label">${icon("circle-alert")}À débloquer</div><div class="d3-kpi-value num">${block.value}</div>${block.foot}</button>
    <a class="d3-kpi d3-kpi-link accent" href="#/clips"><div class="d3-kpi-label">${icon("clapperboard")}Clips à valider</div><div class="d3-kpi-value num">${clips.value}</div>${clips.foot}</a>
    ${dashKpiTile("coins", "Coût LLM", cost.value, costFoot)}
    <div class="d3-kpi" data-section="worker">${dashWorkerTile(data)}</div>
  </div>`;
}

/* ---------- Sections ---------- */

/* « A faire » : tout ce qui attend une decision. Fixe aussi dash.groups pour la relance groupee. */
function dashTodo(data) {
  const parts = [];
  let count = 0;
  const worker = data.worker;
  if (worker && worker.state !== "active") {
    count += 1;
    parts.push(dashCard(`<div class="d3-row"><div class="d3-grow"><div class="d3-title">Le worker est arrêté</div>
      <p class="d3-reason">${esc(worker.reason)}</p>
      <div class="d3-sub"><span>Aucune vidéo mise en file ne démarre tant qu'il est arrêté. Lance-le :</span></div>
      <code class="d3-cmd mono">${esc(worker.command)}</code></div></div>`));
  }
  if (data.failed === null || data.failed === undefined) {
    dash.groups = [];
    parts.push(dashError(data.failed_error || "failed : donnée absente de la réponse."));
  } else {
    dash.groups = dashGroups(data.failed);
    count += data.failed.length;
    parts.push(dash.groups.map(dashGroupCard).join(""));
  }
  if (data.queued === null || data.queued === undefined) parts.push(dashError(data.queued_error || "queued : donnée absente de la réponse."));
  else if (data.queued.length) {
    count += data.queued.length;
    parts.push(dashCard(`<header class="d3-card-head"><div class="d3-grow"><h3>En attente de reprise</h3><p class="d3-cause">Ces vidéos repartent seules à l'heure prévue.</p></div></header>
      <div class="d3-rows">${data.queued.map((video) => dashProblemRow(video, "queued")).join("")}</div>`));
  }
  if (data.watch_pending === null || data.watch_pending === undefined) parts.push(dashError(data.watch_pending_error || "watch_pending : donnée absente de la réponse."));
  else if (data.watch_pending.length) {
    count += data.watch_pending.length;
    parts.push(dashCard(`<header class="d3-card-head"><div class="d3-grow"><h3>VOD à confirmer</h3><p class="d3-cause">Détectées par la surveillance : rien ne part en file sans ta confirmation.</p></div></header>
      <div class="d3-rows">${data.watch_pending.map(dashVodRow).join("")}</div>`));
  }
  if (data.clips_to_review === null || data.clips_to_review === undefined) parts.push(dashError(data.clips_to_review_error || "clips_to_review : donnée absente de la réponse."));
  else if (data.clips_to_review) {
    count += 1;
    parts.push(dashCard(`<a class="d3-row d3-link" href="#/clips">${dashChip("accent", fr(data.clips_to_review))}
      <div class="d3-grow"><div class="d3-title">Clips rendus à valider</div><div class="d3-sub"><span>Approuver pour un compte, refuser, ou re-rendre.</span></div></div>
      <span class="btn" data-variant="outline" data-size="sm">Ouvrir la galerie ${icon("arrow-right", "i-sm")}</span></a>`));
  }
  if (data.zero_views === null || data.zero_views === undefined) parts.push(dashError(data.zero_views_error || "alertes 0 vue : donnée absente de la réponse."));
  else {
    const rows = dashZeroViewsRows(data);
    count += data.zero_views.accounts.reduce((n, group) => n + group.posts.length, 0);
    if (rows) parts.push(dashCard(`<header class="d3-card-head"><div class="d3-grow"><h3>Posts à 0 vue</h3><p class="d3-cause">Aucune vue depuis ${esc(data.zero_views.hours)} h.</p></div></header><div class="d3-rows">${rows}</div>`));
  }
  const body = count === 0 && parts.every((part) => !part)
    ? dashEmpty("circle-check", "Rien à faire", "Aucun échec, aucune reprise, aucune VOD à confirmer ni clip à valider.")
    : parts.join("");
  return { count, html: body || dashEmpty("circle-check", "Rien à faire", "Aucun échec, aucune reprise, aucune VOD à confirmer ni clip à valider.") };
}

function dashRunning(data) {
  const add = `<button type="button" class="btn" data-variant="outline" data-size="sm" data-add>${icon("plus", "i-sm")}Ajouter une vidéo</button>`;
  if (data.running === null || data.running === undefined) return dashSection("running", "En cours", dashError(data.running_error || "running : donnée absente de la réponse."), null, add);
  const waiting = (data.queue || []).filter((e) => e.status === "waiting").length;
  let content;
  if (data.running.length) content = dashCard(`<div class="d3-rows">${data.running.map(dashRunningRow).join("")}</div>`);
  else if (waiting) content = dashEmpty("film", "Rien ne tourne", `${dashPlural(waiting, "vidéo attend", "vidéos attendent")} dans la file : le worker les reprend dès qu'il est disponible.`);
  else content = dashEmpty("film", "Rien ne tourne", "La file est vide. Ajoute une vidéo pour lancer un traitement.");
  return dashSection("running", "En cours", content, fr(data.running.length), add);
}

function dashQueue(data) {
  if (data.queue === null || data.queue === undefined) return dashSection("queue", "File d'attente", dashError(data.queue_error || "queue : donnée absente de la réponse."));
  const waiting = data.queue.filter((e) => e.status === "waiting");
  if (!waiting.length) return "";
  return dashSection("queue", "File d'attente", dashCard(`<div class="d3-rows">${waiting.map(dashQueueRow).join("")}</div>`), fr(waiting.length));
}

/* Les 6 dernieres videos terminees (statut done), la plus recente d'abord : lues dans store.videos. */
function dashRecent(videos) {
  const done = videos.filter((v) => v.status === "done")
    .sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at)).slice(0, DASH_RECENT);
  const all = `<a class="btn" data-variant="ghost" data-size="sm" href="#/videos">Toutes les vidéos ${icon("arrow-right", "i-sm")}</a>`;
  if (!done.length) {
    return dashSection("recent", "Dernières vidéos terminées", dashEmpty("film", "Aucune vidéo terminée pour l'instant", "Les vidéos traitées jusqu'au rendu apparaissent ici."), null, all);
  }
  const rows = done.map((video) => `<tr class="d3-click" data-done-video="${esc(video.video_id)}">
      <td><div class="d3-cell-video">${videoThumb(video.video_id, video.platform_thumbnail)}<div class="d3-grow"><a class="d3-title" href="#/videos/${encodeURIComponent(video.video_id)}">${esc(video.title || video.video_id)}</a>
        <div class="d3-sub"><span class="mono">${esc(video.video_id)}</span></div></div></div></td>
      <td data-l="Style">${video.channel ? `<span class="d3-tag">${esc(video.channel)}</span>` : `<span class="muted">sans style</span>`}</td>
      <td data-l="Clips" class="num">${esc(fr((video.clips || []).length))}</td>
      <td data-l="Calcul" class="mono">${dashCompute(video) || `<span class="muted">non mesuré</span>`}</td>
      <td data-l="Terminée" class="d3-r muted">${esc(dashDate(video.updated_at))}</td></tr>`).join("");
  return dashSection("recent", "Dernières vidéos terminées", `<div class="d3-table-wrap"><table class="d3-table">
    <thead><tr><th>Vidéo</th><th>Style</th><th>Clips</th><th>Calcul</th><th class="d3-r">Terminée</th></tr></thead><tbody>${rows}</tbody></table></div>`, null, all);
}

function dashNextPublications(data) {
  const content = dashList(data, "next_publications", "Aucune publication programmée.", dashPublicationRow);
  return dashSection("next_publications", "Prochaines publications", dashCard(`${content}
    <a class="btn" data-variant="outline" data-size="sm" href="#/publish">Ouvrir Publication ${icon("arrow-right", "i-sm")}</a>`));
}

/* Cout du modele de langage par usage : les 6 premiers, le reste cumule. Le detail par video est dans Statistiques. */
function dashCostRail(data) {
  const cost = data.llm_cost;
  const head = `<span class="muted">7 derniers jours</span>`;
  if (cost === null || cost === undefined) return dashSection("llm_cost", "Coût du modèle de langage", dashError(data.llm_cost_error || "llm_cost : donnée absente de la réponse."));
  const usages = Object.entries(cost.by_usage || {}).sort((a, b) => b[1] - a[1]);
  if (!usages.length && !cost.unreported_calls) {
    return dashSection("llm_cost", "Coût du modèle de langage", dashCard(`<p class="d3-inline-empty">Aucun appel au modèle de langage sur les 7 derniers jours.</p>`), null, head);
  }
  const top = usages.slice(0, DASH_USAGES);
  const rest = usages.slice(DASH_USAGES);
  const max = (usages[0] && usages[0][1]) || 1;
  const bar = (label, value, muted) => `<span class="${muted ? "muted" : "mono"}">${esc(label)}</span><span class="d3-bar"><i style="--p:${(value / max).toFixed(3)}"></i></span><span class="num d3-r">${esc(dashMoney(value))}</span>`;
  const bars = top.map(([name, usd]) => bar(name, usd, false)).join("")
    + (rest.length ? bar(`${fr(rest.length)} ${rest.length > 1 ? "autres usages" : "autre usage"}`, rest.reduce((t, [, v]) => t + v, 0), true) : "");
  const unreported = cost.unreported_calls
    ? `<p class="d3-note">${esc(fr(cost.unreported_calls))} appel(s) sans coût rapporté par le modèle, non comptés. Détail par vidéo dans <a href="#/stats">Statistiques</a>.</p>`
    : `<p class="d3-note">Détail par vidéo dans <a href="#/stats">Statistiques</a>.</p>`;
  return dashSection("llm_cost", "Coût du modèle de langage", dashCard(`<div class="d3-bars">${bars}</div>${unreported}`), null, head);
}

function dashDiskRail() {
  if (dash.diskError) return dashSection("disk", "Espace disque", dashError(dash.diskError.message || String(dash.diskError)));
  if (!dash.disk) return dashSection("disk", "Espace disque", dashCard(`<div class="d3-skel"></div>`));
  const rows = [["workspace", dash.disk.workspace_bytes], ["output", dash.disk.output_bytes]];
  const max = Math.max(...rows.map(([, bytes]) => bytes)) || 1;
  return dashSection("disk", "Espace disque", dashCard(`<div class="d3-bars">${rows.map(([name, bytes]) =>
    `<span class="mono">${name}</span><span class="d3-bar"><i style="--p:${(bytes / max).toFixed(3)}"></i></span><span class="num d3-r">${esc(dashBytes(bytes))}</span>`).join("")}</div>`));
}

function dashLoading() {
  return `<div class="v3 d3" aria-busy="true"><div class="d3-kpis">${"<div class=\"d3-kpi\"><div class=\"d3-skel\"></div></div>".repeat(6)}</div>
    <div class="d3-cols"><div class="d3-main"><div class="d3-skel d3-skel-card"></div><div class="d3-skel d3-skel-card"></div></div>
    <aside class="d3-aside"><div class="d3-skel d3-skel-card"></div></aside></div></div>`;
}

/* ---------- Ecran ---------- */

function wireDash(body) {
  $$("[data-retry-group]", body).forEach((b) => (b.onclick = () => {
    const group = dash.groups[Number(b.dataset.retryGroup)];
    if (group) dashRetryGroup(group.videos);
  }));
  $$("[data-scroll]", body).forEach((b) => (b.onclick = () => {
    const target = document.getElementById(b.dataset.scroll);
    if (target) target.scrollIntoView({ block: "start" });
  }));
  $$("[data-done-video]", body).forEach((row) => (row.onclick = (e) => {
    if (e.target.closest("a, button")) return;
    location.hash = `#/videos/${encodeURIComponent(row.dataset.doneVideo)}`;
  }));
}

Screens.dashboard = {
  render(body, store) {
    const data = store.dashboard;
    if (Date.now() - dash.at > DASH_STALE_MS) loadDashboard();
    if (Date.now() - dash.diskAt > DASH_DISK_STALE_MS) loadDisk();
    if (!data) {
      morph(body, dash.error
        ? `<div class="v3 d3">${dashEmpty("circle-alert", "Chargement impossible", String(dash.error.message || dash.error))}</div>`
        : dashLoading());
      return;
    }
    const todo = dashTodo(data);
    morph(body, `<div class="v3 d3">
      ${dash.error ? dashError(`Actualisation impossible : ${dash.error.message || dash.error}`) : ""}
      ${dashKpis(data)}
      <div class="d3-cols">
        <div class="d3-main">
          <section class="d3-section" id="d3-todo" data-section="todo">
            <div class="d3-section-head"><h2>À faire</h2><span class="d3-count">${esc(fr(todo.count))}</span></div>${todo.html}</section>
          ${dashRunning(data)}
          ${dashQueue(data)}
          ${dashRecent(store.videos || [])}
        </div>
        <aside class="d3-aside">
          ${dashNextPublications(data)}
          ${dashCostRail(data)}
          ${dashDiskRail()}
        </aside>
      </div>
    </div>`);
    wireWatch(body);
    wireDash(body);
  },
};
