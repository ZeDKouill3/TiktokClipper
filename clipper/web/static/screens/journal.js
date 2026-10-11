/* Écran « Journal » (TASK-8067, menu Configuration). Lecture seule de
   GET /api/journal (dernières lignes du journal global de toutes les
   actions, tous les processus, un fichier par jour) ; rafraîchissement
   manuel seulement, jamais automatique. Aucune logique de traitement ici
   (ADR-09ad) : le filtrage texte/niveau est délégué au serveur à chaque
   rafraîchissement. Charge après screens.js dont il remplace l'entrée
   Screens.journal. */
"use strict";

const JRN_LEVELS = ["", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
const JRN_LINES = 300;

const jrnUi = { data: null, loading: null, error: null, q: "", level: "" };

function jrnQuery() {
  const params = new URLSearchParams({ lines: String(JRN_LINES) });
  if (jrnUi.q) params.set("q", jrnUi.q);
  if (jrnUi.level) params.set("level", jrnUi.level);
  return params.toString();
}

function jrnLoad() {
  if (jrnUi.loading) return jrnUi.loading;
  jrnUi.loading = (async () => {
    try {
      jrnUi.data = await api(`/api/journal?${jrnQuery()}`);
      jrnUi.error = null;
    } catch (err) {
      jrnUi.error = err;
      jrnUi.data = jrnUi.data || { failed: true };
    } finally {
      jrnUi.loading = null;
    }
    if (currentScreen === "journal") renderCurrent();
  })();
  return jrnUi.loading;
}

// Niveau colore par les classes .lvl de console.css (error, warning ; le reste reste neutre).
const JRN_LEVEL_CLASS = { ERROR: "error", CRITICAL: "error", WARNING: "warning" };

function jrnRow(line) {
  const level = line.level || "";
  return `<tr>
    <td class="mono" data-l="Horodatage">${esc(line.timestamp || "")}</td>
    <td class="mono" data-l="Processus">${esc(line.process || "")}</td>
    <td data-l="Niveau"><span class="lvl${JRN_LEVEL_CLASS[level] ? " " + JRN_LEVEL_CLASS[level] : ""}">${esc(level)}</span></td>
    <td class="mono" data-l="Logger">${esc(line.logger || "")}</td>
    <td class="wrap" data-l="Message">${esc(line.message || line.raw || "")}</td>
  </tr>`;
}

function jrnHtml() {
  const data = jrnUi.data;
  if (!data) return `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`;
  if (data.failed) return emptyState("circle-alert", "Journal indisponible", jrnUi.error ? jrnUi.error.message : "");
  const toolbar = `<div class="jrn-bar">
      <input class="input mono" id="jrn-q" placeholder="Filtrer le texte..." value="${esc(jrnUi.q)}">
      <select class="input" id="jrn-level" aria-label="Niveau">${JRN_LEVELS.map((l) => `<option value="${esc(l)}"${l === jrnUi.level ? " selected" : ""}>${l ? esc(l) : "Tous les niveaux"}</option>`).join("")}</select>
      <button type="button" class="btn btn-sm" id="jrn-refresh">${icon("refresh-cw", "i-xs")}Rafraîchir</button>
    </div>`;
  if (!data.available) {
    return toolbar + emptyState("activity", "Aucune action journalisée", data.reason || "");
  }
  const rows = data.lines.slice().reverse(); // plus récent en haut
  if (!rows.length) {
    return toolbar + `<div class="list-item muted">Aucune ligne ne correspond au filtre.</div>`;
  }
  return `${toolbar}<table class="table journal"><colgroup><col class="jc-time"><col class="jc-proc"><col class="jc-lvl"><col class="jc-logger"><col></colgroup><thead><tr>
      <th>Horodatage (Paris)</th><th>Processus</th><th>Niveau</th><th>Logger</th><th>Message</th>
    </tr></thead><tbody>${rows.map(jrnRow).join("")}</tbody></table>
    <p class="muted">${esc(data.path || "")}</p>`;
}

function jrnWire(root) {
  root.onclick = (e) => {
    if (e.target.closest("#jrn-refresh")) {
      jrnUi.q = $("#jrn-q", root).value.trim();
      jrnUi.level = $("#jrn-level", root).value;
      jrnLoad();
    }
  };
  root.onkeydown = (e) => {
    if (e.key === "Enter" && e.target.id === "jrn-q") $("#jrn-refresh", root).click();
  };
}

Screens.journal = {
  render(body) {
    if (jrnUi.data === null) jrnLoad();
    morph(body, `<div class="journal">${jrnHtml()}</div>`); // rapprochement du DOM (ADR-a308) : le champ de filtre garde son focus
    jrnWire(body);
  },
};
