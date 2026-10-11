/* Éditeur d'agencement stream split (SPEC-c100 E5, SPEC-76dc). Route
   #/styles/<chaine>/layout : canevas 1080x1920 mis à l'échelle, quatre zones
   (webcam, jeu, badge, sous-titres) à glisser et redimensionner sur une image
   clé d'une vidéo du style, valeurs {x, y, w, h} éditables, enregistrées
   dans [reframe] du preset ; la case « Afficher le badge » écrit
   [render] badge_enabled (décoché : la zone disparaît, badge_dest reste). Aucune validation ici : c'est reframe qui refuse un
   agencement qui déborde, se chevauche ou sort de la zone sûre, et le serveur
   renvoie son message tel quel (422), affiché sous la barre d'outils.
   Charge après channels.js, dont il enveloppe Screens.channels. Un style en
   letterbox ouvre à la place l'éditeur de layout-letterbox.js (lbOpen). */
"use strict";

const LY_CANVAS = { w: 1080, h: 1920 };
const LY_MIN = 20;                 // taille minimale d'une zone (px du canevas)
const LY_ZONES = [
  { key: "split_webcam_dest", label: "Webcam", icon: "scan-face", kind: "cam" },
  { key: "split_gameplay_dest", label: "Jeu", icon: "monitor", kind: "game" },
  { key: "badge_dest", label: "Badge de style", icon: "twitch", kind: "badge" },
  { key: "split_subtitle_dest", label: "Sous-titres", icon: "captions", kind: "subs" },
];
const LY_SAMPLE = "EXEMPLE DE SOUS-TITRE";

const lyUi = { name: null, data: null, draft: null, saved: null, selected: LY_ZONES[0].key, frame: null, frameError: null, error: null, scale: 0.3, safe: false, snap: true, busy: false };

const lyBadgeOn = () => lyUi.draft.badge_enabled !== false;
const lyName = () => {
  const parts = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  return parts[0] === "styles" && parts[2] === "layout" ? decodeURIComponent(parts[1] || "") : "";
};
const lyCopy = (v) => JSON.parse(JSON.stringify(v));
const lyDirty = () => JSON.stringify(lyUi.draft) !== JSON.stringify(lyUi.saved);
const lyUrl = (name, tail) => `/api/channels/${encodeURIComponent(name)}/${tail}`;

function lyZoneInner(z) {
  if (z.kind === "cam" || z.kind === "game") {
    return lyUi.frame ? `<img class="ly-crop" src="${esc(lyUi.frame)}" alt="" draggable="false">` : `<span class="ly-fill"></span>`;
  }
  if (z.kind === "badge") return `<div class="ly-badge"><span class="ly-badge-ico">${icon("twitch")}</span><span class="ly-badge-nm">${esc(lyUi.name)}</span></div>`;
  return `<div class="ly-subs">${esc(LY_SAMPLE)}</div>`;
}

function lyStageHtml() {
  const s = lyUi.data.safe;
  const zones = LY_ZONES.map((z) => `<div class="ly-zone ly-z-${z.kind}${z.key === lyUi.selected ? " sel" : ""}${z.kind === "badge" && !lyBadgeOn() ? " off" : ""}" data-zone="${z.key}" tabindex="0" role="group" aria-label="${esc(z.label)}">
      ${lyZoneInner(z)}<span class="ly-tag">${esc(z.label)}</span>
      ${["nw", "ne", "sw", "se"].map((h) => `<span class="ly-hdl ${h}" data-h="${h}"></span>`).join("")}</div>`).join("");
  return `<img class="ly-bg" src="${esc(lyUi.frame || "")}" alt="" ${lyUi.frame ? "" : "hidden"}>${zones}
    <div class="ly-safe${lyUi.safe ? " show" : ""}" style="left:${s.left}px;top:${s.top}px;width:${s.right - s.left}px;height:${s.bottom - s.top}px"></div>
    <div class="ly-guide"></div>`;
}

function lyValuesHtml() {
  return LY_ZONES.map((z) => {
    const r = lyUi.draft[z.key];
    return `<div class="ly-values${z.key === lyUi.selected ? " sel" : ""}${z.kind === "badge" && !lyBadgeOn() ? " off" : ""}" data-values="${z.key}">
      <button type="button" class="ly-values-head" data-select="${z.key}">${icon(z.icon, "i-sm")}<b>${esc(z.label)}</b><span class="mono muted">${esc(z.key)}</span></button>
      <div class="ly-coords">${["x", "y", "w", "h"].map((k) => `<label>${k}<input class="input" type="number" inputmode="numeric" step="1" data-k="${k}" value="${esc(r[k])}"></label>`).join("")}</div></div>`;
  }).join("");
}

function lyHtml() {
  const frameNote = lyUi.frame
    ? `<p class="muted ly-note">Aperçu sur une image clé d'une vidéo du style : le recadrage réel suit la webcam détectée, ici l'image entière sert de repère.</p>`
    : `<p class="reason bad ly-note" role="alert">${esc(lyUi.frameError || "Aucune image clé disponible.")}</p>`;
  return `<div class="ly">
    <div class="toolbar"><a class="btn" data-variant="ghost" data-size="sm" href="#/styles/${encodeURIComponent(lyUi.name)}">${icon("chevron-left", "i-xs")}Retour au style</a>
      <h2 class="chan-title">Éditeur d'agencement <span class="mono muted">${esc(lyUi.name)}</span></h2><span class="grow"></span>
      <span class="muted ly-dirty" data-ly-dirty></span>
      <button type="button" class="btn" data-variant="ghost" data-size="sm" data-ly-reset>${icon("rotate-ccw", "i-xs")}Réinitialiser aux défauts</button>
      <button type="button" class="btn" data-ly-save>Enregistrer</button></div>
    <p class="reason bad" data-ly-error role="alert" hidden></p>
    <div class="ly-grid">
      <div class="ly-main">
        <div class="ly-tools">
          <label class="ly-check"><input type="checkbox" data-ly-safe ${lyUi.safe ? "checked" : ""}> Zone sûre TikTok</label>
          <label class="ly-check"><input type="checkbox" data-ly-badge ${lyBadgeOn() ? "checked" : ""}> Afficher le badge</label>
          <label class="ly-check"><input type="checkbox" data-ly-snap ${lyUi.snap ? "checked" : ""}> Magnétisme (axe central)</label></div>
        <div class="ly-outer" data-ly-outer><div class="ly-stage" data-ly-stage>${lyStageHtml()}</div></div>
        ${frameNote}
        <p class="muted ly-note">Glisse une zone pour la déplacer, tire un coin pour la redimensionner (souris ou doigt). Flèches : 1 px, Maj : 10 px.</p>
      </div>
      <aside class="ly-side panel panel-pad" data-ly-values>${lyValuesHtml()}</aside>
    </div></div>`;
}

function lyRefreshDirty(root) {
  const el = $("[data-ly-dirty]", root);
  if (el) el.textContent = lyDirty() ? "modifié, non enregistré" : "";
}

function lyPlace(root, key) {
  const el = $(`.ly-zone[data-zone="${key}"]`, root);
  const r = lyUi.draft[key];
  el.style.left = `${r.x}px`; el.style.top = `${r.y}px`; el.style.width = `${r.w}px`; el.style.height = `${r.h}px`;
  const inputs = $$(`[data-values="${key}"] input`, root);
  ["x", "y", "w", "h"].forEach((k, i) => { if (inputs[i] && document.activeElement !== inputs[i]) inputs[i].value = r[k]; });
  lyRefreshDirty(root);
}

function lyPlaceAll(root) { LY_ZONES.forEach((z) => lyPlace(root, z.key)); }

function lyScale(root) {
  const outer = $("[data-ly-outer]", root);
  if (!outer) return;
  const avail = Math.max(160, (outer.parentElement.clientWidth || 360) - 8);
  const s = Math.max(0.12, Math.min(avail / LY_CANVAS.w, 720 / LY_CANVAS.h));
  lyUi.scale = s;
  outer.style.width = `${LY_CANVAS.w * s}px`; outer.style.height = `${LY_CANVAS.h * s}px`;
  $("[data-ly-stage]", root).style.transform = `scale(${s})`;
}

function lySelect(root, key) {
  lyUi.selected = key;
  $$(".ly-zone", root).forEach((el) => el.classList.toggle("sel", el.dataset.zone === key));
  $$(".ly-values", root).forEach((el) => el.classList.toggle("sel", el.dataset.values === key));
}

function lyShowError(root, message) {
  lyUi.error = message;
  const el = $("[data-ly-error]", root);
  el.textContent = message || "";
  el.hidden = !message;
}

/* Glisser / redimensionner : événements pointeur (souris et toucher). */
function lyWireStage(root) {
  const stage = $("[data-ly-stage]", root);
  const guide = $(".ly-guide", stage);
  stage.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".ly-zone");
    if (!el) return;
    e.preventDefault();
    const key = el.dataset.zone;
    const r = lyUi.draft[key];
    const handle = e.target.dataset.h || "";
    if (lyUi.selected !== key) lySelect(root, key);
    const start = { x: e.clientX, y: e.clientY, r: Object.assign({}, r) };
    el.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const dx = (ev.clientX - start.x) / lyUi.scale, dy = (ev.clientY - start.y) / lyUi.scale;
      if (!handle) {
        r.x = Math.round(start.r.x + dx); r.y = Math.round(start.r.y + dy);
        const near = lyUi.snap && Math.abs(r.x + r.w / 2 - LY_CANVAS.w / 2) < 14;
        if (near) r.x = Math.round(LY_CANVAS.w / 2 - r.w / 2);
        guide.classList.toggle("show", near);
      } else {
        if (handle.includes("e")) r.w = Math.max(LY_MIN, Math.round(start.r.w + dx));
        if (handle.includes("s")) r.h = Math.max(LY_MIN, Math.round(start.r.h + dy));
        if (handle.includes("w")) { r.w = Math.max(LY_MIN, Math.round(start.r.w - dx)); r.x = start.r.x + start.r.w - r.w; }
        if (handle.includes("n")) { r.h = Math.max(LY_MIN, Math.round(start.r.h - dy)); r.y = start.r.y + start.r.h - r.h; }
      }
      lyPlace(root, key);
    };
    const up = () => {
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up);
      guide.classList.remove("show");
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  });
  stage.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    const el = e.target.closest(".ly-zone");
    if (!step || !el) return;
    e.preventDefault();
    const r = lyUi.draft[el.dataset.zone], d = e.shiftKey ? 10 : 1;
    r.x += step[0] * d; r.y += step[1] * d;
    lyPlace(root, el.dataset.zone);
  });
}

async function lySave(root) {
  if (lyUi.busy) return;
  lyUi.busy = true;
  lyShowError(root, null);
  const payload = lyCopy(lyUi.draft);
  try {
    const data = await api(lyUrl(lyUi.name, "layout"), jsonBody("PUT", payload));
    lyUi.data = data;
    lyUi.saved = lyCopy(payload);
    toast({ kind: "ok", title: "Agencement enregistré", body: lyUi.name });
    lyRefreshDirty(root);
  } catch (err) {
    lyShowError(root, err.message);
    toastError("Agencement refusé", err);
  } finally {
    lyUi.busy = false;
  }
}

/* Case « Afficher le badge » : masque ou remet la zone, sans toucher à badge_dest. */
function lyApplyBadge(root) {
  const on = lyBadgeOn();
  $$('[data-zone="badge_dest"], [data-values="badge_dest"]', root).forEach((el) => el.classList.toggle("off", !on));
  const box = $("[data-ly-badge]", root);
  if (box) box.checked = on;
  lyRefreshDirty(root);
}

function lyReset(root) {
  const before = lyCopy(lyUi.draft);
  // Les défauts ne portent que les rectangles : l'état du badge est conservé.
  lyUi.draft = Object.assign(lyCopy(lyUi.data.defaults), { badge_enabled: before.badge_enabled });
  lyShowError(root, null);
  lyPlaceAll(root);
  toast({ kind: "info", title: "Défauts rétablis", body: "Pas encore enregistrés.", undo: () => { lyUi.draft = before; lyPlaceAll(root); lyApplyBadge(root); } });
}

function lyWire(root) {
  lyWireStage(root);
  root.onclick = (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.hasAttribute("data-ly-save")) lySave(root);
    else if (t.hasAttribute("data-ly-reset")) lyReset(root);
    else if (t.hasAttribute("data-select")) lySelect(root, t.dataset.select);
  };
  root.onchange = (e) => {
    if (e.target.matches("[data-ly-safe]")) { lyUi.safe = e.target.checked; $(".ly-safe", root).classList.toggle("show", lyUi.safe); }
    else if (e.target.matches("[data-ly-snap]")) lyUi.snap = e.target.checked;
    else if (e.target.matches("[data-ly-badge]")) { lyUi.draft.badge_enabled = e.target.checked; lyApplyBadge(root); }
  };
  root.oninput = (e) => {
    const input = e.target.closest("[data-values] input");
    if (!input) return;
    const key = input.closest("[data-values]").dataset.values;
    if (input.value.trim() === "" || !Number.isFinite(Number(input.value))) return;
    lyUi.draft[key][input.dataset.k] = Math.round(Number(input.value));
    lyPlace(root, key);
  };
  root.onfocusin = (e) => {
    const holder = e.target.closest("[data-values]");
    if (holder && holder.dataset.values !== lyUi.selected) lySelect(root, holder.dataset.values);
  };
  window.addEventListener("resize", () => { if (lyUi.name && $("[data-ly-outer]", root)) lyScale(root); });
}

/* Image clé : lue par fetch pour afficher le détail français d'un 404. */
async function lyLoadFrame(name) {
  lyUi.frame = null; lyUi.frameError = null;
  try {
    const resp = await fetch(`/api/channels/${encodeURIComponent(name)}/keyframe`, { credentials: "same-origin" });
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (err) { /* pas de corps JSON */ }
      lyUi.frameError = typeof detail === "string" ? detail : JSON.stringify(detail);
      return;
    }
    lyUi.frame = URL.createObjectURL(await resp.blob());
  } catch (err) {
    lyUi.frameError = `Image clé illisible : ${err.message}`;
  }
}

async function lyOpen(body, name) {
  body.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`;
  Object.assign(lyUi, { name, data: null, draft: null, saved: null, error: null, pending: true, selected: LY_ZONES[0].key });
  try {
    const [data] = await Promise.all([api(lyUrl(name, "layout")), lyLoadFrame(name)]);
    if (lyUi.name !== name) return;
    // Style en letterbox : éditeur letterbox (layout-letterbox.js), même route et même image clé.
    if (data.mode === "letterbox") { await lbOpen(body, name); return; }
    lyUi.data = data;
    lyUi.draft = Object.fromEntries(LY_ZONES.map((z) => [z.key, lyCopy(data[z.key])]));
    lyUi.draft.badge_enabled = data.badge_enabled === true;
    lyUi.saved = lyCopy(lyUi.draft);
  } catch (err) {
    if (lyUi.name === name) { lyUi.name = null; body.innerHTML = emptyState("circle-alert", "Agencement illisible", err.message); }
    return;
  }
  lyUi.pending = false;
  if (currentScreen !== "channels" || lyName() !== name) return;
  body.innerHTML = lyHtml();
  lyScale(body); lyPlaceAll(body); lyWire(body);
}

/* Enveloppe de l'écran Styles : la sous-route /layout est à nous, le reste
   (liste, formulaire d'un preset) reste à channels.js. */
const lyChannelsRender = Screens.channels.render;
Screens.channels.render = function (body, store) {
  const section = body.closest(".screen");
  if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
  const name = lyName();
  if (!name) {
    if (lyUi.name) { lyUi.name = null; body.onclick = null; body.oninput = null; body.onchange = null; body.onfocusin = null; body.innerHTML = ""; }
    return lyChannelsRender.call(this, body, store);
  }
  if (lyUi.name === name) return;     // déjà ouvert : ne jamais écraser une édition en cours
  chUi.edit = null; chUi.html = "";
  lyOpen(body, name);
};
