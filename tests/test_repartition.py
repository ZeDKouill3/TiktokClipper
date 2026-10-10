"""repartition.py : plan du lendemain par compte TikTok (SPEC-78dc R0-R7). Aucun reseau, aucun navigateur, CPU."""

from __future__ import annotations

import ast
import json
import logging
import re
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from clipper import pipeline, repartition, tiktok
from clipper.config import Config

PARIS = ZoneInfo("Europe/Paris")
DAY = date(2026, 10, 10)  # un samedi
NOW = datetime(2026, 10, 9, 20, 5, tzinfo=PARIS)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _config(tmp_path, **overrides) -> Config:
    return Config(
        mode="auto", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output",
        _sections={
            "tiktok": {"stats_dir": str(tmp_path / "stats")},
            "publish": {"state_dir": str(tmp_path / "pub")},
            "accounts": {"state_file": str(tmp_path / "accounts.json")},
            "veille": {"state_dir": str(tmp_path / "veille")},
            "worker": {"queue_path": str(tmp_path / "queue.json")},
            "watch": {"presets_dir": str(tmp_path / "presets")},
            "repartition": {"state_dir": str(tmp_path / "rep"), **overrides},
        },
    )


def _accounts(config, *accounts) -> None:
    path = Path(config.section("accounts")["state_file"])
    path.write_text(json.dumps({"accounts": list(accounts)}), encoding="utf-8")


def _acc(account_id, *, service="tiktok", ready=True, paused=False, slots=None, label=None) -> dict:
    return {"id": account_id, "label": label or account_id, "service": service, "ready_to_publish": ready,
            "paused_at": "2026-10-08T10:00:00+00:00" if paused else None,
            "slots": [{"day": "sat", "time": t} for t in (slots or [])], "timezone": "Europe/Paris"}


def _clip(config, video, clip="01", *, score=80, ready=True, part=None, streamer=None, game=None, style=None,
          exploration=None, post_id=None) -> None:
    """Un clip pret : sidecar, pipeline.json (style), meta.json (streamer, jeu), moments.json (exploration)."""
    out = Path(config.output_dir) / video
    out.mkdir(parents=True, exist_ok=True)
    sidecar = {"ready": ready, "score": score}
    if part is not None:
        sidecar.update(part=part, parts_total=2)
    if post_id is not None:
        sidecar["tiktok_post"] = {"id": post_id, "account": "a"}
    (out / f"{clip}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    work = Path(config.workspace_dir) / video
    work.mkdir(parents=True, exist_ok=True)
    (work / "pipeline.json").write_text(json.dumps({"channel": style}), encoding="utf-8")
    meta = {}
    if streamer:
        meta["channel"] = streamer
    if game:
        meta["game"] = game
    (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if exploration is not None:
        moments = [{"id": int(clip[:2]), "exploration": exploration}]
        (work / "moments.json").write_text(json.dumps({"moments": moments}), encoding="utf-8")


def _entry(config, video, clip, *, account="a", status="scheduled", slot_at=None, channel="_sans_chaine") -> None:
    path = Path(config.section("publish")["state_dir"]) / f"{channel}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append({"video_id": video, "clip_id": clip, "series_id": None, "part": None, "status": status,
                    "slot_at": slot_at, "decided_at": None, "published_at": None, "error": None,
                    "account": account})
    path.write_text(json.dumps(entries), encoding="utf-8")


def _snapshot(config, account, *posts, fetched_at="2026-10-09T10:00:00+00:00") -> None:
    snapshot = {"account": account, "fetched_at": fetched_at, "source": "tiktok_studio", "origin": "full",
                "overview": {}, "posts": [
                    {"post_id": pid, "post_url": f"https://www.tiktok.com/@x/video/{pid}", "caption": "c",
                     "posted_at": posted_at, "views": views} for pid, posted_at, views in posts]}
    tiktok.append_snapshot(account, tiktok.get_settings(config), snapshot)


def _plan(config, *, day=DAY, now=NOW) -> dict:
    return repartition.compute_plan(day, now, config=config)


def _acc_plan(plan, account) -> dict:
    return next(a for a in plan["accounts"] if a["account"] == account)


def _hours(account_plan) -> list[str]:
    return [datetime.fromisoformat(s["slot_at"]).astimezone(PARIS).strftime("%H:%M") for s in account_plan["slots"]]


def _line_for(plan, account, video, clip="01") -> dict | None:
    return next((line for line in _acc_plan(plan, account)["lines"]
                 if (line["video_id"], line["clip_id"]) == (video, clip)), None)


def _hour_of(line) -> str:
    return datetime.fromisoformat(line["slot_at"]).astimezone(PARIS).strftime("%H:%M")


def _stripped(plan: dict) -> dict:
    return {k: v for k, v in plan.items() if k != "computed_at"}


# ---------------------------------------------------------------- R0 reglages


def test_r0_defaults_are_exactly_the_spec():
    assert repartition.CONFIG_DEFAULTS == {
        "enabled": True, "state_dir": "state/repartition", "compute_time": "20:00", "posts_per_day": 6,
        "default_grid_start": "08:00", "default_grid_end": "22:00", "default_grid_gap_min": 150,
        "account_stagger_min": 30, "max_per_source": 2, "excluded_sources": [], "prime_start": "18:00",
        "prime_end": "22:00", "exploration_per_day": 1, "bonus_window_days": 7, "bonus_min_age_h": 24,
        "bonus_min_posts": 3, "bonus_points": 5.0,
    }


@pytest.mark.parametrize("override", [
    {"compute_time": "8h"}, {"compute_time": "24:00"}, {"posts_per_day": 0}, {"posts_per_day": True},
    {"default_grid_gap_min": 0}, {"max_per_source": 0}, {"prime_end": "18:00"}, {"prime_end": "17:00"},
    {"default_grid_end": "07:00"}, {"bonus_points": -1}, {"excluded_sources": "streamer"},
    {"excluded_sources": [1]}, {"bonus_window_days": 0}, {"bonus_min_posts": 0}, {"enabled": "oui"},
    {"bonus_min_age_h": -1}, {"bonus_min_age_h": True}, {"bonus_min_age_h": "24"},
])
def test_r0_invalid_setting_is_refused_not_corrected(tmp_path, override):
    config = _config(tmp_path, **override)
    _accounts(config, _acc("a"))

    with pytest.raises(repartition.RepartitionError):
        _plan(config)


def test_r0_config_example_documents_the_section():
    text = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8")

    assert "[repartition]" in text
    for key in repartition.CONFIG_DEFAULTS:
        assert key in text


def test_r0_config_example_values_load_with_the_section(tmp_path):
    lines = (REPO_ROOT / "config.example.toml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# [repartition]"))
    block = ["[repartition]"]
    for line in lines[start + 1:]:
        if not line.startswith("#") or line.startswith("# ["):
            break
        body = line[2:].split("#")[0].strip() if line.startswith("# ") else ""
        if "=" in body and not body.startswith("#"):
            block.append(body)
    path = tmp_path / "config.toml"
    path.write_text('mode = "review"\n' + "\n".join(block) + "\n", encoding="utf-8")
    from clipper.config import load_config

    section = load_config(path).section("repartition")

    assert section == repartition.CONFIG_DEFAULTS


# ---------------------------------------------------------------- R1 comptes et creneaux


def test_r1_only_active_tiktok_accounts_are_planned_and_others_noted(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("pause", paused=True), _acc("off", ready=False),
              _acc("yt", service="youtube", label="Chaine YT"))

    plan = _plan(config)

    assert [a["account"] for a in plan["accounts"]] == ["a"]
    assert any("Chaine YT" in note and "hors périmètre v1" in note for note in plan["notes"])


def test_r1_default_grid_staggered_by_account_rank(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"), _acc("c"))

    plan = _plan(config)

    assert _hours(_acc_plan(plan, "a")) == ["08:00", "10:30", "13:00", "15:30", "18:00", "20:30"]
    assert _hours(_acc_plan(plan, "b")) == ["08:30", "11:00", "13:30", "16:00", "18:30", "21:00"]
    assert _hours(_acc_plan(plan, "c")) == ["09:00", "11:30", "14:00", "16:30", "19:00", "21:30"]


def test_r1_fixed_slots_are_all_kept_and_grid_completes_with_the_gap(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a", slots=["18:30", "21:30"]))

    plan = _plan(config)

    assert _hours(_acc_plan(plan, "a")) == ["08:00", "10:30", "13:00", "15:30", "18:30", "21:30"]
    kinds = {_hours({"slots": [s]})[0]: s["kind"] for s in _acc_plan(plan, "a")["slots"]}
    assert kinds["18:30"] == "fixed" and kinds["08:00"] == "grid"


def test_r1_posts_already_planned_that_day_count_and_are_not_doubled(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _entry(config, "VX", "01", slot_at="2026-10-10T10:30:00+02:00")

    plan = _plan(config)

    hours = _hours(_acc_plan(plan, "a"))
    assert hours == ["08:00", "13:00", "15:30", "18:00", "20:30"]  # 5 + le post prevu = 6 ; rien a moins de 2 h 30


def test_r1_fewer_possible_slots_than_wanted_is_said_never_squeezed(tmp_path):
    config = _config(tmp_path, default_grid_gap_min=600)
    _accounts(config, _acc("a", label="Compte A"))

    plan = _plan(config)

    assert _hours(_acc_plan(plan, "a")) == ["08:00", "18:00"]
    assert any("Compte A" in note and "2" in note and "6" in note for note in _acc_plan(plan, "a")["notes"])


# ---------------------------------------------------------------- R2 vivier


def test_r2_pool_has_only_ready_clips_never_one_already_in_a_queue(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01")
    _clip(config, "V1", "02", ready=False)
    _clip(config, "V1", "03")
    _entry(config, "V1", "03", account="other", status="published")

    plan = _plan(config)

    assert plan["pool"] == 1
    assert _line_for(plan, "a", "V1", "01") is not None
    assert _line_for(plan, "a", "V1", "03") is None


def test_r2_multi_part_series_are_excluded_entirely_and_counted(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01-p1", part=1)
    _clip(config, "V1", "01-p2", part=2)
    _clip(config, "V2", "01")

    plan = _plan(config)

    assert plan["pool"] == 1
    assert sorted((e["clip_id"], e["reason"]) for e in plan["excluded"]) == [
        ("01-p1", "multi_part_series"), ("01-p2", "multi_part_series")]
    assert {e["video_id"] for e in plan["excluded"]} == {"V1"}


def test_r2_single_clip_with_part_1_of_1_stays_in_the_pool(tmp_path):
    """Un clip seul porte ``part: 1, parts_total: 1`` dans son sidecar (render) : ce n'est pas une série."""
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01")
    sidecar = Path(config.output_dir) / "V1" / "01.json"
    sidecar.write_text(json.dumps({"ready": True, "score": 80, "part": 1, "parts_total": 1}), encoding="utf-8")

    plan = _plan(config)

    assert plan["pool"] == 1
    assert plan["excluded"] == []


def test_r2_excluded_source_by_streamer_and_by_style_case_insensitive(tmp_path):
    config = _config(tmp_path, excluded_sources=["streamerx", "STYLES"])
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01", streamer="StreamerX")
    _clip(config, "V2", "01", style="styleS")
    _clip(config, "V3", "01", streamer="Autre")

    plan = _plan(config)

    assert plan["pool"] == 1
    assert sorted((e["video_id"], e["reason"]) for e in plan["excluded"]) == [
        ("V1", "excluded_source"), ("V2", "excluded_source")]
    assert not any("aucune vidéo pour cette source" in n for n in plan["notes"])


def test_r2_excluded_source_matching_no_video_is_noted_not_an_error(tmp_path):
    config = _config(tmp_path, excluded_sources=["fantome"])
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01")

    plan = _plan(config)

    assert any("fantome" in n and "aucune vidéo pour cette source" in n for n in plan["notes"])


def test_r2_video_still_in_the_processing_queue_is_excluded(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01")
    _clip(config, "V2", "01")
    (tmp_path / "queue.json").write_text(json.dumps(
        [{"video_id": "V1", "action": "run", "status": "running"}]), encoding="utf-8")

    plan = _plan(config)

    assert plan["pool"] == 1
    assert [(e["video_id"], e["reason"]) for e in plan["excluded"]] == [("V1", "in_processing_queue")]


def test_r2_account_pool_drops_excluded_source_and_queued_video(tmp_path):
    config = _config(tmp_path, excluded_sources=["banni"])
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01", streamer="Banni")
    _clip(config, "V2", "01")
    _clip(config, "V3", "01")
    (tmp_path / "queue.json").write_text(json.dumps(
        [{"video_id": "V2", "action": "run", "status": "waiting"}]), encoding="utf-8")

    pool = repartition.account_pool("a", world=repartition.World(config), settings=repartition.read_settings(config))

    assert [unit["video_id"] for unit in pool] == ["V3"]


def test_r2_line_error_refuses_excluded_source_and_queued_video(tmp_path):
    config = _config(tmp_path, excluded_sources=["banni"])
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01", streamer="Banni")
    _clip(config, "V2", "01")
    _clip(config, "V3", "01")
    (tmp_path / "queue.json").write_text(json.dumps(
        [{"video_id": "V2", "action": "run", "status": "waiting"}]), encoding="utf-8")
    world = repartition.World(config)
    slot = datetime(2026, 10, 10, 10, 0, tzinfo=PARIS)

    def error(video_id):
        return repartition.line_error(world, "a", DAY, video_id=video_id, clip_id="01", slot_at=slot, entries=[])

    assert "excluded_source" in error("V1")
    assert "in_processing_queue" in error("V2")
    assert error("V3") is None


# ---------------------------------------------------------------- R3 source et plafond


def test_r3_source_key_meta_then_veille_then_vod(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "VM", "01", game="Zelda: Wild!", score=90)
    _clip(config, "VV", "01", score=80)
    _clip(config, "VN", "01", score=70)
    veille = tmp_path / "veille"
    veille.mkdir()
    (veille / "seen.json").write_text(json.dumps({"queued": [
        {"video_id": "VV", "game_name": "Elden Ring"}, {"video_id": "VM", "game_name": "Autre jeu"}],
        "ignored": []}), encoding="utf-8")

    plan = _plan(config)

    meta, veil, vod = (_line_for(plan, "a", v) for v in ("VM", "VV", "VN"))
    assert (meta["source_key"], meta["game_name"], meta["source_from"]) == ("jeu:zelda wild", "Zelda: Wild!", "meta")
    assert (veil["source_key"], veil["game_name"], veil["source_from"]) == ("jeu:elden ring", "Elden Ring", "veille")
    assert (vod["source_key"], vod["game_name"], vod["source_from"]) == ("vod:VN", None, "vod")


def test_r3_max_per_source_per_account_refused_clip_goes_to_the_next(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for i, score in enumerate((90, 85, 80), start=1):
        _clip(config, "V1", f"0{i}", score=score)
    _clip(config, "V2", "01", score=70)

    plan = _plan(config)

    taken = {(line["video_id"], line["clip_id"]) for line in _acc_plan(plan, "a")["lines"]}
    assert taken == {("V1", "01"), ("V1", "02"), ("V2", "01")}


def test_r3_max_per_source_counts_the_posts_already_planned(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _entry(config, "V1", "09", slot_at="2026-10-10T09:00:00+02:00")
    _clip(config, "V1", "01", score=90)
    _clip(config, "V1", "02", score=85)

    plan = _plan(config)

    assert [(line["video_id"], line["clip_id"]) for line in _acc_plan(plan, "a")["lines"]] == [("V1", "01")]


def test_r3_same_game_in_two_videos_shares_the_cap(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for video, score in (("V1", 90), ("V2", 85), ("V3", 80)):
        _clip(config, video, "01", score=score, game="Jeu")

    plan = _plan(config)

    assert len(_acc_plan(plan, "a")["lines"]) == 2


# ---------------------------------------------------------------- R4 bonus


def _posted(config, video, game, post_id):
    _clip(config, video, "01", game=game, ready=False, post_id=post_id)


def test_r4_bonus_from_real_medians_only_and_ranks_the_clips(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for video, game, ids in (("H1", "fort", ("p1", "p2", "p5")), ("H2", "faible", ("p3", "p4", "p6"))):
        for i, pid in enumerate(ids):
            _clip(config, f"{video}{i}", "01", game=game, ready=False, post_id=pid)
    _snapshot(config, "a", ("p1", "2026-10-05T12:00:00", 12000), ("p2", "2026-10-06T12:00:00", 12000),
              ("p5", "2026-10-06T12:30:00", 12000),
              ("p3", "2026-10-05T13:00:00", 3000), ("p4", "2026-10-06T13:00:00", 3000),
              ("p6", "2026-10-06T13:30:00", 3000),
              ("p9", "2026-10-06T14:00:00", 99999),  # non relie a un clip : ignore
              ("p8", "2026-09-01T14:00:00", 50))  # hors fenetre
    _clip(config, "PF", "01", game="fort", score=70)
    _clip(config, "PW", "01", game="faible", score=72)

    plan = _plan(config)

    strong, weak = _line_for(plan, "a", "PF"), _line_for(plan, "a", "PW")
    assert strong["bonus"] == pytest.approx(3.0) and weak["bonus"] == pytest.approx(-3.0)
    assert strong["adjusted"] == pytest.approx(73.0) and weak["adjusted"] == pytest.approx(69.0)
    assert strong["bonus_reason"] == "3 posts, médiane 12 000 vues, référence 7 500, posts d'au moins 24 h"
    assert _hour_of(strong) == "18:00" and _hour_of(weak) == "20:30"  # le meilleur score ajuste prend le meilleur creneau


def test_r4_bonus_is_clamped_to_plus_or_minus_one(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for i in range(5):
        _posted(config, f"L{i}", "bas", f"l{i}")
    for i in range(3):
        _posted(config, f"U{i}", "haut", f"u{i}")
    _snapshot(config, "a", *[(f"l{i}", "2026-10-05T12:00:00", 1000) for i in range(5)],
              *[(f"u{i}", "2026-10-05T12:00:00", 100000) for i in range(3)])
    _clip(config, "PH", "01", game="haut", score=70)
    _clip(config, "PL", "01", game="bas", score=70)

    plan = _plan(config)

    assert _line_for(plan, "a", "PH")["bonus"] == pytest.approx(5.0)
    assert _line_for(plan, "a", "PL")["bonus"] == pytest.approx(0.0)


def test_r4_source_below_min_posts_has_no_bonus_with_its_reason(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _posted(config, "H1", "rare", "r1")
    for i in range(2):
        _posted(config, f"M{i}", "commun", f"m{i}")
    _snapshot(config, "a", ("r1", "2026-10-05T12:00:00", 90000), ("m0", "2026-10-05T12:00:00", 1000),
              ("m1", "2026-10-05T12:00:00", 1000))
    _clip(config, "PR", "01", game="rare", score=70)

    plan = _plan(config)

    line = _line_for(plan, "a", "PR")
    assert line["bonus"] == 0 and line["bonus_reason"] == "below_min_posts"


def test_r4_no_stats_for_the_source_or_at_all(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "PN", "01", game="inconnu", score=70)

    plan = _plan(config)

    line = _line_for(plan, "a", "PN")
    assert line["bonus"] == 0 and line["bonus_reason"] == "no_stats"
    assert line["adjusted"] == 70
    assert "aucun relevé récent" in plan["notes"]


def test_r4_post_younger_than_min_age_is_ignored_and_one_exactly_at_min_age_counted(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for pid in ("m0", "m1", "m2", "o0", "b24", "y0"):
        _posted(config, pid.upper(), "fort", pid)
    _snapshot(config, "a", *[(f"m{i}", "2026-10-05T12:00:00", 12000) for i in range(3)],
              ("o0", "2026-10-08T14:05:00", 12000),  # 30 h avant now : comptée
              ("b24", "2026-10-08T20:05:00", 12000),  # exactement 24 h avant now : comptée (bord inclus)
              ("y0", "2026-10-09T18:05:00", 900000))  # 2 h avant now : ignorée
    _clip(config, "PF", "01", game="fort", score=70)

    line = _line_for(_plan(config), "a", "PF")

    assert line["bonus_reason"].startswith("5 posts, médiane 12 000 vues")


def test_r4_source_with_only_young_posts_has_no_stats(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for i in range(3):
        _posted(config, f"Y{i}", "neuf", f"y{i}")
    _snapshot(config, "a", *[(f"y{i}", "2026-10-09T18:05:00", 50000) for i in range(3)])
    _clip(config, "PN", "01", game="neuf", score=70)

    line = _line_for(_plan(config), "a", "PN")

    assert line["bonus"] == 0 and line["bonus_reason"] == "no_stats"


def test_r4_reference_is_the_median_of_mature_posts_only(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for i in range(3):
        _posted(config, f"H{i}", "fort", f"h{i}")
        _posted(config, f"B{i}", "faible", f"b{i}")
        _posted(config, f"J{i}", "faible", f"j{i}")
    _snapshot(config, "a", *[(f"h{i}", "2026-10-05T12:00:00", 12000) for i in range(3)],
              *[(f"b{i}", "2026-10-05T12:00:00", 3000) for i in range(3)],
              *[(f"j{i}", "2026-10-09T18:05:00", 900000) for i in range(3)])  # jeunes : hors référence
    _clip(config, "PF", "01", game="fort", score=70)

    line = _line_for(_plan(config), "a", "PF")

    assert line["bonus_reason"] == "3 posts, médiane 12 000 vues, référence 7 500, posts d'au moins 24 h"


def test_r4_bonus_reason_says_the_min_age_kept(tmp_path):
    config = _config(tmp_path, bonus_min_age_h=6)
    _accounts(config, _acc("a"))
    for pid in ("m0", "m1", "m2", "y0"):
        _posted(config, pid.upper(), "fort", pid)
    _snapshot(config, "a", *[(f"m{i}", "2026-10-05T12:00:00", 12000) for i in range(3)],
              ("y0", "2026-10-09T16:05:00", 900000))  # 4 h avant now : ignorée avec un minimum de 6 h
    _clip(config, "PF", "01", game="fort", score=70)

    line = _line_for(_plan(config), "a", "PF")

    assert line["bonus_reason"] == "3 posts, médiane 12 000 vues, référence 12 000, posts d'au moins 6 h"


def test_r4_stats_of_other_accounts_count_for_a_game(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    for i in range(3):
        _posted(config, f"H{i}", "fort", f"p{i}")
    _posted(config, "H9", "faible", "p9")
    _posted(config, "H8", "faible", "p8")
    _posted(config, "H7", "faible", "p7")
    _snapshot(config, "b", ("p0", "2026-10-05T12:00:00", 12000), ("p1", "2026-10-05T12:00:00", 12000),
              ("p2", "2026-10-05T12:00:00", 12000),
              ("p9", "2026-10-05T12:00:00", 3000), ("p8", "2026-10-05T12:00:00", 3000),
              ("p7", "2026-10-05T12:00:00", 3000))
    _clip(config, "PF", "01", game="fort", score=70)

    plan = _plan(config)

    assert _line_for(plan, "a", "PF")["bonus"] == pytest.approx(3.0)


def test_r4_clip_without_score_is_ranked_last(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01", score=None)
    _clip(config, "V2", "01", score=10)

    plan = _plan(config)

    assert _hour_of(_line_for(plan, "a", "V2")) == "18:00"
    assert _line_for(plan, "a", "V1")["adjusted"] is None
    assert _hour_of(_line_for(plan, "a", "V1")) == "20:30"


# ---------------------------------------------------------------- R5 attribution


def test_r5_best_clips_take_the_evening_slots_then_the_others_by_time(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    for video, score in (("V1", 90), ("V2", 80), ("V3", 70)):
        _clip(config, video, "01", score=score)

    plan = _plan(config)

    assert [_hour_of(_line_for(plan, "a", v)) for v in ("V1", "V2", "V3")] == ["18:00", "20:30", "08:00"]
    assert [_line_for(plan, "a", v)["prime"] for v in ("V1", "V2", "V3")] == [True, True, False]


def test_r5_round_robin_between_accounts(tmp_path):
    config = _config(tmp_path, posts_per_day=2)
    _accounts(config, _acc("a"), _acc("b"))
    for video, score in (("V1", 90), ("V2", 80), ("V3", 70), ("V4", 60)):
        _clip(config, video, "01", score=score)

    plan = _plan(config)

    assert sorted(line["video_id"] for line in _acc_plan(plan, "a")["lines"]) == ["V1", "V3"]
    assert sorted(line["video_id"] for line in _acc_plan(plan, "b")["lines"]) == ["V2", "V4"]


def test_r5_a_clip_is_planned_once_across_accounts(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "V1", "01")

    plan = _plan(config)

    assert sum(len(a["lines"]) for a in plan["accounts"]) == 1


def test_r5_ties_are_broken_by_video_then_clip_id(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "VB", "01", score=50)
    _clip(config, "VA", "02", score=50)
    _clip(config, "VA", "01", score=50)

    plan = _plan(config)

    assert _hour_of(_line_for(plan, "a", "VA", "01")) == "18:00"
    assert _hour_of(_line_for(plan, "a", "VA", "02")) == "20:30"
    assert _hour_of(_line_for(plan, "a", "VB", "01")) == "08:00"


# ---------------------------------------------------------------- R6 exploration


def test_r6_one_exploration_clip_per_day_on_the_cheapest_slot(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "V1", "01", score=95, exploration=True)
    _clip(config, "V2", "01", score=90, exploration=True)
    for i, video in enumerate(("V3", "V4", "V5")):
        _clip(config, video, "01", score=80 - i, exploration=False)

    plan = _plan(config)

    explo = [line for a in plan["accounts"] for line in a["lines"] if line["exploration"]]
    assert [(l["video_id"], l["prime"]) for l in explo] == [("V1", False)]
    assert _hour_of(explo[0]) == "08:00"
    assert _line_for(plan, "a", "V2") is None and _line_for(plan, "b", "V2") is None


def test_r6_no_off_peak_slot_means_no_exploration_for_that_account(tmp_path):
    config = _config(tmp_path, prime_start="08:00", prime_end="22:00")
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01", score=95, exploration=True)
    _clip(config, "V2", "01", score=80)

    plan = _plan(config)

    assert _line_for(plan, "a", "V1") is None
    assert _line_for(plan, "a", "V2") is not None


def test_r6_missing_or_unreadable_moments_is_not_exploration_and_noted(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "V1", "01")
    _clip(config, "V2", "01")
    (Path(config.workspace_dir) / "V2" / "moments.json").write_text("{bad", encoding="utf-8")

    plan = _plan(config)

    assert _line_for(plan, "a", "V1")["exploration"] is False
    assert _line_for(plan, "a", "V2")["exploration"] is False
    assert any("V1" in n and "moments.json" in n for n in plan["notes"])
    assert any("V2" in n and "moments.json" in n for n in plan["notes"])


def _explo_lines(plan) -> list[tuple[str, dict]]:
    return [(a["account"], line) for a in plan["accounts"] for line in a["lines"] if line["exploration"]]


def test_r6_low_score_exploration_clip_is_reserved_ahead_of_the_scores(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "EXP1", "01", score=20, exploration=True)
    _clip(config, "EXP2", "01", score=10, exploration=True)
    for i in range(28):
        _clip(config, f"V{i:02d}", "01", score=90 - i, exploration=False)

    plan = _plan(config)

    explo = _explo_lines(plan)
    assert [(account, line["video_id"]) for account, line in explo] == [("a", "EXP1")]
    assert explo[0][1]["prime"] is False
    assert _hour_of(explo[0][1]) == "08:00"
    assert "EXP2" not in [line["video_id"] for a in plan["accounts"] for line in a["lines"]]
    assert "exploration : EXP1/01 sur a à 08:00" in plan["notes"]


def test_r6_per_day_two_goes_to_two_different_accounts_off_peak(tmp_path):
    config = _config(tmp_path, exploration_per_day=2)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "EXP1", "01", score=20, exploration=True)
    _clip(config, "EXP2", "01", score=10, exploration=True)
    for i in range(30):
        _clip(config, f"V{i:02d}", "01", score=80 - i, exploration=False)

    plan = _plan(config)

    explo = _explo_lines(plan)
    assert sorted(line["video_id"] for _account, line in explo) == ["EXP1", "EXP2"]
    assert sorted(account for account, _line in explo) == ["a", "b"]
    assert all(line["prime"] is False for _account, line in explo)


def test_r6_per_day_zero_reserves_no_exploration(tmp_path):
    config = _config(tmp_path, exploration_per_day=0)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "EXP1", "01", score=20, exploration=True)
    for i in range(4):
        _clip(config, f"V{i}", "01", score=80 - i, exploration=False)

    plan = _plan(config)

    assert _explo_lines(plan) == []
    assert _line_for(plan, "a", "EXP1") is None and _line_for(plan, "b", "EXP1") is None


def test_r6_no_off_peak_slot_reserves_nothing_and_says_so(tmp_path):
    config = _config(tmp_path, prime_start="00:00", prime_end="23:59")
    _accounts(config, _acc("a"))
    _clip(config, "EXP1", "01", score=20, exploration=True)
    _clip(config, "V1", "01", score=80)

    plan = _plan(config)

    assert _explo_lines(plan) == []
    assert _line_for(plan, "a", "EXP1") is None
    assert any("exploration" in n and "aucun créneau hors soir" in n for n in plan["notes"])


def test_r6_no_exploration_clip_keeps_today_s_plan_exactly(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    for i in range(8):
        _clip(config, f"V{i}", "01", score=90 - i, exploration=False)

    plan = _plan(config)

    assert [
        (line["video_id"], _hour_of(line), line["prime"], line["score"], line["bonus"])
        for line in _acc_plan(plan, "a")["lines"]
    ] == [("V4", "08:00", False, 86, 0), ("V6", "10:30", False, 84, 0),
          ("V0", "18:00", True, 90, 0), ("V2", "20:30", True, 88, 0)]
    assert [
        (line["video_id"], _hour_of(line), line["prime"], line["score"], line["bonus"])
        for line in _acc_plan(plan, "b")["lines"]
    ] == [("V5", "08:30", False, 85, 0), ("V7", "11:00", False, 83, 0),
          ("V1", "18:30", True, 89, 0), ("V3", "21:00", True, 87, 0)]


# ---------------------------------------------------------------- R7 fichier, calcul, obsolescence


def _populate(config):
    _accounts(config, _acc("a", slots=["18:30"]), _acc("b"))
    for i, video in enumerate(("V1", "V2", "V3", "V4")):
        _clip(config, video, "01", score=90 - i, game=f"jeu{i}")


def test_r7_plan_file_shape_and_location(tmp_path):
    config = _config(tmp_path)
    _populate(config)

    plan = _plan(config)

    path = tmp_path / "rep" / "2026-10-10.json"
    assert json.loads(path.read_text(encoding="utf-8")) == plan
    assert plan["day"] == "2026-10-10" and plan["status"] == "proposed" and plan["computed_by"] == "worker"
    assert plan["computed_at"] == NOW.isoformat()
    assert {"accounts", "pool", "excluded", "notes", "validated_at", "created"} <= set(plan)
    line = _acc_plan(plan, "a")["lines"][0]
    assert {"slot_at", "video_id", "clip_id", "score", "bonus", "bonus_reason", "adjusted", "source_key",
            "game_name", "source_from", "exploration", "prime"} <= set(line)
    assert datetime.fromisoformat(line["slot_at"]).tzinfo is not None
    assert _acc_plan(plan, "a")["label"] == "a"
    assert not list((tmp_path / "rep").glob("*.tmp"))


def test_r7_plan_is_deterministic_apart_from_computed_at(tmp_path):
    config = _config(tmp_path)
    _populate(config)

    first = _plan(config)
    second = _plan(config, now=NOW + timedelta(minutes=7))

    assert first["computed_at"] != second["computed_at"]
    assert _stripped(first) == _stripped(second)


def test_r7_recompute_replaces_a_proposed_plan_and_refuses_a_validated_one(tmp_path):
    config = _config(tmp_path)
    _populate(config)
    _plan(config)
    path = tmp_path / "rep" / "2026-10-10.json"

    again = repartition.compute_plan(DAY, NOW, config=config, computed_by="web")
    assert again["computed_by"] == "web"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "validated"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(repartition.RepartitionError):
        _plan(config)


def test_r7_run_if_due_waits_for_compute_time_paris(tmp_path):
    config = _config(tmp_path)
    _populate(config)

    assert repartition.run_if_due(datetime(2026, 10, 9, 19, 59, tzinfo=PARIS), config=config) is None
    assert not (tmp_path / "rep").exists() or not list((tmp_path / "rep").glob("*.json"))

    plan = repartition.run_if_due(datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc), config=config)  # 20:30 Paris

    assert plan is not None and plan["day"] == "2026-10-10" and plan["computed_by"] == "worker"
    assert (tmp_path / "rep" / "2026-10-10.json").exists()


def test_r7_run_if_due_never_computes_twice_for_the_same_day(tmp_path):
    config = _config(tmp_path)
    _populate(config)
    path = tmp_path / "rep" / "2026-10-10.json"

    assert repartition.run_if_due(NOW, config=config) is not None
    before = path.read_bytes()
    assert repartition.run_if_due(NOW + timedelta(hours=1), config=config) is None
    assert path.read_bytes() == before

    data = json.loads(before)
    data["status"] = "validated"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert repartition.run_if_due(NOW + timedelta(hours=2), config=config) is None
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "validated"


def test_r7_run_if_due_disabled_does_nothing(tmp_path):
    config = _config(tmp_path, enabled=False)
    _populate(config)

    assert repartition.run_if_due(NOW, config=config) is None
    assert not (tmp_path / "rep").exists() or not list((tmp_path / "rep").glob("*.json"))


def test_r7_run_if_due_custom_compute_time(tmp_path):
    config = _config(tmp_path, compute_time="21:30")
    _populate(config)

    assert repartition.run_if_due(NOW, config=config) is None
    assert repartition.run_if_due(NOW.replace(hour=21, minute=31), config=config) is not None


def test_r7_run_if_due_writes_the_error_and_logs_it_once(tmp_path, caplog):
    config = _config(tmp_path)
    (tmp_path / "accounts.json").write_text("{pas du json", encoding="utf-8")
    path = tmp_path / "rep" / "2026-10-10.json"

    with caplog.at_level(logging.WARNING, logger="clipper.repartition"):
        assert repartition.run_if_due(NOW, config=config) is None
        assert repartition.run_if_due(NOW + timedelta(minutes=10), config=config) is None

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "error" and data["day"] == "2026-10-10"
    assert data["error"]["message"] and data["error"]["type"] == "AccountsError"
    assert len([r for r in caplog.records if r.name == "clipper.repartition"]) == 1


def test_r7_run_if_due_retries_after_an_error_once_fixed(tmp_path):
    config = _config(tmp_path)
    _populate(config)
    (tmp_path / "accounts.json").write_text("{pas du json", encoding="utf-8")
    assert repartition.run_if_due(NOW, config=config) is None

    _accounts(config, _acc("a"))
    plan = repartition.run_if_due(NOW + timedelta(minutes=10), config=config)

    assert plan is not None and plan["status"] == "proposed"


def test_r7_plan_file_is_written_under_a_lock(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _populate(config)
    locked = []
    real = repartition.channel.file_lock

    def spy(path):
        locked.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(repartition.channel, "file_lock", spy)

    _plan(config)

    assert "2026-10-10.json" in locked


def test_r7_module_imports_neither_web_nor_a_pipeline_step():
    tree = ast.parse((REPO_ROOT / "clipper" / "repartition.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.add(base)
            imported.update(f"{base}.{alias.name}".strip(".") for alias in node.names)

    forbidden = {"clipper.web", *(f"clipper.{step}" for step in pipeline.STEPS)}
    assert not [name for name in imported if any(name == f or name.startswith(f + ".") for f in forbidden)]
    assert {"clipper.publish", "clipper.accounts", "clipper.tiktok", "clipper.channel"} <= imported


def test_repartition_source_names_no_real_account_or_channel():
    text = (REPO_ROOT / "clipper" / "repartition.py").read_text(encoding="utf-8")
    assert "tiktok.com/@" not in text


# ---------------------------------------------------------------- R3 jeu de la veille : fichiers de jour


def _day_file(config, day, *candidates) -> None:
    """Un fichier ``days/<AAAA-MM-JJ>.json`` de la veille : candidats numérotés sans le préfixe ``v`` (Twitch)."""
    days = Path(config.section("veille")["state_dir"]) / "days"
    days.mkdir(parents=True, exist_ok=True)
    rows = [{"id": f"twitch:{vid}", "source": "twitch", "video_id": vid, "game_name": game} for vid, game in candidates]
    (days / f"{day}.json").write_text(json.dumps({"date": day, "candidates": rows}), encoding="utf-8")


def test_veille_game_of_a_vod_queued_by_hand_is_read_from_the_day_file(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "v2895096466", "01", score=80)
    _day_file(config, "2026-10-09", ("2895096466", "AION 2"))

    plan = _plan(config)

    line = _line_for(plan, "a", "v2895096466")
    assert (line["source_key"], line["game_name"], line["source_from"]) == ("jeu:aion 2", "AION 2", "veille")


def test_veille_seen_json_queued_keeps_priority_over_the_day_file(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "vP", "01", score=80)
    veille = Path(config.section("veille")["state_dir"])
    veille.mkdir(parents=True, exist_ok=True)
    (veille / "seen.json").write_text(json.dumps({"queued": [
        {"video_id": "vP", "game_name": "Elden Ring"}], "ignored": []}), encoding="utf-8")
    _day_file(config, "2026-10-09", ("P", "Autre jeu"))

    plan = _plan(config)

    assert _line_for(plan, "a", "vP")["game_name"] == "Elden Ring"


def test_veille_game_from_the_most_recent_day_file_wins(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "vQ", "01", score=80)
    _day_file(config, "2026-10-07", ("Q", "Ancien"))
    _day_file(config, "2026-10-08", ("Q", "Recent"))

    plan = _plan(config)

    assert _line_for(plan, "a", "vQ")["game_name"] == "Recent"


def test_unreadable_veille_day_file_is_noted_and_the_vod_stays_vod(tmp_path):
    config = _config(tmp_path)
    _accounts(config, _acc("a"))
    _clip(config, "vU", "01", score=80)
    _day_file(config, "2026-10-07", ("X", "Autre jeu"))
    veille_days = Path(config.section("veille")["state_dir"]) / "days"
    (veille_days / "2026-10-08.json").write_text("{bad", encoding="utf-8")

    plan = _plan(config)

    line = _line_for(plan, "a", "vU")
    assert (line["source_key"], line["game_name"], line["source_from"]) == ("vod:vU", None, "vod")
    assert any("2026-10-08.json" in n for n in plan["notes"])


# ---------------------------------------------------------------- API publique (écran et routes web)


def _line(video, clip, source_key, slot, *, exploration=False, prime=False) -> dict:
    return {"video_id": video, "clip_id": clip, "source_key": source_key, "slot_at": slot,
            "exploration": exploration, "prime": prime, "warning": "non calculé"}


def test_public_read_settings_is_the_validated_section(tmp_path):
    assert repartition.read_settings(_config(tmp_path, posts_per_day=4))["posts_per_day"] == 4
    assert repartition.read_settings(None) == repartition.CONFIG_DEFAULTS


def test_public_read_settings_refuses_an_out_of_domain_value(tmp_path):
    with pytest.raises(repartition.RepartitionError):
        repartition.read_settings(_config(tmp_path, prime_end="17:00"))


def test_public_source_bonus_is_the_median_ratio_with_its_reason():
    settings = repartition.read_settings(None)

    bonus, reason = repartition.source_bonus("jeu:fort", {"jeu:fort": [200, 200, 200]}, [200, 200, 100, 100], settings)

    assert (bonus, reason) == (1.67, "3 posts, médiane 200 vues, référence 150, posts d'au moins 24 h")


def test_public_source_bonus_without_posts_is_zero_and_says_why():
    assert repartition.source_bonus("jeu:x", {}, [], repartition.read_settings(None)) == (0, "no_stats")


def test_public_describe_line_gives_the_line_as_the_plan_writes_it(tmp_path):
    config = _config(tmp_path)
    _clip(config, "V1", "01", game="Fort", exploration=True)
    world = repartition.World(config)

    line = repartition.describe_line(
        world, repartition.read_settings(config), ({}, []), video_id="V1", clip_id="01",
        slot_at=datetime(2026, 10, 10, 20, 0, tzinfo=PARIS), score=80)

    assert line == {
        "slot_at": "2026-10-10T20:00:00+02:00", "video_id": "V1", "clip_id": "01", "score": 80, "bonus": 0,
        "bonus_reason": "no_stats", "adjusted": 80, "source_key": "jeu:fort", "game_name": "Fort",
        "source_from": "meta", "exploration": True, "prime": True, "warning": None}


def test_public_describe_line_without_a_numeric_score_has_no_adjusted_value(tmp_path):
    config = _config(tmp_path)
    _clip(config, "V1", "01", game="Fort")

    line = repartition.describe_line(
        repartition.World(config), repartition.read_settings(config), ({}, []), video_id="V1", clip_id="01",
        slot_at=datetime(2026, 10, 10, 9, 0, tzinfo=PARIS), score=True)

    assert (line["score"], line["adjusted"], line["prime"], line["exploration"]) == (None, None, False, False)


def test_public_line_warnings_flags_the_clip_over_the_source_cap(tmp_path):
    config = _config(tmp_path, max_per_source=1)
    first = _line("V1", "01", "jeu:fort", "2026-10-10T09:00:00+02:00")
    second = _line("V1", "02", "jeu:fort", "2026-10-10T10:00:00+02:00")
    plan = {"accounts": [{"account": "a", "lines": [first, second]}]}

    repartition.line_warnings(plan, DAY.isoformat(), repartition.read_settings(config), repartition.World(config), [])

    assert first["warning"] is None
    assert second["warning"] == "plus de 1 clips de la même source (jeu:fort) ce jour-là sur ce compte"


def test_public_line_warnings_counts_the_posts_already_scheduled(tmp_path):
    config = _config(tmp_path, max_per_source=1)
    _clip(config, "V0", "01", game="Fort")
    first = _line("V1", "01", "jeu:fort", "2026-10-10T09:00:00+02:00")
    plan = {"accounts": [{"account": "a", "lines": [first]}]}
    entries = [("_sans_chaine", {"video_id": "V0", "clip_id": "01", "account": "a", "status": "scheduled",
                                 "slot_at": "2026-10-10T08:00:00+02:00"})]

    repartition.line_warnings(plan, DAY.isoformat(), repartition.read_settings(config),
                              repartition.World(config), entries)

    assert first["warning"] == "plus de 1 clips de la même source (jeu:fort) ce jour-là sur ce compte"


def test_public_line_warnings_flags_exploration_over_the_day_and_on_prime(tmp_path):
    config = _config(tmp_path, exploration_per_day=1)
    first = _line("V1", "01", "vod:V1", "2026-10-10T09:00:00+02:00", exploration=True)
    second = _line("V2", "01", "vod:V2", "2026-10-10T20:00:00+02:00", exploration=True, prime=True)
    plan = {"accounts": [{"account": "a", "lines": [first, second]}]}

    repartition.line_warnings(plan, DAY.isoformat(), repartition.read_settings(config),
                              repartition.World(config), [])

    assert first["warning"] is None
    assert second["warning"] == ("plus de 1 clip(s) d'exploration par jour; "
                                 "clip d'exploration sur un créneau du soir")


def test_web_routes_call_only_public_repartition_functions():
    text = (REPO_ROOT / "clipper" / "web" / "app.py").read_text(encoding="utf-8")

    assert not re.search(r"repartition_mod\._", text)


def test_r7_run_if_due_without_injected_config_reads_config_toml_again_each_time(tmp_path, monkeypatch):
    """Audit 10/10 lot C : sans config injectée, une source ajoutée à ``excluded_sources`` entre deux appels est vue."""
    config = _config(tmp_path)
    _accounts(config, _acc("a"), _acc("b"))
    _clip(config, "V1", "01", streamer="Banni")
    _clip(config, "V2", "01")
    monkeypatch.chdir(tmp_path)
    toml = tmp_path / "config.toml"

    def write(excluded):
        sections = {name: config.section(name) for name in ("tiktok", "publish", "accounts", "veille", "worker", "watch")}
        lines = [f'workspace_dir = "{config.workspace_dir.as_posix()}"', f'output_dir = "{config.output_dir.as_posix()}"']
        for name, table in {**{n: {k: v for k, v in t.items() if k in config._sections[n]} for n, t in sections.items()},
                            "repartition": {"state_dir": str(tmp_path / "rep"), "excluded_sources": excluded}}.items():
            lines.append(f"[{name}]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in table.items())
        toml.write_text("\n".join(lines), encoding="utf-8")

    write([])
    first = repartition.run_if_due(NOW)
    assert first is not None and first["excluded"] == []
    (tmp_path / "rep" / "2026-10-10.json").unlink()

    write(["banni"])
    second = repartition.run_if_due(NOW)

    assert second is not None and [(e["video_id"], e["reason"]) for e in second["excluded"]] == [("V1", "excluded_source")]
