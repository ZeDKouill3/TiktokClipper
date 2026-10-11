/* Radar du jury par moment (TASK-3a08), panneau de l'etape Moments de la
   fiche video. Fonctions pures : elles recoivent la reponse de
   GET /api/videos/{id}/jury et rendent du HTML / un SVG inline, sans
   bibliotheque. Charge avant videos.js, qui branche les clics.

   ui = { key, round } : moment choisi (null = le premier), tour affiche
   (null = le dernier, donc apres debat s'il y en a eu un). */
"use strict";

const JR_R = 112;       // rayon de la grille (note 10)
const JR_C = 180;       // centre du SVG (viewBox elargi pour les etiquettes)
const JR_COLORS = 6;    // --jr-c0 ... --jr-c5 (console.css)

const jrEsc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const jrNum = (n) => (Math.round(n * 10) / 10).toString();

function jrTime(seconds) {
  const s = Math.round(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/* Un juge garde la meme couleur d'un moment a l'autre : rang dans l'ordre d'apparition. */
function jrJudgeNames(data) {
  const names = [];
  data.moments.forEach((m) => m.rounds.forEach((r) => Object.keys(r.judges).forEach((n) => { if (!names.includes(n)) names.push(n); })));
  return names;
}

function jrRoundIndex(moment, round) {
  const last = moment.rounds.length - 1;
  return round == null ? last : Math.min(Math.max(round, 0), last);
}

function jrScore(scores, name, what) {
  const v = scores ? scores[name] : undefined;
  if (typeof v !== "number" || !Number.isFinite(v)) throw new Error(`note manquante ou invalide : ${what}, critère « ${name} »`);
  return Math.min(Math.max(v, 0), 10);
}

/* Confiance 0-100 -> opacite du trait ; confiance inconnue = trait pale (jamais plein par defaut). */
const jrOpacity = (confidence) => (typeof confidence === "number" ? 0.3 + 0.7 * Math.min(Math.max(confidence, 0), 100) / 100 : 0.3);

function jrPoint(i, n, value) {
  const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
  return [JR_C + Math.cos(angle) * JR_R * value / 10, JR_C + Math.sin(angle) * JR_R * value / 10];
}

const jrPoints = (criteria, scores, what) => criteria.map((c, i) => jrPoint(i, criteria.length, jrScore(scores, c.name, what)).map((v) => v.toFixed(1)).join(",")).join(" ");

function juryRadarSvg(data, moment, round) {
  const criteria = data.criteria;
  const n = criteria.length;
  if (n < 3) throw new Error(`grille à ${n} critère(s) : un radar demande au moins 3 critères`);
  const names = jrJudgeNames(data);
  const rnd = moment.rounds[jrRoundIndex(moment, round)];
  const rings = [2, 4, 6, 8, 10].map((v) => `<polygon class="jr-ring" points="${criteria.map((_, i) => jrPoint(i, n, v).map((x) => x.toFixed(1)).join(",")).join(" ")}"/>`).join("");
  const axes = criteria.map((c, i) => {
    const [x, y] = jrPoint(i, n, 10);
    const [lx, ly] = jrPoint(i, n, 11.6);
    const anchor = Math.abs(lx - JR_C) < 6 ? "middle" : lx > JR_C ? "start" : "end";
    const zero = c.weight === 0;
    return `<line class="jr-axis${zero ? " jr-zero" : ""}" x1="${JR_C}" y1="${JR_C}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}"/>`
      + `<text class="jr-label${zero ? " jr-zero" : ""}" x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="${anchor}" dominant-baseline="middle">${jrEsc(c.name)} <tspan class="jr-w">poids ${jrEsc(c.weight)}</tspan></text>`;
  }).join("");
  const judges = Object.entries(rnd.judges).map(([name, j]) => {
    const vetoer = Boolean(j.veto) || (moment.veto && moment.veto.judge === name);
    return `<polygon class="jr-judge jr-c${names.indexOf(name) % JR_COLORS}${vetoer ? " jr-vetoer" : ""}" data-judge="${jrEsc(name)}" stroke-opacity="${jrOpacity(j.confidence).toFixed(2)}" fill-opacity="${(jrOpacity(j.confidence) * 0.12).toFixed(3)}" points="${jrPoints(criteria, j.scores, `juge ${name}`)}"/>`;
  }).join("");
  const retained = `<polygon class="jr-retained" points="${jrPoints(criteria, moment.scores, "note retenue")}"/>`;
  const label = `Radar du jury, moment ${jrTime(moment.start)} à ${jrTime(moment.end)} : ${criteria.map((c) => `${c.name} ${jrNum(jrScore(moment.scores, c.name, "note retenue"))}/10`).join(", ")}`;
  return `<svg class="jr-svg" viewBox="-70 0 500 360" role="img" aria-label="${jrEsc(label)}"><title>${jrEsc(label)}</title>${rings}${axes}${judges}${retained}</svg>`;
}

function jrLegend(data, moment, rnd) {
  const names = jrJudgeNames(data);
  const items = Object.entries(rnd.judges).map(([name, j]) => {
    const vetoer = Boolean(j.veto) || (moment.veto && moment.veto.judge === name);
    return `<li><i class="jr-sw jr-c${names.indexOf(name) % JR_COLORS}${vetoer ? " jr-vetoer" : ""}" style="opacity:${jrOpacity(j.confidence).toFixed(2)}"></i><span class="jr-jname">${jrEsc(name)}</span>`
      + `<span class="muted">${j.score != null ? ` ${jrEsc(jrNum(j.score))}` : ""} · confiance ${j.confidence != null ? jrEsc(j.confidence) + " %" : "inconnue"}${vetoer ? ` · <b class="jr-bad">veto</b>` : ""}</span></li>`;
  }).join("");
  return `<ul class="jr-legend"><li><i class="jr-sw jr-sw-retained"></i><span class="jr-jname">Note retenue</span><span class="muted"> médiane pondérée des juges</span></li>${items}</ul>`
    + `<p class="muted jr-hint">Trait pâle : juge peu sûr de lui. Branche grisée : critère de poids 0, sans effet sur le score.</p>`;
}

function jrTable(data, moment, rnd) {
  const judges = Object.keys(rnd.judges);
  const head = `<tr><th scope="col">Critère</th><th scope="col">Poids</th>${judges.map((j) => `<th scope="col">${jrEsc(j)}</th>`).join("")}<th scope="col">Retenue</th></tr>`;
  const rows = data.criteria.map((c) => `<tr class="${c.weight === 0 ? "jr-zero" : ""}"><th scope="row">${jrEsc(c.name)}</th><td>${jrEsc(c.weight)}</td>`
    + `${judges.map((j) => `<td>${jrEsc(jrNum(jrScore(rnd.judges[j].scores, c.name, `juge ${j}`)))}</td>`).join("")}<td><b>${jrEsc(jrNum(jrScore(moment.scores, c.name, "note retenue")))}</b></td></tr>`).join("");
  return `<div class="jr-table-wrap"><table class="jr-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}

const JR_SOURCE_LABEL = { transcript: "transcription", action: "passage d'action" };
const JR_KIND_LABEL = { retenu: "Retenu", decoupage: "Écarté au découpage", exploration: "Exploration", score: "Sous le seuil", plafond: "Plafond par heure", veto: "Veto", autre: "Rejeté" };

function jrRow(m, selected) {
  const tone = m.reason_kind === "decoupage" ? "warn" : m.retained ? "ok" : m.reason_kind === "veto" ? "bad" : "pending";
  return `<button type="button" class="jr-item${m.key === selected ? " sel" : ""}" data-jr-pick="${jrEsc(m.key)}" aria-pressed="${m.key === selected}"${m.cut_rejected ? ` title="${jrEsc(`Retenu par le jury, écarté au découpage : ${m.cut_rejected}`)}"` : ""}>`
    + `<span class="mono">${jrTime(m.start)}–${jrTime(m.end)}</span><span class="jr-item-hook"${m.source ? ` title="Origine du moment : ${jrEsc(JR_SOURCE_LABEL[m.source] || m.source)}"` : ""}>${m.source ? `<b>${jrEsc(JR_SOURCE_LABEL[m.source] || m.source)}</b> · ` : ""}${jrEsc(m.hook_text || "sans phrase d'accroche")}</span>`
    + `<span class="jr-item-score">${m.final_score != null ? jrEsc(jrNum(m.final_score)) : "–"}</span><span class="chip ${tone} plain">${jrEsc(JR_KIND_LABEL[m.reason_kind] || m.reason_kind)}</span></button>`;
}

function juryPanelHtml(data, ui) {
  if (!data.available) {
    return `<div class="jr-empty"><b>Jury indisponible.</b> ${jrEsc(data.reason)}</div>`;
  }
  if (!data.moments.length) return `<div class="jr-empty"><b>Aucun moment noté par le jury.</b></div>`;
  try {
    const moment = data.moments.find((m) => m.key === ui.key) || data.moments[0];
    const ri = jrRoundIndex(moment, ui.round);
    const rnd = moment.rounds[ri];
    const kept = data.moments.filter((m) => m.retained && !m.cut_rejected);
    const cut = data.moments.filter((m) => m.cut_rejected);
    const rest = data.moments.filter((m) => !m.retained);
    const toggle = moment.debated && moment.rounds.length > 1
      ? `<div class="seg jr-toggle" role="group" aria-label="Tour du jury">${moment.rounds.map((r, i) => `<button type="button" class="${i === ri ? "on" : ""}" data-jr-round="${i}" aria-pressed="${i === ri}">${i === 0 ? "Avant débat" : "Après débat"}</button>`).join("")}</div>`
      : "";
    const veto = moment.veto
      ? `<div class="jr-veto" role="alert"><b>Veto du juge ${jrEsc(moment.veto.judge)}</b> : ${jrEsc(moment.veto.reason)}</div>` : "";
    return `<div class="jr">
      <div class="jr-list" role="group" aria-label="Moments notés par le jury">
        <div class="jr-group">Retenus <span class="muted">${kept.length}</span></div>${kept.map((m) => jrRow(m, moment.key)).join("") || `<p class="muted">Aucun.</p>`}
        ${cut.length ? `<div class="jr-group">Retenus, écartés au découpage <span class="muted">${cut.length}</span></div>${cut.map((m) => jrRow(m, moment.key)).join("")}` : ""}
        <div class="jr-group">Non retenus <span class="muted">${rest.length}</span></div>${rest.map((m) => jrRow(m, moment.key)).join("") || `<p class="muted">Aucun.</p>`}
      </div>
      <div class="jr-main">
        <div class="jr-head"><span class="mono">${jrTime(moment.start)}–${jrTime(moment.end)}</span>${toggle}</div>
        <div class="jr-chart">${juryRadarSvg(data, moment, ui.round)}${jrLegend(data, moment, rnd)}</div>
        ${veto}
        <dl class="kv jr-facts">
          <dt>Score final</dt><dd>${moment.final_score != null ? `<b>${jrEsc(jrNum(moment.final_score))}</b>` : "aucun (rejeté avant le score final)"}${moment.confidence != null ? ` · confiance du jury ${jrEsc(moment.confidence)} %` : ""}</dd>
          <dt>Seuil</dt><dd>${data.threshold != null ? jrEsc(data.threshold) : "inconnu : rubric.min_score absent de moments.json"}</dd>
          <dt>${moment.cut_rejected ? "Retenu puis écarté" : moment.retained ? "Retenu" : "Rejeté"}</dt><dd>${moment.cut_rejected ? `Retenu par le jury, écarté au découpage : ${jrEsc(moment.cut_rejected)}` : jrEsc(moment.reason)}</dd>
          ${moment.justification ? `<dt>Justification</dt><dd>${jrEsc(moment.justification)}</dd>` : ""}
          ${moment.hook_text ? `<dt>Phrase d'accroche</dt><dd>${jrEsc(moment.hook_text)}</dd>` : ""}
        </dl>
        ${jrTable(data, moment, rnd)}
      </div>
    </div>`;
  } catch (err) {
    return `<div class="jr-empty"><b>Radar impossible.</b> ${jrEsc(err.message)}</div>`;
  }
}
