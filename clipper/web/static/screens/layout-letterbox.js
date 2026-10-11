/* Éditeur d'agencement letterbox (TASK-3be3, format par défaut SPEC-6a47).
   Même route et même canevas 1080x1920 que l'éditeur stream split (layout.js,
   dont il reprend l'image clé, la mise à l'échelle, les classes ly-*) : ouvert
   par lyOpen quand le style est en letterbox. Zones = réglages existants du
   rendu letterbox : vidéo nette ([reframe] letterbox_top / letterbox_zoom),
   titre d'écran et bande des sous-titres ([reframe] letterbox_title_dest /
   letterbox_subtitle_dest, {} = zone déduite de la vidéo), pseudo de chaîne
   ([render] cta_handle_gap, si l'appel à l'abonnement est actif). Une valeur
   non modifiée reste celle du standard (config.toml) ; le serveur n'écrit
   dans le preset que ce qui en diffère. Aucune validation ici : reframe et
   render refusent, le serveur renvoie leur message (422). */
"use strict";

/* GEOMETRIE */
// Comme reframe._letterbox_geometry : fenêtre source de largeur paire
// round(src.w / zoom), mise à la largeur du canevas, hauteur paire.
const lbEven = (n) => { const r = Math.round(n); return r % 2 ? r - 1 : r; };
const lbHas = (d) => Boolean(d) && Object.keys(d).length > 0;

function lbRects(view, v, src) {
  const s = view.safe, gap = view.text_gap;
  const w = lbEven(src.w / v.letterbox_zoom);
  const video = { x: 0, y: v.letterbox_top, w: view.canvas.w, h: lbEven(src.h * view.canvas.w / w) };
  const box = (x0, y0, x1, y1) => ({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
  const rect = (d) => ({ x: d.x, y: d.y, w: d.w, h: d.h });
  return {
    video,
    title: lbHas(v.letterbox_title_dest) ? rect(v.letterbox_title_dest) : box(s.left, s.top, s.right, video.y - gap),
    subtitles: lbHas(v.letterbox_subtitle_dest) ? rect(v.letterbox_subtitle_dest)
      : box(s.left, video.y + video.h + gap, s.right, s.bottom - view.part_height - gap),
    part: box(s.left, s.bottom - view.part_height, s.right, s.bottom),
  };
}

// Rectangle de la vidéo nette (glissé ou redimensionné) -> réglages reframe :
// la hauteur donne le zoom (h = src.h * canevas.w * zoom / src.w), 2 décimales.
function lbFromVideoRect(view, r, src) {
  const zoom = Math.round(r.h * src.w / (src.h * view.canvas.w) * 100) / 100;
  return { letterbox_top: Math.round(r.y), letterbox_zoom: Math.max(1, zoom) };
}
/* FIN GEOMETRIE */

const LB_ZONES = [
  { key: "video", label: "Vidéo nette", icon: "monitor", kind: "video", fields: [["letterbox_top", "haut (y)", 1], ["letterbox_zoom", "zoom", 0.01]] },
  { key: "title", label: "Titre d'écran", icon: "type", kind: "title", dest: "letterbox_title_dest" },
  { key: "subtitles", label: "Sous-titres", icon: "captions", kind: "subs", dest: "letterbox_subtitle_dest" },
  { key: "pseudo", label: "Pseudo", icon: "user", kind: "pseudo", fields: [["cta_handle_gap", "écart sous le titre (px)", 1]] },
];
const LB_TITLE_SAMPLE = "TITRE D'ÉCRAN";
const lbUrl = (name) => `/api/channels/${encodeURIComponent(name)}/layout/letterbox`;

const lbUi = { view: null, draft: null, saved: null, src: { w: 1920, h: 1080 }, selected: "video", busy: false, resizeWired: false };

const lbZones = () => LB_ZONES.filter((z) => z.kind !== "pseudo" || lbPseudoShown());
const lbPseudoShown = () => Boolean(lbUi.view.cta.enabled && String(lbUi.view.cta.handle).trim() && lbUi.view.title_enabled);
const lbKeysOf = (z) => (z.dest ? [z.dest] : z.fields.map((f) => f[0]));
const lbSame = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const lbChanged = () => Object.keys(lbUi.draft).filter((k) => !lbSame(lbUi.draft[k], lbUi.saved[k]));

// Source supposée 1920 de large, au ratio de l'image clé (les images clés
// de scenes peuvent être réduites ; seul le ratio compte), 16:9 sans image.
function lbSourceSize(frameUrl) {
  return new Promise((resolve) => {
    if (!frameUrl) { resolve({ w: 1920, h: 1080 }); return; }
    const img = new Image();
    img.onload = () => resolve(img.naturalWidth > 0 ? { w: 1920, h: Math.round(1920 * img.naturalHeight / img.naturalWidth) } : { w: 1920, h: 1080 });
    img.onerror = () => resolve({ w: 1920, h: 1080 });
    img.src = frameUrl;
  });
}

function lbZoneInner(z) {
  if (z.kind === "video") return lyUi.frame ? `<img class="lb-net" src="${esc(lyUi.frame)}" alt="" draggable="false">` : `<span class="ly-fill"></span>`;
  if (z.kind === "title") return `<div class="lb-title"><span class="lb-title-box">${esc(LB_TITLE_SAMPLE)}</span></div>`;
  if (z.kind === "subs") return `<div class="ly-subs">${esc(LY_SAMPLE)}</div>`;
  return `<span class="lb-pseudo-txt">${esc(lbUi.view.cta.handle)}</span>`;
}

function lbStageHtml() {
  const s = lbUi.view.safe;
  const zones = lbZones().map((z) => {
    const handles = z.kind === "pseudo" ? "" : ["nw", "ne", "sw", "se"].map((h) => `<span class="ly-hdl ${h}" data-h="${h}"></span>`).join("");
    return `<div class="ly-zone lb-z-${z.kind}${z.key === lbUi.selected ? " sel" : ""}" data-zone="${z.key}" tabindex="0" role="group" aria-label="${esc(z.label)}">
      ${lbZoneInner(z)}<span class="ly-tag">${esc(z.label)}</span>${handles}</div>`;
  }).join("");
  return `<img class="ly-bg" src="${esc(lyUi.frame || "")}" alt="" ${lyUi.frame ? "" : "hidden"}>${zones}
    <div class="lb-part" data-lb-part><span>Partie N</span></div>
    <div class="ly-safe${lyUi.safe ? " show" : ""}" style="left:${s.left}px;top:${s.top}px;width:${s.right - s.left}px;height:${s.bottom - s.top}px"></div>`;
}

function lbStateHtml(z) {
  const changed = lbKeysOf(z).some((k) => !lbSame(lbUi.draft[k], lbUi.view.standard[k]));
  return changed ? `<span class="chip warn plain">modifié</span>` : `<span class="chip pending plain">standard</span>`;
}

function lbValuesHtml() {
  return lbZones().map((z) => {
    const inputs = z.dest
      ? ["x", "y", "w", "h"].map((k) => `<label>${k}<input class="input" type="number" inputmode="numeric" step="1" data-k="${k}"></label>`).join("")
      : z.fields.map(([key, label, step]) => `<label class="lb-wide">${esc(label)}<input class="input" type="number" inputmode="decimal" step="${step}" data-key="${key}"></label>`).join("");
    const note = z.dest ? `<p class="muted lb-hint">Standard : déduite de la vidéo nette. Déplacée ou redimensionnée, elle devient propre au style.</p>`
      : z.kind === "pseudo" ? `<p class="muted lb-hint">Toujours sous l'encadré du titre, au bas de sa zone : seul l'écart se règle.</p>` : "";
    return `<div class="ly-values${z.key === lbUi.selected ? " sel" : ""}" data-values="${z.key}">
      <button type="button" class="ly-values-head" data-select="${z.key}">${icon(z.icon, "i-sm")}<b>${esc(z.label)}</b><span data-lb-state>${lbStateHtml(z)}</span><span class="mono muted">${esc(lbKeysOf(z).join(" / "))}</span></button>
      <div class="ly-coords${z.dest ? "" : " lb-fields"}">${inputs}</div>${note}</div>`;
  }).join("");
}

function lbHtml() {
  const frameNote = lyUi.frame
    ? `<p class="muted ly-note">Aperçu sur une image clé d'une vidéo du style : fond flou, vidéo nette au zoom réglé.</p>`
    : `<p class="reason bad ly-note" role="alert">${esc(lyUi.frameError || "Aucune image clé disponible.")}</p>`;
  return `<div class="ly">
    <div class="toolbar"><a class="btn" data-variant="ghost" data-size="sm" href="#/styles/${encodeURIComponent(lyUi.name)}">${icon("chevron-left", "i-xs")}Retour au style</a>
      <h2 class="chan-title">Éditeur d'agencement <span class="mono muted">${esc(lyUi.name)} · letterbox</span></h2><span class="grow"></span>
      <span class="muted ly-dirty" data-ly-dirty></span>
      <button type="button" class="btn" data-variant="ghost" data-size="sm" data-lb-standard>${icon("rotate-ccw", "i-xs")}Revenir au standard</button>
      <button type="button" class="btn" data-lb-save>Enregistrer</button></div>
    <p class="reason bad" data-ly-error role="alert" hidden></p>
    <div class="ly-grid">
      <div class="ly-main">
        <div class="ly-tools">
          <label class="ly-check"><input type="checkbox" data-ly-safe ${lyUi.safe ? "checked" : ""}> Zone sûre TikTok</label></div>
        <div class="ly-outer" data-ly-outer><div class="ly-stage" data-ly-stage>${lbStageHtml()}</div></div>
        ${frameNote}
        <p class="muted ly-note">Glisse une zone pour la déplacer, tire un coin pour la redimensionner (souris ou doigt). La vidéo nette ne bouge que verticalement : sa hauteur règle le zoom. Flèches : 1 px, Maj : 10 px.</p>
      </div>
      <aside class="ly-side panel panel-pad" data-ly-values>${lbValuesHtml()}</aside>
    </div></div>`;
}

function lbRefresh(root) {
  const rects = lbRects(lbUi.view, lbUi.draft, lbUi.src);
  const place = (el, r) => { el.style.left = `${r.x}px`; el.style.top = `${r.y}px`; el.style.width = `${r.w}px`; el.style.height = `${Math.max(0, r.h)}px`; };
  ["video", "title", "subtitles"].forEach((key) => { const el = $(`.ly-zone[data-zone="${key}"]`, root); if (el) place(el, rects[key]); });
  place($("[data-lb-part]", root), rects.part);
  // Vidéo nette : l'image clé entière à la largeur canevas x zoom, centrée (fenêtre centrale de la source).
  const net = $(".lb-net", root);
  if (net) {
    const width = lbUi.view.canvas.w * lbUi.draft.letterbox_zoom;
    Object.assign(net.style, { width: `${width}px`, left: `${(lbUi.view.canvas.w - width) / 2}px` });
  }
  // Titre : encadré collé en bas de sa zone à title_lift, remonté pour le pseudo s'il est affiché.
  const pseudoH = Math.round(lbUi.view.cta.font_size * 1.0);
  const reserved = lbPseudoShown() ? Number(lbUi.draft.cta_handle_gap) + pseudoH : 0;
  const titleBox = $(".lb-title", root);
  if (titleBox) titleBox.style.paddingBottom = `${lbUi.view.title_lift + reserved}px`;
  const pseudo = $('.ly-zone[data-zone="pseudo"]', root);
  if (pseudo) place(pseudo, { x: rects.title.x, y: rects.title.y + rects.title.h - lbUi.view.title_lift - pseudoH, w: rects.title.w, h: pseudoH });
  // Valeurs numériques (sauf le champ en cours de saisie) et états standard / modifié.
  lbZones().forEach((z) => {
    const holder = $(`[data-values="${z.key}"]`, root);
    if (!holder) return;
    const r = rects[z.key];
    $$("input", holder).forEach((input) => {
      if (document.activeElement === input) return;
      input.value = input.dataset.k ? r[input.dataset.k] : lbUi.draft[input.dataset.key];
    });
    $("[data-lb-state]", holder).innerHTML = lbStateHtml(z);
  });
  const dirty = $("[data-ly-dirty]", root);
  if (dirty) dirty.textContent = lbChanged().length ? "modifié, non enregistré" : "";
}

function lbSelect(root, key) {
  lbUi.selected = key;
  $$(".ly-zone", root).forEach((el) => el.classList.toggle("sel", el.dataset.zone === key));
  $$(".ly-values", root).forEach((el) => el.classList.toggle("sel", el.dataset.values === key));
}

// Applique le rectangle ``r`` (pixels du canevas) à la zone ``key``.
function lbApply(key, r) {
  if (key === "video") Object.assign(lbUi.draft, lbFromVideoRect(lbUi.view, r, lbUi.src));
  else {
    const z = LB_ZONES.find((zone) => zone.key === key);
    lbUi.draft[z.dest] = { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) };
  }
}

function lbWireStage(root) {
  const stage = $("[data-ly-stage]", root);
  stage.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".ly-zone");
    if (!el) return;
    e.preventDefault();
    const key = el.dataset.zone;
    if (lbUi.selected !== key) lbSelect(root, key);
    if (key === "pseudo") return;
    const handle = e.target.dataset.h || "";
    const start = { x: e.clientX, y: e.clientY, r: lbRects(lbUi.view, lbUi.draft, lbUi.src)[key] };
    el.setPointerCapture(e.pointerId);
    const move = (ev) => {
      const dx = (ev.clientX - start.x) / lyUi.scale, dy = (ev.clientY - start.y) / lyUi.scale;
      const r = Object.assign({}, start.r);
      if (!handle) { r.y = start.r.y + dy; if (key !== "video") r.x = start.r.x + dx; }
      else {
        if (key !== "video" && handle.includes("e")) r.w = Math.max(LY_MIN, start.r.w + dx);
        if (key !== "video" && handle.includes("w")) { r.w = Math.max(LY_MIN, start.r.w - dx); r.x = start.r.x + start.r.w - r.w; }
        if (handle.includes("s")) r.h = Math.max(LY_MIN, start.r.h + dy);
        if (handle.includes("n")) { r.h = Math.max(LY_MIN, start.r.h - dy); r.y = start.r.y + start.r.h - r.h; }
      }
      lbApply(key, r);
      lbRefresh(root);
    };
    const up = () => { el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up); };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  });
  stage.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
    const el = e.target.closest(".ly-zone");
    if (!step || !el || el.dataset.zone === "pseudo") return;
    e.preventDefault();
    const key = el.dataset.zone, d = e.shiftKey ? 10 : 1;
    const r = Object.assign({}, lbRects(lbUi.view, lbUi.draft, lbUi.src)[key]);
    if (key !== "video") r.x += step[0] * d;
    r.y += step[1] * d;
    lbApply(key, r);
    lbRefresh(root);
  });
}

function lbLoad(view) {
  lbUi.view = view;
  lbUi.draft = lyCopy(view.values);
  lbUi.saved = lyCopy(view.values);
}

async function lbSave(root) {
  if (lbUi.busy) return;
  const changed = lbChanged();
  if (!changed.length) { toast({ kind: "info", title: "Rien à enregistrer", body: "Aucune valeur modifiée." }); return; }
  lbUi.busy = true;
  lyShowError(root, null);
  try {
    // Seules les valeurs modifiées partent ; le serveur retire du preset celles revenues au standard.
    const payload = Object.fromEntries(changed.map((k) => [k, lbUi.draft[k]]));
    lbLoad(await api(lbUrl(lyUi.name), jsonBody("PUT", payload)));
    toast({ kind: "ok", title: "Agencement enregistré", body: lyUi.name });
    lbRefresh(root);
  } catch (err) {
    lyShowError(root, err.message);
    toastError("Agencement refusé", err);
  } finally {
    lbUi.busy = false;
  }
}

async function lbStandard(root) {
  if (lbUi.busy) return;
  lbUi.busy = true;
  lyShowError(root, null);
  const before = Object.fromEntries(lbUi.view.overridden.map((k) => [k, lbUi.view.values[k]]));
  try {
    lbLoad(await api(lbUrl(lyUi.name), { method: "DELETE" }));
    lbRedraw(root);
    toast({
      kind: "ok", title: "Agencement standard rétabli", body: "Les réglages propres au style sont effacés.",
      undo: Object.keys(before).length ? async () => {
        try { lbLoad(await api(lbUrl(lyUi.name), jsonBody("PUT", before))); lbRedraw(root); }
        catch (err) { toastError("Annulation impossible", err); }
      } : null,
    });
  } catch (err) {
    lyShowError(root, err.message);
    toastError("Retour au standard refusé", err);
  } finally {
    lbUi.busy = false;
  }
}

// Le pseudo apparaît ou non selon le style : zones et valeurs redessinées.
function lbRedraw(root) {
  $("[data-ly-stage]", root).innerHTML = lbStageHtml();
  $("[data-ly-values]", root).innerHTML = lbValuesHtml();
  lbRefresh(root);
}

function lbWire(root) {
  lbWireStage(root);
  root.onclick = (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    if (t.hasAttribute("data-lb-save")) lbSave(root);
    else if (t.hasAttribute("data-lb-standard")) lbStandard(root);
    else if (t.hasAttribute("data-select")) lbSelect(root, t.dataset.select);
  };
  root.onchange = (e) => {
    if (e.target.matches("[data-ly-safe]")) { lyUi.safe = e.target.checked; $(".ly-safe", root).classList.toggle("show", lyUi.safe); }
  };
  root.oninput = (e) => {
    const input = e.target.closest("[data-values] input");
    if (!input || input.value.trim() === "" || !Number.isFinite(Number(input.value))) return;
    const key = input.closest("[data-values]").dataset.values;
    const value = Number(input.value);
    if (input.dataset.key) lbUi.draft[input.dataset.key] = input.dataset.key === "letterbox_zoom" ? value : Math.round(value);
    else {
      const r = Object.assign({}, lbRects(lbUi.view, lbUi.draft, lbUi.src)[key]);
      r[input.dataset.k] = Math.round(value);
      lbApply(key, r);
    }
    lbRefresh(root);
  };
  root.onfocusin = (e) => {
    const holder = e.target.closest("[data-values]");
    if (holder && holder.dataset.values !== lbUi.selected) lbSelect(root, holder.dataset.values);
  };
  if (!lbUi.resizeWired) {
    lbUi.resizeWired = true;
    window.addEventListener("resize", () => { if (lyUi.name && lbUi.view && $("[data-lb-save]", root)) lyScale(root); });
  }
}

/* Appelé par lyOpen (layout.js) quand /layout répond mode = "letterbox" ;
   l'image clé est déjà chargée dans lyUi.frame. */
async function lbOpen(body, name) {
  const [view, src] = await Promise.all([api(lbUrl(name)), lbSourceSize(lyUi.frame)]);
  if (lyUi.name !== name) return;
  lbLoad(view);
  lbUi.src = src;
  lbUi.selected = "video";
  lyUi.pending = false;
  if (currentScreen !== "channels" || lyName() !== name) return;
  body.innerHTML = lbHtml();
  lyScale(body); lbRefresh(body); lbWire(body);
}
