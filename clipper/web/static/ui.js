/* Briques d'interface partagees : format, icones, rapprochement du DOM (morph), toasts Basecoat avec
   « Annuler », dialogues natifs (openDialog, confirmDialog), tiroir (drawer), et openPanel (overlay et panneau en <div> :
   « Nouveau style », alerte reseau). */
"use strict";

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Toutes les heures affichees par la console sont celles de Paris (Europe/Paris : +02:00 l'ete, +01:00 l'hiver),
// jamais le fuseau du navigateur ni un decalage fixe.
const CLIPPER_TZ = "Europe/Paris";
const fmtParis = (iso, opts) => new Date(iso).toLocaleString("fr-FR", Object.assign({ timeZone: CLIPPER_TZ }, opts));
const fr = (n, d) => Number(n).toLocaleString("fr-FR", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });

/* Confiance du jury (SPEC-73d0 R4) : agregat (mediane des confiances finales) puis
   confiance finale de chaque juge. Sans jury : le dit, sans valeur inventee. */
function juryConfidenceHtml(confidence, judges) {
  if (confidence == null) return `<span class="muted">non évaluée (pas de jury pour ce moment)</span>`;
  const per = Object.entries(judges || {}).map(([name, c]) => `<span class="tag">${esc(name)} ${fr(c)} %</span>`).join("");
  return `<b class="num">${fr(confidence)} %</b>${per ? `<span class="row wrap" style="gap:6px;margin-top:6px">${per}</span>` : ""}`;
}

// Delai pendant lequel « Annuler » reste proposé dans un toast (SPEC-c100 T4).
const UNDO_MS = 5000;

/* Remplace chaque <span data-icon="nom"> par l'icone inline correspondante. */
function hydrateIcons(root) {
  $$("[data-icon]", root).forEach((el) => {
    el.outerHTML = icon(el.dataset.icon);
  });
}

/* ---------- Rapprochement du DOM (ADR-a308 §3) ----------
   Remplace `el.innerHTML = html` : Idiomorph rapproche le DOM existant du HTML calcule, donc focus, defilement,
   lecteur video et hauteurs survivent a un evenement SSE. Idiomorph est vendored et charge avant ce script :
   son absence est une erreur de la page, jamais un repli silencieux (ADR-ad2e). */
function morph(el, html) {
  if (typeof Idiomorph === "undefined") throw new Error("Idiomorph absent : /static/vendor/idiomorph.min.js n'est pas chargé");
  Idiomorph.morph(el, html, { morphStyle: "innerHTML" });
}

/* ---------- Toasts (Basecoat .toaster, API toast() conservee) ----------
   Le markup est le notre (classes Basecoat) et porte data-toast-initialized : basecoat.all.min.js laisse alors la
   minuterie, le survol et la fermeture a cette fonction (son toaster.toast() attend des onclick en chaine). */
const TOAST_CATEGORY = { ok: "success", bad: "error", warn: "warning", info: "info" };
const TOAST_ICON = { ok: "circle-check", bad: "circle-x", warn: "triangle-alert", info: "info" };

function toastHtml(o) {
  const cat = TOAST_CATEGORY[o.kind] || "info";
  const undo = o.undo ? `<button type="button" class="btn" data-size="sm" data-toast-cancel>${icon("undo-2", "i-xs")}Annuler</button>` : "";
  const act = o.action ? `<button type="button" class="btn" data-variant="outline" data-size="sm" data-toast-action>${esc(o.action.label)}</button>` : "";
  return `<div class="toast" role="${o.kind === "bad" ? "alert" : "status"}" aria-atomic="true" data-category="${cat}" data-duration="${o.ms}" data-toast-initialized="true">
    <div class="toast-content">${icon(TOAST_ICON[o.kind] || "info")}
      <section><h2>${esc(o.title)}</h2>${o.body ? `<p>${esc(o.body)}</p>` : ""}</section>
      <footer>${undo}${act}<button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-close aria-label="Fermer">${icon("x", "i-xs")}</button></footer>
    </div></div>`;
}

function toast(opts) {
  const o = Object.assign({ kind: "info", title: "", body: "", undo: null, action: null, ms: 5200 }, opts);
  if (o.undo) o.ms = UNDO_MS;
  const tpl = document.createElement("template");
  tpl.innerHTML = toastHtml(o).trim();
  const el = tpl.content.firstElementChild;
  const host = $("#toaster");
  host.appendChild(el);
  let timer = setTimeout(close, o.ms);
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    el.setAttribute("aria-hidden", "true"); // Basecoat replie le toast en transition, puis on le retire
    const gone = () => el.remove();
    el.addEventListener("transitionend", gone, { once: true });
    setTimeout(gone, 400);
  }
  el.addEventListener("mouseenter", () => clearTimeout(timer));
  el.addEventListener("mouseleave", () => { timer = setTimeout(close, 2000); });
  el.querySelector("[data-close]").onclick = close;
  if (o.undo) el.querySelector("[data-toast-cancel]").onclick = () => { o.undo(); close(); };
  if (o.action) el.querySelector("[data-toast-action]").onclick = () => { o.action.run(); close(); };
  const all = $$(".toast:not([aria-hidden=\"true\"])", host);
  if (all.length > 4) all[0].remove();
  return close;
}

/* Erreur d'API : toast rouge portant le detail en francais envoyé par le serveur (T2). */
function toastError(title, err) {
  // 409 « la vidéo n'a pas de style » (approbation, publication) : le choix du style se fait depuis l'erreur.
  const needs = err && err.body && err.body.needs_channel;
  toast({
    kind: "bad", title, body: err && err.message ? err.message : String(err), ms: 7000,
    action: needs ? { label: "Attribuer un style", run: () => openAssignChannel(err.body.video_id, err.body.channels) } : null,
  });
}

/* ---------- Dialogues natifs (<dialog>, SPEC-7715 T10) ----------
   openDialog(html, opts) cree un <dialog class="dialog"> dans #layers, l'ouvre en modal et le retire du DOM a la fermeture. Fermeture : Echap (natif), clic sur
   le fond, tout bouton [data-dismiss], form method="dialog". `html` = header / section / footer (markup Basecoat) ;
   un `html` qui est lui-meme un <form> devient le cadre du dialogue (ses header / section / footer gardent leur mise en page). */
function openDialog(html, opts) {
  const o = Object.assign({ size: null, role: null, label: null, closeButton: true, onOpen: null, onClose: null }, opts);
  const dlg = document.createElement("dialog");
  dlg.className = "dialog";
  if (o.role) dlg.setAttribute("role", o.role);
  if (o.label) dlg.setAttribute("aria-label", o.label);
  dlg.innerHTML = html;
  if (!(dlg.children.length === 1 && dlg.firstElementChild.tagName === "FORM")) dlg.innerHTML = `<div>${html}</div>`;
  const box = dlg.firstElementChild;
  if (o.size) box.dataset.size = o.size;
  if (o.closeButton) box.insertAdjacentHTML("beforeend", `<button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-dismiss aria-label="Fermer">${icon("x")}</button>`);
  $("#layers").appendChild(dlg);
  $$("[data-dismiss]", dlg).forEach((b) => (b.onclick = () => dlg.close()));
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); }); // clic sur le fond (hors du cadre)
  dlg.addEventListener("close", () => {
    if (o.onClose) o.onClose(dlg);
    dlg.remove();
  });
  dlg.showModal();
  if (o.onOpen) {
    try { o.onOpen(dlg); } catch (err) { toastError("Fenêtre incomplète", err); }
  }
  return dlg;
}

/* Confirmation d'une action destructive ou coûteuse sans inverse (T4).
   Retourne une promesse : true si l'utilisateur confirme, false sinon (Échap, fond, Annuler). */
function confirmDialog(opts) {
  const o = Object.assign({ title: "Confirmer ?", body: "", confirmLabel: "Confirmer", danger: true }, opts);
  return new Promise((resolve) => {
    let answered = false;
    const answer = (v) => { if (!answered) { answered = true; resolve(v); } };
    const dlg = openDialog(`
      <header><h2>${esc(o.title)}</h2>${o.body ? `<p>${esc(o.body)}</p>` : ""}</header>
      <footer>
        <button type="button" class="btn" data-variant="outline" data-dismiss>Annuler</button>
        <button type="button" class="btn"${o.danger ? ' data-variant="destructive"' : ""} data-confirm>${esc(o.confirmLabel)}</button>
      </footer>`, { size: "sm", role: "alertdialog", closeButton: false, onClose: () => answer(false) });
    $("[data-confirm]", dlg).onclick = () => { answer(true); dlg.close(); };
    setTimeout(() => $("[data-confirm]", dlg).focus(), 30);
  });
}

/* ---------- Tiroir (T10 : fiche ouverte par-dessus la liste, l'ecran garde son contexte) ----------
   drawer.open(html, { onOpen }) rapproche #drawer du HTML (drawer-head / drawer-body / drawer-foot), l'affiche avec
   son fond ; Echap (app.js), clic sur le fond et tout [data-drawer-close] ferment. La fermeture emet
   « clipper:drawer-close » pour que l'ecran remette son URL (#/...) en ordre. */
const drawer = {
  open(html, opts) {
    const o = Object.assign({ onOpen: null }, opts);
    const el = $("#drawer");
    el.hidden = false;
    morph(el, html);
    hydrateIcons(el);
    $("#drawer-scrim").classList.add("show");
    el.classList.add("show");
    $$("[data-drawer-close]", el).forEach((b) => (b.onclick = () => drawer.close()));
    $("#drawer-scrim").onclick = () => drawer.close();
    if (o.onOpen) {
      try { o.onOpen(el); } catch (err) { toastError("Fiche incomplète", err); }
    }
    setTimeout(() => { const f = $("[data-drawer-close]", el); if (f) f.focus(); }, 60);
    return el;
  },
  close() {
    if (!drawer.isOpen()) return;
    $("#drawer-scrim").classList.remove("show");
    const el = $("#drawer");
    el.classList.remove("show");
    setTimeout(() => { if (!el.classList.contains("show")) el.hidden = true; }, 240);
    document.dispatchEvent(new CustomEvent("clipper:drawer-close"));
  },
  isOpen() { return $("#drawer").classList.contains("show"); },
};

/* ---------- Overlay et panneau en <div> (« Nouveau style », alerte reseau) ----------
   Le contenu injecte vient de l'appelant ; les regles .overlay / .modal sont dans le socle de console.css. */
let closeCurrent = null;
function showOverlay(onClose) {
  const ov = $("#overlay");
  ov.hidden = false;
  const reveal = () => ov.classList.add("show");
  requestAnimationFrame(reveal);
  setTimeout(reveal, 60); // requestAnimationFrame est suspendu dans un onglet masque : le fond ne doit pas rester invisible
  ov.onclick = () => closeLayer();
  closeCurrent = onClose;
}
function closeLayer() {
  const ov = $("#overlay");
  ov.classList.remove("show");
  setTimeout(() => { if (!closeCurrent) ov.hidden = true; }, 220);
  if (closeCurrent) { const f = closeCurrent; closeCurrent = null; f(); }
}
function openPanel(cls, html, onOpen) {
  if (closeCurrent) closeLayer();
  const el = document.createElement("div");
  el.className = cls;
  el.setAttribute("role", "dialog");
  el.setAttribute("aria-modal", "true");
  // basecoat.all.min.js attache son composant « drawer » (prevu pour un <dialog>) a tout .drawer ajoute au DOM :
  // marque comme deja initialise, il le laisse tranquille.
  el.dataset.drawerInitialized = "true";
  el.innerHTML = html;
  document.body.appendChild(el);
  showOverlay(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 320); });
  const reveal = () => el.classList.add("show");
  requestAnimationFrame(() => requestAnimationFrame(reveal));
  setTimeout(reveal, 80); // meme garde-fou : le panneau (opacity 0 tant que .show manque) s'affiche toujours
  $$("[data-dismiss]", el).forEach((b) => (b.onclick = closeLayer));
  if (onOpen) {
    try { onOpen(el); } catch (err) { toastError("Fenêtre incomplète", err); }
  }
  return el;
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && closeCurrent) closeLayer(); });

/* ---------- Garde réseau au clic (TASK-120a) ----------
   Une seule source : la dernière valeur de /api/network relevée par app.js (paintNetwork -> setNetLast).
   Toute action qui publie ou programme passe par netGuard() : ok=false ouvre une fenêtre d'alerte (rien
   n'est lancé sans « Continuer quand même », absent si le serveur bloque le navigateur) ; ok=true ou ok=null
   (inconnu) : aucune fenêtre. Aucune alerte quand le réseau change, seulement au clic. Le refus serveur
   (clipper.browser) reste la vraie barrière. */
let netLast = null;
function setNetLast(net) { netLast = net || null; }

// null : pas de fenêtre ; "block" : le serveur refusera, on ne propose que Fermer ; "ask" : Continuer possible.
function netAlertMode(net) {
  if (!net || net.ok !== false) return null;
  return net.block_browser ? "block" : "ask";
}

function netAlertDialog(net, mode) {
  return new Promise((resolve) => {
    const detected = net.country_name ? `${net.country_name} (${net.country})` : (net.country || "inconnu");
    const expected = net.expected_country_name || net.expected_country || "inconnu";
    const root = document.createElement("div");
    root.className = "net-alert";
    root.innerHTML = `<div class="net-alert-back"></div>
      <div class="modal modal-confirm net-alert-box show" role="alertdialog" aria-modal="true">
        <div class="modal-head"><h2>IP hors du pays attendu</h2></div>
        <div class="modal-body">
          <p>Pays détecté : <b>${esc(detected)}</b><br>Pays attendu : <b>${esc(expected)}</b></p>
          <p class="muted">${mode === "block"
    ? "La publication sera refusée : Clipper ne pilote pas le navigateur depuis cette IP. Change de réseau (VPN) puis recommence."
    : "La publication est risquée : le compte peut être signalé ou bloqué depuis cette IP."}</p>
        </div>
        <div class="modal-foot">
          ${mode === "block"
    ? `<button type="button" class="btn btn-primary" data-net-close>Fermer</button>`
    : `<button type="button" class="btn btn-ghost" data-net-cancel>Annuler</button>
          <button type="button" class="btn btn-bad" data-net-go>Continuer quand même</button>`}
        </div>
      </div>`;
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      root.remove();
      resolve(v);
    };
    const onKey = (e) => { if (e.key === "Escape") finish(false); };
    document.addEventListener("keydown", onKey);
    document.body.appendChild(root);
    const bind = (sel, v) => { const b = root.querySelector(sel); if (b) b.onclick = () => finish(v); };
    bind("[data-net-close]", false);
    bind("[data-net-cancel]", false);
    bind("[data-net-go]", true);
    root.querySelector(".net-alert-back").onclick = () => finish(false);
    const first = root.querySelector("[data-net-close], [data-net-cancel]");
    if (first) setTimeout(() => first.focus(), 30);
  });
}

/* true : l'action peut partir. */
async function netGuard() {
  if (netLast === null) {
    try { netLast = await api("/api/network"); } catch (err) { netLast = null; }
  }
  const mode = netAlertMode(netLast);
  if (!mode) return true;
  return netAlertDialog(netLast, mode);
}

/* ---------- Presse-papiers ---------- */
function copyText(text, what) {
  const done = () => toast({ kind: "ok", title: `${what} copiée`, ms: 2600 });
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, (e) => toastError("Copie impossible", e));
  else {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { toastError("Copie impossible", e); }
    ta.remove();
  }
}
