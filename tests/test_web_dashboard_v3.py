"""Tableau de bord v3 (TASK-d4123fd6829d, ADR-a308, SPEC-7715 E1, T2, T4, T9).

Aucun test ne touche le réseau ni un navigateur : `screens/dashboard.js` est lu tel quel et exécuté sous node
(quand il est là) avec des doublures minimales de la coquille (`api`, `morph`, `confirmDialog`...).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "clipper" / "web" / "static"
NODE = pytest.mark.skipif(shutil.which("node") is None, reason="node absent du PATH")

DASH_JS = (STATIC / "screens" / "dashboard.js").read_text(encoding="utf-8")

QUOTA = ("transcribe : TransientLLMError: claude -p (code 1, statut 429) : You've hit your session limit "
         "· resets 6:40pm (Europe/London) (5 echecs transitoires, max_attempts = 5)")
QUOTA_REFRAME = QUOTA.replace("transcribe", "reframe")
CANCELLED = "annulée par l'utilisateur (interrompue à l'étape plans)"
OTHER = "ffmpeg a échoué : code 1"

# Doublures de la coquille (ui.js, screens.js, app.js) : seulement ce que dashboard.js appelle.
STUBS = r"""
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fr = (n, d) => Number(n).toLocaleString('fr-FR', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});
const icon = (n, cls) => `<svg class="i ${cls || ''}" data-name="${n}"></svg>`;
const fmtParis = (iso) => 'PARIS<' + iso + '>';
const $ = () => null, $$ = () => [];
const document = { addEventListener() {} };
const STEP_LABELS = { transcribe: 'Transcription', reframe: 'Recadrage', scenes: 'Plans', render: 'Rendu' };
const store = { videos: null, queue: null, channels: [], dashboard: null };
let currentScreen = 'dashboard';
const Screens = {};
let painted = null;
const morph = (el, html) => { painted = html; };
const videoThumb = (id) => `<span class="job-thumb" data-thumb="${id}"></span>`;
const progressBar = (pct) => `<div class="bar" data-pct="${pct}"></div>`;
const addVideoButton = '<button data-add>Ajouter une vidéo</button>';
const emptyState = (ic, title, text) => `<div class="empty"><h3>${title}</h3><p>${text}</p></div>`;
const wireWatch = () => {};
const jsonBody = (method, body) => ({ method, body: JSON.stringify(body) });
const calls = [], toasts = [], errors = [], reloaded = [];
let confirmAnswer = true, failFor = null, confirmed = [];
const api = (url, opts) => {
  calls.push({ url, opts });
  if (failFor && url.includes(failFor)) return Promise.reject(new Error('HTTP 500 pour ' + failFor));
  return Promise.resolve({});
};
const confirmDialog = async (o) => { confirmed.push(o); return confirmAnswer; };
const toast = (o) => toasts.push(o);
const toastError = (title, err) => errors.push({ title, message: String(err && err.message || err) });
const afterVideoAction = async (id) => { reloaded.push(id); };
const loadQueue = async () => {};
const renderCurrent = () => {};
"""


def _node(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        path = handle.name
    try:
        done = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _run(expr: str):
    """Évalue `expr` après le script du tableau de bord (les doublures d'abord) ; renvoie son JSON."""
    script = STUBS + DASH_JS + f"\nconsole.log(JSON.stringify({expr}));"
    return json.loads(_node(script))


def _video(video_id: str, step: str, reason: str, **extra) -> dict:
    return {"video_id": video_id, "title": f"Titre {video_id}", "channel": "ma_chaine", "step": step,
            "reason": reason, "platform_thumbnail": None, **extra}


def _dashboard(**over) -> dict:
    data = {
        "running": [], "failed": [], "queued": [], "queue": [], "watch_pending": [], "clips_to_review": 252,
        "worker": {"state": "active", "pid": 4242, "age_s": 3.0},
        "next_publications": [],
        "llm_cost": {"today": 1.25, "week": 221.05, "unreported_calls": 74,
                     "by_usage": {f"usage_{i}": 40.0 - i for i in range(9)},
                     "by_video": {"zz_cost_video": {"cost": 9.0, "calls": 4, "unreported_calls": 0}}},
        "zero_views": {"hours": 24, "accounts": [], "no_reading": []},
        "hardware": {"device": "cuda", "vram_used_mb": 0},
    }
    data.update(over)
    return data


def _done(index: int) -> dict:
    return {"video_id": f"done{index:02d}", "title": f"Terminée {index}", "channel": "ma_chaine", "status": "done",
            "clips": [{}] * (index + 1), "updated_at": f"2026-10-{10 + index:02d}T08:00:00+00:00",
            "durations": {"download": 60.0, "render": 120.0, "qa": None}}


def _render(dashboard: dict, videos: list | None = None) -> str:
    return _run(f"""(() => {{
      store.dashboard = {json.dumps(dashboard)}; store.videos = {json.dumps(videos or [])};
      Screens.dashboard.render({{}}, store); return painted; }})()""")


# --- Clause 1 : dashGroups regroupe les échecs par cause ----------------------------------------------------------


@NODE
def test_dash_groups_puts_every_429_in_one_group_and_the_cancelled_ones_in_another():
    failed = [_video("aaaaaaaaaaa", "transcribe", QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA_REFRAME),
              _video("ccccccccccc", "plans", CANCELLED), _video("ddddddddddd", "transcribe", QUOTA),
              _video("eeeeeeeeeee", "render", OTHER)]
    groups = _run(f"dashGroups({json.dumps(failed)})")

    by_kind = {g["kind"]: g for g in groups}
    assert [g["kind"] for g in groups] == ["quota", "cancel", "other"]  # ordre d'apparition de la première vidéo
    assert [v["video_id"] for v in by_kind["quota"]["videos"]] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ddddddddddd"]
    assert [v["video_id"] for v in by_kind["cancel"]["videos"]] == ["ccccccccccc"]
    assert [v["video_id"] for v in by_kind["other"]["videos"]] == ["eeeeeeeeeee"]
    assert by_kind["quota"]["title"] == "3 vidéos bloquées par le quota Claude"
    assert by_kind["cancel"]["title"] == "1 vidéo annulée par l'utilisateur"
    assert by_kind["other"]["cause"] == OTHER  # la cause est la raison journalisée, telle quelle


@NODE
def test_dash_groups_keeps_distinct_unknown_causes_apart_and_handles_no_failure():
    failed = [_video("aaaaaaaaaaa", "render", OTHER), _video("bbbbbbbbbbb", "render", "disque plein"),
              _video("ccccccccccc", "qa", OTHER)]
    groups = _run(f"dashGroups({json.dumps(failed)})")

    assert [(g["kind"], len(g["videos"])) for g in groups] == [("other", 2), ("other", 1)]
    assert [g["cause"] for g in groups] == [OTHER, "disque plein"]
    assert _run("dashGroups([])") == []


# --- Clause 2 : rendu avec une fixture /api/dashboard -------------------------------------------------------------


@NODE
def test_render_groups_the_failures_with_a_group_retry_that_names_the_count():
    failed = [_video("aaaaaaaaaaa", "transcribe", QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA_REFRAME),
              _video("ccccccccccc", "plans", CANCELLED)]
    html = _render(_dashboard(failed=failed))

    assert "Relancer les 2" in html
    assert "Relancer les 1" not in html          # un groupe d'une vidéo dit « Relancer » tout court
    assert "2 vidéos bloquées par le quota Claude" in html and "1 vidéo annulée par l&#39;utilisateur" in html
    for video_id in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):                       # action par ligne conservée
        assert f'data-retry-video="{video_id}"' in html and f'data-dismiss-video="{video_id}"' in html
    assert 'data-from-step="reframe"' in html and f'href="#/videos/{"aaaaaaaaaaa"}"' in html
    assert "Relancer depuis recadrage" in html


@NODE
def test_render_shows_the_empty_state_the_six_last_done_videos_and_no_cost_per_video_table():
    videos = [_done(i) for i in range(8)] + [{**_done(9), "status": "failed"}]
    html = _render(_dashboard(), videos)

    assert "Rien ne tourne" in html and "La file est vide" in html
    assert re.findall(r'data-done-video="([^"]+)"', html) == [f"done{i:02d}" for i in (7, 6, 5, 4, 3, 2)]  # 6 plus récentes
    assert "done00" not in html and "done01" not in html and "done09" not in html
    assert "Dernières vidéos terminées" in html
    assert "zz_cost_video" not in html and "Par vidéo" not in html and "<th" not in html.split("Dernières vidéos terminées")[0]
    assert "usage_0" in html and "usage_5" in html and "usage_6" not in html     # 6 premiers usages, le reste cumulé
    assert "3 autres usages" in html
    assert "Mesures internes" not in html and "Durée par étape" not in html     # déplacés dans Statistiques (E7/E8)


@NODE
def test_render_without_any_done_video_says_so_instead_of_an_empty_table():
    html = _render(_dashboard(), [])
    assert "Aucune vidéo terminée pour l&#39;instant" in html and "data-done-video" not in html


@NODE
def test_render_running_video_shows_step_progress_and_hides_the_empty_state():
    running = [_video("aaaaaaaaaaa", "transcribe", None, progress={"fraction": 0.4, "eta_s": 600, "message": None},
                      source_url="https://example.invalid/v/1")]
    html = _render(_dashboard(running=running))

    assert "Rien ne tourne" not in html
    assert "Transcription" in html and "40 %" in html and "reste 10 min" in html and 'data-pct="40"' in html
    assert 'data-cancel="aaaaaaaaaaa"' in html


@NODE
def test_render_lists_the_waiting_queue_with_front_and_remove_actions():
    queue = [{"video_id": "aaaaaaaaaaa", "status": "waiting", "action": "full", "channel": "ma_chaine"},
             {"video_id": "bbbbbbbbbbb", "status": "waiting", "action": "render", "channel": None},
             {"video_id": "ccccccccccc", "status": "running", "action": "full", "channel": None}]
    html = _render(_dashboard(queue=queue))

    assert 'data-remove="aaaaaaaaaaa"' in html and 'data-remove="bbbbbbbbbbb"' in html
    assert 'data-front="bbbbbbbbbbb"' in html and 'data-front="aaaaaaaaaaa"' not in html   # la première est déjà en tête
    assert "ccccccccccc" not in html and "La file est vide" not in html


@NODE
def test_render_shows_each_to_do_item_vod_to_confirm_queued_with_resume_time_and_zero_view_posts():
    vod = {"video_id": "v123", "channel": "ma_chaine", "title": "Mon direct", "url": "https://example.invalid/v123",
           "found_at": "2026-10-10T10:00:00+00:00", "duration_s": 5400}
    queued = [_video("qqqqqqqqqqq", "transcribe", "quota", retry_at="2026-10-11T18:40:00+00:00")]
    zero = {"hours": 24, "no_reading": [], "accounts": [{"account": "compte_alpha", "level": "post", "posts": [
        {"post_id": "p1", "clip_id": "c1", "video_id": None, "posted_at": "2026-10-09T10:00:00+00:00",
         "read_at": "2026-10-10T10:00:00+00:00", "views": 0}]}]}
    html = _render(_dashboard(watch_pending=[vod], queued=queued, zero_views=zero))

    assert "VOD à confirmer" in html and "data-vod-confirm" in html and 'data-vod="v123"' in html
    assert "Reprise prévue le PARIS&lt;2026-10-11T18:40:00+00:00&gt;" in html
    assert 'data-retry-video="qqqqqqqqqqq"' in html
    assert "0 vue" in html and "compte_alpha" in html and "p1" in html
    assert "252" in html and 'href="#/clips"' in html                      # clips à valider


@NODE
def test_render_with_nothing_to_do_says_so():
    html = _render(_dashboard(clips_to_review=0))
    assert "Rien à faire" in html


# --- Clause 3 : <champ>_error affiché tel quel, jamais un 0 muet -------------------------------------------------


@NODE
def test_every_field_error_is_shown_verbatim_in_place_and_never_as_a_silent_zero():
    errors = {
        "failed": None, "failed_error": "pipeline.json de aaaaaaaaaaa illisible : JSON invalide ligne 3",
        "llm_cost": None, "llm_cost_error": "llm_usage.jsonl : ligne 12 illisible",
        "zero_views": None, "zero_views_error": "state/publish/x.json corrompu",
        "next_publications": None, "next_publications_error": "state/publish absent",
        "worker": None, "worker_error": "state/worker.json illisible",
        "clips_to_review": None, "clips_to_review_error": "publish.json de ma_chaine invalide",
        "hardware": None, "hardware_error": "torch absent",
    }
    html = _render(_dashboard(**errors))

    for key, text in errors.items():
        if key.endswith("_error"):
            assert esc_text(text) in html, key
    assert 'role="alert"' in html


def esc_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")


@NODE
def test_a_failed_dashboard_load_shows_the_error_in_place():
    out = _run("""(() => {
      dash.error = new Error('HTTP 500 : /api/dashboard indisponible'); store.dashboard = null; store.videos = [];
      Screens.dashboard.render({}, store); return painted; })()""")
    assert "HTTP 500 : /api/dashboard indisponible" in out and "Chargement impossible" in out


@NODE
def test_the_vram_error_is_shown_next_to_the_device():
    html = _render(_dashboard(hardware={"device": "cuda", "vram_used_mb": None, "vram_used_mb_error": "nvml absent"}))
    assert "nvml absent" in html and "CUDA" in html


# --- Relance groupée : une confirmation qui nomme le nombre, puis une relance par vidéo ---------------------------


def _run_async(expr: str):
    script = STUBS + DASH_JS + f"\n(async () => console.log(JSON.stringify(await ({expr}))))();"
    return json.loads(_node(script))


@NODE
def test_group_retry_confirms_once_naming_the_count_then_retries_each_video_from_its_own_step():
    videos = [_video("aaaaaaaaaaa", "transcribe", QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA)]
    out = _run_async(f"""(async () => {{
      await dashRetryGroup({json.dumps(videos)});
      return {{ confirmed, calls: calls.map((c) => [c.url, JSON.parse(c.opts.body)]), toasts, errors, reloaded }};
    }})()""")

    assert len(out["confirmed"]) == 1 and "2" in out["confirmed"][0]["title"] and out["confirmed"][0]["confirmLabel"] == "Relancer les 2"
    assert out["calls"] == [["/api/videos/aaaaaaaaaaa/retry", {"from_step": "transcribe"}],
                            ["/api/videos/bbbbbbbbbbb/retry", {"from_step": "reframe"}]]
    assert out["reloaded"] == ["aaaaaaaaaaa", "bbbbbbbbbbb"] and out["errors"] == []
    assert out["toasts"][0]["kind"] == "ok" and "2" in out["toasts"][0]["title"]


@NODE
def test_group_retry_declined_calls_nothing():
    videos = [_video("aaaaaaaaaaa", "transcribe", QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA)]
    out = _run_async(f"""(async () => {{
      confirmAnswer = false;
      await dashRetryGroup({json.dumps(videos)});
      return {{ calls: calls.length, toasts: toasts.length, errors: errors.length }};
    }})()""")
    assert out == {"calls": 0, "toasts": 0, "errors": 0}


@NODE
def test_group_retry_keeps_going_after_a_failure_and_reports_it_by_video_never_silently():
    videos = [_video("aaaaaaaaaaa", "transcribe", QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA),
              _video("ccccccccccc", "render", QUOTA)]
    out = _run_async(f"""(async () => {{
      failFor = '/bbbbbbbbbbb/';
      await dashRetryGroup({json.dumps(videos)});
      return {{ calls: calls.map((c) => c.url), toasts, errors, reloaded }};
    }})()""")

    assert len(out["calls"]) == 3                                            # la panne d'une vidéo n'arrête pas les autres
    assert out["reloaded"] == ["aaaaaaaaaaa", "ccccccccccc"]
    assert len(out["errors"]) == 1 and "bbbbbbbbbbb" in out["errors"][0]["message"] and "HTTP 500" in out["errors"][0]["message"]
    assert out["toasts"][0]["kind"] == "ok" and "2" in out["toasts"][0]["title"]


@NODE
def test_group_retry_refuses_a_video_without_a_known_step_instead_of_guessing_one():
    videos = [_video("aaaaaaaaaaa", None, QUOTA), _video("bbbbbbbbbbb", "reframe", QUOTA)]
    out = _run_async(f"""(async () => {{
      await dashRetryGroup({json.dumps(videos)});
      return {{ calls: calls.map((c) => c.url), errors }};
    }})()""")
    assert out["calls"] == ["/api/videos/bbbbbbbbbbb/retry"]
    assert len(out["errors"]) == 1 and "aaaaaaaaaaa" in out["errors"][0]["message"]


# --- Rapprochement du DOM, pas de réécriture --------------------------------------------------------------------------


def test_the_dashboard_script_never_assigns_innerhtml_and_paints_through_morph():
    code = "\n".join(l for l in DASH_JS.splitlines() if not l.lstrip().startswith(("//", "/*", "*")))
    assert not re.search(r"\.innerHTML\s*=(?!=)", code), "innerHTML = hors morph"
    assert not re.search(r"\.(outerHTML|insertAdjacentHTML)\b", code)
    assert code.count("morph(") >= 2                                          # corps de l'écran et voyant du worker
    heartbeat = DASH_JS[DASH_JS.index('addEventListener("clipper:worker"'):DASH_JS.index("const dashDate")]
    assert "morph(" in heartbeat and "innerHTML" not in heartbeat


def test_the_dashboard_script_no_longer_loads_the_internal_measures():
    code = "\n".join(l for l in DASH_JS.splitlines() if not l.lstrip().startswith(("//", "/*", "*")))
    render = code[code.index("Screens.dashboard = {"):]
    assert "/api/measures" not in render and "loadMeasures" not in render and "dashMeasuresSection" not in render


@NODE
def test_node_check_of_the_dashboard_script():
    subprocess.run(["node", "--check", str(STATIC / "screens" / "dashboard.js")], capture_output=True, text=True,
                   encoding="utf-8", check=True)


# --- CSS : bloc de l'écran retiré de style.css, repris dans v3.css sous .v3 ---------------------------------------


def _blocks(css: str, name: str) -> list[str]:
    return re.findall(rf"/\* >>> {name} \*/(.*?)/\* <<< {name} \*/", css, flags=re.S)


def test_the_dashboard_block_lives_in_console_css_with_its_own_d3_classes():
    assert not (STATIC / "style.css").exists()
    css = (STATIC / "console.css").read_text(encoding="utf-8")
    blocks = _blocks(css, "dashboard")
    assert len(blocks) == 1 and blocks[0].strip()
    rules = re.sub(r"/\*.*?\*/", "", blocks[0], flags=re.S)
    selectors = [s.strip() for s in re.findall(r"(?:^|\})\s*([^{}@]+?)\s*\{", rules)]
    assert selectors and all(part.strip().startswith(".d3") for s in selectors for part in s.split(",")), selectors
    assert "@media (max-width: 900px)" in blocks[0] and "44px" in blocks[0]      # mobile : cibles de 44 px, une colonne
    assert len(css.encode("utf-8")) <= 163_840  # budget de console.css : voir test_web_shell


def test_the_dashboard_markup_uses_no_legacy_screen_class_names():
    """Les classes de v3 sont à elles (préfixe d3-) : jamais .chip/.kpi/.panel/.list-item, que style.css restyle."""
    code = "\n".join(l for l in DASH_JS.splitlines() if not l.lstrip().startswith(("//", "/*", "*")))
    classes = set()
    for match in re.finditer(r'class="([^"$]*)', code):
        classes.update(match.group(1).split())
    legacy = {"chip", "kpi", "kpis", "panel", "list-item", "li-main", "li-title", "li-sub", "section-title", "job",
              "job-main", "job-title", "job-meta", "job-side", "job-prog", "job-step", "reason", "when"}
    assert not (classes & legacy), classes & legacy
    assert "d3-section" in code or "d3-" in code
