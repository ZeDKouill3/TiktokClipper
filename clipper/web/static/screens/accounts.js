/* Ecran « Comptes » (SPEC-6fa4 R6, v3 : ADR-a308, SPEC-7715 E10). Pense-bete local des comptes (TikTok, YouTube,
   e-mail...) : la liste vient de GET /api/accounts et ne porte JAMAIS de mot de passe (has_password seulement). Un mot
   de passe n'est demandé à GET /api/accounts/<id>/password qu'au clic (Copier ou Afficher) ; il n'est ni mémorisé dans
   le navigateur (aucun stockage local), ni gardé après son masquage (30 s). Le générateur est côté serveur
   (POST /api/accounts/generate). Aucune logique de coffre ici : les erreurs du serveur (403, 422, 503) sont affichées
   telles quelles. Table : une ligne par compte, au plus 3 boutons visibles, le reste dans le menu « … ». L'édition
   s'ouvre dans le tiroir partagé (drawer, ui.js) ; l'écran se rapproche par morph(). Charge après screens.js dont il
   complète Screens. */
"use strict";

const ACC_REVEAL_MS = 30 * 1000; // un mot de passe affiché est masqué à nouveau après 30 s
const ACC_COPIED_MS = 1500;
const ACC_LOCAL_HOSTS = ["127.0.0.1", "localhost"];
const ACC_PLATFORMS = ["TikTok", "YouTube", "Twitch", "E-mail"];
// Service de publication d'un compte (SPEC-5e50 R1) : choisi à la création, ne change plus ensuite.
const ACC_SERVICES = { tiktok: "TikTok", youtube: "YouTube" };
// Créneaux de publication réguliers d'un compte (SPEC-6076 R2) : jour + heure locale, dans le fuseau du compte.
const ACC_DAYS = [["mon", "Lundi"], ["tue", "Mardi"], ["wed", "Mercredi"], ["thu", "Jeudi"], ["fri", "Vendredi"], ["sat", "Samedi"], ["sun", "Dimanche"]];
const ACC_DEFAULT_TZ = "Europe/Paris";
const accDayLabel = (k) => (ACC_DAYS.find(([d]) => d === k) || [k, k])[1];
const accService = (a) => ACC_SERVICES[a.service] ? a.service : "tiktok";

// revealed : id -> { value, timer } ; menu : id de la ligne dont le menu « … » est ouvert ; gen : générateur autonome.
const accUi = { list: null, error: null, loading: null, revealed: {}, menu: null, gen: { length: "", symbols: false, ambiguous: false, value: "" } };

const accIsRemote = () => !ACC_LOCAL_HOSTS.includes(location.hostname);

/* ---------- Données ---------- */

function accLoad() {
  if (accUi.loading) return accUi.loading;
  accUi.loading = (async () => {
    try {
      accUi.list = await api("/api/accounts");
      accUi.error = null;
    } catch (err) {
      accUi.error = err;
    } finally {
      accUi.loading = null;
    }
    if (currentScreen === "accounts") renderCurrent();
  })();
  return accUi.loading;
}

function accHide(id) {
  const shown = accUi.revealed[id];
  if (shown) clearTimeout(shown.timer);
  delete accUi.revealed[id];
}

function accHideAll() {
  Object.keys(accUi.revealed).forEach(accHide);
  accUi.gen.value = "";
  accUi.menu = null;
}

window.addEventListener("hashchange", () => {
  if (!/^#\/?accounts/.test(location.hash)) accHideAll(); // plus aucun secret en mémoire hors de l'écran
  else accLoad(); // la connexion TikTok de chaque compte est vérifiée à l'ouverture de l'écran (SPEC-00d1 R2)
});

// La fenêtre de connexion fermée, on revient sur la console : l'état est relu (le serveur le revérifie).
window.addEventListener("focus", () => { if (currentScreen === "accounts" && !accIsRemote()) accLoad(); });

// Le menu « … » se ferme au clic ailleurs (comme « Relancer depuis… » de Vidéos).
document.addEventListener("click", (e) => {
  if (accUi.menu && currentScreen === "accounts" && !e.target.closest(".acc-menu-wrap")) {
    accUi.menu = null;
    renderCurrent();
  }
});

/* ---------- Presse-papiers ---------- */

async function accCopy(text, button) {
  if (!navigator.clipboard) {
    toastError("Copie impossible", new Error("le presse-papiers n'est disponible que sur 127.0.0.1 ou localhost"));
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
  } catch (err) {
    toastError("Copie impossible", err);
    return;
  }
  if (!button) return;
  const label = button.innerHTML;
  button.innerHTML = `${icon("check", "i-xs")}Copié`;
  button.classList.add("acc-copied");
  setTimeout(() => { button.innerHTML = label; button.classList.remove("acc-copied"); }, ACC_COPIED_MS);
}

async function accFetchPassword(id) {
  return (await api(`/api/accounts/${encodeURIComponent(id)}/password`)).password;
}

/* ---------- Générateur (côté serveur) ---------- */

function accGenOptions(root) {
  const raw = $("[data-gen-length]", root).value.trim();
  const body = { symbols: $("[data-gen-symbols]", root).checked, avoid_ambiguous: $("[data-gen-ambiguous]", root).checked };
  if (raw !== "") body.length = Number(raw);
  return body;
}

async function accGenerate(root) {
  try {
    return (await api("/api/accounts/generate", jsonBody("POST", accGenOptions(root)))).password;
  } catch (err) {
    toastError("Génération impossible", err);
    return null;
  }
}

function accGenControls(state) {
  return `<div class="acc-gen-opts">
    <div class="field acc-gen-length"><label>Longueur<input class="input" data-gen-length type="number" min="12" max="64" step="1" placeholder="20" value="${esc(state.length)}"></label></div>
    <label class="acc-check"><input type="checkbox" data-gen-symbols${state.symbols ? " checked" : ""}> Symboles</label>
    <label class="acc-check"><input type="checkbox" data-gen-ambiguous${state.ambiguous ? " checked" : ""}> Sans caractères ambigus</label>
  </div>`;
}

function accGenPanel() {
  const g = accUi.gen;
  return `<section class="card acc-gen">
    <div class="acc-gen-body"><div class="section-head"><h2>Générateur de mot de passe</h2></div>${accGenControls(g)}
      <div class="acc-gen-out">
        <input class="input mono" data-gen-out type="text" readonly value="${esc(g.value)}" placeholder="12 à 64 caractères" aria-label="Mot de passe généré" autocomplete="off">
        <button type="button" class="btn" data-variant="outline" data-gen-make>${icon("refresh-cw")}Générer</button>
        <button type="button" class="btn" data-variant="outline" data-gen-copy${g.value ? "" : " disabled"}>${icon("copy")}Copier</button>
      </div></div></section>`;
}

function accWireGenPanel(body) {
  const root = $(".acc-gen", body);
  const sync = () => {
    accUi.gen.length = $("[data-gen-length]", root).value;
    accUi.gen.symbols = $("[data-gen-symbols]", root).checked;
    accUi.gen.ambiguous = $("[data-gen-ambiguous]", root).checked;
  };
  // onchange (et non addEventListener) : le morph garde le même nœud d'un rendu à l'autre, pas de doublon.
  $$("input", root).forEach((el) => (el.onchange = sync));
  $("[data-gen-make]", root).onclick = async () => {
    sync();
    const value = await accGenerate(root);
    if (value === null) return;
    accUi.gen.value = value;
    $("[data-gen-out]", root).value = value;
    $("[data-gen-copy]", root).disabled = false;
  };
  $("[data-gen-copy]", root).onclick = (e) => accCopy(accUi.gen.value, e.currentTarget);
}

/* ---------- Profil du navigateur (SPEC-9225 R1) ---------- */

function accBrowserState(a) {
  const b = a.browser;
  if (!b) return "";
  if (b.error) return `<div class="acc-line bad">Profil du navigateur illisible : ${esc(b.error)}</div>`;
  const when = b.modified_at ? ` · ${esc(fmtParis(b.modified_at, { dateStyle: "medium", timeStyle: "short" }))}` : "";
  return `<div class="acc-line" data-acc-browser-state>Profil : ${b.present ? `<b>présent</b>${when}` : "<b>absent</b> (jamais connecté)"}</div>`;
}

/* Après « Se connecter » : la console revérifie la connexion à la fermeture de la fenêtre ; on relit l'écran
   jusqu'à ce que le compte soit prêt (ou 10 min), sans quitter l'écran Comptes. */
const ACC_WATCH_MS = 4000;
const ACC_WATCH_MAX = 150;
function accWatchLogin(id, n = 0) {
  setTimeout(async () => {
    if (currentScreen !== "accounts" || n >= ACC_WATCH_MAX) return;
    await accLoad();
    const a = accUi.list.find((x) => x.id === id);
    if (a && !a.ready_to_publish) accWatchLogin(id, n + 1);
  }, ACC_WATCH_MS);
}

async function accBrowserLogin(account, button) {
  button.disabled = true;
  try {
    await api(`/api/accounts/${encodeURIComponent(account.id)}/browser/login`, jsonBody("POST", {}));
    toast({ kind: "ok", title: "Navigateur ouvert", body: `Connecte-toi à la main dans la fenêtre Chrome, puis ferme-la (${account.label}).` });
    await accLoad();
    accWatchLogin(account.id);
  } catch (err) {
    toastError("Ouverture du navigateur impossible", err);
  } finally {
    button.disabled = false;
  }
}

/* ---------- Compte de publication (SPEC-00d1) ---------- */

const ACC_LOGIN = {
  never: { label: "jamais connecté", cls: "pending" },
  connected: { label: "connecté", cls: "ok" },
  expired: { label: "session expirée", cls: "bad" },
};
const accFmtDate = (iso) => (iso ? fmtParis(iso, { dateStyle: "medium", timeStyle: "short" }) : "");

function accLoginState(a) {
  const l = a.login || { state: "never" };
  const s = ACC_LOGIN[l.state] || ACC_LOGIN.never;
  const when = l.state === "connected" && l.checked_at ? `<span class="muted"> · vérifié le ${esc(accFmtDate(l.checked_at))}</span>` : "";
  const err = a.login_error ? `<div class="acc-line bad">Connexion non vérifiable : ${esc(a.login_error)}</div>` : "";
  const channel = accService(a) === "youtube" && a.channel
    ? `<div class="acc-line" data-acc-channel>Compte YouTube : <b>${esc(a.channel.name)}</b> <span class="mono">${esc(a.channel.id)}</span></div>` : "";
  return `<div class="acc-login" data-acc-login><span class="chip ${s.cls}">${esc(ACC_SERVICES[accService(a)])} : ${esc(s.label)}</span>${when}</div>${channel}${err}`;
}

/* La note de pause n'est écrite qu'une fois : le chip « En pause (manuel) » la porte ; les textes du serveur
   (ready_blocked_reason, ready_note) qui la répètent sont retirés. Les autres notes (décochage automatique...) restent. */
function accNotes(a) {
  const pause = (t) => t.includes("En pause (manuel)");
  const texts = [a.ready_to_publish ? "" : a.ready_blocked_reason, a.ready_note]
    .filter((t, i, all) => t && all.indexOf(t) === i)
    .filter((t) => !(a.paused_at && pause(t)));
  return texts.map((t) => `<span class="acc-line bad" data-acc-ready-note>${esc(t)}</span>`).join("");
}

/* « Prêt à publier » est un état (SPEC-e500 R3), jamais coché à la main, mais cliquable (SPEC-f348 R5) :
   cochée, le clic met le compte en pause ; en pause, il le reprend ; sinon il lance « Se connecter ». Après un
   arrêt R4, « J'ai réglé le problème » (menu « … ») revérifie la connexion et efface l'arrêt. */
function accReadyBox(a) {
  const state = a.ready_to_publish ? `Prêt à publier : connexion ${ACC_SERVICES[accService(a)]} vérifiée` : "Pas prêt à publier";
  const action = a.ready_to_publish ? "clique pour mettre le compte en pause"
    : a.paused_at ? "clique pour reprendre le compte" : "clique pour te connecter";
  return `<div class="acc-ready" data-acc-ready-cell>
    <label class="acc-check"><input type="checkbox" data-acc-ready="${esc(a.id)}"${a.ready_to_publish ? " checked" : ""} title="${esc(state)} : ${esc(action)}"> Prêt à publier</label>
    ${a.paused_at ? `<span class="chip pending" data-acc-paused>En pause</span><span class="acc-line" title="Pause manuelle depuis le ${esc(accFmtDate(a.paused_at))}">depuis le ${esc(accFmtDate(a.paused_at))}</span>` : ""}
    ${accNotes(a)}</div>`;
}

function accSlots(a) {
  const slots = a.slots || [];
  const text = slots.length
    ? slots.map((s) => `<span class="mono">${esc(accDayLabel(s.day))} ${esc(s.time)}</span>`).join(" · ") + ` <span class="faint">(${esc(a.timezone || ACC_DEFAULT_TZ)})</span>`
    : `<span class="faint">aucun (« Nouvelle publication » publie à la date choisie)</span>`;
  return `<div class="acc-line" data-acc-slots>${text}</div>`;
}

function accSlotsEditor(slots) {
  const rows = (slots || []).map((s, i) => `<div class="chan-slot" data-slot="${i}">
    <select class="input" data-slot-day aria-label="Jour">${ACC_DAYS.map(([k, l]) => `<option value="${k}"${k === s.day ? " selected" : ""}>${l}</option>`).join("")}</select>
    <input class="input mono" type="time" data-slot-time aria-label="Heure" value="${esc(s.time)}" required>
    <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-slot-del="${i}" aria-label="Retirer ce créneau">${icon("x", "i-xs")}</button></div>`).join("");
  return `<div class="chan-slots" data-acc-slots-editor>${rows || `<span class="muted">Aucun créneau.</span>`}
    <button type="button" class="btn" data-variant="outline" data-size="sm" data-slot-add>${icon("plus", "i-xs")}Ajouter un créneau</button></div>`;
}

function accReadSlots(root) {
  return $$(".chan-slot", root).map((row) => ({ day: $("[data-slot-day]", row).value, time: $("[data-slot-time]", row).value }));
}

function accPosts(a) {
  if (a.publish_error) return `<div class="acc-line bad">Publications illisibles : ${esc(a.publish_error)}</div>`;
  const today = a.posts_today === null || a.posts_today === undefined ? "—" : fr(a.posts_today);
  const cap = a.max_posts_per_day === null || a.max_posts_per_day === undefined ? "—" : fr(a.max_posts_per_day);
  return `<div class="acc-line" data-acc-posts title="Posts du jour"><b>${today} / ${cap}</b></div>`;
}

function accLastFailure(a) {
  const f = a.last_failure;
  if (!f) return "";
  const capture = f.capture_url ? ` · <a href="${esc(f.capture_url)}" target="_blank" rel="noopener" data-acc-capture>voir la capture</a>` : "";
  return `<div class="acc-line bad" data-acc-failure>Dernier échec (${esc(f.channel)}, ${esc(f.clip_id)}) : ${esc(f.reason || "raison inconnue")}${capture}</div>`;
}

async function accResolve(account, button) {
  button.disabled = true;
  try {
    const out = await api(`/api/accounts/${encodeURIComponent(account.id)}/resolve`, jsonBody("POST", {}));
    toast({ kind: out.ready_to_publish ? "ok" : "warn", title: out.ready_to_publish ? "Compte prêt à publier" : "Arrêt effacé",
            body: out.ready_to_publish ? account.label : `${account.label} : ${out.ready_note || "connexion à refaire (Se connecter)"}` });
  } catch (err) {
    toastError("Vérification impossible", err);
  }
  await accLoad();
}

async function accPause(account) {
  try {
    await api(`/api/accounts/${encodeURIComponent(account.id)}/pause`, jsonBody("POST", {}));
    toast({ kind: "ok", title: "Compte en pause", body: account.label });
  } catch (err) {
    toastError("Pause impossible", err);
  }
  await accLoad();
}

async function accResume(account) {
  try {
    const out = await api(`/api/accounts/${encodeURIComponent(account.id)}/resume`, jsonBody("POST", {}));
    toast({ kind: out.ready_to_publish ? "ok" : "warn", title: out.ready_to_publish ? "Compte prêt à publier" : "Toujours pas prêt",
            body: out.ready_to_publish ? account.label : `${account.label} : ${out.ready_blocked_reason || out.ready_note || "connexion à refaire (Se connecter)"}` });
  } catch (err) {
    toastError("Reprise impossible", err);
  }
  await accLoad();
}

/* Compte YouTube : la connexion se lit dans YouTube Studio (chaîne affichée), pas dans des cookies ; la
   vérification ouvre le navigateur sur le profil du compte. */
async function accVerifyYoutube(account, button) {
  button.disabled = true;
  try {
    const out = await api(`/api/accounts/${encodeURIComponent(account.id)}/verify`, jsonBody("POST", {}));
    toast({ kind: out.ready_to_publish ? "ok" : "warn", title: out.ready_to_publish ? "YouTube vérifié" : "Pas prêt à publier",
            body: out.ready_to_publish ? `${account.label} : ${out.channel.name}` : `${account.label} : ${out.ready_blocked_reason}` });
  } catch (err) {
    toastError("Vérification impossible", err);
  }
  button.disabled = false;
  await accLoad();
}

async function accRefreshStats(account, button) {
  button.disabled = true;
  try {
    const out = await api("/api/stats/tiktok/refresh", jsonBody("POST", { account: account.id }));
    const done = out.accounts[account.id];
    toast({ kind: "ok", title: "Statistiques relevées", body: `${account.label} : ${done ? done.posts : 0} post(s)` });
  } catch (err) {
    toastError("Relevé des statistiques impossible", err);
  } finally {
    button.disabled = false;
  }
}

/* ---------- Liste (table) ---------- */

/* Une ligne : Compte | Connexion | Prêt | Créneaux | Posts | actions. Trois boutons visibles au plus (Se connecter,
   Modifier, « … ») ; tout le reste vit dans le menu [role=menu], fermé tant qu'on ne l'ouvre pas. */
function accMenuItems(a) {
  const shown = accUi.revealed[a.id];
  const items = [];
  if (a.username) items.push(`<button type="button" role="menuitem" data-acc-copy-user="${esc(a.id)}">${icon("copy", "i-xs")}Copier l'identifiant</button>`);
  if (a.has_password) {
    items.push(`<button type="button" role="menuitem" data-acc-copy-pass="${esc(a.id)}">${icon("lock", "i-xs")}Copier le mot de passe</button>`);
    items.push(`<button type="button" role="menuitem" data-acc-show="${esc(a.id)}">${icon(shown ? "eye-off" : "eye", "i-xs")}${shown ? "Masquer le mot de passe" : "Afficher le mot de passe"}</button>`);
  }
  if (accService(a) === "youtube") items.push(`<button type="button" role="menuitem" data-acc-verify="${esc(a.id)}" title="Ouvre YouTube Studio sur le profil et lit le compte YouTube">${icon("check", "i-xs")}Vérifier YouTube</button>`);
  else items.push(`<button type="button" role="menuitem" data-acc-stats="${esc(a.id)}">${icon("refresh-cw", "i-xs")}Relever les stats</button>`);
  if (a.r4_halt) items.push(`<button type="button" role="menuitem" data-acc-resolve="${esc(a.id)}">${icon("check", "i-xs")}J'ai réglé le problème</button>`);
  items.push(`<button type="button" role="menuitem" class="bad" data-acc-delete="${esc(a.id)}">${icon("trash-2", "i-xs")}Supprimer</button>`);
  return items.join("");
}

function accRow(a) {
  const shown = accUi.revealed[a.id];
  const open = accUi.menu === a.id;
  const pass = !a.has_password
    ? `<span class="faint">aucun</span>`
    : shown ? `<code class="mono acc-pass">${esc(shown.value)}</code>` : `<span class="mono acc-pass" aria-label="Mot de passe masqué">••••</span>`;
  return `<tr class="acc-row" data-acc="${esc(a.id)}">
    <td data-l="Compte">
      <div class="acc-title"><span class="tag" data-acc-service>${esc(ACC_SERVICES[accService(a)])}</span> ${a.platform && a.platform !== ACC_SERVICES[accService(a)] ? `<span class="tag">${esc(a.platform)}</span> ` : ""}<b>${esc(a.label)}</b></div>
      <div class="acc-line mono acc-ellipsis" title="${esc(a.username)}">${a.username ? esc(a.username) : "—"}</div>
      ${a.notes ? `<div class="acc-line faint">${esc(a.notes)}</div>` : ""}
      <div class="acc-line">Mot de passe : ${pass}</div>
      ${accBrowserState(a)}
    </td>
    <td data-l="Connexion">${accLoginState(a)}</td>
    <td data-l="Prêt">${accReadyBox(a)}</td>
    <td data-l="Créneaux">${accSlots(a)}</td>
    <td data-l="Posts">${accPosts(a)}${accLastFailure(a)}</td>
    <td class="acc-act">
      <div class="acc-actions">
        <button type="button" class="btn" data-variant="outline" data-size="sm" data-acc-browser-login="${esc(a.id)}" title="Se connecter dans le navigateur (Chrome normal, à la main)">${icon("external-link", "i-xs")}Se connecter</button>
        <button type="button" class="btn" data-variant="outline" data-size="sm" data-acc-edit="${esc(a.id)}">${icon("pencil", "i-xs")}Modifier</button>
        <div class="acc-menu-wrap">
          <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-acc-menu="${esc(a.id)}" aria-haspopup="menu" aria-expanded="${open}" aria-label="Autres actions pour ${esc(a.label)}">${icon("ellipsis")}</button>
          <div class="vmenu" role="menu" aria-label="Autres actions"${open ? "" : " hidden"}>${accMenuItems(a)}</div>
        </div>
      </div>
    </td>
  </tr>`;
}

function accWireList(body) {
  const find = (id) => accUi.list.find((a) => a.id === id);
  $$("[data-acc-menu]", body).forEach((b) => (b.onclick = () => {
    const id = b.dataset.accMenu;
    accUi.menu = accUi.menu === id ? null : id;
    renderCurrent();
  }));
  $$("[data-acc-copy-user]", body).forEach((b) => (b.onclick = () => accCopy(find(b.dataset.accCopyUser).username, b)));
  $$("[data-acc-copy-pass]", body).forEach((b) => (b.onclick = async () => {
    try {
      await accCopy(await accFetchPassword(b.dataset.accCopyPass), b);
    } catch (err) { toastError("Mot de passe indisponible", err); }
  }));
  $$("[data-acc-show]", body).forEach((b) => (b.onclick = async () => {
    const id = b.dataset.accShow;
    if (accUi.revealed[id]) { accHide(id); renderCurrent(); return; }
    try {
      const value = await accFetchPassword(id);
      accUi.revealed[id] = { value, timer: setTimeout(() => { accHide(id); if (currentScreen === "accounts") renderCurrent(); }, ACC_REVEAL_MS) };
      renderCurrent();
    } catch (err) { toastError("Mot de passe indisponible", err); }
  }));
  $$("[data-acc-browser-login]", body).forEach((b) => (b.onclick = () => accBrowserLogin(find(b.dataset.accBrowserLogin), b)));
  $$("[data-acc-verify]", body).forEach((b) => (b.onclick = () => accVerifyYoutube(find(b.dataset.accVerify), b)));
  $$("[data-acc-stats]", body).forEach((b) => (b.onclick = () => accRefreshStats(find(b.dataset.accStats), b)));
  $$("[data-acc-ready]", body).forEach((b) => (b.onclick = (e) => {
    e.preventDefault();  // la coche suit l'état rendu par le serveur, jamais le clic
    const account = find(b.dataset.accReady);
    if (account.ready_to_publish) accPause(account);
    else if (account.paused_at) accResume(account);
    else accBrowserLogin(account, b);
  }));
  $$("[data-acc-resolve]", body).forEach((b) => (b.onclick = () => accResolve(find(b.dataset.accResolve), b)));
  $$("[data-acc-edit]", body).forEach((b) => (b.onclick = () => accOpenForm(find(b.dataset.accEdit))));
  $$("[data-acc-delete]", body).forEach((b) => (b.onclick = async () => {
    const account = find(b.dataset.accDelete);
    const ok = await confirmDialog({
      title: "Supprimer ce compte ?",
      body: `« ${account.label} » sera supprimé${account.has_password ? ", ainsi que son mot de passe dans le coffre de l'OS" : ""}. Cette action est définitive.`,
      confirmLabel: "Supprimer",
    });
    if (!ok) return;
    try {
      await api(`/api/accounts/${encodeURIComponent(account.id)}`, jsonBody("DELETE", {}));
      accHide(account.id);
      toast({ kind: "ok", title: "Compte supprimé", body: account.label });
      await accLoad();
    } catch (err) { toastError("Suppression impossible", err); }
  }));
  // Un choix du menu le referme avant d'agir (l'action rerend l'écran de toute façon).
  $$("[role=menuitem]", body).forEach((b) => {
    const run = b.onclick;
    b.onclick = (e) => { accUi.menu = null; run(e); };
  });
  $("[data-acc-add]", body).onclick = () => accOpenForm(null);
}

/* ---------- Formulaire Ajouter / Modifier (tiroir partagé, T10) ---------- */

function accOpenForm(account) {
  const editing = Boolean(account);
  const a = account || { label: "", platform: "", username: "", notes: "", has_password: false, service: "tiktok", slots: [], timezone: ACC_DEFAULT_TZ };
  let slots = (a.slots || []).map((s) => ({ ...s }));
  drawer.open(`
    <div class="drawer-head">
      <button type="button" class="btn" data-variant="ghost" data-size="icon-sm" data-drawer-close aria-label="Fermer (Échap)">${icon("x")}</button>
      <h2 id="drawer-title">${editing ? "Modifier le compte" : "Ajouter un compte"}</h2>
    </div>
    <div class="drawer-body">
      <form id="acc-form" class="acc-form" autocomplete="off">
        <p class="muted">Le mot de passe est rangé dans le coffre de l'OS, jamais dans un fichier du projet.</p>
        <div class="field"><label for="acc-label">Libellé</label><input class="input" id="acc-label" name="label" required maxlength="120" value="${esc(a.label)}" placeholder="Compte principal"></div>
        <div class="field"><label for="acc-service">Service de publication</label>
          <select class="input" id="acc-service" name="service"${editing ? " disabled" : ""}>${Object.entries(ACC_SERVICES).map(([k, v]) => `<option value="${k}"${accService(a) === k ? " selected" : ""}>${esc(v)}</option>`).join("")}</select>
          ${editing ? `<span class="muted">Le service d'un compte ne change pas.</span>` : ""}</div>
        <div class="field"><label for="acc-platform">Plateforme</label><input class="input" id="acc-platform" name="platform" maxlength="60" list="acc-platforms" value="${esc(a.platform)}" placeholder="TikTok">
          <datalist id="acc-platforms">${ACC_PLATFORMS.map((p) => `<option value="${esc(p)}">`).join("")}</datalist></div>
        <div class="field"><label for="acc-username">E-mail ou identifiant</label><input class="input" id="acc-username" name="username" maxlength="254" value="${esc(a.username)}" placeholder="exemple@example.com"></div>
        <div class="field"><label for="acc-password">Mot de passe</label>
          <input class="input mono" id="acc-password" name="password" type="password" maxlength="512" autocomplete="new-password" placeholder="${editing && a.has_password ? "laisser vide pour ne pas le changer" : ""}">
          ${editing && a.has_password ? `<label class="acc-check"><input type="checkbox" id="acc-clear"> Retirer le mot de passe du coffre</label>` : ""}</div>
        <div class="acc-gen-inline">${accGenControls({ length: "", symbols: false, ambiguous: false })}
          <div class="acc-gen-out"><button type="button" class="btn" data-variant="outline" data-size="sm" data-gen-fill>${icon("wand-sparkles")}Générer et remplir</button>
            <button type="button" class="btn" data-variant="outline" data-size="sm" data-gen-copy-only>${icon("copy")}Générer et copier</button></div></div>
        <div class="field"><label>Créneaux de publication</label>
          <div data-acc-slots-box>${accSlotsEditor(slots)}</div>
          <span class="muted">Créneaux hebdomadaires de ce compte : le calendrier de Publication les propose. Facultatifs.</span></div>
        <div class="field"><label for="acc-timezone">Fuseau horaire des créneaux</label><input class="input mono" id="acc-timezone" name="timezone" maxlength="60" value="${esc(a.timezone || ACC_DEFAULT_TZ)}" placeholder="${ACC_DEFAULT_TZ}"></div>
        <div class="field"><label for="acc-notes">Notes</label><textarea class="input" id="acc-notes" name="notes" rows="2" maxlength="2000" placeholder="ma_chaine, usage, rappel...">${esc(a.notes)}</textarea></div>
      </form>
    </div>
    <div class="drawer-foot">
      <button type="button" class="btn" data-variant="ghost" data-drawer-close>Annuler</button>
      <span class="spacer"></span>
      <button type="submit" form="acc-form" class="btn btn-primary">${editing ? "Enregistrer" : "Ajouter"}</button>
    </div>`,
  {
    onOpen: (el) => {
      setTimeout(() => $("#acc-label", el).focus(), 60);
      const slotsBox = $("[data-acc-slots-box]", el);
      const repaintSlots = () => { slotsBox.innerHTML = accSlotsEditor(slots); };
      slotsBox.onclick = (e) => {
        const t = e.target.closest("button");
        if (!t) return;
        slots = accReadSlots(slotsBox);
        if (t.hasAttribute("data-slot-add")) slots.push({ day: "mon", time: "18:00" });
        else if (t.hasAttribute("data-slot-del")) slots = slots.filter((_, i) => i !== Number(t.dataset.slotDel));
        else return;
        repaintSlots();
      };
      const box = $(".acc-gen-inline", el);
      $("[data-gen-fill]", el).onclick = async () => {
        const value = await accGenerate(box);
        if (value === null) return;
        const field = $("#acc-password", el);
        field.type = "text"; // visible pour que le mot de passe généré soit lisible avant l'enregistrement
        field.value = value;
        const clear = $("#acc-clear", el);
        if (clear) clear.checked = false;
      };
      $("[data-gen-copy-only]", el).onclick = async (e) => {
        const value = await accGenerate(box);
        if (value !== null) accCopy(value, e.currentTarget);
      };
      $("#acc-form", el).onsubmit = async (e) => {
        e.preventDefault();
        const payload = { service: $("#acc-service", el).value, label: $("#acc-label", el).value, platform: $("#acc-platform", el).value, username: $("#acc-username", el).value, notes: $("#acc-notes", el).value,
          slots: accReadSlots(slotsBox), timezone: $("#acc-timezone", el).value.trim() };
        const password = $("#acc-password", el).value;
        const clear = $("#acc-clear", el);
        if (password) payload.password = password;
        else if (clear && clear.checked) payload.password = "";
        try {
          await api(editing ? `/api/accounts/${encodeURIComponent(a.id)}` : "/api/accounts", jsonBody(editing ? "PUT" : "POST", payload));
        } catch (err) { toastError(editing ? "Modification impossible" : "Ajout impossible", err); return; }
        drawer.close();
        if (editing) accHide(a.id);
        toast({ kind: "ok", title: editing ? "Compte modifié" : "Compte ajouté", body: payload.label });
        await accLoad();
      };
    },
  });
}

/* ---------- Écran ---------- */

function accNotice() {
  return `<div class="banner" role="status">${icon("triangle-alert")}<p><b>Console ouverte à distance.</b> Les comptes ne sont accessibles que depuis le PC qui héberge la console, via 127.0.0.1 ou localhost : le serveur refuse toute autre adresse.</p></div>`;
}

Screens.accounts = {
  render(body) {
    const section = body.closest(".screen");
    if (section && !section.classList.contains("v3")) section.classList.add("v3"); // écran migré (ADR-a308 §6)
    if (accIsRemote()) {
      morph(body, accNotice());
      return;
    }
    if (accUi.list === null && !accUi.loading) accLoad();
    if (accUi.list === null) {
      morph(body, accUi.error
        ? emptyState("circle-alert", "Comptes indisponibles", String(accUi.error.message || accUi.error))
        : `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-card"></div>`);
      return;
    }
    const rows = accUi.list.length
      ? accUi.list.map(accRow).join("")
      : `<tr><td colspan="6" class="muted">Aucun compte. Ajoute-en un, par exemple « ma_chaine » avec exemple@example.com.</td></tr>`;
    morph(body, `
      <div class="acc-toolbar"><span class="muted">${fr(accUi.list.length)} compte${accUi.list.length > 1 ? "s" : ""} · mots de passe dans le coffre de l'OS</span>
        <button type="button" class="btn btn-primary" data-acc-add>${icon("plus")}Ajouter un compte</button></div>
      ${accUi.error ? `<p class="reason bad">Actualisation impossible : ${esc(accUi.error.message)}</p>` : ""}
      <div class="table-container"><table class="table acc-table">
        <thead><tr><th>Compte</th><th>Connexion</th><th>Prêt</th><th>Créneaux</th><th>Posts</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      ${accGenPanel()}`);
    accWireList(body);
    accWireGenPanel(body);
  },
};
