"""learning.py : rattachement post -> clip après relevé (TASK-32ae). Aucun réseau, aucun navigateur."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clipper import jury_calibration, learning, outcomes, publish, tiktok
from clipper.config import Config

ACCOUNT = "compte_a"
OTHER = "compte_b"
VIDEO = "VVVVVVVVVVV"
PUBLISH_AT = "2026-10-07T09:00:00+02:00"


def _config(tmp_path, **learning_overrides) -> Config:
    return Config(
        mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={
            "tiktok": {"stats_dir": str(tmp_path / "stats")},
            "publish": {"state_dir": str(tmp_path / "pub")},
            "learning": {"state_dir": str(tmp_path / "learning"), **learning_overrides},
            "outcomes": {"journal_path": str(tmp_path / "outcomes.jsonl")},
            "jury_calibration": {"weights_path": str(tmp_path / "jury_weights.json")},
            "accounts": {"state_file": str(tmp_path / "accounts.json")},
        },
    )


def _sidecar(config, clip_id, *, account=ACCOUNT, post_id=None, url=None, caption="Un super clip",
             hashtags=("#jeu", "#fun"), publish_at=PUBLISH_AT, state="scheduled_on_tiktok", video=VIDEO) -> Path:
    path = Path(config.output_dir) / video / f"{clip_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "caption": caption, "hashtags": list(hashtags),
        "tiktok_post": {"url": url, "id": post_id, "state": state, "publish_at": publish_at,
                        "account": account, "note": "post programmé"},
    }), encoding="utf-8")
    return path


def _entry(config, clip_id, *, channel="chaine", post_id=None, video=VIDEO) -> Path:
    path = Path(config.section("publish")["state_dir"]) / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append({"video_id": video, "clip_id": clip_id, "series_id": None, "part": None, "status": "published", "slot_at": None,
                    "decided_at": None, "published_at": "2026-10-07T07:00:00+00:00", "error": None,
                    "account": ACCOUNT, "tiktok_state": "scheduled_on_tiktok", "post_id": post_id, "post_url": None})
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _snapshot(config, fetched_at, *posts, account=ACCOUNT) -> None:
    snapshot = {"account": account, "fetched_at": fetched_at, "source": "tiktok_studio", "origin": "full",
                "overview": {}, "posts": [
                    {"post_id": pid, "post_url": f"https://www.tiktok.com/@x/video/{pid}", "caption": caption,
                     "posted_at": posted_at} for pid, caption, posted_at in posts]}
    tiktok.append_snapshot(account, tiktok.get_settings(config), snapshot)


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _links(config) -> dict:
    return _read(Path(config.section("learning")["state_dir"]) / "links.json")


def test_defaults_declared():
    assert learning.CONFIG_DEFAULTS["state_dir"] == "state/learning"
    assert learning.CONFIG_DEFAULTS["enabled"] is True
    assert learning.CONFIG_DEFAULTS["link_window_h"] == 12


def test_links_scheduled_post_by_caption_and_time(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-02")
    queue = _entry(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000001", "Un super clip #jeu #fun", "2026-10-07T09:02:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    post = _read(side)["tiktok_post"]
    assert post["id"] == "7000000000000000001"
    assert post["url"] == "https://www.tiktok.com/@x/video/7000000000000000001"
    assert post["linked_by"] == "stats"
    assert datetime.fromisoformat(post["linked_at"]).tzinfo is not None
    assert post["note"] == "post programmé" and post["state"] == "scheduled_on_tiktok"
    entry = _read(queue)[0]
    assert entry["post_id"] == "7000000000000000001"
    assert entry["post_url"].endswith("/video/7000000000000000001")
    assert result["linked"] == [{"video_id": VIDEO, "clip_id": "clip-02", "post_id": "7000000000000000001"}]
    assert result["unlinked"] == {"none": 0, "ambiguous": 0}


def test_truncated_caption_with_ellipsis_still_matches(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-01", caption="Une légende vraiment très longue pour ce clip")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000002", "Une légende vraiment très…", "2026-10-07T09:00:00"))
    learning.link_posts(ACCOUNT, config=config)
    assert _read(side)["tiktok_post"]["id"] == "7000000000000000002"


def test_no_candidate_is_recorded_with_reason_none(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-01")
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000003", "Autre légende", "2026-10-07T09:00:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    assert side.read_text(encoding="utf-8") == before
    assert result["unlinked"] == {"none": 1, "ambiguous": 0} and result["linked"] == []
    links = _links(config)
    [record] = links["unlinked"]
    assert {k: record[k] for k in ("video_id", "clip_id", "account", "reason", "matches")} == {
        "video_id": VIDEO, "clip_id": "clip-01", "account": ACCOUNT, "reason": "none", "matches": []}
    assert datetime.fromisoformat(record["checked_at"]).tzinfo is not None
    assert links["counts"][ACCOUNT] == {"linked": 0, "none": 1, "ambiguous": 0}


def test_post_outside_the_time_window_is_not_a_candidate(tmp_path):
    config = _config(tmp_path, link_window_h=1)
    side = _sidecar(config, "clip-01")
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T12:00:00+00:00", ("7000000000000000004", "Un super clip #jeu #fun", "2026-10-07T11:30:00"))
    result = learning.link_posts(ACCOUNT, config=config)
    assert side.read_text(encoding="utf-8") == before and result["unlinked"]["none"] == 1


def test_two_candidates_are_ambiguous_and_nothing_is_written(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-01")
    queue = _entry(config, "clip-01")
    before = (side.read_text(encoding="utf-8"), queue.read_text(encoding="utf-8"))
    _snapshot(config, "2026-10-07T10:00:00+00:00",
              ("7000000000000000005", "Un super clip #jeu #fun", "2026-10-07T09:00:00"),
              ("7000000000000000006", "Un super clip #jeu #fun", "2026-10-07T09:05:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    assert (side.read_text(encoding="utf-8"), queue.read_text(encoding="utf-8")) == before
    assert result["unlinked"] == {"none": 0, "ambiguous": 1}
    [record] = _links(config)["unlinked"]
    assert record["reason"] == "ambiguous"
    assert sorted(record["matches"]) == ["7000000000000000005", "7000000000000000006"]


def test_post_already_carried_by_another_sidecar_is_never_candidate(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "clip-01", post_id="7000000000000000007")
    side = _sidecar(config, "clip-02")
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000007", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    result = learning.link_posts(ACCOUNT, config=config)
    assert side.read_text(encoding="utf-8") == before and result["unlinked"]["none"] == 1


def test_sidecar_with_an_id_is_never_modified(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-01", post_id="7000000000000000008")
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000009", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    result = learning.link_posts(ACCOUNT, config=config)
    assert side.read_text(encoding="utf-8") == before
    assert result == {"linked": [], "unlinked": {"none": 0, "ambiguous": 0}}


def test_other_accounts_sidecars_are_left_alone(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-01", account=OTHER)
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000010", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    learning.link_posts(ACCOUNT, config=config)
    assert side.read_text(encoding="utf-8") == before


def test_list_videos_reports_the_clip_after_linking(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000011", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    before = tiktok.list_videos(ACCOUNT, config=config)
    assert before[0]["outside_clipper"] is True
    learning.link_posts(ACCOUNT, config=config)
    [video] = tiktok.list_videos(ACCOUNT, config=config)
    assert video["clip"] == {"video_id": VIDEO, "clip_id": "clip-02"} and video["outside_clipper"] is False


def test_unreadable_sidecar_is_an_explicit_error_naming_the_file(tmp_path):
    config = _config(tmp_path)
    bad = Path(config.output_dir) / VIDEO / "casse.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{pas du json", encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000012", "x", "2026-10-07T09:00:00"))
    with pytest.raises(learning.LearningError, match="casse.json"):
        learning.link_posts(ACCOUNT, config=config)


def test_link_if_due_only_processes_accounts_with_a_newer_snapshot(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000013", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    now = datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc)

    done = learning.link_if_due(now, config=config)

    assert [(d["clip_id"], d["post_id"]) for d in done] == [("clip-02", "7000000000000000013")]
    assert _links(config)["last_run"][ACCOUNT] == now.isoformat()
    # rien de neuf depuis : aucun traitement (un nouveau sidecar non relié reste intact)
    side = _sidecar(config, "clip-03")
    before = side.read_text(encoding="utf-8")
    assert learning.link_if_due(datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc), config=config) == []
    assert side.read_text(encoding="utf-8") == before
    assert "unlinked" not in _links(config) or all(r["clip_id"] != "clip-03" for r in _links(config)["unlinked"])
    # un nouveau relevé : le compte est de nouveau traité
    _snapshot(config, "2026-10-07T11:30:00+00:00", ("7000000000000000014", "Un super clip #jeu #fun", "2026-10-07T09:10:00"))
    again = learning.link_if_due(datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc), config=config)
    assert [d["clip_id"] for d in again] == ["clip-03"]


def test_link_if_due_reads_a_links_json_written_before_snapshots_existed(tmp_path):
    """links.json d'avant TASK-2d9a (sans « snapshots ») : le worker ne plante plus (KeyError réel du 08/10)."""
    config = _config(tmp_path)
    path = Path(config.section("learning")["state_dir"]) / "links.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run": {}, "unlinked": [], "counts": {}}), encoding="utf-8")
    _sidecar(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000013", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))

    done = learning.link_if_due(datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc), config=config)

    assert [d["clip_id"] for d in done] == ["clip-02"]
    assert ACCOUNT in _links(config)["snapshots"]


def test_link_if_due_takes_a_snapshot_written_after_the_pass_even_if_fetched_before_it(tmp_path):
    """Un relevé commencé avant le passage du worker mais fini après (fetched_at < last_run) n'est pas ignoré."""
    config = _config(tmp_path)
    _sidecar(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000016", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    learning.link_if_due(datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc), config=config)
    _sidecar(config, "clip-03")
    # relevé web lancé à 10:10, fini après le passage de 10:30 : son fetched_at (10:10) précède last_run
    _snapshot(config, "2026-10-07T10:10:00+00:00", ("7000000000000000017", "Un super clip #jeu #fun", "2026-10-07T09:10:00"))

    again = learning.link_if_due(datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc), config=config)

    assert [d["clip_id"] for d in again] == ["clip-03"]


def test_snapshot_written_after_the_sync_is_due_even_if_fetched_before_it(tmp_path):
    config = _config(tmp_path)
    settings = learning._settings(config)
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000018", "x", "2026-10-07T09:00:00"))
    learning.sync(datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc), config=config)
    assert learning._snapshot_newer_than_sync(settings, config) is False
    _snapshot(config, "2026-10-07T10:10:00+00:00", ("7000000000000000019", "x", "2026-10-07T09:10:00"))
    assert learning._snapshot_newer_than_sync(settings, config) is True


def test_link_if_due_disabled_does_nothing(tmp_path):
    config = _config(tmp_path, enabled=False)
    side = _sidecar(config, "clip-02")
    before = side.read_text(encoding="utf-8")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000015", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    assert learning.link_if_due(datetime(2026, 10, 7, 10, 30, tzinfo=timezone.utc), config=config) == []
    assert side.read_text(encoding="utf-8") == before


def test_a_linked_clip_leaves_the_unlinked_list(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "clip-01")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000016", "Autre", "2026-10-07T09:00:00"))
    learning.link_posts(ACCOUNT, config=config)
    assert len(_links(config)["unlinked"]) == 1
    _snapshot(config, "2026-10-07T11:00:00+00:00", ("7000000000000000017", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    learning.link_posts(ACCOUNT, config=config)
    links = _links(config)
    assert links["unlinked"] == [] and links["counts"][ACCOUNT]["linked"] == 1


# ---- publish.attach_post

def test_attach_post_refuses_to_overwrite_a_different_post_id(tmp_path):
    config = _config(tmp_path)
    state = config.section("publish")["state_dir"]
    _entry(config, "clip-01", post_id="7000000000000000020")
    with pytest.raises(publish.PublishError, match="7000000000000000020"):
        publish.attach_post(VIDEO, "clip-01", "chaine", post_url="https://t/video/7000000000000000021",
                            post_id="7000000000000000021", state_dir=state)
    same = publish.attach_post(VIDEO, "clip-01", "chaine", post_url="https://t/video/7000000000000000020",
                               post_id="7000000000000000020", state_dir=state)
    assert same["post_id"] == "7000000000000000020"


def test_attach_post_unknown_entry_is_an_error(tmp_path):
    with pytest.raises(publish.PublishError, match="absent"):
        publish.attach_post(VIDEO, "nope", "chaine", post_url="u", post_id="1", state_dir=tmp_path)


def test_concurrent_link_passes_do_not_corrupt_links_json_nor_double_count(tmp_path):
    """Plusieurs workers : deux link_posts simultanés ne se marchent pas dessus (verrou sur links.json)."""
    import threading

    config = _config(tmp_path)
    posts = []
    for n in range(6):
        _sidecar(config, f"{n:02d}", caption=f"Clip numero {n}", hashtags=())
        posts.append((f"70000000000000001{n:02d}", f"Clip numero {n}", "2026-10-07T09:00:00"))
    _snapshot(config, "2026-10-07T10:00:00+00:00", *posts)
    barrier, errors = threading.Barrier(4), []

    def run():
        barrier.wait()
        try:
            for _ in range(5):
                learning.link_posts(ACCOUNT, config=config)
        except Exception as exc:  # noqa: BLE001 : on veut voir toute erreur
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert _links(config)["counts"][ACCOUNT]["linked"] == 6


# ---- versement stats -> outcomes (TASK-7136)

NOW = datetime(2026, 10, 10, 13, 0, tzinfo=timezone.utc)
POST = "7000000000000000101"
OTHER_POST = "7000000000000000102"
PAUSED_POST = "7000000000000000103"
CLIP_POSTED = "2026-09-20T09:00:00"  # heure de Paris


def _stats_snapshot(config, fetched_at, posts, account=ACCOUNT) -> None:
    """posts : {post_id: (posted_at, views, extras)} ; les autres champs du relevé valent null."""
    rows = []
    for post_id, (posted_at, views, extras) in posts.items():
        row = {"post_id": post_id, "post_url": f"https://www.tiktok.com/@x/video/{post_id}", "caption": f"Legende {post_id}",
               "posted_at": posted_at, "views": views, "likes": None, "comments": None, "shares": None,
               "avg_watch_s": None, "watched_full": None, "new_followers": None}
        rows.append({**row, **extras})
    tiktok.append_snapshot(account, tiktok.get_settings(config), {
        "account": account, "fetched_at": fetched_at, "source": "tiktok_studio", "origin": "full", "overview": {},
        "posts": rows})


def _others(views_list):
    return {f"60000000000000000{i:02d}": ("2026-09-15T10:00:00", v, {}) for i, v in enumerate(views_list)}


def _linked_clip(config, clip_id="03", *, post_id=POST, account=ACCOUNT, video=VIDEO, qa=None):
    path = _sidecar(config, clip_id, account=account, post_id=post_id, url=f"https://www.tiktok.com/@x/video/{post_id}",
                    state="published", video=video)
    side = _read(path)
    side["qa"] = qa or {"status": "passed", "issues": []}
    path.write_text(json.dumps(side), encoding="utf-8")
    return path


def _journal(config) -> list[dict]:
    return outcomes.read(config.section("outcomes")["journal_path"])


def _sync(config) -> dict:
    learning.sync(NOW, config=config)
    return _read(Path(config.section("learning")["state_dir"]) / "sync.json")


def _scored_account(config, clip_views=750, others=(100, 200, 300, 400, 500, 600, 700, 800, 900, 1000), **extras):
    """Un compte éligible (10 posts à vues) et un clip relié dont les vues évoluent : 10 à J+1, clip_views à J+4, 2000 à la fin."""
    for fetched, own in (("2026-09-21T12:00:00+00:00", 10), ("2026-09-24T12:00:00+00:00", clip_views),
                         ("2026-10-10T12:00:00+00:00", 2000)):
        _stats_snapshot(config, fetched, {**_others(others), POST: (CLIP_POSTED, own, extras)})


def test_new_settings_declared():
    assert learning.CONFIG_DEFAULTS["maturity_days"] == 3
    assert learning.CONFIG_DEFAULTS["window_days"] == 90
    assert learning.CONFIG_DEFAULTS["min_account_posts"] == 10


def test_sync_writes_one_result_and_one_stats_entry_per_mature_clip(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03", qa={"status": "passed", "issues": [{"type": "weak_hook"}]})
    _scored_account(config, likes=42, new_followers=0)

    learning.sync(NOW, config=config)

    result, stats = _journal(config)
    assert result["kind"] == "result" and result["qa"] == {"status": "passed", "issues": [{"type": "weak_hook"}]}
    assert result["human_decision"] is None
    assert (result["video_id"], result["clip_id"], result["moment_id"]) == (VIDEO, "03", 3)
    assert stats["kind"] == "stats"
    assert (stats["video_id"], stats["clip_id"], stats["moment_id"]) == (VIDEO, "03", 3)
    assert (stats["post_id"], stats["account"], stats["posted_at"]) == (POST, ACCOUNT, CLIP_POSTED)
    assert stats["fetched_at"] == "2026-09-24T12:00:00+00:00"
    assert stats["age_days"] == pytest.approx(4.21, abs=0.01)
    assert stats["stats"] == {"views": 2000, "views_at_maturity": 750, "views_percentile": 0.7, "likes": 42, "comments": None,
                              "shares": None, "avg_watch_s": None, "watched_full": None, "new_followers": 0, "pct_watched": None}


def test_two_syncs_add_nothing_more(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config)
    learning.sync(NOW, config=config)
    before = _journal(config)

    learning.sync(NOW, config=config)

    assert _journal(config) == before and len(before) == 2


def test_part_clip_id_gives_the_moment_id(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "12-p2")
    _scored_account(config)

    learning.sync(NOW, config=config)

    assert {e["moment_id"] for e in _journal(config)} == {12}


def test_clip_id_of_another_form_is_a_learning_error(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "clip-02")
    _scored_account(config)

    with pytest.raises(learning.LearningError, match="clip-02"):
        learning.sync(NOW, config=config)


def test_young_clip_is_excluded_immature_but_its_result_is_recorded(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _stats_snapshot(config, "2026-09-21T12:00:00+00:00", {**_others(range(100, 1100, 100)), POST: (CLIP_POSTED, 10, {})})

    sync = _sync(config)

    assert [e["kind"] for e in _journal(config)] == ["result"]
    assert sync["excluded"] == [{"video_id": VIDEO, "clip_id": "03", "account": ACCOUNT, "reason": "immature"}]


def test_maturity_keeps_the_first_snapshot_old_enough_with_views(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    base = _others(range(100, 1100, 100))
    _stats_snapshot(config, "2026-09-21T12:00:00+00:00", {**base, POST: (CLIP_POSTED, 5, {})})  # 1 j : trop jeune
    _stats_snapshot(config, "2026-09-24T12:00:00+00:00", {**base, POST: (CLIP_POSTED, None, {})})  # mûr, vues non lues
    _stats_snapshot(config, "2026-09-25T12:00:00+00:00", {**base, POST: (CLIP_POSTED, 777, {})})  # mûr avec vues

    learning.sync(NOW, config=config)

    stats = _journal(config)[1]
    assert stats["stats"]["views_at_maturity"] == 777 and stats["stats"]["views"] == 777
    assert stats["fetched_at"] == "2026-09-25T12:00:00+00:00"


def test_percentile_ties_take_the_average_rank(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config, clip_views=100, others=[100] * 5 + [900] * 5)

    learning.sync(NOW, config=config)

    assert _journal(config)[1]["stats"]["views_percentile"] == 0.25  # 6 ex aequo : rang moyen 3,5 -> (3,5-1)/10


def test_reference_is_limited_to_the_window(tmp_path):
    config = _config(tmp_path, window_days=30)
    _linked_clip(config)
    old = {f"50000000000000000{i:02d}": ("2026-06-01T10:00:00", 99999, {}) for i in range(5)}
    _stats_snapshot(config, "2026-09-24T12:00:00+00:00", {**_others(range(100, 1100, 100)), **old, POST: (CLIP_POSTED, 750, {})})

    learning.sync(NOW, config=config)

    assert _journal(config)[1]["stats"]["views_percentile"] == 0.7  # les 5 vieux posts hors fenêtre ne comptent pas


def test_account_below_min_posts_has_no_stats_entry(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config, others=(100, 200, 0, 0, 0, 0, 0, 0, 0, 0))  # 2 posts à vues seulement

    sync = _sync(config)

    assert [e["kind"] for e in _journal(config)] == ["result"]
    assert sync["excluded"] == [{"video_id": VIDEO, "clip_id": "03", "account": ACCOUNT, "reason": "account_below_min"}]
    assert sync["accounts"][ACCOUNT]["eligible"] is False


def test_eligible_account_is_reported(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config)

    sync = _sync(config)

    assert sync["accounts"][ACCOUNT] == {"mature_posts": 11, "viewed_posts": 11, "eligible": True}


def test_youtube_account_is_excluded_without_stats(tmp_path):
    config = _config(tmp_path)
    Path(config.section("accounts")["state_file"]).write_text(json.dumps({"accounts": [
        {"id": "yt1", "service": "youtube", "label": "Chaine", "platform": "youtube", "username": "u"}]}), encoding="utf-8")
    _linked_clip(config, account="yt1")

    sync = _sync(config)

    assert _journal(config) == []
    assert sync["excluded"] == [{"video_id": VIDEO, "clip_id": "03", "account": "yt1", "reason": "service_without_stats"}]


def _moments(config, video, moments, rubric=None, jury=None):
    path = Path(config.workspace_dir) / video / "moments.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"video_id": video, "moments": moments}
    if rubric is not None:
        data["rubric"] = rubric
    if jury is not None:  # comme moments.run : le jury (juges, perspective_sha) est au niveau du fichier
        data["jury"] = jury
    path.write_text(json.dumps(data), encoding="utf-8")


def _trace(score):
    return {"rounds": [{"round": 1, "judges": {"retention": {"score": score}, "conformite": {"score": 50, "veto": False}}}]}


def test_sync_calibrates_with_the_traces_and_counts_the_untraced(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _linked_clip(config, "04", post_id="7000000000000000102")  # moment 4 : pas de trace
    _linked_clip(config, "05", post_id="7000000000000000103", video="WWWWWWWWWWW")  # pas de moments.json
    _moments(config, VIDEO, [{"id": 3, "jury": {"trace": _trace(80)}, "exploration": True}, {"id": 4}])
    for fetched in ("2026-09-24T12:00:00+00:00", "2026-10-10T12:00:00+00:00"):
        _stats_snapshot(config, fetched, {**_others(range(100, 1100, 100)), POST: (CLIP_POSTED, 750, {}),
                                          "7000000000000000102": (CLIP_POSTED, 300, {}),
                                          "7000000000000000103": (CLIP_POSTED, 200, {})})
    calls = []
    real = jury_calibration.calibrate
    monkeypatch.setattr(jury_calibration, "calibrate", lambda traces, **kw: calls.append(list(traces)) or real(traces, **kw))

    sync = _sync(config)

    assert calls == [[{"video_id": VIDEO, "moment_id": 3, "candidate": {"trace": _trace(80)}}]]
    assert sync["calibration"]["clips"] == 1 and sync["calibration"]["untraced"] == 2
    assert sync["calibration"]["weights_path"] == str(tmp_path / "jury_weights.json")
    assert sync["calibration"]["at"] == NOW.isoformat()
    assert (tmp_path / "jury_weights.json").exists()
    stats = {e["clip_id"]: e for e in _journal(config) if e["kind"] == "stats"}
    assert stats["03"]["exploration"] is True and "exploration" not in stats["04"]


def test_no_calibration_when_nothing_was_added(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config)
    learning.sync(NOW, config=config)
    calls = []
    monkeypatch.setattr(jury_calibration, "calibrate", lambda *a, **k: calls.append(1))

    learning.sync(NOW, config=config)

    assert calls == []


def test_sync_json_shape(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config)
    _scored_account(config)

    sync = _sync(config)

    assert sync["last_sync"] == NOW.isoformat() and sync["last_error"] is None
    assert sync["results"] == [f"{VIDEO}/03"] and sync["scored"] == [f"{VIDEO}/03"] and sync["excluded"] == []
    assert set(sync["calibration"]) == {"at", "clips", "untraced", "weights_path"}


# ---------------------------------------------------------------- rétention à maturité (TASK-58d6dbbf1687)


def _sidecar_fields(config, clip_id, **fields) -> None:
    """Modifie le sidecar du clip ; une valeur None retire le champ."""
    path = Path(config.output_dir) / VIDEO / f"{clip_id}.json"
    side = _read(path)
    for key, value in fields.items():
        if value is None:
            side.pop(key, None)
        else:
            side[key] = value
    path.write_text(json.dumps(side), encoding="utf-8")


def _stats_entry(config) -> dict:
    return next(e for e in _journal(config) if e["kind"] == "stats")


def _stats_row(config, clip_id, pct, *, percentile=0.5, source="action", duration=20.0):
    outcomes._append({"kind": "stats", "video_id": VIDEO, "clip_id": clip_id, "moment_id": 1, "post_id": f"p{clip_id}",
                      "duration": duration, "pct_watched": pct, "moment_source": source,
                      "stats": {"views_percentile": percentile, "watched_full": 0.1}},
                     config.section("outcomes")["journal_path"])


def test_retention_min_n_declared():
    assert learning.CONFIG_DEFAULTS["retention_min_n"] == 30


def test_stats_entry_gives_duration_pct_watched_and_moment_source(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _moments(config, VIDEO, [{"id": 3, "source": "action"}])
    _scored_account(config, avg_watch_s=13.38, watched_full=0.4)

    learning.sync(NOW, config=config)

    stats = _stats_entry(config)
    assert stats["duration"] == 24.47
    assert stats["pct_watched"] == pytest.approx(0.547, abs=1e-3)
    assert stats["moment_source"] == "action"
    assert stats["stats"]["avg_watch_s"] == 13.38


@pytest.mark.parametrize("avg_watch_s, duration", [(None, 24.47), (13.38, None), (13.38, 0)])
def test_pct_watched_is_null_when_a_part_is_missing(tmp_path, avg_watch_s, duration):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=duration)
    _moments(config, VIDEO, [{"id": 3, "source": "transcript"}])
    _scored_account(config, avg_watch_s=avg_watch_s)

    learning.sync(NOW, config=config)

    assert _stats_entry(config)["pct_watched"] is None


@pytest.mark.parametrize("moments, expected", [
    ([{"id": 3}], "transcript"),  # sans champ source : moments.json n'écrit ce champ qu'en transcript+action
    ([{"id": 3, "source": "transcript"}], "transcript"),
    ([{"id": 3, "source": "action"}], "action"),
    ([{"id": 3, "source": "xyz"}], None),  # valeur inconnue : jamais devinée
    ([{"id": 4, "source": "action"}], None),  # id du clip absent de moments.json
], ids=["sans-source", "transcript", "action", "source-inconnue", "id-absent"])
def test_moment_source_is_read_from_the_moment(tmp_path, moments, expected):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _moments(config, VIDEO, moments)
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    assert _stats_entry(config)["moment_source"] == expected


def test_moment_source_is_null_when_moments_json_is_absent(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    assert _stats_entry(config)["moment_source"] is None


def test_sync_does_not_rewrite_stats_entries_already_in_the_journal(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _moments(config, VIDEO, [{"id": 3}])
    _scored_account(config, avg_watch_s=13.38)
    outcomes._append({"kind": "stats", "recorded_at": NOW.isoformat(), "video_id": VIDEO, "clip_id": "03",
                      "moment_id": 3, "post_id": POST, "duration": 24.47, "pct_watched": 0.547,
                      "moment_source": None, "stats": {"views_percentile": 0.5, "watched_full": 0.1}},
                     config.section("outcomes")["journal_path"])  # entrée déjà écrite avant la correction
    before = [e for e in _journal(config) if e["kind"] == "stats"]

    learning.sync(NOW, config=config)

    assert [e for e in _journal(config) if e["kind"] == "stats"] == before
    assert _stats_entry(config)["moment_source"] is None


def test_retention_table_sorted_by_pct_watched_with_nulls_last(tmp_path):
    config = _config(tmp_path)
    for i in range(29):
        _stats_row(config, f"{i:02d}", i / 100)
    _stats_row(config, "99", None)

    retention = learning.status(config)["retention"]

    assert retention["n"] == 30 and retention["min_n"] == 30 and retention["message"] is None
    pcts = [row["pct_watched"] for row in retention["rows"]]
    assert pcts == [i / 100 for i in range(28, -1, -1)] + [None]
    assert set(retention["rows"][0]) == {"video_id", "clip_id", "duration", "watched_full", "pct_watched",
                                         "views_percentile", "moment_source"}


def test_retention_below_threshold_says_too_few_and_still_lists_the_table(tmp_path):
    config = _config(tmp_path)
    for i, pct in enumerate([0.2, 0.9, 0.5, None, 0.7]):
        _stats_row(config, f"{i:02d}", pct)

    retention = learning.status(config)["retention"]

    assert retention["n"] == 5
    assert retention["message"] == "n = 5, trop peu pour conclure (minimum 30)"
    assert [row["pct_watched"] for row in retention["rows"]] == [0.9, 0.7, 0.5, 0.2, None]


@pytest.mark.parametrize("bad", [0, -3, 1.5, True, "30"])
def test_retention_min_n_invalid_is_a_named_error(tmp_path, bad):
    config = _config(tmp_path, retention_min_n=bad)

    with pytest.raises(learning.LearningError, match="retention_min_n"):
        learning.status(config)


# ---------------------------------------------------------------- coach des prompts (TASK-c108, SPEC-00db R6)

import re  # noqa: E402
from datetime import timedelta  # noqa: E402

from clipper import jury, jury_coach, llm  # noqa: E402
from clipper.llm.fake import FakeBackend  # noqa: E402

JUDGES = ("retention", "spectateur", "monteur", "avocat", "conformite")
NEW_PERSPECTIVE = "NEUVE perspective de {judge} : {judge} juge la chute autant que l'accroche, avec nuance."


def _coach_config(tmp_path, **learning_overrides) -> Config:
    config = _config(tmp_path, **learning_overrides)
    config._sections["moments"] = {"rubric_path": "builtin"}
    config._sections["jury_coach"] = {"prompts_dir": str(tmp_path / "prompts")}
    return config


def _builtin_rubric(source="builtin"):
    """Bloc ``rubric`` d'un moments.json : la grille embarquee ``source`` (comme l'ecrit l'etape moments)."""
    from clipper import moments as moments_mod
    return {"path": str(moments_mod.resolve_rubric_path(source)), "source": source}


def _coach_world(config, n=12, *, traced=True, at=NOW - timedelta(days=1), rubric="default") -> None:
    """n clips scored (moments 1..n) : sidecar avec transcript, trace du jury, result et stats au journal."""
    journal = config.section("outcomes")["journal_path"]
    moments, scored = [], []
    for k in range(1, n + 1):
        clip_id = f"{k:02d}"
        path = _sidecar(config, clip_id, post_id=f"70000000000001{k:02d}", state="published")
        side = _read(path)
        side.update(transcript=f"texte {k}", source_title="Titre source", screen_title=f"Titre ecran {k}")
        path.write_text(json.dumps(side), encoding="utf-8")
        outcomes.record(VIDEO, clip_id, k, qa={"status": "passed" if k % 2 == 0 else "rejected", "issues": []}, path=journal)
        outcomes._append({"kind": "stats", "video_id": VIDEO, "clip_id": clip_id, "moment_id": k, "recorded_at": at.isoformat(),
                          "stats": {"views_percentile": 1.0 if k % 2 == 0 else 0.0}}, journal)
        wrong = 0 if k % 2 == 0 else 100  # note passee a l'envers du resultat reel
        trace = {"rounds": [{"round": 1, "judges": {j: {"score": wrong, "argument": "..."} for j in JUDGES}}]}
        moments.append({"id": k, "jury": {"trace": trace}} if traced else {"id": k})
        scored.append(f"{VIDEO}/{clip_id}")
    _moments(config, VIDEO, moments, _builtin_rubric() if rubric == "default" else rubric)
    state = Path(config.section("learning")["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    (state / "sync.json").write_text(json.dumps({"last_sync": at.isoformat(), "scored": scored, "results": scored}), encoding="utf-8")


def _coach_responder(request):
    if request.usage == "coach":
        judge = next(j for j in JUDGES if f"juge {j}" in request.prompt)
        return {"perspective": NEW_PERSPECTIVE.format(judge=judge), "justification": "mieux sur les cas"}
    k = int(re.search(r"texte (\d+)", request.prompt)[1])
    good = (k % 2 == 0) == ("NEUVE" in request.prompt)
    return {"scores": {name: 10 if good else 0 for name in request.schema["properties"]["scores"]["properties"]}}


def _coach_file(config) -> dict:
    return _read(Path(config.section("learning")["state_dir"]) / "coach.json")


def test_coach_settings_declared():
    assert learning.CONFIG_DEFAULTS["coach_min_new_cases"] == 10
    assert learning.CONFIG_DEFAULTS["coach_min_interval_days"] == 7


def test_coach_does_not_call_the_llm_below_the_new_cases_threshold(tmp_path):
    config = _coach_config(tmp_path, coach_min_new_cases=13)
    _coach_world(config, n=12)
    fake = FakeBackend([_coach_responder])
    with llm.use_backend(fake):
        assert learning.coach_if_due(NOW, config=config) == []
    assert fake.calls == [] and not (Path(config.section("learning")["state_dir"]) / "coach.json").exists()


def test_coach_does_not_call_the_llm_before_the_interval(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12, at=NOW - timedelta(days=1))
    state = Path(config.section("learning")["state_dir"])
    # 12 clips scored apres last_run : le seuil est atteint, seul l'intervalle (7 j) bloque
    last = (NOW - timedelta(days=3)).isoformat()
    (state / "coach.json").write_text(json.dumps({"last_run": last, "runs": []}), encoding="utf-8")
    _coach_world(config, n=12, at=NOW - timedelta(days=1))
    fake = FakeBackend([_coach_responder])
    with llm.use_backend(fake):
        assert learning.coach_if_due(NOW, config=config) == []
    assert fake.calls == []


def test_coach_counts_only_cases_newer_than_last_run(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12, at=NOW - timedelta(days=20))  # tous anterieurs a last_run
    state = Path(config.section("learning")["state_dir"])
    (state / "coach.json").write_text(json.dumps({"last_run": (NOW - timedelta(days=10)).isoformat(), "runs": []}),
                                      encoding="utf-8")
    fake = FakeBackend([_coach_responder])
    with llm.use_backend(fake):
        assert learning.coach_if_due(NOW, config=config) == []
    assert fake.calls == []


def test_coach_records_every_entry_as_proposed_or_rejected_and_touches_nothing_else(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12)
    config_toml = tmp_path / "config.toml"
    config_toml.write_text("mode = 'auto'\n", encoding="utf-8")
    jury_py = Path(jury.__file__)
    before = (config_toml.read_bytes(), jury_py.read_bytes())
    fake = FakeBackend([_coach_responder])

    with llm.use_backend(fake):
        entries = learning.coach_if_due(NOW, config=config)

    coach = _coach_file(config)
    assert coach["last_run"] == NOW.isoformat() and len(coach["runs"]) == 1
    run = coach["runs"][0]
    assert run["at"] == NOW.isoformat() and run["cases"] == 12
    assert {e["judge"] for e in run["judges"]} == set(JUDGES) - set(jury_coach.EXCLUDED_JUDGES)
    assert run["judges"] == entries
    for entry in run["judges"]:
        assert entry["accepted"] is True and entry["status"] == "proposed"
        assert entry["decided_at"] is None and entry["decided_by"] is None
        assert entry["version"] == 1 and Path(entry["path"]).exists()
        assert entry["metric"]["after"] < entry["metric"]["before"]
        assert NEW_PERSPECTIVE.format(judge=entry["judge"]) in Path(entry["path"]).read_text(encoding="utf-8")
    assert (config_toml.read_bytes(), jury_py.read_bytes()) == before
    assert not (tmp_path / "jury_weights.json").exists()


def test_coach_passes_the_documented_cases_to_propose(tmp_path, monkeypatch):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12)
    seen = {}

    def spy(cases, rubric, judges, **kw):
        seen.update(cases=cases, judges=judges)
        return [{"judge": "retention", "accepted": False, "reason": "pas assez de cas connus", "version": None,
                 "path": None, "metric": None}]

    monkeypatch.setattr(jury_coach, "propose", spy)
    entries = learning.coach_if_due(NOW, config=config)

    first = seen["cases"][0]
    assert set(first) == {"video_id", "moment_id", "text", "context", "trace", "rubric"}
    assert first["rubric"]["id"] == "builtin" and "emotion" in first["rubric"]["criteria"]
    assert first["video_id"] == VIDEO and first["moment_id"] == 1 and first["text"] == "texte 1"
    assert first["context"] == "Titre source — Titre ecran 1" and "rounds" in first["trace"]
    assert len(seen["cases"]) == 12
    assert set(seen["judges"]) == set(JUDGES)  # perspectives actives du jury, defauts de clipper.jury
    assert seen["judges"]["retention"] == jury.CONFIG_DEFAULTS["judges"]["retention"]["perspective"]
    assert entries[0]["status"] == "rejected" and entries[0]["reason"] == "pas assez de cas connus"


def test_coach_skips_clips_without_trace(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12, traced=False)
    fake = FakeBackend([_coach_responder])
    with llm.use_backend(fake):
        assert learning.coach_if_due(NOW, config=config) == []
    assert fake.calls == []


def test_coach_disabled_does_nothing(tmp_path):
    config = _coach_config(tmp_path, enabled=False)
    _coach_world(config, n=12)
    fake = FakeBackend([_coach_responder])
    with llm.use_backend(fake):
        assert learning.coach_if_due(NOW, config=config) == []
    assert fake.calls == []


def test_run_if_due_chains_the_coach_after_the_calibration(tmp_path, monkeypatch):
    config = _coach_config(tmp_path)
    order = []
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: order.append("link") or [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: True)
    monkeypatch.setattr(learning, "sync", lambda *a, **k: order.append("sync"))
    monkeypatch.setattr(learning, "coach_if_due", lambda *a, **k: order.append("coach") or [])

    result = learning.run_if_due(NOW, config=config)

    assert order == ["link", "sync", "coach"] and result["synced"] is True


def test_run_if_due_writes_a_coach_error_in_sync_json_and_logs_it_once(tmp_path, monkeypatch, caplog):
    config = _coach_config(tmp_path)
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: False)

    def boom(*a, **k):
        raise jury_coach.CoachError("statut qa inconnu 'x' (clip '01')")

    monkeypatch.setattr(learning, "coach_if_due", boom)
    learning._logged_coach_errors.clear()
    with caplog.at_level("ERROR", logger="clipper.learning"):
        learning.run_if_due(NOW, config=config)
        learning.run_if_due(NOW, config=config)

    error = _read(Path(config.section("learning")["state_dir"]) / "sync.json")["last_error"]
    assert error["where"] == "coach" and "statut qa inconnu" in error["message"] and error["at"] == NOW.isoformat()
    assert len([r for r in caplog.records if "statut qa inconnu" in r.getMessage()]) == 1


def test_decide_proposal_marks_it_once_and_unknown_is_404(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12)
    with llm.use_backend(FakeBackend([_coach_responder])):
        learning.coach_if_due(NOW, config=config)

    entry = learning.decide_proposal(config, "retention", 1, "adopted", by="web", now=NOW)
    assert entry["status"] == "adopted" and entry["decided_by"] == "web" and entry["decided_at"] == NOW.isoformat()
    assert learning.proposal_perspective(entry["path"]) == NEW_PERSPECTIVE.format(judge="retention")
    with pytest.raises(learning.ProposalError) as again:
        learning.decide_proposal(config, "retention", 1, "refused", by="web")
    assert again.value.status == 409
    with pytest.raises(learning.ProposalError) as unknown:
        learning.decide_proposal(config, "retention", 9, "refused", by="web")
    assert unknown.value.status == 404


# ---------------------------------------------------------------- bilan des VOD de veille (TASK-9dac, SPEC-00db R8)


def _veille_dir(config) -> Path:
    path = Path(config.section("veille")["state_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _bilan_config(tmp_path, **learning_overrides) -> Config:
    config = _config(tmp_path, **learning_overrides)
    config._sections["veille"] = {"state_dir": str(tmp_path / "veille")}
    config._sections["worker"] = {"queue_path": str(tmp_path / "queue.json")}
    return config


def _worker_queue(config, *videos):
    """File d'attente du worker : ces VOD sont encore en traitement (waiting ou running)."""
    path = Path(config.section("worker")["queue_path"])
    path.write_text(json.dumps([{"id": f"q{i}", "video_id": v, "status": "running" if i == 0 else "waiting"}
                                for i, v in enumerate(videos)]), encoding="utf-8")


def _queue_vod(config, video, *, day="2026-10-05", at="2026-10-05T08:00:00+00:00", title=None):
    """Une VOD mise en file : seen.json (queued) + instantané du candidat dans days/<jour>.json."""
    sdir = _veille_dir(config)
    seen_path = sdir / "seen.json"
    seen = _read(seen_path) if seen_path.exists() else {"queued": [], "ignored": []}
    cid = f"twitch:{video}"
    seen["queued"].append({"candidate_id": cid, "video_id": video, "url": f"https://x/{video}", "date": day,
                           "channel": "chaine", "queue_entry_id": "q", "at": at})
    seen_path.write_text(json.dumps(seen), encoding="utf-8")
    day_path = sdir / "days" / f"{day}.json"
    day_path.parent.mkdir(parents=True, exist_ok=True)
    state = _read(day_path) if day_path.exists() else {"proposals": []}
    state["proposals"].append({"candidate_id": cid, "candidate": {
        "id": cid, "source": "twitch", "video_id": video, "title": title or f"Titre {video}",
        "channel_name": "streamer_a", "game_name": "Jeu Alpha"}})
    day_path.write_text(json.dumps(state), encoding="utf-8")


def _bilan(config) -> dict:
    return _read(Path(config.section("veille")["state_dir"]) / "bilan.json")


def _by_video(bilan) -> dict:
    return {e["video_id"]: e for e in bilan["entries"]}


def test_veille_report_settings_declared():
    assert learning.CONFIG_DEFAULTS["veille_report_days"] == 30
    assert learning.CONFIG_DEFAULTS["veille_report_max"] == 20


def test_veille_report_gives_figures_from_the_journal_and_a_reason_when_absent(tmp_path):
    config = _bilan_config(tmp_path)
    _linked_clip(config, "03")  # VIDEO : clip publie et mur, compte eligible
    _scored_account(config, clip_views=750)
    learning.sync(NOW, config=config)
    _queue_vod(config, VIDEO, title="Un titre")
    _queue_vod(config, "NOCLIPVIDEO")
    _sidecar(config, "01", video="NOTPUBLISHED", post_id=None)  # sidecar sans tiktok_post renseigne : voir plus bas
    path = Path(config.output_dir) / "NOTPUBLISHED" / "01.json"
    path.write_text(json.dumps({"caption": "c"}), encoding="utf-8")
    _queue_vod(config, "NOTPUBLISHED")

    learning.write_veille_report(NOW, config=config)

    bilan = _bilan(config)
    assert bilan["days"] == 30 and bilan["computed_at"].startswith("2026-10-10")
    entries = _by_video(bilan)
    stats = [e for e in _journal(config) if e["kind"] == "stats"][0]["stats"]
    assert entries[VIDEO] == {
        "picked_on": "2026-10-05", "candidate_id": f"twitch:{VIDEO}", "source": "twitch", "game_name": "Jeu Alpha",
        "channel_name": "streamer_a", "title": "Un titre", "video_id": VIDEO, "clips_produced": 1, "processing": False,
        "clips_published": 1, "clips_mature": 1,
        "views_percentile_mean": stats["views_percentile"], "views_at_maturity_max": stats["views_at_maturity"],
        "missing": None}
    assert stats["views_at_maturity"] == 750
    for video, reason, published in (("NOCLIPVIDEO", "no_clips", 0), ("NOTPUBLISHED", "not_published", 0)):
        assert entries[video]["missing"] == reason and entries[video]["clips_published"] == published
        assert entries[video]["views_percentile_mean"] is None and entries[video]["views_at_maturity_max"] is None
        assert entries[video]["clips_mature"] == 0


def test_veille_report_counts_the_clips_a_vod_really_produced(tmp_path):
    """Constat 08/10 : VOD choisie (id source twitch:…) dont le worker a produit des clips sous v… : jamais « aucun clip »."""
    config = _bilan_config(tmp_path)
    for clip in ("00-p1", "00-p2", "00-p3"):
        _sidecar(config, clip, video="v2894088024", post_id=None)
        path = Path(config.output_dir) / "v2894088024" / f"{clip}.json"
        path.write_text(json.dumps({"caption": "c"}), encoding="utf-8")
    _queue_vod(config, "v2894088024")

    learning.write_veille_report(NOW, config=config)

    entry = _by_video(_bilan(config))["v2894088024"]
    assert entry["candidate_id"] == "twitch:v2894088024"
    assert (entry["clips_produced"], entry["clips_published"], entry["processing"]) == (3, 0, False)
    assert entry["missing"] == "not_published"


def test_veille_report_flags_a_vod_still_in_the_worker_queue_as_processing(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, "RUNNING1")
    _queue_vod(config, "WAITING1")
    _worker_queue(config, "RUNNING1", "WAITING1")

    learning.write_veille_report(NOW, config=config)

    entries = _by_video(_bilan(config))
    for video in ("RUNNING1", "WAITING1"):
        assert entries[video]["processing"] is True and entries[video]["missing"] == "processing"
        assert entries[video]["clips_produced"] == 0


def test_veille_report_a_vod_off_the_queue_without_clip_is_a_real_zero(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, "EMPTYVOD")
    _worker_queue(config, "OTHERVOD")

    learning.write_veille_report(NOW, config=config)

    entry = _by_video(_bilan(config))["EMPTYVOD"]
    assert (entry["clips_produced"], entry["processing"], entry["missing"]) == (0, False, "no_clips")


def test_veille_report_unreadable_worker_queue_is_an_error_naming_the_file(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, VIDEO)
    Path(config.section("worker")["queue_path"]).write_text("{pas du json", encoding="utf-8")
    with pytest.raises(learning.LearningError, match="queue.json"):
        learning.write_veille_report(NOW, config=config)


def test_run_if_due_refreshes_the_veille_report_even_without_a_new_snapshot(tmp_path, monkeypatch):
    """Le bilan n'attend plus un relevé TikTok : sinon il reste figé avant que les clips existent."""
    config = _bilan_config(tmp_path)
    calls = []
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: False)
    monkeypatch.setattr(learning, "coach_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "write_veille_report", lambda *a, **k: calls.append("bilan"))

    learning.run_if_due(NOW, config=config)

    assert calls == ["bilan"]


def _rewrite_seen(config, video, **fields):
    """Complète l'entrée seen.queued de ``video`` (instantané écrit à la décision, SPEC-8a45 R32)."""
    path = _veille_dir(config) / "seen.json"
    seen = _read(path)
    for entry in seen["queued"]:
        if entry["video_id"] == video:
            entry.update(fields)
    path.write_text(json.dumps(seen), encoding="utf-8")


def _drop_proposals(config, day="2026-10-05"):
    path = _veille_dir(config) / "days" / f"{day}.json"
    state = _read(path)
    state["proposals"] = []
    path.write_text(json.dumps(state), encoding="utf-8")


def test_veille_report_reads_the_seen_snapshot_when_the_day_file_lost_the_proposal(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, "REPLAYED", title="Titre du fichier")
    _rewrite_seen(config, "REPLAYED", source="youtube", title="Titre gardé", game_name="Jeu Beta",
                  channel_name="streamer_b")
    _drop_proposals(config)  # relevé rejoué : la proposition a disparu du jour

    learning.write_veille_report(NOW, config=config)

    entry = _by_video(_bilan(config))["REPLAYED"]
    assert (entry["source"], entry["title"], entry["game_name"], entry["channel_name"]) == (
        "youtube", "Titre gardé", "Jeu Beta", "streamer_b")
    assert entry["clips_published"] == 0 and entry["missing"] == "no_clips"


def test_veille_report_falls_back_to_the_day_file_for_an_entry_without_snapshot(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, "OLDENTRY", title="Titre du fichier")  # seen sans title (ancien format)

    learning.write_veille_report(NOW, config=config)

    entry = _by_video(_bilan(config))["OLDENTRY"]
    assert (entry["source"], entry["title"], entry["game_name"], entry["channel_name"]) == (
        "twitch", "Titre du fichier", "Jeu Alpha", "streamer_a")


def test_veille_report_gives_null_when_neither_seen_nor_the_day_file_knows_the_vod(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, "LOSTENTRY")
    _drop_proposals(config)

    learning.write_veille_report(NOW, config=config)

    entry = _by_video(_bilan(config))["LOSTENTRY"]
    assert (entry["source"], entry["title"], entry["game_name"], entry["channel_name"]) == (None, None, None, None)


def test_veille_report_immature_and_account_below_min(tmp_path):
    config = _bilan_config(tmp_path)
    _linked_clip(config, "03")  # jeune : un seul releve recent
    _stats_snapshot(config, "2026-10-09T12:00:00+00:00", {**_others(range(100, 1100, 100)), POST: ("2026-10-08T09:00:00", 5, {})})
    _linked_clip(config, "01", post_id="7000000000000000202", account=OTHER, video="OTHERVIDEO")
    _stats_snapshot(config, "2026-10-01T12:00:00+00:00", {"7000000000000000202": ("2026-09-20T09:00:00", 50, {})}, account=OTHER)
    _queue_vod(config, VIDEO)
    _queue_vod(config, "OTHERVIDEO")
    learning.sync(NOW, config=config)

    learning.write_veille_report(NOW, config=config)

    entries = _by_video(_bilan(config))
    assert entries[VIDEO]["missing"] == "immature" and entries[VIDEO]["clips_published"] == 1
    assert entries["OTHERVIDEO"]["missing"] == "account_below_min"
    assert entries[VIDEO]["views_at_maturity_max"] is None


def test_veille_report_keeps_recent_vods_newest_first_within_the_cap(tmp_path):
    config = _bilan_config(tmp_path, veille_report_days=10, veille_report_max=2)
    _queue_vod(config, "OLDVIDEO", day="2026-09-20", at="2026-09-20T08:00:00+00:00")
    _queue_vod(config, "VIDEOA", day="2026-10-03", at="2026-10-03T08:00:00+00:00")
    _queue_vod(config, "VIDEOB", day="2026-10-09", at="2026-10-09T08:00:00+00:00")
    _queue_vod(config, "VIDEOC", day="2026-10-06", at="2026-10-06T08:00:00+00:00")

    learning.write_veille_report(NOW, config=config)

    bilan = _bilan(config)
    assert bilan["days"] == 10
    assert [e["video_id"] for e in bilan["entries"]] == ["VIDEOB", "VIDEOC"]


def test_veille_report_without_seen_file_writes_an_empty_report(tmp_path):
    config = _bilan_config(tmp_path)
    learning.write_veille_report(NOW, config=config)
    assert _bilan(config)["entries"] == []


def test_veille_report_twice_gives_the_same_content(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, VIDEO)
    learning.write_veille_report(NOW, config=config)
    first = _bilan(config)
    learning.write_veille_report(NOW + timedelta(hours=1), config=config)
    second = _bilan(config)
    # TASK-1e46f : contenu inchangé = fichier non réécrit, donc computed_at reste celui du premier calcul
    assert first["computed_at"] == second["computed_at"]
    assert {**first, "computed_at": None} == {**second, "computed_at": None}


def test_veille_report_unreadable_seen_is_an_error_naming_the_file(tmp_path):
    config = _bilan_config(tmp_path)
    (_veille_dir(config) / "seen.json").write_text("{pas du json", encoding="utf-8")
    with pytest.raises(learning.LearningError, match="seen.json"):
        learning.write_veille_report(NOW, config=config)


def test_run_if_due_writes_the_veille_report_after_each_sync(tmp_path, monkeypatch):
    config = _bilan_config(tmp_path)
    order = []
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: True)
    monkeypatch.setattr(learning, "sync", lambda *a, **k: order.append("sync"))
    monkeypatch.setattr(learning, "coach_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "write_veille_report", lambda *a, **k: order.append("bilan"))

    learning.run_if_due(NOW, config=config)

    assert order == ["sync", "bilan"]


# --------------------------------------------------------------------------
# TASK-5a7b750462c4 : un post supprimé de la plateforme n'est jamais un résultat à 0 vue
# --------------------------------------------------------------------------


def _mark_removed(path: Path) -> None:
    side = _read(path)
    side["removed_from_platform"] = {"at": "2026-10-08T10:00:00+00:00", "reason": "mal cadré"}
    path.write_text(json.dumps(side), encoding="utf-8")


def test_sync_ignores_a_post_removed_from_the_platform(tmp_path):
    config = _config(tmp_path)
    _mark_removed(_linked_clip(config, "03"))
    _scored_account(config)  # POST reste dans les relevés : le clip 03 serait noté sinon

    sync = _sync(config)

    assert _journal(config) == []
    assert {"video_id": VIDEO, "clip_id": "03", "account": ACCOUNT, "reason": "removed_from_platform"} in sync["excluded"]
    assert all(r != "03" for r in sync["results"]) and all(r != f"{VIDEO}/03" for r in sync["scored"])


def test_a_removed_post_with_no_view_does_not_enter_the_calibration(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _mark_removed(_linked_clip(config, "03"))
    _scored_account(config, clip_views=0)
    seen = []
    monkeypatch.setattr(learning, "_calibrate", lambda linked, *a, **k: seen.append(list(linked)) or {})

    learning.sync(NOW, config=config)

    assert seen == [] and _journal(config) == []


def test_link_posts_does_not_attach_a_removed_clip(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "clip-02")
    _mark_removed(side)
    _entry(config, "clip-02")
    _snapshot(config, "2026-10-07T10:00:00+00:00", ("7000000000000000001", "Un super clip #jeu #fun", "2026-10-07T09:02:00"))

    result = learning.link_posts(ACCOUNT, config=config, now=NOW)

    assert result["linked"] == [] and not _read(side)["tiktok_post"].get("id")


def test_coach_llm_calls_go_to_the_coach_own_usage_log_not_the_one_of_a_video_in_progress(tmp_path):
    config = _coach_config(tmp_path)
    _coach_world(config, n=12)
    video_log = tmp_path / "workspace" / VIDEO / "llm_usage.jsonl"
    fake = FakeBackend([_coach_responder])

    with llm.use_backend(fake), llm.usage_log(video_log):  # la reprise d'une vidéo journalise ses propres appels
        learning.coach_if_due(NOW, config=config)

    assert not video_log.exists()
    own = Path(config.section("learning")["state_dir"]) / "llm_usage.jsonl"
    assert {json.loads(line)["usage"] for line in own.read_text(encoding="utf-8").splitlines()} >= {"coach"}


# ---------------------------------------------------------------- alerte « 0 vue à 24 h » (TASK-974e)

ALERT_POSTED = "2026-10-08T09:00:00+00:00"  # 52 h avant NOW : au-delà de 24 h
RECENT_POSTED = "2026-10-10T03:00:00+00:00"  # 10 h avant NOW : en deçà de 24 h


def _reading(config, fetched_at, *rows, account=ACCOUNT, origin="full") -> None:
    """Un relevé complet : rows = (post_id, posted_at, views)."""
    snapshot = {"account": account, "fetched_at": fetched_at, "source": "tiktok_studio", "origin": origin,
                "overview": {}, "posts": [
                    {"post_id": pid, "post_url": f"https://www.tiktok.com/@x/video/{pid}", "caption": "légende",
                     "posted_at": posted_at, "views": views} for pid, posted_at, views in rows]}
    tiktok.append_snapshot(account, tiktok.get_settings(config), snapshot)


def _accounts(config, *paused) -> None:
    path = Path(config.section("accounts")["state_file"])
    path.write_text(json.dumps({"accounts": [
        {"id": pid, "label": pid, "service": "tiktok", "paused_at": "2026-10-09T10:00:00+00:00" if pid in paused else None}
        for pid in (ACCOUNT, OTHER)]}), encoding="utf-8")


def test_zero_view_alert_settings_declared():
    assert learning.CONFIG_DEFAULTS["zero_view_alert_hours"] == 24
    assert learning.CONFIG_DEFAULTS["zero_view_alert_max_views"] == 0
    assert learning.CONFIG_DEFAULTS["zero_view_alert_account_min"] == 2


def test_zero_view_alert_settings_invalid_is_a_named_error(tmp_path):
    config = _config(tmp_path, zero_view_alert_hours=0)
    with pytest.raises(learning.LearningError, match="zero_view_alert_hours"):
        learning.zero_view_alerts(NOW, config=config)


def test_post_with_no_view_after_24h_is_an_alert(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, ALERT_POSTED, 0))

    alerts = learning.zero_view_alerts(NOW, config=config)

    assert alerts["no_reading"] == []
    assert len(alerts["accounts"]) == 1
    account = alerts["accounts"][0]
    assert (account["account"], account["level"]) == (ACCOUNT, "post")
    assert account["posts"] == [{"video_id": VIDEO, "clip_id": "01", "post_id": POST, "posted_at": ALERT_POSTED,
                                 "views": 0, "read_at": "2026-10-10T12:00:00+00:00"}]


def test_post_with_no_view_before_24h_is_not_an_alert(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, RECENT_POSTED, 0))

    alerts = learning.zero_view_alerts(NOW, config=config)

    assert alerts["accounts"] == [] and alerts["no_reading"] == []


def test_post_with_views_is_not_an_alert(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, ALERT_POSTED, 3))

    assert learning.zero_view_alerts(NOW, config=config)["accounts"] == []


def test_post_without_reading_after_24h_is_no_reading_never_zero(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST, publish_at=ALERT_POSTED)
    _reading(config, "2026-10-10T12:00:00+00:00", ("7000000000000000999", ALERT_POSTED, 500))

    alerts = learning.zero_view_alerts(NOW, config=config)

    assert alerts["accounts"] == []
    assert alerts["no_reading"] == [{"video_id": VIDEO, "clip_id": "01", "account": ACCOUNT, "post_id": POST,
                                     "reason": "no_reading"}]


def test_two_zero_view_posts_of_one_account_give_an_account_alert(tmp_path):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST)
    _sidecar(config, "02", post_id=OTHER_POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, ALERT_POSTED, 0), (OTHER_POST, ALERT_POSTED, 0))

    alerts = learning.zero_view_alerts(NOW, config=config)

    assert len(alerts["accounts"]) == 1
    account = alerts["accounts"][0]
    assert (account["account"], account["level"]) == (ACCOUNT, "account")
    assert sorted(p["post_id"] for p in account["posts"]) == sorted([POST, OTHER_POST])


def test_account_min_and_hours_are_configurable(tmp_path):
    config = _config(tmp_path, zero_view_alert_hours=10, zero_view_alert_account_min=3)
    _sidecar(config, "01", post_id=POST)
    _sidecar(config, "02", post_id=OTHER_POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, RECENT_POSTED, 0), (OTHER_POST, ALERT_POSTED, 0))

    account = learning.zero_view_alerts(NOW, config=config)["accounts"][0]

    assert account["level"] == "post"  # 2 posts < 3 : pas d'alerte de compte
    assert len(account["posts"]) == 2  # 10 h : le post de 10 h est déjà en alerte


def test_removed_deleted_and_paused_posts_are_ignored(tmp_path):
    config = _config(tmp_path)
    _mark_removed(_sidecar(config, "01", post_id=POST))  # supprimé de la plateforme (TASK-5a7b)
    _sidecar(config, "02", post_id=OTHER_POST)  # présent au premier relevé complet, absent du dernier : supprimé
    _sidecar(config, "03", account=OTHER, post_id=PAUSED_POST)  # compte en pause
    _reading(config, "2026-10-09T12:00:00+00:00", (POST, ALERT_POSTED, 0), (OTHER_POST, ALERT_POSTED, 0))
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, ALERT_POSTED, 0))
    _reading(config, "2026-10-10T12:00:00+00:00", (PAUSED_POST, ALERT_POSTED, 0), account=OTHER)
    _accounts(config, OTHER)

    alerts = learning.zero_view_alerts(NOW, config=config)

    assert alerts["accounts"] == []
    assert alerts["no_reading"] == []


def test_zero_view_alert_is_logged_once_per_post(tmp_path, caplog):
    config = _config(tmp_path)
    _sidecar(config, "01", post_id=POST)
    _reading(config, "2026-10-10T12:00:00+00:00", (POST, ALERT_POSTED, 0))

    with caplog.at_level(logging.WARNING, logger="clipper.learning"):
        learning.log_zero_view_alerts(NOW, config=config)
        learning.log_zero_view_alerts(NOW, config=config)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and POST in r.getMessage()]
    assert len(warnings) == 1


def test_run_if_due_logs_zero_view_alerts(tmp_path, monkeypatch):
    config = _config(tmp_path)
    calls = []
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: False)
    monkeypatch.setattr(learning, "coach_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "write_veille_report", lambda *a, **k: None)
    monkeypatch.setattr(learning, "log_zero_view_alerts", lambda *a, **k: calls.append("alerte"))

    learning.run_if_due(NOW, config=config)

    assert calls == ["alerte"]


def test_truncated_outcomes_journal_is_an_explicit_error_naming_the_file(tmp_path):
    config = _config(tmp_path)
    journal = Path(config.section("outcomes")["journal_path"])
    journal.write_text('{"kind": "stats", "video_id": "VVVVVVVVVVV"\n', encoding="utf-8")

    with pytest.raises(learning.LearningError, match="outcomes.jsonl"):
        learning.status(config)


# ---------------------------------------------------------------- vues médianes par jeu, streamer, heure, compte (TASK-3034e047c2d4)

def _bd_config(tmp_path, **learning_overrides) -> Config:
    config = _config(tmp_path, **learning_overrides)
    config._sections["veille"] = {"state_dir": str(tmp_path / "veille")}
    return config


def _bd_meta(config, video, **fields):
    path = Path(config.workspace_dir) / video / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def _bd_day(tmp_path, day, candidates):
    path = tmp_path / "veille" / "days" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day, "candidates": candidates}), encoding="utf-8")


def _bd_row(config, video, clip, views, *, account="acc1", posted_at="2026-10-01 14:30", pct=None, **extra):
    stats = {"views_at_maturity": views, "views": views}
    outcomes._append({"kind": "stats", "video_id": video, "clip_id": clip, "moment_id": 1, "post_id": f"{video}{clip}",
                      "account": account, "posted_at": posted_at, "pct_watched": pct, "stats": stats, **extra},
                     config.section("outcomes")["journal_path"])


def _groups(config, name):
    return {g["key"]: g for g in learning.breakdown(config)["groups"][name]}


def test_breakdown_min_n_declared():
    assert learning.CONFIG_DEFAULTS["breakdown_min_n"] == 5


def test_breakdown_median_exact_best_clip_and_sort(tmp_path):
    config = _bd_config(tmp_path)
    _bd_meta(config, "vA", game="Zelda", channel="Alice")
    _bd_meta(config, "vB", game="Mario", channel="Bob")
    for clip, views in (("01", 100), ("02", 400), ("03", 200), ("04", 1000)):
        _bd_row(config, "vA", clip, views, pct=0.5 if clip == "01" else None)
    _bd_row(config, "vB", "01", 900)

    result = learning.breakdown(config)

    assert result["n"] == 5 and result["min_n"] == 5 and result["skipped"] == 0
    games = result["groups"]["game"]
    assert [g["key"] for g in games] == ["Mario", "Zelda"]  # 900 puis médiane (200+400)/2 = 300
    zelda = games[1]
    assert zelda == {"key": "Zelda", "label": "Zelda", "n": 4, "median_views": 300, "median_pct_watched": 0.5,
                     "best": {"video_id": "vA", "clip_id": "04", "views": 1000}, "few": True}
    assert games[0]["median_pct_watched"] is None
    assert {g["key"] for g in result["groups"]["streamer"]} == {"Alice", "Bob"}
    assert set(result["groups"]) == {"game", "streamer", "hour", "account"}


def test_breakdown_game_from_meta_then_veille_then_unknown_with_v_prefix(tmp_path):
    config = _bd_config(tmp_path)
    _bd_meta(config, "v111", game="Meta Game")
    _bd_meta(config, "v222")  # meta sans jeu : la veille répond
    _bd_day(tmp_path, "2026-10-05", [{"source": "twitch", "video_id": "111", "game_name": "Veille Ancien"},
                                      {"source": "twitch", "video_id": "222", "game_name": "Veille Ancien"}])
    _bd_day(tmp_path, "2026-10-07", [{"source": "twitch", "video_id": "222", "game_name": "Veille Récent"}])
    for video in ("v111", "v222", "v333"):
        _bd_row(config, video, "01", 10)

    games = _groups(config, "game")

    assert set(games) == {"Meta Game", "Veille Récent", "inconnu"}
    assert games["inconnu"]["label"] == "jeu inconnu"


def test_breakdown_streamer_from_meta_channel_else_unknown(tmp_path):
    config = _bd_config(tmp_path)
    _bd_meta(config, "vA", channel="Alice")
    _bd_row(config, "vA", "01", 10)
    _bd_row(config, "vZ", "01", 20)

    streamers = _groups(config, "streamer")

    assert set(streamers) == {"Alice", "inconnu"} and streamers["inconnu"]["label"] == "streamer inconnu"


def test_breakdown_hour_is_naive_paris_hour_without_conversion_and_falls_back_to_slot_at(tmp_path):
    config = _bd_config(tmp_path)
    _bd_row(config, "vA", "01", 10, posted_at="2026-10-01 23:59")
    _bd_row(config, "vA", "02", 30, posted_at="2026-10-02 23:05")
    _bd_row(config, "vA", "03", 50, posted_at=None, slot_at="2026-10-02T08:15:00")

    hours = _groups(config, "hour")

    assert set(hours) == {"23", "08"}
    assert hours["23"]["n"] == 2 and hours["23"]["median_views"] == 20
    assert hours["23"]["label"] == "23 h" and hours["08"]["median_views"] == 50


def test_breakdown_account_group(tmp_path):
    config = _bd_config(tmp_path)
    _bd_row(config, "vA", "01", 10, account="a")
    _bd_row(config, "vA", "02", 70, account="b")

    assert {k: g["median_views"] for k, g in _groups(config, "account").items()} == {"a": 10, "b": 70}


def test_breakdown_few_flag_follows_breakdown_min_n_and_never_hides(tmp_path):
    config = _bd_config(tmp_path, breakdown_min_n=2)
    _bd_row(config, "vA", "01", 10, account="a")
    _bd_row(config, "vA", "02", 20, account="a")
    _bd_row(config, "vA", "03", 30, account="b")

    accounts = _groups(config, "account")

    assert accounts["a"]["few"] is False and accounts["b"]["few"] is True and accounts["b"]["n"] == 1


def test_breakdown_skips_entries_without_views_and_counts_them(tmp_path):
    config = _bd_config(tmp_path)
    _bd_row(config, "vA", "01", 100)
    _bd_row(config, "vA", "02", None)
    outcomes._append({"kind": "stats", "video_id": "vA", "clip_id": "03", "account": "acc1", "stats": {}},
                     config.section("outcomes")["journal_path"])
    outcomes.record("vA", "04", 4, qa=None, human_decision=None, path=config.section("outcomes")["journal_path"])

    result = learning.breakdown(config)

    assert result["n"] == 1 and result["skipped"] == 2
    assert result["groups"]["account"][0]["median_views"] == 100


def test_breakdown_empty_journal_gives_empty_groups(tmp_path):
    result = learning.breakdown(_bd_config(tmp_path))

    assert result["n"] == 0 and result["groups"] == {"game": [], "streamer": [], "hour": [], "account": []}


def test_breakdown_unreadable_journal_is_a_named_error(tmp_path):
    config = _bd_config(tmp_path)
    Path(config.section("outcomes")["journal_path"]).write_text("{pas du json\n", encoding="utf-8")

    with pytest.raises(learning.LearningError, match="outcomes.jsonl"):
        learning.breakdown(config)


def test_breakdown_unreadable_meta_or_veille_day_is_a_named_error(tmp_path):
    config = _bd_config(tmp_path)
    _bd_row(config, "vA", "01", 10)
    path = Path(config.workspace_dir) / "vA" / "meta.json"
    path.parent.mkdir(parents=True)
    path.write_text("{cassé", encoding="utf-8")
    with pytest.raises(learning.LearningError, match="meta.json"):
        learning.breakdown(config)
    path.write_text("{}", encoding="utf-8")
    day = tmp_path / "veille" / "days" / "2026-10-01.json"
    day.parent.mkdir(parents=True)
    day.write_text("{cassé", encoding="utf-8")
    with pytest.raises(learning.LearningError, match="2026-10-01.json"):
        learning.breakdown(config)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "5"])
def test_breakdown_min_n_invalid_is_a_named_error(tmp_path, bad):
    with pytest.raises(learning.LearningError, match="breakdown_min_n"):
        learning.breakdown(_bd_config(tmp_path, breakdown_min_n=bad))


def test_status_carries_the_breakdown(tmp_path):
    config = _bd_config(tmp_path)
    _bd_row(config, "vA", "01", 10)

    assert learning.status(config)["breakdown"]["n"] == 1


# ---------------------------------------------------------------- bilan réécrit seulement quand son contenu change (TASK-1e46f)

SENTINEL_MTIME = 1_000_000_000  # une date fixe : un bilan réécrit en sort, un bilan intact y reste


def _age_bilan(config) -> Path:
    path = Path(config.section("veille")["state_dir"]) / "bilan.json"
    os.utime(path, (SENTINEL_MTIME, SENTINEL_MTIME))
    return path


def _quiet_run(monkeypatch) -> None:
    """Un passage du worker sans réseau ni relevé : seul le bilan est en jeu."""
    monkeypatch.setattr(learning, "link_if_due", lambda *a, **k: [])
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: False)
    monkeypatch.setattr(learning, "coach_if_due", lambda *a, **k: [])


def test_veille_report_keeps_the_file_when_only_computed_at_would_change(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, VIDEO)
    learning.write_veille_report(NOW, config=config)
    path = _age_bilan(config)

    learning.write_veille_report(NOW + timedelta(minutes=5), config=config)

    assert path.stat().st_mtime == SENTINEL_MTIME
    assert _bilan(config)["computed_at"].startswith("2026-10-10T13:00")


def test_veille_report_rewrites_the_file_when_its_content_changes(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, VIDEO)
    learning.write_veille_report(NOW, config=config)
    path = _age_bilan(config)
    _queue_vod(config, "NEWVOD")

    learning.write_veille_report(NOW, config=config)

    assert path.stat().st_mtime != SENTINEL_MTIME
    assert "NEWVOD" in _by_video(_bilan(config))


def test_veille_report_returns_the_report_even_when_the_file_is_kept(tmp_path):
    config = _bilan_config(tmp_path)
    _queue_vod(config, VIDEO)
    learning.write_veille_report(NOW, config=config)

    report = learning.write_veille_report(NOW + timedelta(minutes=5), config=config)

    assert report["computed_at"].startswith("2026-10-10T13:05")
    assert report["entries"] == _bilan(config)["entries"]


def test_veille_report_min_interval_is_declared_and_validated(tmp_path):
    assert learning.CONFIG_DEFAULTS["veille_report_min_interval_s"] == 60
    for bad in (-1, True, "60"):
        config = _config(tmp_path, veille_report_min_interval_s=bad)
        with pytest.raises(learning.LearningError, match="veille_report_min_interval_s"):
            learning.run_if_due(NOW, config=config)


def test_run_if_due_does_not_recompute_the_veille_report_twice_within_the_interval(tmp_path, monkeypatch):
    config = _bilan_config(tmp_path)
    _quiet_run(monkeypatch)
    _queue_vod(config, VIDEO)
    learning.run_if_due(NOW, config=config)
    path = _age_bilan(config)

    learning.run_if_due(NOW + timedelta(seconds=2), config=config)

    assert path.stat().st_mtime == SENTINEL_MTIME


def test_run_if_due_rewrites_the_veille_report_once_the_interval_has_passed(tmp_path, monkeypatch):
    config = _bilan_config(tmp_path)
    _quiet_run(monkeypatch)
    _queue_vod(config, VIDEO)
    learning.run_if_due(NOW, config=config)
    _queue_vod(config, "NEWVOD")
    path = _age_bilan(config)

    learning.run_if_due(NOW + timedelta(seconds=30), config=config)
    assert path.stat().st_mtime == SENTINEL_MTIME and "NEWVOD" not in _by_video(_bilan(config))

    learning.run_if_due(NOW + timedelta(seconds=61), config=config)
    assert "NEWVOD" in _by_video(_bilan(config))


def test_run_if_due_recomputes_the_veille_report_at_once_when_a_sync_ran(tmp_path, monkeypatch):
    config = _bilan_config(tmp_path)
    _quiet_run(monkeypatch)
    monkeypatch.setattr(learning, "_snapshot_newer_than_sync", lambda *a, **k: True)
    monkeypatch.setattr(learning, "sync", lambda *a, **k: {})
    calls = []
    real = learning.write_veille_report
    monkeypatch.setattr(learning, "write_veille_report", lambda *a, **k: calls.append(1) or real(*a, **k))

    learning.run_if_due(NOW, config=config)
    learning.run_if_due(NOW + timedelta(seconds=2), config=config)

    assert len(calls) == 2


def test_an_immature_clip_does_not_enter_the_calibration(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _linked_clip(config, "04", post_id=OTHER_POST)  # relié mais sans aucune statistique à maturité
    for fetched, own in (("2026-09-21T12:00:00+00:00", 10), ("2026-09-24T12:00:00+00:00", 750), ("2026-10-10T12:00:00+00:00", 2000)):
        _stats_snapshot(config, fetched, {**_others(range(100, 1100, 100)), POST: (CLIP_POSTED, own, {}),
                                          OTHER_POST: ("2026-10-09T09:00:00", 5, {})})
    seen = []
    monkeypatch.setattr(learning, "_calibrate", lambda linked, *a, **k: seen.append(list(linked)) or {})

    sync = _sync(config)

    assert seen == [[(VIDEO, "03", 3)]]
    assert {"video_id": VIDEO, "clip_id": "04", "account": ACCOUNT, "reason": "immature"} in sync["excluded"]


def test_a_clip_already_scored_by_an_earlier_sync_stays_in_the_calibration(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _scored_account(config)
    learning.sync(NOW, config=config)
    _linked_clip(config, "04", post_id=OTHER_POST)  # nouveau résultat : déclenche une calibration
    _stats_snapshot(config, "2026-10-10T12:00:00+00:00", {**_others(range(100, 1100, 100)), POST: (CLIP_POSTED, 2000, {}),
                                                          OTHER_POST: ("2026-10-09T09:00:00", 5, {})})
    seen = []
    monkeypatch.setattr(learning, "_calibrate", lambda linked, *a, **k: seen.append(list(linked)) or {})

    learning.sync(NOW, config=config)

    assert seen == [[(VIDEO, "03", 3)]]


# ---------------------------------------------------------------- métrique de calibration (TASK-93343051750b)


def _config_metric(tmp_path, metric) -> Config:
    config = _config(tmp_path)
    config._sections["jury_calibration"]["stats_metric"] = metric
    return config


def test_sync_also_writes_pct_watched_into_stats_and_keeps_the_entry_key(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    stats = _stats_entry(config)
    assert stats["stats"]["pct_watched"] == pytest.approx(0.547, abs=1e-3)
    assert stats["pct_watched"] == stats["stats"]["pct_watched"]


def test_calibration_on_pct_watched_ignores_no_stats_entry_after_sync(tmp_path):
    config = _config_metric(tmp_path, "pct_watched")
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _moments(config, VIDEO, [{"id": 3, "jury": {"trace": _trace(80)}}])
    _scored_account(config, avg_watch_s=13.38)

    sync = _sync(config)

    weights = _read(tmp_path / "jury_weights.json")
    assert weights["stats_metric"] == "pct_watched"
    assert weights["ignored_stats"] == []
    assert sync["last_error"] is None


def test_metric_absent_from_every_stats_entry_is_a_visible_error(tmp_path):
    config = _config_metric(tmp_path, "watched_full")  # le relevé ne porte pas watched_full
    _linked_clip(config, "03")
    _moments(config, VIDEO, [{"id": 3, "jury": {"trace": _trace(80)}}])
    _scored_account(config)

    with pytest.raises(jury_calibration.CalibrationError, match="watched_full"):
        learning.sync(NOW, config=config)

    error = _read(Path(config.section("learning")["state_dir"]) / "sync.json")["last_error"]
    assert error["where"] == "calibrate" and "watched_full" in error["message"]


def test_coach_cases_carry_the_grid_that_scored_them_and_unreadable_ones_are_skipped_visibly(tmp_path, caplog):
    config = _coach_config(tmp_path)
    _coach_world(config, n=4, rubric=_builtin_rubric("builtin:gaming"))
    _moments(config, "autre", [{"id": 99, "jury": {"trace": {"rounds": []}}}])  # aucune grille enregistree
    _sidecar(config, "99", video="autre", post_id="700000000000199", state="published")
    skipped: list = []
    scored = {f"{VIDEO}/{k:02d}" for k in range(1, 5)} | {"autre/99"}

    cases = learning._coach_cases(scored, config, skipped)

    assert {c["rubric"]["id"] for c in cases} == {"builtin:gaming"} and len(cases) == 4
    assert [(s["video_id"], s["moment_id"]) for s in skipped] == [("autre", 99)]
    assert "rubric.path absent" in skipped[0]["reason"]


# --------------------------------------------------------------------------
# Version de perspective recopiée dans le journal (TASK-4e58554d15d3)
# --------------------------------------------------------------------------


def _judges_moments(config, judges):
    _moments(config, VIDEO, [{"id": 3, "source": "action", "jury": {"score": 70}}], jury={"judges": judges})


def test_stats_entry_copies_the_perspective_sha_of_each_judge(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _judges_moments(config, [
        {"name": "retention", "usage": "jury_retention", "model": "strong", "veto": False, "perspective_sha": "f87084fffd75"},
        {"name": "conformite", "usage": "jury_conformite", "model": "fast", "veto": True, "perspective_sha": "0bd5e25e82a2"},
    ])
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    assert _stats_entry(config)["perspectives"] == {"retention": "f87084fffd75", "conformite": "0bd5e25e82a2"}


def test_moments_json_without_sha_gives_a_stats_entry_without_perspectives(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _judges_moments(config, [
        {"name": "retention", "usage": "jury_retention", "model": "strong", "veto": False},
    ])
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    entry = _stats_entry(config)
    assert "perspectives" not in entry
    assert entry["moment_source"] == "action"


def test_moment_without_jury_gives_a_stats_entry_without_perspectives(tmp_path):
    config = _config(tmp_path)
    _linked_clip(config, "03")
    _sidecar_fields(config, "03", duration=24.47)
    _moments(config, VIDEO, [{"id": 3, "source": "transcript"}])
    _scored_account(config, avg_watch_s=13.38)

    learning.sync(NOW, config=config)

    assert "perspectives" not in _stats_entry(config)


# ---------------------------------------------------------------- revue des merges : jeu de seen.json, métrique avant écriture (TASK-c93eae4732c6)


def _bd_seen(tmp_path, queued):
    path = tmp_path / "veille" / "seen.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"queued": queued, "ignored": []}), encoding="utf-8")


def test_queued_vod_takes_its_game_from_seen_json_when_days_are_gone(tmp_path):
    config = _bd_config(tmp_path)  # aucun days/ : seul seen.json porte le jeu
    _bd_seen(tmp_path, [{"candidate_id": "twitch:123", "video_id": "123", "game_name": "Jeu Q"},
                        {"candidate_id": "twitch:456", "video_id": "v456", "game_name": "Jeu R"}])
    _bd_meta(config, "v123")
    _bd_meta(config, "v456")
    _bd_row(config, "v123", "01", 500)
    _bd_row(config, "v456", "01", 300)

    games = _groups(config, "game")

    assert games["Jeu Q"]["n"] == 1 and games["Jeu R"]["n"] == 1
    assert "jeu inconnu" not in {g["label"] for g in games.values()}


def test_seen_json_wins_over_days_for_the_same_vod(tmp_path):
    config = _bd_config(tmp_path)
    _bd_seen(tmp_path, [{"candidate_id": "twitch:789", "video_id": "789", "game_name": "Jeu Q"}])
    _bd_day(tmp_path, "2026-10-05", [{"video_id": "789", "source": "twitch", "game_name": "Jeu D"}])
    _bd_meta(config, "v789")
    _bd_row(config, "v789", "01", 500)

    assert set(_groups(config, "game")) == {"Jeu Q"}


def test_vod_only_in_days_keeps_its_game(tmp_path):
    config = _bd_config(tmp_path)
    _bd_day(tmp_path, "2026-10-05", [{"video_id": "789", "source": "twitch", "game_name": "Jeu D"}])
    _bd_meta(config, "v789")
    _bd_row(config, "v789", "01", 500)

    assert set(_groups(config, "game")) == {"Jeu D"}


def test_absent_metric_leaves_the_existing_weights_file_untouched(tmp_path):
    config = _config_metric(tmp_path, "watched_full")  # aucune entrée stats ne porte watched_full
    _linked_clip(config, "03")
    _moments(config, VIDEO, [{"id": 3, "jury": {"trace": _trace(80)}}])
    _scored_account(config)
    weights = tmp_path / "jury_weights.json"
    jury_calibration.calibrate([], config=config, now=NOW)  # poids valides déjà appris (fichier réel)
    before = weights.read_bytes()

    with pytest.raises(jury_calibration.CalibrationError, match="watched_full"):
        learning.sync(NOW, config=config)

    assert weights.read_bytes() == before


# ---------------------------------------------------------------- rattachement symetrique (TASK-c86f268b82c7, audit stats I1/M1/M3)
CAP_LONG_A = "Sur ce jeu, le streamer donne son avis sur le boss final"
CAP_LONG_B = "Sur ce jeu, le streamer donne son avis sur la fin du jeu"
SHOWN_CUT = "Sur ce jeu, le streamer donne son avis sur…"


def test_one_post_for_two_prefix_captions_links_nobody_and_both_are_ambiguous(tmp_path):
    config = _config(tmp_path)
    side_01 = _sidecar(config, "01", caption=CAP_LONG_B, hashtags=(), publish_at="2026-10-07T14:00:00+02:00")
    side_02 = _sidecar(config, "02", caption=CAP_LONG_A, hashtags=(), publish_at="2026-10-07T10:00:00+02:00")
    _snapshot(config, "2026-10-07T10:30:00+02:00", ("7000000000000000002", SHOWN_CUT, "2026-10-07T10:00:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    assert result["linked"] == [] and result["unlinked"] == {"none": 0, "ambiguous": 2}
    assert _read(side_01)["tiktok_post"]["id"] is None and _read(side_02)["tiktok_post"]["id"] is None
    records = _links(config)["unlinked"]
    assert {r["clip_id"] for r in records} == {"01", "02"}
    assert all(r["reason"] == "ambiguous" and "post partagé" in r["detail"] for r in records)


def test_two_posts_of_the_day_are_each_linked_to_the_sidecar_planned_at_their_minute(tmp_path):
    config = _config(tmp_path)  # fenetre par defaut (12 h) : l'heure prevue a la minute departage
    side_01 = _sidecar(config, "01", caption=CAP_LONG_B, hashtags=(), publish_at="2026-10-07T14:00:00+02:00")
    side_02 = _sidecar(config, "02", caption=CAP_LONG_A, hashtags=(), publish_at="2026-10-07T10:00:00+02:00")
    _snapshot(config, "2026-10-07T15:00:00+02:00", ("7000000000000000002", SHOWN_CUT, "2026-10-07T10:00:00"),
              ("7000000000000000001", SHOWN_CUT, "2026-10-07T14:00:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    assert result["unlinked"] == {"none": 0, "ambiguous": 0} and len(result["linked"]) == 2
    assert _read(side_01)["tiktok_post"]["id"] == "7000000000000000001"
    assert _read(side_02)["tiktok_post"]["id"] == "7000000000000000002"


def test_exact_caption_wins_over_a_prefix_caption(tmp_path):
    config = _config(tmp_path)
    side_short = _sidecar(config, "02", caption="Un clip", hashtags=())
    side_long = _sidecar(config, "01", caption="Un clip de folie", hashtags=())
    _snapshot(config, "2026-10-07T10:00:00+02:00", ("7000000000000000003", "Un clip", "2026-10-07T09:00:00"))

    result = learning.link_posts(ACCOUNT, config=config)

    assert [r["clip_id"] for r in result["linked"]] == ["02"]
    assert _read(side_short)["tiktok_post"]["id"] == "7000000000000000003"
    assert _read(side_long)["tiktok_post"]["id"] is None and result["unlinked"] == {"none": 1, "ambiguous": 0}


def test_a_post_deleted_from_tiktok_is_never_linked(tmp_path):
    config = _config(tmp_path)
    side = _sidecar(config, "01")
    _snapshot(config, "2026-10-07T10:05:00+02:00", ("7000000000000000009", "Un super clip #jeu #fun", "2026-10-07T09:00:00"))
    _snapshot(config, "2026-10-07T12:00:00+02:00")  # releve complet sans le post : supprime
    assert tiktok.deleted_post_ids(tiktok.read_history(ACCOUNT, config=config)) == {"7000000000000000009"}

    result = learning.link_posts(ACCOUNT, config=config)

    assert result["linked"] == [] and result["unlinked"] == {"none": 1, "ambiguous": 0}
    assert _read(side)["tiktok_post"]["id"] is None


def test_pct_watched_is_capped_at_one_when_loops_are_counted():
    assert learning._pct_watched(30, 20) == 1.0
    assert learning._pct_watched(10, 20) == 0.5
