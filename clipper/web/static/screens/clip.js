/* Ecran « Fiche clip » (TASK-fa7619ccf83a, SPEC-7715 E12) : #/clip/<video_id>/<clip_id>, la page plein ecran d'un
   lien partage. Lit GET /api/clips/.../sheet, lecture seule : rien n'est recalcule ici. Une donnee absente
   s'affiche « inconnu », jamais 0 (ADR-ad2e). Une video supprimee (.mp4 absent) garde la fiche avec la mention
   « vidéo supprimée, fiche conservée ». Rendu v3 (cartes Basecoat, morph), les actions vivent dans le tiroir de
   l'ecran Clips (« Ouvrir dans Clips »). */
"use strict";

const clipSheetUi = { key: null, data: null, accounts: null, error: null, loading: null, at: 0 };
const CLIP_SHEET_STALE_MS = 4000;

/* Cle (video/clip) lue dans l'adresse #/clip/<video_id>/<clip_id>. */
function clipSheetHashKey() {
  const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (parts[0] !== "clip" || !parts[1] || !parts[2]) return null;
  return { video: decodeURIComponent(parts[1]), clip: decodeURIComponent(parts[2]) };
}

function loadClipSheet(target) {
  const key = `${target.video}/${target.clip}`;
  if (clipSheetUi.loading && clipSheetUi.key === key) return clipSheetUi.loading;
  clipSheetUi.key = key;
  clipSheetUi.data = null;
  clipSheetUi.error = null;
  clipSheetUi.loading = (async () => {
    try {
      const url = `/api/clips/${encodeURIComponent(target.video)}/${encodeURIComponent(target.clip)}/sheet`;
      // Le nom du compte vient de la liste des comptes ; une erreur de l'une ou l'autre reste visible (ADR-ad2e).
      const [data, accounts] = await Promise.all([api(url), api("/api/accounts")]);
      if (clipSheetUi.key === key) {
        clipSheetUi.data = data;
        clipSheetUi.accounts = accounts;
      }
    } catch (err) {
      if (clipSheetUi.key === key) clipSheetUi.error = err;
    } finally {
      if (clipSheetUi.key === key) clipSheetUi.loading = null;
      clipSheetUi.at = Date.now();
    }
    if (currentScreen === "clip") renderCurrent();
  })();
  return clipSheetUi.loading;
}

/* Valeur affichee : « inconnu » quand la donnee manque (null / undefined / vide). */
const sheetVal = (v, suffix) => (v == null || v === "" ? "inconnu" : esc(`${v}${suffix || ""}`));
const sheetRow = (label, value) => `<div class="sheet-row"><span class="muted">${esc(label)}</span><span>${value}</span></div>`;
const sheetLink = (url, text) => (url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(text || url)}</a>` : "inconnu");

function clipSheetVideoHtml(video) {
  if (video.deleted || !video.video_url) {
    return `<div class="empty"><p>${esc(video.note || "vidéo supprimée, fiche conservée")}</p></div>`;
  }
  return `<video controls preload="metadata" playsinline src="${esc(video.video_url)}" class="sheet-video"></video>`;
}

function clipSheetJuryHtml(jury) {
  const judges = jury.judges && typeof jury.judges === "object"
    ? Object.entries(jury.judges).map(([name, value]) => sheetRow(`Juge ${name}`, sheetVal(value))).join("")
    : "";
  const rounds = (jury.rounds || []).map((r) => sheetRow(`Tour ${r.round}`, Object.entries(r.judges || {}).map(([name, j]) =>
    `${esc(name)} : note ${sheetVal(j.score)}, confiance ${sheetVal(j.confidence)}${j.argument ? ` — ${esc(j.argument)}` : ""}`,
  ).join("<br>") || "inconnu")).join("");
  const confidence = jury.confidence == null ? `<span class="muted">non évaluée (pas de jury pour ce moment)</span>` : sheetVal(jury.confidence, " %");
  return `${sheetRow("Confiance du jury", confidence)}${judges}${rounds || sheetRow("Tours du jury", "inconnu")}`;
}

function clipSheetScoresHtml(scores) {
  if (!scores || typeof scores !== "object" || !Object.keys(scores).length) return sheetRow("Critères", "inconnu");
  return Object.entries(scores).map(([name, value]) => sheetRow(name, sheetVal(value))).join("");
}

function clipSheetStatsHtml(stats, clip) {
  if (!stats) {
    const reason = clip.post_url || clip.post_id ? "pas de relevé pour ce post" : "aucun post lié à ce clip";
    return `<p class="muted">Statistiques : inconnu (${esc(reason)}).</p>`;
  }
  if (stats.error) return `<p class="muted">Statistiques indisponibles : ${esc(stats.error)}</p>`;
  const history = (stats.history || []).map((h) => sheetRow(
    h.fetched_at ? h.fetched_at.replace("T", " ").slice(0, 16) : "relevé",
    `vues ${sheetVal(h.views)} · likes ${sheetVal(h.likes)} · commentaires ${sheetVal(h.comments)}`,
  )).join("");
  return `${sheetRow("Vues", sheetVal(stats.views))}${sheetRow("Likes", sheetVal(stats.likes))}${history}`;
}

/* Date ISO affichee a l'heure de Paris, format court francais (« jeu. 8 oct., 09:00 », comme le tableau de bord).
   Absente = « inconnu » ; illisible = texte brut, jamais une date inventee. */
function clipSheetDate(iso) {
  if (iso == null || iso === "") return "inconnu";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? esc(iso) : esc(fmtParis(iso, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }));
}

/* Compte de publication : son nom (label de GET /api/accounts), l'id en secondaire ; « inconnu » si le compte n'existe plus. */
function clipSheetAccountHtml(id, accounts) {
  if (id == null || id === "") return "inconnu";
  const found = (accounts || []).find((a) => a.id === id);
  if (!found) return `inconnu <span class="muted">${esc(id)}</span>`;
  return `${esc(found.label || id)} <span class="muted">${esc(id)}</span>`;
}

/* Problemes du controle qualite : type, severite, detail en texte. Un probleme en texte seul reste tel quel. */
function clipSheetIssuesHtml(issues) {
  if (!Array.isArray(issues) || !issues.length) return "";
  const items = issues.map((issue) => {
    if (typeof issue === "string") return `<li>${esc(issue)}</li>`;
    const head = [issue.type, issue.severity].filter((x) => x != null && x !== "").map((x) => esc(x)).join(" · ");
    const detail = issue.detail == null || issue.detail === "" ? "" : esc(issue.detail);
    return `<li>${head ? `<strong>${head}</strong>` : ""}${detail ? `${head ? " — " : ""}${detail}` : ""}${!head && !detail ? "inconnu" : ""}</li>`;
  }).join("");
  return `<ul class="sheet-issues">${items}</ul>`;
}

function clipSheetHtml(sheet, accounts) {
  const { clip, video, jury, stats } = sheet;
  const s = clipStatus(clip);
  const qa = clip.qa_status ? `${esc(clip.qa_status)}${clipSheetIssuesHtml(clip.issues)}` : "inconnu";
  const card = (title, body) => `<section class="card sheet-card"><header><h3>${esc(title)}</h3></header><section>${body}</section></section>`;
  return `
    <div class="sheet-head">
      <div class="sheet-head-row"><span class="chip ${s.cls}">${esc(s.label)}</span><a class="btn" data-variant="outline" data-size="sm" href="#/clips/${encodeURIComponent(clip.video_id)}/${encodeURIComponent(clip.clip_id)}">${icon("clapperboard", "i-sm")}Ouvrir dans Clips</a></div>
      <h2>${esc(clip.screen_title || clip.title || clip.clip_id)}</h2>
      <p class="muted">${esc(video.title || clip.video_id)} · <span class="mono">${esc(clip.video_id)} / ${esc(clip.clip_id)}</span></p>
    </div>
    <div class="sheet-grid">
      ${card("Vidéo", `${clipSheetVideoHtml(video)}
        ${sheetRow("Passage source", sheetLink(video.passage_url, "Voir le passage"))}
        ${sheetRow("Source", sheetLink(video.source_url, "Vidéo source"))}`)}
      ${card("Clip", `
        ${sheetRow("Titre d'écran", sheetVal(clip.screen_title))}
        ${sheetRow("Légende", sheetVal(clip.caption))}
        ${sheetRow("Score", sheetVal(clip.score))}
        ${clipSheetScoresHtml(clip.scores)}
        ${sheetRow("Raison", sheetVal(clip.reason))}
        ${sheetRow("Accroche", sheetVal(clip.hook_text))}
        ${sheetRow("Début / fin dans la VOD", clip.start == null ? "inconnu" : `${fr(clip.start, 1)} s → ${fr(clip.end, 1)} s`)}
        ${sheetRow("Durée", clip.duration == null ? "inconnu" : clipSeconds(clip.duration))}
        ${sheetRow("Format", sheetVal(clip.layout))}
        ${sheetRow("Contrôle qualité", qa)}`)}
      ${card("Jury", clipSheetJuryHtml(jury))}
      ${card("Publication", `
        ${sheetRow("Compte", clipSheetAccountHtml(clip.account, accounts))}
        ${sheetRow("Statut", esc(s.label))}
        ${sheetRow("Créneau", clipSheetDate(clip.slot_at_paris))}
        ${sheetRow("Envoyé à TikTok le", clipSheetDate(clip.published_at_paris))}
        ${sheetRow("Lien du post", sheetLink(clip.post_url))}
        ${clip.publish_error ? sheetRow("Erreur", esc(clip.publish_error)) : ""}`)}
      ${card("Statistiques", clipSheetStatsHtml(stats, clip))}
    </div>`;
}

Screens.clip = {
  render(body) {
    const section = body.closest(".screen");
    if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
    const target = clipSheetHashKey();
    if (!target) {
      morph(body, emptyState("circle-alert", "Clip introuvable", "Adresse de fiche incomplète : ouvre un clip depuis l'écran Clips."));
      return;
    }
    const key = `${target.video}/${target.clip}`;
    if (clipSheetUi.key !== key || (Date.now() - clipSheetUi.at > CLIP_SHEET_STALE_MS && !clipSheetUi.loading)) loadClipSheet(target);
    if (clipSheetUi.error) {
      morph(body, emptyState("circle-alert", "Chargement impossible", String(clipSheetUi.error.message || clipSheetUi.error)));
      return;
    }
    if (!clipSheetUi.data) {
      morph(body, `<div class="skeleton sk-line"></div><div class="sheet-grid"><div class="skeleton sk-card"></div><div class="skeleton sk-card"></div></div>`);
      return;
    }
    morph(body, clipSheetHtml(clipSheetUi.data, clipSheetUi.accounts));
  },
};
