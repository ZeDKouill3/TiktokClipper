from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def _write_config(cwd: Path, mode: str = "review") -> None:
    (cwd / "config.toml").write_text(f'mode = "{mode}"\n', encoding="utf-8")


def _write_preset(cwd: Path, name: str, body: str = "[channel]\n") -> Path:
    presets_dir = cwd / "presets"
    presets_dir.mkdir(exist_ok=True)
    path = presets_dir / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


_SLOT_PRESET = '[channel]\ntimezone = "UTC"\n'  # un style n'a ni compte ni creneaux (SPEC-6076 R2)


def _sched(*slots):
    """Creneaux d'un compte (accounts.schedule_of) : couples (jour, heure), fuseau UTC."""
    return {"slots": [{"day": d, "time": t} for d, t in slots], "timezone": "UTC"}


_MON9 = _sched(("mon", "09:00"))
_MON9_WED9 = _sched(("mon", "09:00"), ("wed", "09:00"))


def _write_sidecar(
    cwd: Path,
    video_id: str,
    clip_id: str,
    *,
    ready: bool = True,
    qa_status: str = "passed",
    part: int = 1,
    parts_total: int = 1,
    **extra,
) -> Path:
    out_dir = cwd / "output" / video_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{clip_id}.json"
    data = {
        "video_id": video_id,
        "source_url": "https://example.invalid/x",
        "source_title": "titre",
        "clip_id": clip_id,
        "part": part,
        "parts_total": parts_total,
        "start": 0.0,
        "end": 10.0,
        "duration": 10.0,
        "language": "fr",
        "score": 80,
        "scores": {},
        "reason": "raison",
        "hook_text": "accroche",
        "screen_title": "titre ecran",
        "title": "titre",
        "caption": "legende initiale",
        "hashtags": ["#exemple"],
        "transcript": "transcript",
        "layout": "letterbox",
        "qa": {"status": qa_status, "issues": []},
        "created_at": "2026-09-30T00:00:00+00:00",
        "cta": False,
        "ready": ready,
    }
    data.update(extra)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _read_sidecar(cwd: Path, video_id: str, clip_id: str) -> dict:
    path = cwd / "output" / video_id / f"{clip_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state_file(cwd: Path, channel: str) -> Path:
    return cwd / "state" / "publish" / f"{channel}.json"


def _read_state(cwd: Path, channel: str) -> list[dict]:
    path = _state_file(cwd, channel)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# (1) approve : entree SPEC-fc0c 4.1, slots / sans slots, serie
# --------------------------------------------------------------------------


def test_approve_without_slots_sets_status_approved(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03")

    entry = publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)

    assert entry["video_id"] == "vid1"
    assert entry["clip_id"] == "03"
    assert entry["series_id"] is None
    assert entry["part"] is None
    assert entry["status"] == "approved"
    assert entry["slot_at"] is None
    assert entry["published_at"] is None
    assert entry["error"] is None
    assert entry["decided_at"] is not None

    on_disk = _read_state(isolated_cwd, "ma_chaine")
    assert on_disk == [entry]


def test_approve_with_slots_schedules_next_free_slot_after_now(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n',
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # monday, past 09:00

    entry = publish.approve("vid1", "03", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00")))

    assert entry["status"] == "scheduled"
    assert entry["slot_at"] == datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC")).isoformat()


def test_approve_series_parts_get_consecutive_slots_in_part_order(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n'
    )
    _write_sidecar(isolated_cwd, "vid1", "03-p1", part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "03-p2", part=2, parts_total=2)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # monday, past 09:00

    part1 = publish.approve("vid1", "03-p1", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00"), ("wed", "09:00")))
    part2 = publish.approve("vid1", "03-p2", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00"), ("wed", "09:00")))

    assert part1["series_id"] == part2["series_id"]
    assert part1["series_id"] is not None
    assert part1["part"] == 1
    assert part2["part"] == 2
    assert part1["slot_at"] == datetime(2026, 9, 30, 9, 0, tzinfo=ZoneInfo("UTC")).isoformat()
    assert part2["slot_at"] == datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC")).isoformat()


# --------------------------------------------------------------------------
# (2) approve refuse un clip non pret
# --------------------------------------------------------------------------


def test_approve_refuses_clip_not_ready(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03", ready=False)

    with pytest.raises(publish.PublishError, match="vid1/03"):
        publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)

    assert _read_state(isolated_cwd, "ma_chaine") == []


# --------------------------------------------------------------------------
# (3) reject d'une partie rejette toute la serie
# --------------------------------------------------------------------------


def test_reject_a_series_part_rejects_the_whole_series(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03-p1", part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "03-p2", part=2, parts_total=2)
    publish.approve("vid1", "03-p1", "ma_chaine", account=_ACCOUNT)

    rejected = publish.reject("vid1", "03-p2", "ma_chaine")

    assert rejected["status"] == "rejected"
    entries = {(e["video_id"], e["clip_id"]): e for e in _read_state(isolated_cwd, "ma_chaine")}
    assert entries[("vid1", "03-p1")]["status"] == "rejected"
    assert entries[("vid1", "03-p1")]["slot_at"] is None
    assert entries[("vid1", "03-p2")]["status"] == "rejected"


def test_reject_a_clip_without_a_series_only_rejects_that_clip(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03")
    _write_sidecar(isolated_cwd, "vid1", "04")
    publish.approve("vid1", "04", "ma_chaine", account=_ACCOUNT)

    publish.reject("vid1", "03", "ma_chaine")

    entries = {(e["video_id"], e["clip_id"]): e for e in _read_state(isolated_cwd, "ma_chaine")}
    assert entries[("vid1", "03")]["status"] == "rejected"
    assert entries[("vid1", "04")]["status"] == "approved"


# --------------------------------------------------------------------------
# (4) move : creneau deja pris / hors des slots de la chaine
# --------------------------------------------------------------------------


def test_move_refuses_a_slot_already_taken(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n'
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    _write_sidecar(isolated_cwd, "vid1", "04")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    publish.approve("vid1", "03", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00"), ("wed", "09:00")))  # -> wed 2026-09-30 09:00
    publish.approve("vid1", "04", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00"), ("wed", "09:00")))  # -> mon 2026-10-05 09:00
    taken_slot = datetime(2026, 9, 30, 9, 0, tzinfo=ZoneInfo("UTC"))

    with pytest.raises(publish.PublishError, match="vid1/04"):
        publish.move("vid1", "04", "ma_chaine", taken_slot, schedule=_MON9_WED9)


def test_move_refuses_a_slot_outside_the_account_slots(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n',
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    publish.approve("vid1", "03", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00")))
    out_of_slots = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("UTC"))  # tuesday

    with pytest.raises(publish.PublishError, match="vid1/03"):
        publish.move("vid1", "03", "ma_chaine", out_of_slots, schedule=_MON9)


def test_move_to_a_free_slot_updates_slot_at(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n'
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    publish.approve("vid1", "03", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00"), ("wed", "09:00")))  # -> wed 2026-09-30 09:00
    new_slot = datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC"))  # mon, free

    moved = publish.move("vid1", "03", "ma_chaine", new_slot, schedule=_MON9_WED9)

    assert moved["slot_at"] == new_slot.isoformat()
    assert moved["status"] == "scheduled"


# --------------------------------------------------------------------------
# (5) mark_published et unschedule
# --------------------------------------------------------------------------


def test_mark_published_sets_status_and_published_at(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)  # avec creneau : scheduled
    _write_sidecar(isolated_cwd, "vid1", "03")
    publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT, schedule=_MON9)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    entry = publish.mark_published("vid1", "03", "ma_chaine", now=now)

    assert entry["status"] == "published"
    assert entry["published_at"] == now.isoformat()


def test_unschedule_returns_a_scheduled_clip_to_approved(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n',
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    publish.approve("vid1", "03", "ma_chaine", now=now, account=_ACCOUNT, schedule=_sched(("mon", "09:00")))

    entry = publish.unschedule("vid1", "03", "ma_chaine")

    assert entry["status"] == "approved"
    assert entry["slot_at"] is None


# --------------------------------------------------------------------------
# (6) edit_caption
# --------------------------------------------------------------------------


def test_edit_caption_rewrites_sidecar_fields_and_edited_at(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    publish.edit_caption("vid1", "03", "ma_chaine", "nouvelle legende", ["#nouveau"], now=now)

    sidecar = _read_sidecar(isolated_cwd, "vid1", "03")
    assert sidecar["caption"] == "nouvelle legende"
    assert sidecar["hashtags"] == ["#nouveau"]
    assert sidecar["edited_at"] == now.isoformat()


def test_edit_caption_refuses_on_a_scheduled_entry(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(
        isolated_cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n',
    )
    _write_sidecar(isolated_cwd, "vid1", "03")
    publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT, schedule=_sched(("mon", "09:00")))

    with pytest.raises(publish.PublishError, match="vid1/03"):
        publish.edit_caption("vid1", "03", "ma_chaine", "x", [])


def test_edit_caption_refuses_on_a_published_entry(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)  # avec creneau : scheduled
    _write_sidecar(isolated_cwd, "vid1", "03")
    publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT, schedule=_MON9)
    publish.mark_published("vid1", "03", "ma_chaine")

    with pytest.raises(publish.PublishError, match="vid1/03"):
        publish.edit_caption("vid1", "03", "ma_chaine", "x", [])


# --------------------------------------------------------------------------
# (7) list_pending
# --------------------------------------------------------------------------


def test_list_pending_returns_ready_clips_absent_from_the_publish_file(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03")
    _write_sidecar(isolated_cwd, "vid1", "04")
    _write_sidecar(isolated_cwd, "vid1", "05", ready=False)
    workspace_dir = isolated_cwd / "workspace" / "vid1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "pipeline.json").write_text(
        json.dumps({"channel": "ma_chaine"}), encoding="utf-8"
    )
    publish.approve("vid1", "04", "ma_chaine", account=_ACCOUNT)

    pending = publish.list_pending("ma_chaine")

    assert [clip["clip_id"] for clip in pending] == ["03"]


def test_list_pending_ignores_videos_from_another_channel(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "03")
    workspace_dir = isolated_cwd / "workspace" / "vid1"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "pipeline.json").write_text(
        json.dumps({"channel": "autre_chaine"}), encoding="utf-8"
    )

    assert publish.list_pending("ma_chaine") == []


# --------------------------------------------------------------------------
# (8) entree invalide
# --------------------------------------------------------------------------


def test_invalid_entry_in_the_file_raises_publish_error_naming_the_field(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    state_dir = isolated_cwd / "state" / "publish"
    state_dir.mkdir(parents=True)
    (state_dir / "ma_chaine.json").write_text(
        json.dumps([{
            "video_id": "vid1", "clip_id": "03", "series_id": None, "part": None,
            "status": "bogus", "slot_at": None, "decided_at": None,
            "published_at": None, "error": None,
        }]),
        encoding="utf-8",
    )

    with pytest.raises(publish.PublishError, match="status"):
        publish.list_pending("ma_chaine")


def test_invalid_entry_missing_a_field_raises_publish_error_naming_it(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    state_dir = isolated_cwd / "state" / "publish"
    state_dir.mkdir(parents=True)
    (state_dir / "ma_chaine.json").write_text(
        json.dumps([{"video_id": "vid1", "clip_id": "03"}]), encoding="utf-8"
    )

    with pytest.raises(publish.PublishError, match="series_id"):
        publish.list_pending("ma_chaine")


# --------------------------------------------------------------------------
# TASK-ded3 : verrou, statuts coherents, ordre des parties, JSON corrompu
# --------------------------------------------------------------------------


def _approve_many(cwd, prefix, n):
    import os

    from clipper import publish

    os.chdir(cwd)
    for i in range(n):
        publish.approve(f"{prefix}vid", f"{i:02d}", "ma_chaine", account=_ACCOUNT)


def test_approve_from_two_processes_loses_no_entry(isolated_cwd):
    import multiprocessing
    import sys

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    for prefix in ("a", "b"):
        for i in range(20):
            _write_sidecar(isolated_cwd, f"{prefix}vid", f"{i:02d}")
    ctx = multiprocessing.get_context("fork" if sys.platform != "win32" else "spawn")
    procs = [ctx.Process(target=_approve_many, args=(isolated_cwd, prefix, 20)) for prefix in ("a", "b")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)
        assert p.exitcode == 0

    keys = {(e["video_id"], e["clip_id"]) for e in _read_state(isolated_cwd, "ma_chaine")}
    assert len(keys) == 40


def _seed_entry(cwd, status, clip_id="03"):
    _write_config(cwd)
    _write_preset(
        cwd, "ma_chaine",
        '[channel]\ntimezone = "UTC"\n',
    )
    _write_sidecar(cwd, "vid1", clip_id)
    state = _state_file(cwd, "ma_chaine")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps([{
        "video_id": "vid1", "clip_id": clip_id, "series_id": None, "part": None,
        "status": status, "slot_at": None, "decided_at": "2026-09-28T00:00:00+00:00",
        "published_at": None, "error": None,
    }]), encoding="utf-8")


@pytest.mark.parametrize("status", ["rejected", "published"])
def test_move_refuses_a_rejected_or_published_entry(isolated_cwd, status):
    from clipper import publish

    _seed_entry(isolated_cwd, status)
    slot = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    with pytest.raises(publish.PublishError, match=status):
        publish.move("vid1", "03", "ma_chaine", slot, schedule=_MON9)


@pytest.mark.parametrize("status", ["approved", "rejected", "published", "failed"])
def test_mark_published_requires_a_scheduled_entry(isolated_cwd, status):
    from clipper import publish

    _seed_entry(isolated_cwd, status)

    with pytest.raises(publish.PublishError, match="scheduled"):
        publish.mark_published("vid1", "03", "ma_chaine")


def test_mark_published_accepts_a_scheduled_entry(isolated_cwd):
    from clipper import publish

    _seed_entry(isolated_cwd, "scheduled")

    assert publish.mark_published("vid1", "03", "ma_chaine")["status"] == "published"


def _series(cwd):
    _write_config(cwd)
    _write_preset(cwd, "ma_chaine")
    _write_sidecar(cwd, "vid1", "03-p1", part=1, parts_total=3)
    _write_sidecar(cwd, "vid1", "03-p2", part=2, parts_total=3)
    _write_sidecar(cwd, "vid1", "03-p3", part=3, parts_total=3)


def test_approve_part_n_refuses_when_previous_part_was_never_approved(isolated_cwd):
    from clipper import publish

    _series(isolated_cwd)

    with pytest.raises(publish.PublishError, match="03-p1"):
        publish.approve("vid1", "03-p2", "ma_chaine", account=_ACCOUNT)
    assert _read_state(isolated_cwd, "ma_chaine") == []


def test_approve_part_n_refuses_when_previous_part_is_rejected(isolated_cwd):
    from clipper import publish

    _series(isolated_cwd)
    publish.approve("vid1", "03-p1", "ma_chaine", account=_ACCOUNT)
    state = _state_file(isolated_cwd, "ma_chaine")
    entries = json.loads(state.read_text(encoding="utf-8"))
    entries[0]["status"] = "rejected"
    state.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(publish.PublishError, match="03-p1"):
        publish.approve("vid1", "03-p2", "ma_chaine", account=_ACCOUNT)


def test_approve_part_n_accepts_when_previous_part_is_approved(isolated_cwd):
    from clipper import publish

    _series(isolated_cwd)
    publish.approve("vid1", "03-p1", "ma_chaine", account=_ACCOUNT)
    publish.approve("vid1", "03-p2", "ma_chaine", account=_ACCOUNT)

    assert publish.approve("vid1", "03-p3", "ma_chaine", account=_ACCOUNT)["part"] == 3


def test_sibling_clip_ids_raises_publish_error_on_corrupt_json(isolated_cwd):
    from clipper import publish

    _write_sidecar(isolated_cwd, "vid1", "03-p1", part=1, parts_total=2)
    (isolated_cwd / "output" / "vid1" / "03-p2.json").write_text("{pas du json", encoding="utf-8")

    with pytest.raises(publish.PublishError, match="03-p2.json"):
        publish._sibling_clip_ids(isolated_cwd / "output", "vid1", "vid1:03", exclude="03-p1")


# --------------------------------------------------------------------------
# TASK-0b78 : etat de publication TikTok (SPEC-9225 R3, R4, R6)
# --------------------------------------------------------------------------

_ACCOUNT = "ab12cd"
_OTHER_ACCOUNT = "ef34ab"  # TASK-16eeaccfaf09 : compte autre que _ACCOUNT, pour les clips valides ailleurs
_TWO_SLOTS = '[channel]\ntimezone = "UTC"\n'
_TWO_SCHED = _sched(("mon", "09:00"), ("mon", "18:00"))
_MON = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)


def _tiktok_env(cwd, clips=("01", "02", "03"), preset=_TWO_SLOTS):
    _write_config(cwd)
    (cwd / "state").mkdir(exist_ok=True)
    (cwd / "state" / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": _ACCOUNT, "label": "Compte"}]}), encoding="utf-8")
    _write_preset(cwd, "ma_chaine", preset)
    from clipper import publish

    for clip in clips:
        _write_sidecar(cwd, "vid1", clip)
        publish.approve("vid1", clip, "ma_chaine", now=_MON, account=_ACCOUNT, schedule=_TWO_SCHED)
    return publish


def test_mark_published_with_a_tiktok_post_records_it_in_the_entry_and_the_sidecar(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    at = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

    entry = publish.mark_published(
        "vid1", "01", "ma_chaine", now=at, post_url="https://example.invalid/@ma_chaine/video/42", post_id="42",
        tiktok_state="published", publish_at=at.isoformat(), account=_ACCOUNT)

    assert entry["status"] == "published"
    assert (entry["post_url"], entry["post_id"], entry["tiktok_state"]) == ("https://example.invalid/@ma_chaine/video/42", "42", "published")
    assert entry["tiktok_publish_at"] == at.isoformat()
    assert _read_state(isolated_cwd, "ma_chaine")[0]["post_id"] == "42"
    sidecar = _read_sidecar(isolated_cwd, "vid1", "01")
    assert sidecar["tiktok_post"] == {
        "url": "https://example.invalid/@ma_chaine/video/42", "id": "42", "state": "published",
        "publish_at": at.isoformat(), "account": _ACCOUNT, "note": None}


def test_mark_published_records_the_full_url_and_the_id_taken_from_the_end_of_the_link(isolated_cwd):
    from clipper import tiktok

    publish = _tiktok_env(isolated_cwd, ("01",))
    url = "https://www.tiktok.com/@ma_chaine/video/7300000000000000042"
    post_id = tiktok._POST_ID_END.search(url).group(1)

    entry = publish.mark_published("vid1", "01", "ma_chaine", post_url=url, post_id=post_id,
                                   tiktok_state="published", publish_at="2026-10-01T12:00:00+00:00", account=_ACCOUNT)

    assert (entry["post_url"], entry["post_id"]) == (url, "7300000000000000042")
    sidecar = _read_sidecar(isolated_cwd, "vid1", "01")
    assert (sidecar["tiktok_post"]["url"], sidecar["tiktok_post"]["id"]) == (url, "7300000000000000042")


def test_mark_published_with_a_missing_post_link_keeps_the_note_and_invents_no_id(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    note = "publication réussie mais lien du post introuvable sur la page Publications (aucun lien) : à vérifier à la main"

    entry = publish.mark_published("vid1", "01", "ma_chaine", post_url=None, post_id=None, tiktok_state="published",
                                   publish_at="2026-10-01T12:00:00+00:00", post_note=note, account=_ACCOUNT)

    assert entry["status"] == "published"  # la publication a reussi
    assert entry["post_url"] is None and entry["post_id"] is None and entry["post_note"] == note
    tiktok_post = _read_sidecar(isolated_cwd, "vid1", "01")["tiktok_post"]
    assert tiktok_post["url"] is None and tiktok_post["id"] is None and tiktok_post["note"] == note


def test_mark_failed_records_reason_capture_and_halt_then_retry_puts_it_back(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))

    entry = publish.mark_failed("vid1", "01", "ma_chaine", "captcha détecté",
                                capture="state/browser/ab12cd/captures/x.png", halted=True)

    assert entry["status"] == "failed"
    assert entry["error"] == "captcha détecté"
    assert entry["capture"] == "state/browser/ab12cd/captures/x.png"
    assert publish.halted_account(_ACCOUNT)["error"] == "captcha détecté"

    retried = publish.retry("vid1", "01", "ma_chaine")

    assert retried["status"] == "scheduled"
    assert retried["slot_at"] == entry["slot_at"]
    assert retried["error"] is None and retried["capture"] is None and not retried["halted"]
    assert publish.halted_account(_ACCOUNT) is None


def test_a_to_verify_failure_is_flagged_not_halting_and_retry_clears_the_flag(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))

    entry = publish.mark_failed("vid1", "01", "ma_chaine", "programmation à vérifier", to_verify=True)

    assert entry["status"] == "failed" and entry["to_verify"] is True and not entry["halted"]
    assert publish.halted_account(_ACCOUNT) is None
    assert publish.retry("vid1", "01", "ma_chaine")["to_verify"] is False


def test_flag_missing_on_tiktok_only_touches_a_scheduled_entry_without_post_id_and_only_once(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))
    publish.mark_published("vid1", "01", "ma_chaine", tiktok_state="scheduled_on_tiktok", post_url=None, post_id=None)
    publish.mark_published("vid1", "02", "ma_chaine", tiktok_state="scheduled_on_tiktok", post_id="7300000000000000001")

    assert publish.flag_missing_on_tiktok("vid1", "01", "ma_chaine", "absente du relevé") is True
    assert publish.flag_missing_on_tiktok("vid1", "01", "ma_chaine", "absente du relevé") is False
    assert publish.flag_missing_on_tiktok("vid1", "02", "ma_chaine", "absente du relevé") is False
    first, second = publish.list_entries("ma_chaine")
    assert first["missing_on_tiktok"] is True and first["post_note"] == "absente du relevé"
    assert "missing_on_tiktok" not in second


def test_retry_refuses_an_entry_that_did_not_fail(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    with pytest.raises(publish.PublishError, match="failed"):
        publish.retry("vid1", "01", "ma_chaine")
    with pytest.raises(publish.PublishError, match="absent"):
        publish.retry("vid1", "99", "ma_chaine")


def test_mark_failed_refuses_a_published_or_unknown_entry(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_published("vid1", "01", "ma_chaine")
    with pytest.raises(publish.PublishError, match="published"):
        publish.mark_failed("vid1", "01", "ma_chaine", "x")
    with pytest.raises(publish.PublishError, match="absent"):
        publish.mark_failed("vid1", "99", "ma_chaine", "x")


def test_account_publish_times_lists_the_posts_of_every_channel_of_the_account(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))
    at = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    publish.mark_published("vid1", "01", "ma_chaine", now=at, tiktok_state="published", publish_at=at.isoformat(), account=_ACCOUNT)
    later = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    publish.mark_published("vid1", "02", "ma_chaine", now=at, tiktok_state="scheduled_on_tiktok", publish_at=later.isoformat())
    _write_preset(isolated_cwd, "autre", f'[channel]\ntiktok_account = "{_ACCOUNT}"\n')
    _write_preset(isolated_cwd, "sans_compte", "[channel]\n")

    assert publish.account_publish_times(_ACCOUNT) == [at, later]
    assert publish.account_publish_times("zz99") == []


def test_manually_published_entries_count_by_their_published_at(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_published("vid1", "01", "ma_chaine", now=_MON)
    assert publish.account_publish_times(_ACCOUNT) == [_MON]


def test_postpone_moves_to_the_next_free_allowed_slot_and_records_why(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))  # 01 : lun 09:00, 02 : lun 18:00
    first = publish.list_entries("ma_chaine")[0]
    blocked_until = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)

    entry = publish.postpone(
        "vid1", "01", "ma_chaine", "plafond atteint",
        allowed=lambda slot: "trop tot" if slot < blocked_until else None, now=_MON, schedule=_TWO_SCHED)

    assert first["slot_at"] == "2026-09-28T09:00:00+00:00"
    assert entry["slot_at"] == "2026-10-05T09:00:00+00:00"  # le 18:00 du 28 est pris par 02 ; le 05 09:00 est libre
    assert entry["status"] == "scheduled"
    assert "plafond atteint" in entry["postponed_reason"] and "2026-10-05" in entry["postponed_reason"]
    assert _read_state(isolated_cwd, "ma_chaine")[0]["slot_at"] == entry["slot_at"]


def test_postpone_without_any_allowed_slot_is_an_explicit_error(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    with pytest.raises(publish.PublishError, match="créneau"):
        publish.postpone("vid1", "01", "ma_chaine", "plafond", allowed=lambda slot: "jamais", now=_MON, schedule=_TWO_SCHED)


def test_set_mode_overrides_the_publish_mode_of_one_entry(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    assert publish.set_mode("vid1", "01", "ma_chaine", "scheduled")["publish_mode"] == "scheduled"
    assert publish.set_mode("vid1", "01", "ma_chaine", None)["publish_mode"] is None
    with pytest.raises(publish.PublishError, match="mode"):
        publish.set_mode("vid1", "01", "ma_chaine", "demain")


# --------------------------------------------------------------------------
# SPEC-00d1 R4 : compte choisi par publication
# --------------------------------------------------------------------------

_OTHER = "ef34ab"


def test_approve_records_the_account_it_was_given(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))

    assert publish.list_entries("ma_chaine")[0]["account"] == _ACCOUNT


def test_approve_without_an_account_is_an_explicit_failure_and_writes_nothing(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)
    _write_sidecar(isolated_cwd, "vid1", "01")

    for missing in (None, ""):
        with pytest.raises(publish.PublishError, match="compte de publication manquant"):
            publish.approve("vid1", "01", "ma_chaine", now=_MON, account=missing, schedule=_MON9)
    assert _read_state(isolated_cwd, "ma_chaine") == []


def test_approve_takes_the_chosen_account_and_its_own_slots(isolated_cwd):
    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)
    from clipper import publish

    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid1", "02")
    publish.approve("vid1", "01", "ma_chaine", now=_MON, account=_ACCOUNT, schedule=_MON9)
    publish.approve("vid1", "02", "ma_chaine", now=_MON, account=_OTHER, schedule=_sched(("tue", "10:00")))

    first, second = publish.list_entries("ma_chaine")
    assert (first["account"], first["slot_at"]) == (_ACCOUNT, "2026-09-28T09:00:00+00:00")
    assert (second["account"], second["slot_at"]) == (_OTHER, "2026-09-29T10:00:00+00:00")


def test_move_and_postpone_without_account_slots_are_explicit_errors(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    slot = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    with pytest.raises(publish.PublishError, match="aucun créneau"):
        publish.move("vid1", "01", "ma_chaine", slot, schedule=_sched())
    with pytest.raises(publish.PublishError, match="aucun créneau"):
        publish.postpone("vid1", "01", "ma_chaine", "plafond", allowed=lambda s: None, now=_MON, schedule=None)


def test_set_account_changes_the_account_of_an_entry_until_it_is_published(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))

    entry = publish.set_account("vid1", "01", "ma_chaine", _OTHER)
    assert entry["account"] == _OTHER and publish.list_entries("ma_chaine")[0]["account"] == _OTHER
    assert publish.list_entries("ma_chaine")[1]["account"] == _ACCOUNT  # les autres entrees ne bougent pas

    publish.mark_published("vid1", "02", "ma_chaine", now=_MON)
    with pytest.raises(publish.PublishError, match="changement de compte refusé"):
        publish.set_account("vid1", "02", "ma_chaine", _OTHER)
    with pytest.raises(publish.PublishError, match="absent de la file"):
        publish.set_account("vid1", "99", "ma_chaine", _OTHER)
    with pytest.raises(publish.PublishError, match="compte de publication manquant"):
        publish.set_account("vid1", "01", "ma_chaine", "")


def test_entry_account_never_falls_back_to_another_account(isolated_cwd):
    from clipper import publish

    assert publish.entry_account({"account": _OTHER}) == _OTHER
    assert publish.entry_account({"account": None}) is None  # aucun compte choisi : jamais un autre
    assert publish.entry_account({}) is None  # file d'avant R4 : plus de compte de style en repli (SPEC-6076 R2)


# --------------------------------------------------------------------------
# TASK-5c00d0c09c98 : « Après la dernière programmation + N h » (SPEC-1ed3)
# --------------------------------------------------------------------------


def test_after_last_schedule_adds_the_interval_to_the_last_future_publication(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))  # 01 : lun 09:00, 02 : lun 18:00
    noon = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # entre les deux : 09:00 est passé, 18:00 à venir

    when = publish.after_last_schedule(_ACCOUNT, 2, now=noon)

    assert when == datetime(2026, 9, 28, 20, 0, tzinfo=timezone.utc)  # 18:00 (à venir) + 2 h ; 09:00 (passé) ignoré


def test_after_last_schedule_falls_back_to_now_without_any_future_publication(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ())
    now = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)

    when = publish.after_last_schedule(_ACCOUNT, 3, now=now)

    assert when == now + timedelta(hours=3)


def test_after_last_schedule_counts_a_post_scheduled_on_the_service_not_yet_live(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    at = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    later = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    publish.mark_published("vid1", "01", "ma_chaine", now=at, tiktok_state="scheduled_on_tiktok",
                           publish_at=later.isoformat(), account=_ACCOUNT)

    when = publish.after_last_schedule(_ACCOUNT, 1, now=at)

    assert when == later + timedelta(hours=1)


def test_after_last_schedule_ignores_entries_moved_to_another_account(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))  # 01 : lun 09:00
    now = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
    publish.set_account("vid1", "01", "ma_chaine", _OTHER_ACCOUNT)

    when = publish.after_last_schedule(_ACCOUNT, 2, now=now)

    assert when == now + timedelta(hours=2)


def test_the_posts_and_halt_of_an_account_follow_the_entry_account_across_channels(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))
    _write_preset(isolated_cwd, "autre", _TWO_SLOTS.replace(_ACCOUNT, _OTHER))
    (isolated_cwd / "state" / "accounts.json").write_text(json.dumps({"accounts": [
        {"id": _ACCOUNT, "label": "A"}, {"id": _OTHER, "label": "B"}]}), encoding="utf-8")
    publish.set_account("vid1", "01", "ma_chaine", _OTHER)  # le clip de ma_chaine part sur l'autre compte
    at = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    publish.mark_published("vid1", "01", "ma_chaine", now=at, tiktok_state="published", post_id="1",
                           publish_at=at.isoformat(), account=_OTHER)
    publish.mark_failed("vid1", "02", "ma_chaine", "captcha", halted=True)

    assert publish.account_publish_times(_OTHER) == [at]
    assert publish.account_publish_times(_ACCOUNT) == []
    assert publish.halted_account(_OTHER) is None
    assert publish.halted_account(_ACCOUNT)["clip_id"] == "02"


def test_mark_failed_records_when_and_last_failure_is_the_most_recent_of_the_account(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))
    early = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

    first = publish.mark_failed("vid1", "01", "ma_chaine", "premier", now=early)
    publish.mark_failed("vid1", "02", "ma_chaine", "second", now=early.replace(hour=11), capture="c.png")

    assert first["failed_at"] == early.isoformat()
    last = publish.last_failure(_ACCOUNT)
    assert (last["clip_id"], last["error"], last["channel"], last["capture"]) == ("02", "second", "ma_chaine", "c.png")
    assert publish.last_failure(_OTHER) is None


def test_waiting_reason_is_set_once_and_cleared_by_publishing_failing_or_changing_account(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02", "03"))

    assert publish.set_waiting_reason("vid1", "01", "ma_chaine", "compte non prêt") is True
    assert publish.set_waiting_reason("vid1", "01", "ma_chaine", "compte non prêt") is False  # inchange
    assert publish.list_entries("ma_chaine")[0]["waiting_reason"] == "compte non prêt"
    publish.set_account("vid1", "01", "ma_chaine", _OTHER)
    assert publish.list_entries("ma_chaine")[0]["waiting_reason"] is None

    publish.set_waiting_reason("vid1", "02", "ma_chaine", "raison")
    publish.mark_failed("vid1", "02", "ma_chaine", "echec")
    assert publish.list_entries("ma_chaine")[1]["waiting_reason"] is None
    publish.set_waiting_reason("vid1", "03", "ma_chaine", "raison")
    publish.mark_published("vid1", "03", "ma_chaine", now=_MON)
    assert publish.list_entries("ma_chaine")[2]["waiting_reason"] is None
    with pytest.raises(publish.PublishError, match="absent de la file"):
        publish.set_waiting_reason("vid1", "99", "ma_chaine", "x")


# --------------------------------------------------------------------------
# SPEC-1ed3 : publication pilotee (create_post / update_post / cancel_post)
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _post(isolated_cwd, clip="03", channel="ma_chaine", **kwargs):
    from clipper import publish

    kwargs.setdefault("account", "compte1")
    kwargs.setdefault("mode", "immediate")
    kwargs.setdefault("now", NOW)
    return publish.create_post("vid1", clip, channel, **kwargs)


def _setup(cwd, preset=True, **sidecar):
    _write_config(cwd)
    if preset:
        _write_preset(cwd, "ma_chaine")
    _write_sidecar(cwd, "vid1", "03", **sidecar)


def test_create_post_now_approves_implicitly_and_is_due_right_away(isolated_cwd):
    _setup(isolated_cwd)

    entry = _post(isolated_cwd)

    assert entry["status"] == "scheduled"  # approbation implicite : jamais une etape « approved » a part
    assert entry["publish_mode"] == "immediate"
    assert entry["account"] == "compte1"
    assert entry["slot_at"] == NOW.isoformat()  # due tout de suite
    assert entry["decided_at"] == NOW.isoformat() and entry["manual"] is True
    assert _read_state(isolated_cwd, "ma_chaine") == [entry]


def test_create_post_scheduled_keeps_the_date_mode_and_post_options(isolated_cwd):
    _setup(isolated_cwd)
    when = datetime(2026, 10, 3, 18, 30, tzinfo=timezone.utc)
    options = {"visibility": "friends", "allow_comments": False, "allow_reuse": True,
               "ai_generated": True, "content_check": "wait"}

    entry = _post(isolated_cwd, mode="scheduled", publish_at=when, options=options)

    assert entry["slot_at"] == when.isoformat() and entry["publish_mode"] == "scheduled"
    assert entry["post_options"] == options


def test_a_video_without_channel_is_publishable_the_account_is_enough(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd, preset=False)

    entry = _post(isolated_cwd, channel=None)

    assert entry["status"] == "scheduled" and entry["account"] == "compte1"
    assert _read_state(isolated_cwd, publish.NO_CHANNEL) == [entry]
    assert publish.list_entries(publish.NO_CHANNEL) == [entry]


def test_a_channel_without_slots_is_publishable_slots_are_never_required(isolated_cwd):
    _setup(isolated_cwd)  # preset sans [[channel.slots]]
    assert _post(isolated_cwd)["slot_at"] is not None


def test_create_post_refuses_a_clip_not_ready_or_unknown(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd, ready=False)
    with pytest.raises(publish.PublishError, match="non prêt|non pret"):
        _post(isolated_cwd)
    with pytest.raises(publish.PublishError, match="introuvable"):
        _post(isolated_cwd, clip="99")


@pytest.mark.parametrize("status", ["rejected", "published"])
def test_create_post_refuses_rejected_and_published_clips(isolated_cwd, status):
    from clipper import publish

    _setup(isolated_cwd)
    publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)
    path = _state_file(isolated_cwd, "ma_chaine")
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["status"] = status
    path.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(publish.PublishError, match=status):
        _post(isolated_cwd)


def test_create_post_accepts_a_clip_approved_the_old_way(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)

    entry = _post(isolated_cwd)

    assert entry["status"] == "scheduled"
    assert len(_read_state(isolated_cwd, "ma_chaine")) == 1  # remplace l'entree, pas de doublon


def test_create_post_refuses_a_clip_already_in_the_queue(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    with pytest.raises(publish.PublishError, match="déjà"):
        _post(isolated_cwd)


def test_private_and_scheduled_is_refused_explicitly(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    with pytest.raises(publish.PublishError, match="privée.*programmée|programmée.*privée"):
        _post(isolated_cwd, mode="scheduled", publish_at=datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc),
              options={"visibility": "private"})
    assert _read_state(isolated_cwd, "ma_chaine") == []
    # privee + maintenant : permis
    assert _post(isolated_cwd, options={"visibility": "private"})["post_options"]["visibility"] == "private"


@pytest.mark.parametrize("kwargs, message", [
    ({"mode": "scheduled"}, "date"),
    ({"mode": "scheduled", "publish_at": datetime(2026, 10, 3, 9, 0)}, "fuseau"),
    ({"mode": "scheduled", "publish_at": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)}, "passé"),
    ({"mode": "scheduled", "publish_at": datetime(2026, 10, 1, 12, 5, tzinfo=timezone.utc)}, "15 minutes"),
    ({"mode": "demain"}, "mode"),
    ({"account": ""}, "compte"),
    ({"options": {"visibility": "secret"}}, "visibility"),
])
def test_create_post_refuses_invalid_input_in_french(isolated_cwd, kwargs, message):
    from clipper import publish

    _setup(isolated_cwd)
    with pytest.raises(publish.PublishError, match=message):
        _post(isolated_cwd, **kwargs)
    assert _read_state(isolated_cwd, "ma_chaine") == []


def test_a_date_beyond_the_tiktok_window_is_accepted_and_kept(isolated_cwd):
    _setup(isolated_cwd)
    far = NOW.replace(month=11, day=20)  # > 10 jours : Clipper la garde

    assert _post(isolated_cwd, mode="scheduled", publish_at=far)["slot_at"] == far.isoformat()


def test_daily_cap_is_refused_with_the_reason_and_the_next_possible_time(isolated_cwd):
    from clipper import publish, tiktok

    _setup(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "04")
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 1, "min_gap_minutes": 0}
    _post(isolated_cwd, settings=settings)

    with pytest.raises(publish.LimitError) as err:
        _post(isolated_cwd, clip="04", settings=settings)

    assert "plafond de 1 publication" in str(err.value)
    assert "prochaine heure possible" in str(err.value)
    # lendemain 00:00 (fuseau de la chaine : Europe/Paris) : 2026-10-01 22:00 UTC
    assert err.value.next_at == datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    assert len(_read_state(isolated_cwd, "ma_chaine")) == 1  # rien n'est ecrit, aucun report silencieux


def test_daily_cap_uses_the_given_schedule_timezone_not_the_styles(isolated_cwd):
    """Revue r-comptes 9 : _check_caps (via create_post) compte le jour dans le fuseau du COMPTE (``schedule``),
    pas celui du style, quand on le lui donne."""
    from clipper import publish, tiktok

    _setup(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "04")
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 1, "min_gap_minutes": 0}
    a = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)   # Europe/Paris : 10-01 ; Asia/Tokyo : 10-02
    b = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)   # Europe/Paris : 10-02 (jour different de a)
    _post(isolated_cwd, settings=settings, now=a)  # clip 03, ancre : plafond de 1 pris sur son jour

    # fuseau du compte (schedule) en Asia/Tokyo : meme jour que l'ancre (10-02 aux deux instants) -> refuse,
    # alors que le fuseau du style (Europe/Paris, par defaut) verrait deux jours differents.
    with pytest.raises(publish.LimitError, match="plafond de 1 publication"):
        _post(isolated_cwd, clip="04", settings=settings, now=b, schedule={"slots": [], "timezone": "Asia/Tokyo"})


def test_min_gap_is_refused_and_the_next_time_respects_the_gap(isolated_cwd):
    from clipper import publish, tiktok

    _setup(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "04")
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 5, "min_gap_minutes": 120}
    _post(isolated_cwd, settings=settings)

    with pytest.raises(publish.LimitError, match="écart minimal") as err:
        _post(isolated_cwd, clip="04", settings=settings, now=NOW.replace(minute=30))

    assert err.value.next_at == NOW.replace(hour=14)
    # a l'heure proposee, la creation passe
    ok = _post(isolated_cwd, clip="04", settings=settings, now=NOW.replace(minute=30),
               mode="scheduled", publish_at=err.value.next_at)
    assert ok["slot_at"] == err.value.next_at.isoformat()


def test_caps_count_the_accounts_posts_across_channels_and_pending_entries(isolated_cwd):
    from clipper import publish, tiktok

    _setup(isolated_cwd, preset=False)
    _write_sidecar(isolated_cwd, "vid1", "04")
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 1, "min_gap_minutes": 0}
    _post(isolated_cwd, channel=None, settings=settings)  # compte1, file « sans chaine »
    _write_preset(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.LimitError):  # meme compte, autre fichier de file
        _post(isolated_cwd, clip="04", settings=settings)
    assert _post(isolated_cwd, clip="04", settings=settings, account="compte2")["account"] == "compte2"


def test_update_post_changes_time_account_options_and_revalidates(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    when = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)

    entry = publish.update_post(
        "vid1", "03", "ma_chaine", now=NOW, account="compte2", mode="scheduled", publish_at=when,
        options={"visibility": "friends", "ai_generated": True})

    assert (entry["account"], entry["publish_mode"], entry["slot_at"]) == ("compte2", "scheduled", when.isoformat())
    assert entry["post_options"] == {"visibility": "friends", "ai_generated": True}
    assert entry["waiting_reason"] is None
    with pytest.raises(publish.PublishError, match="privée"):
        publish.update_post("vid1", "03", "ma_chaine", now=NOW, options={"visibility": "private"})
    assert _read_state(isolated_cwd, "ma_chaine") == [entry]


def test_update_post_keeps_the_own_entry_out_of_the_cap(isolated_cwd):
    from clipper import publish, tiktok

    _setup(isolated_cwd)
    settings = {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 1, "min_gap_minutes": 0}
    _post(isolated_cwd, settings=settings)
    publish.update_post("vid1", "03", "ma_chaine", now=NOW, settings=settings, options={"allow_comments": False})


def test_update_post_rewrites_the_caption_and_hashtags_in_the_sidecar(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd, caption="Nouvelle legende", hashtags=["#a", "#b"])
    sidecar = _read_sidecar(isolated_cwd, "vid1", "03")
    assert sidecar["caption"] == "Nouvelle legende" and sidecar["hashtags"] == ["#a", "#b"]
    publish.update_post("vid1", "03", "ma_chaine", now=NOW, caption="Encore", hashtags=["#c"])
    assert _read_sidecar(isolated_cwd, "vid1", "03")["caption"] == "Encore"


def test_cancel_post_removes_the_entry_and_the_clip_is_to_validate_again(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)

    publish.cancel_post("vid1", "03", "ma_chaine")

    assert _read_state(isolated_cwd, "ma_chaine") == []


def test_an_entry_in_progress_can_be_neither_edited_nor_cancelled(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    entry = publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    assert entry["in_progress_since"] == NOW.isoformat()

    with pytest.raises(publish.PublishError, match="en cours"):
        publish.update_post("vid1", "03", "ma_chaine", now=NOW, account="compte2")
    with pytest.raises(publish.PublishError, match="en cours"):
        publish.cancel_post("vid1", "03", "ma_chaine")
    with pytest.raises(publish.PublishError, match="en cours"):
        publish.unschedule("vid1", "03", "ma_chaine")


def test_a_published_entry_can_be_neither_edited_nor_cancelled(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_published("vid1", "03", "ma_chaine", now=NOW)

    with pytest.raises(publish.PublishError, match="publi"):
        publish.update_post("vid1", "03", "ma_chaine", now=NOW, account="compte2")
    with pytest.raises(publish.PublishError, match="publi"):
        publish.cancel_post("vid1", "03", "ma_chaine")


def test_an_entry_scheduled_on_tiktok_is_not_cancelled_from_clipper(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_published("vid1", "03", "ma_chaine", now=NOW, tiktok_state="scheduled_on_tiktok",
                           post_url=None, post_id=None, publish_at=NOW.isoformat(), account="compte1")
    with pytest.raises(publish.PublishError, match="TikTok Studio"):
        publish.cancel_post("vid1", "03", "ma_chaine")


def test_in_progress_is_cleared_on_success_and_failure(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    failed = publish.mark_failed("vid1", "03", "ma_chaine", "raison", now=NOW)
    assert failed["in_progress_since"] is None
    publish.retry("vid1", "03", "ma_chaine")
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    # succes du worker : il enregistre son post (tiktok_state) ; une declaration a la main est refusee pendant
    # le pilotage (revue fable-publication I2)
    done = publish.mark_published("vid1", "03", "ma_chaine", now=NOW, tiktok_state="published",
                                  publish_at=NOW.isoformat(), account="compte1")
    assert done["in_progress_since"] is None


def test_interrupted_entries_are_failed_with_an_explicit_reason(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    dead_pid, dead_created = _dead_process()  # le pilote est mort (worker arrete pendant la publication)
    path = isolated_cwd / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0].update(in_progress_pid=dead_pid, in_progress_pid_created_at=dead_created)
    path.write_text(json.dumps(entries), encoding="utf-8")

    assert publish.fail_interrupted("ma_chaine", now=NOW) == 1

    entry = _read_state(isolated_cwd, "ma_chaine")[0]
    assert entry["status"] == "failed" and "interrompue" in entry["error"] and entry["halted"] is False
    assert publish.fail_interrupted("ma_chaine", now=NOW) == 0


def test_all_entries_lists_every_channel_and_the_no_channel_file(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "04")
    _post(isolated_cwd)
    _post(isolated_cwd, clip="04", channel=None, account="compte2")

    found = publish.all_entries()

    assert sorted((c, e["clip_id"]) for c, e in found) == [(publish.NO_CHANNEL, "04"), ("ma_chaine", "03")]


# --------------------------------------------------------------------------
# TASK-9776 : publications YouTube (SPEC-5e50 R2, R5)
# --------------------------------------------------------------------------


def test_mark_published_with_a_youtube_post_records_it_in_the_entry_and_the_sidecar(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    at = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    url = "https://youtube.com/shorts/OOOeOwbvu34"

    entry = publish.mark_published(
        "vid1", "01", "ma_chaine", now=at, post_url=url, post_id="OOOeOwbvu34", tiktok_state="published",
        publish_at=at.isoformat(), account=_ACCOUNT, service="youtube")

    assert entry["status"] == "published" and entry["service"] == "youtube"
    assert (entry["post_url"], entry["post_id"]) == (url, "OOOeOwbvu34")
    sidecar = _read_sidecar(isolated_cwd, "vid1", "01")
    assert sidecar["youtube_post"] == {"url": url, "id": "OOOeOwbvu34", "state": "published",
                                       "publish_at": at.isoformat(), "account": _ACCOUNT, "note": None}
    assert "tiktok_post" not in sidecar


def test_a_youtube_publication_accepts_scheduled_on_youtube_and_only_that_scheduled_state(isolated_cwd):
    from clipper import publish

    publish = _tiktok_env(isolated_cwd, ("01", "02", "03"))
    at = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    entry = publish.mark_published("vid1", "01", "ma_chaine", now=at, post_url=None, post_id=None,
                                   tiktok_state="scheduled_on_youtube", publish_at=at.isoformat(), service="youtube")
    assert entry["tiktok_state"] == "scheduled_on_youtube"
    with pytest.raises(publish.PublishError, match="YouTube"):
        publish.mark_published("vid1", "02", "ma_chaine", tiktok_state="scheduled_on_tiktok", service="youtube")
    with pytest.raises(publish.PublishError, match="TikTok"):
        publish.mark_published("vid1", "03", "ma_chaine", tiktok_state="scheduled_on_youtube")
    with pytest.raises(publish.PublishError, match="service invalide"):
        publish.mark_published("vid1", "03", "ma_chaine", tiktok_state="published", service="autre")


def test_a_youtube_post_is_validated_with_the_youtube_options_and_remembers_its_service(isolated_cwd):
    _setup(isolated_cwd)
    options = {"title": "Mon titre", "visibility": "unlisted", "made_for_kids": False}

    entry = _post(isolated_cwd, options=options, service="youtube")

    assert entry["service"] == "youtube" and entry["post_options"] == options


def test_a_tiktok_only_option_is_refused_on_a_youtube_post_and_the_reverse(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    with pytest.raises(publish.PublishError, match="allow_comments"):
        _post(isolated_cwd, options={"allow_comments": False}, service="youtube")
    with pytest.raises(publish.PublishError, match="made_for_kids"):
        _post(isolated_cwd, options={"made_for_kids": True})


def test_a_scheduled_youtube_post_must_be_public(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    with pytest.raises(publish.PublishError, match="programm"):
        _post(isolated_cwd, mode="scheduled", publish_at=NOW + timedelta(days=1), service="youtube",
              options={"visibility": "private"})


def test_a_youtube_post_uses_the_youtube_caps_not_the_tiktok_ones(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    first = _post(isolated_cwd, service="youtube")  # YouTube : 3 par jour, 120 min d'ecart
    _write_sidecar(isolated_cwd, "vid1", "04")
    _write_sidecar(isolated_cwd, "vid1", "05")
    later = _post(isolated_cwd, clip="04", mode="scheduled", publish_at=NOW + timedelta(hours=3), service="youtube")

    assert first["service"] == later["service"] == "youtube"
    with pytest.raises(publish.LimitError, match="120 minutes"):
        _post(isolated_cwd, clip="05", mode="scheduled", publish_at=NOW + timedelta(minutes=30), service="youtube")
    # le meme 2e post, sous les plafonds TikTok (1 par jour) serait refuse
    with pytest.raises(publish.LimitError, match="plafond de 1 publication"):
        _post(isolated_cwd, clip="05", mode="scheduled", publish_at=NOW + timedelta(hours=9))


def test_update_post_revalidates_with_the_youtube_service(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd, service="youtube")

    entry = publish.update_post("vid1", "03", "ma_chaine", options={"visibility": "private"}, now=NOW,
                                service="youtube")
    assert entry["post_options"] == {"visibility": "private"} and entry["service"] == "youtube"
    with pytest.raises(publish.PublishError, match="programm"):
        publish.update_post("vid1", "03", "ma_chaine", mode="scheduled", publish_at=NOW + timedelta(days=1), now=NOW,
                            service="youtube")


def test_cancelling_a_publication_scheduled_on_youtube_points_to_youtube_studio(isolated_cwd):
    from clipper import publish

    publish_mod = _tiktok_env(isolated_cwd, ("01",))
    publish_mod.mark_published("vid1", "01", "ma_chaine", now=NOW, tiktok_state="scheduled_on_youtube",
                               publish_at=NOW.isoformat(), service="youtube")
    with pytest.raises(publish.PublishError, match="YouTube Studio"):
        publish.cancel_post("vid1", "01", "ma_chaine")


# --------------------------------------------------------------------------
# TASK-5bbf : série programmée (SPEC-1ed3, SPEC-6076 R3/R6) : plan_series_dates,
# available_series_clips, preview_series, create_series.
# --------------------------------------------------------------------------

SERIES_NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _write_video_channel(cwd: Path, video_id: str, channel: str | None) -> None:
    video_dir = cwd / "workspace" / video_id
    video_dir.mkdir(parents=True, exist_ok=True)
    data = {"channel": channel} if channel is not None else {}
    (video_dir / "pipeline.json").write_text(json.dumps(data), encoding="utf-8")


def _series_env(cwd: Path, channel: str = "ma_chaine") -> None:
    _write_config(cwd)
    _write_preset(cwd, channel)
    (cwd / "state").mkdir(exist_ok=True)
    (cwd / "state" / "accounts.json").write_text(
        json.dumps({"accounts": [{"id": _ACCOUNT, "label": "Compte"}]}), encoding="utf-8")


def _series_settings(**over):
    from clipper import tiktok
    return {**tiktok.CONFIG_DEFAULTS, "max_posts_per_day": 10, "min_gap_minutes": 0, **over}


# ---------- plan_series_dates ----------


def test_plan_series_dates_spaces_posts_by_the_interval_in_real_duration(isolated_cwd):
    from clipper import publish

    start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    dates = publish.plan_series_dates(start, 3, 4)

    assert dates == [start, start + timedelta(hours=3), start + timedelta(hours=6), start + timedelta(hours=9)]


def test_plan_series_dates_crosses_the_2026_dst_fallback_without_drifting_the_real_gap(isolated_cwd):
    from clipper import publish

    paris = ZoneInfo("Europe/Paris")
    start = datetime(2026, 10, 24, 20, 0, tzinfo=paris)  # CEST (+02:00), avant le passage a l'heure d'hiver

    dates = publish.plan_series_dates(start, 24, 3)

    assert dates[1] - dates[0] == timedelta(hours=24)  # duree reelle constante malgre le changement d'heure
    assert dates[2] - dates[1] == timedelta(hours=24)
    assert dates[0].astimezone(paris).strftime("%H:%M %z") == "20:00 +0200"
    assert dates[1].astimezone(paris).strftime("%H:%M %z") == "19:00 +0100"  # heure murale decalee par le changement
    assert dates[2].astimezone(paris).strftime("%H:%M %z") == "19:00 +0100"


def test_plan_series_dates_refuses_invalid_interval_count_or_naive_start(isolated_cwd):
    from clipper import publish

    start = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    with pytest.raises(publish.PublishError, match="intervalle"):
        publish.plan_series_dates(start, 0, 2)
    with pytest.raises(publish.PublishError, match="intervalle"):
        publish.plan_series_dates(start, 1.5, 2)  # pas de 0,5 heure
    with pytest.raises(publish.PublishError, match="nombre"):
        publish.plan_series_dates(start, 1, 0)
    with pytest.raises(publish.PublishError, match="fuseau"):
        publish.plan_series_dates(datetime(2026, 10, 1, 9, 0), 1, 2)


# ---------- available_series_clips ----------


def test_available_series_clips_excludes_not_ready_or_already_queued(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80, ready=False)  # pas pret
    _write_sidecar(isolated_cwd, "vid1", "c", score=70)
    publish.create_post("vid1", "c", "ma_chaine", account=_ACCOUNT, mode="immediate", now=SERIES_NOW)  # deja en file

    units = publish.available_series_clips("ma_chaine")

    assert [u["clip_ids"] for u in units] == [["a"]]
    assert units[0]["validated"] is False


def test_available_series_clips_includes_validated_clips_marked_validated(isolated_cwd):
    """TASK-16eeaccfaf09 : un clip approuve (statut 'approved', sans creneau) apparait dans le pool,
    marque ``validated``, a cote des clips prets jamais entres en file (mode manuel : les deux sont
    proposes)."""
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)  # pret, pas encore valide
    _write_sidecar(isolated_cwd, "vid1", "c", score=70)
    publish.approve("vid1", "c", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)  # valide, sans creneau

    units = {tuple(u["clip_ids"]): u for u in publish.available_series_clips("ma_chaine")}

    assert units[("a",)]["validated"] is False
    assert units[("c",)]["validated"] is True
    assert units[("c",)]["score"] == 70


def test_available_series_clips_excludes_a_validated_clip_once_it_has_a_slot(isolated_cwd):
    """Une entree 'approved' qui recoit un creneau devient 'scheduled' : plus 'validee sans creneau' (TASK-16eeaccfaf09)."""
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    publish.approve("vid1", "a", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT, schedule=_sched(("mon", "09:00")))

    assert publish.available_series_clips("ma_chaine") == []


def test_available_series_clips_excludes_a_validated_clip_in_progress(isolated_cwd):
    """Cas defensif (inatteignable via approve/create_post) : une entree 'approved' sans creneau mais 'en
    cours' n'est pas non plus prise (TASK-16eeaccfaf09, clause 'pas en cours')."""
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    state = _state_file(isolated_cwd, "ma_chaine")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps([{
        "video_id": "vid1", "clip_id": "b", "series_id": None, "part": None, "status": "approved",
        "slot_at": None, "decided_at": SERIES_NOW.isoformat(), "published_at": None, "error": None,
        "account": _ACCOUNT, "in_progress_since": SERIES_NOW.isoformat(),
    }]), encoding="utf-8")

    assert publish.available_series_clips("ma_chaine") == []


def test_available_series_clips_groups_the_parts_of_a_clip_together_in_order(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)

    units = publish.available_series_clips("ma_chaine")

    assert len(units) == 1
    assert units[0]["clip_ids"] == ["x-p1", "x-p2"]  # triees par numero de partie, jamais dans l'ordre du disque
    assert units[0]["score"] == 95


def test_available_series_clips_drops_a_whole_series_if_one_part_is_not_available(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    publish.create_post("vid1", "x-p2", "ma_chaine", account=_ACCOUNT, mode="immediate",
                        now=SERIES_NOW)  # partie 2 deja en file

    assert publish.available_series_clips("ma_chaine") == []


def test_available_series_clips_filters_by_style_none_means_every_style(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd, "style_a")
    _write_preset(isolated_cwd, "style_b")
    _write_video_channel(isolated_cwd, "vid1", "style_a")
    _write_video_channel(isolated_cwd, "vid2", "style_b")
    _write_video_channel(isolated_cwd, "vid3", None)  # video sans style
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid2", "b", score=80)
    _write_sidecar(isolated_cwd, "vid3", "c", score=70)

    assert [u["video_id"] for u in publish.available_series_clips("style_a")] == ["vid1"]
    assert sorted(u["video_id"] for u in publish.available_series_clips(None)) == ["vid1", "vid2", "vid3"]


# ---------- preview_series : mode auto ----------


def _auto_preview(cwd, **kwargs):
    from clipper import publish

    kwargs.setdefault("mode", "auto")
    kwargs.setdefault("style", "ma_chaine")
    kwargs.setdefault("account", _ACCOUNT)
    kwargs.setdefault("service", "tiktok")
    kwargs.setdefault("interval_hours", 2)
    kwargs.setdefault("start_at", SERIES_NOW + timedelta(hours=1))
    kwargs.setdefault("now", SERIES_NOW)
    kwargs.setdefault("settings", _series_settings())
    return publish.preview_series(**kwargs)


def _approve_all(cwd, video_id, clip_ids, channel="ma_chaine", account=_ACCOUNT):
    """Approuve chaque clip (dans l'ordre : requis pour les parties > 1 d'une serie), compte ``account``,
    sans creneau (TASK-16eeaccfaf09 : le mode auto ne prend que des clips valides)."""
    from clipper import publish

    for clip_id in clip_ids:
        publish.approve(video_id, clip_id, channel, now=SERIES_NOW, account=account)


def test_preview_series_auto_picks_the_n_best_clips_by_score(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=70)
    _write_sidecar(isolated_cwd, "vid1", "c", score=50)
    _approve_all(isolated_cwd, "vid1", ["a", "b", "c"])

    preview = _auto_preview(isolated_cwd, count=2)

    assert [it["clip_id"] for it in preview["items"]] == ["a", "b"]
    assert preview["ok"] is True and preview["available"] == 2 and preview["insufficient"] is False


def test_preview_series_auto_never_takes_a_ready_but_unvalidated_clip(isolated_cwd):
    """Clause TASK-16eeaccfaf09 : un clip pret mais non valide n'est jamais pris en auto, meme s'il a le
    meilleur score ; le message est clair."""
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=99)  # jamais approuve
    _write_sidecar(isolated_cwd, "vid1", "b", score=50)
    _approve_all(isolated_cwd, "vid1", ["b"])

    preview = _auto_preview(isolated_cwd, count=2)

    assert [it["clip_id"] for it in preview["items"]] == ["b"]
    assert preview["available"] == 1 and preview["insufficient"] is True
    assert "1" in preview["insufficient_reason"] and "2" in preview["insufficient_reason"]


def test_preview_series_auto_reports_a_clear_message_when_nothing_is_validated(isolated_cwd):
    """Clause TASK-16eeaccfaf09 : « valide d'abord des clips dans l'écran Clips » quand aucun clip n'est valide."""
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)  # pret, jamais approuve

    preview = _auto_preview(isolated_cwd, count=1)

    assert preview["items"] == [] and preview["available"] == 0 and preview["insufficient"] is True
    assert "valide d'abord des clips dans l'écran Clips" in preview["insufficient_reason"]


def test_preview_series_auto_never_takes_a_clip_validated_for_another_account(isolated_cwd):
    """Clause TASK-16eeaccfaf09 : un clip valide pour un autre compte n'est jamais pris en auto."""
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _approve_all(isolated_cwd, "vid1", ["a"], account=_OTHER_ACCOUNT)  # valide, mais pour un autre compte

    preview = _auto_preview(isolated_cwd, count=1)  # _auto_preview : account=_ACCOUNT par defaut

    assert preview["items"] == [] and preview["available"] == 0
    assert "valide d'abord des clips dans l'écran Clips" in preview["insufficient_reason"]


def test_preview_series_auto_dates_are_start_plus_k_times_interval_per_post(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=70)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])
    start = SERIES_NOW + timedelta(hours=1)

    preview = _auto_preview(isolated_cwd, count=2, start_at=start, interval_hours=3)

    assert [it["publish_at"] for it in preview["items"]] == [
        start.isoformat(), (start + timedelta(hours=3)).isoformat()]


def test_preview_series_auto_counts_posts_not_clips_a_multipart_clip_takes_several_slots(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "y", score=90)
    _write_sidecar(isolated_cwd, "vid1", "z", score=80)
    _approve_all(isolated_cwd, "vid1", ["x-p1", "x-p2", "y", "z"])

    preview = _auto_preview(isolated_cwd, count=3)

    assert [it["clip_id"] for it in preview["items"]] == ["x-p1", "x-p2", "y"]  # z laisse de cote : N=3 atteint


def test_preview_series_auto_skips_a_series_that_does_not_fit_and_takes_the_next_clip(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a-p1", score=95, part=1, parts_total=3)
    _write_sidecar(isolated_cwd, "vid1", "a-p2", score=95, part=2, parts_total=3)
    _write_sidecar(isolated_cwd, "vid1", "a-p3", score=95, part=3, parts_total=3)
    _write_sidecar(isolated_cwd, "vid1", "b", score=90)
    _write_sidecar(isolated_cwd, "vid1", "c", score=85)
    _approve_all(isolated_cwd, "vid1", ["a-p1", "a-p2", "a-p3", "b", "c"])

    preview = _auto_preview(isolated_cwd, count=2)

    # la serie de 3 parties (meilleur score) ne tient pas dans les 2 places : jamais coupee, on prend b puis c
    assert [it["clip_id"] for it in preview["items"]] == ["b", "c"]
    assert preview["ok"] is True


def test_preview_series_auto_says_a_validated_series_does_not_fit_instead_of_none_validated(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    ids = [f"a-p{n}" for n in range(1, 9)]
    for n, clip_id in enumerate(ids, start=1):
        _write_sidecar(isolated_cwd, "vid1", clip_id, score=95, part=n, parts_total=8)
    _approve_all(isolated_cwd, "vid1", ids)

    preview = _auto_preview(isolated_cwd, count=2)

    reason = preview["insufficient_reason"]
    assert preview["ok"] is False and preview["insufficient"] is True and preview["available"] == 0
    assert "aucun clip validé" not in reason
    assert "1 clip validé en 8 parties ne tient pas dans 2 places" in reason
    assert "passe à 8 vidéos" in reason and "Parties ensemble" in reason


def test_preview_series_auto_reports_when_not_enough_clips_are_available(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])

    preview = _auto_preview(isolated_cwd, count=5)

    assert preview["ok"] is False and preview["insufficient"] is True and preview["available"] == 2
    assert "2" in preview["insufficient_reason"] and "5" in preview["insufficient_reason"]


def test_preview_series_auto_filters_by_style(isolated_cwd):
    _series_env(isolated_cwd, "style_a")
    _write_preset(isolated_cwd, "style_b")
    _write_video_channel(isolated_cwd, "vid1", "style_a")
    _write_video_channel(isolated_cwd, "vid2", "style_b")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid2", "b", score=99)
    _approve_all(isolated_cwd, "vid1", ["a"], channel="style_a")
    _approve_all(isolated_cwd, "vid2", ["b"], channel="style_b")

    preview = _auto_preview(isolated_cwd, count=5, style="style_a")

    assert [it["clip_id"] for it in preview["items"]] == ["a"]
    assert preview["insufficient"] is True and preview["available"] == 1


def test_preview_series_refuses_a_date_under_the_minimum_advance(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _approve_all(isolated_cwd, "vid1", ["a"])

    preview = _auto_preview(isolated_cwd, count=1, start_at=SERIES_NOW + timedelta(minutes=5))

    assert preview["ok"] is False
    assert "minutes" in preview["items"][0]["refusal"]


def test_preview_series_refuses_a_date_beyond_the_scheduling_window(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])

    preview = _auto_preview(isolated_cwd, count=2, interval_hours=24 * 10)  # 2e post a plus de 10 j : hors fenetre

    assert preview["items"][0]["refusal"] is None
    assert preview["items"][1]["refusal"] is not None and "fenêtre" in preview["items"][1]["refusal"]
    assert preview["ok"] is False


def test_preview_series_refuses_a_series_that_violates_the_account_caps(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])

    preview = _auto_preview(isolated_cwd, count=2, interval_hours=1, settings=_series_settings(max_posts_per_day=1))

    assert preview["items"][0]["refusal"] is None
    assert "plafond de 1 publication" in preview["items"][1]["refusal"]
    assert preview["ok"] is False


# ---------- preview_series : mode manuel ----------


def test_preview_series_manual_follows_the_selection_order(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=50)
    _write_sidecar(isolated_cwd, "vid1", "b", score=99)

    preview = _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "a"), ("vid1", "b")])

    assert [it["clip_id"] for it in preview["items"]] == ["a", "b"]  # ordre de selection, pas le score
    assert preview["available"] == 2 and preview["requested"] == 2


def test_preview_series_manual_checking_one_part_adds_the_whole_series_in_order(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=50, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=50, part=2, parts_total=2)

    preview = _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "x-p2")])  # coche la partie 2

    assert [it["clip_id"] for it in preview["items"]] == ["x-p1", "x-p2"]  # les deux, dans l'ordre


def test_preview_series_manual_ignores_a_second_pick_of_the_same_series(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=50, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=50, part=2, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "y", score=60)

    preview = _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "x-p1"), ("vid1", "y"), ("vid1", "x-p2")])

    assert [it["clip_id"] for it in preview["items"]] == ["x-p1", "x-p2", "y"]


def test_preview_series_manual_refuses_an_unavailable_selection(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=50)
    publish.create_post("vid1", "a", "ma_chaine", account=_ACCOUNT, mode="immediate", now=SERIES_NOW)  # deja en file

    with pytest.raises(publish.PublishError, match="vid1/a"):
        _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "a")])


def test_preview_series_manual_can_select_a_validated_clip(isolated_cwd):
    """Clause TASK-16eeaccfaf09 : le mode manuel propose aussi les clips deja valides, marque « validé »."""
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=50)
    _approve_all(isolated_cwd, "vid1", ["a"])

    preview = _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "a")])

    assert [it["clip_id"] for it in preview["items"]] == ["a"]
    assert preview["ok"] is True


def test_preview_series_manual_can_select_a_clip_validated_for_another_account(isolated_cwd):
    """Contrairement au mode auto, le mode manuel n'a pas de restriction de compte sur les clips valides."""
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=50)
    _approve_all(isolated_cwd, "vid1", ["a"], account=_OTHER_ACCOUNT)

    preview = _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "a")])  # compte de la serie = _ACCOUNT

    assert [it["clip_id"] for it in preview["items"]] == ["a"]
    assert preview["ok"] is True


# ---------- create_series ----------


def test_create_series_creates_the_scheduled_entries_at_the_computed_dates(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])
    start = SERIES_NOW + timedelta(hours=1)

    created = publish.create_series(
        mode="auto", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=4,
        start_at=start, count=2, settings=_series_settings(), now=SERIES_NOW)

    assert [(e["clip_id"], e["status"], e["slot_at"], e["account"]) for e in created] == [
        ("a", "scheduled", start.isoformat(), _ACCOUNT), ("b", "scheduled", (start + timedelta(hours=4)).isoformat(), _ACCOUNT)]
    assert len(_read_state(isolated_cwd, "ma_chaine")) == 2


def test_create_series_is_all_or_nothing_when_a_date_is_refused(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])

    with pytest.raises(publish.PublishError):
        publish.create_series(
            mode="auto", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=1,
            start_at=SERIES_NOW + timedelta(hours=1), count=2, now=SERIES_NOW,
            settings=_series_settings(max_posts_per_day=1))

    # tout ou rien : les deux restent 'approved' (deja valides avant l'appel), rien n'est passe en 'scheduled'
    assert {e["clip_id"]: e["status"] for e in _read_state(isolated_cwd, "ma_chaine")} == {
        "a": "approved", "b": "approved"}


def test_create_series_manual_respects_the_chosen_order(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=50)
    _write_sidecar(isolated_cwd, "vid1", "b", score=99)
    start = SERIES_NOW + timedelta(hours=1)

    created = publish.create_series(
        mode="manual", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=2,
        start_at=start, selection=[("vid1", "a"), ("vid1", "b")], settings=_series_settings(), now=SERIES_NOW)

    assert [e["clip_id"] for e in created] == ["a", "b"]
    assert [e["slot_at"] for e in created] == [start.isoformat(), (start + timedelta(hours=2)).isoformat()]


# --------------------------------------------------------------------------
# TASK-fc561e4dc7e9 : coche « Parties ensemble » du formulaire série (together=False) --
# chaque partie devient une unité indépendante (auto et manuel), et auto_series_capacity
# donne le max du champ « Nombre de vidéos ».
# --------------------------------------------------------------------------


def test_available_series_clips_together_false_lists_each_part_as_its_own_unit(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)

    grouped = publish.available_series_clips("ma_chaine")
    apart = publish.available_series_clips("ma_chaine", together=False)

    assert [u["clip_ids"] for u in grouped] == [["x-p1", "x-p2"]]
    assert sorted(u["clip_ids"] for u in apart) == [["x-p1"], ["x-p2"]]


def test_available_series_clips_together_false_validates_a_part_without_its_sibling(isolated_cwd):
    """ON exige que toute la serie soit validee (une partie non validee exclut tout) ; OFF valide
    chaque partie seule, meme si sa soeur n'est pas encore approuvee."""
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    publish.approve("vid1", "x-p1", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)  # seule la partie 1 est validee

    assert publish.available_series_clips("ma_chaine") == []  # ON : la serie entiere est exclue

    apart = publish.available_series_clips("ma_chaine", together=False)
    by_clip = {tuple(u["clip_ids"]): u["validated"] for u in apart}
    assert by_clip[("x-p1",)] is True  # validee seule, sans attendre sa soeur
    assert by_clip[("x-p2",)] is False  # prete, jamais entree en file, pas encore validee


def test_auto_series_capacity_counts_validated_parts_for_the_account(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "y", score=70)
    publish.approve("vid1", "x-p1", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)  # x-p2 jamais valide
    publish.approve("vid1", "y", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)

    # ON : la serie x (partie 2 manquante) ne compte pas, seul y (serie entiere a lui seul) compte
    assert publish.auto_series_capacity("ma_chaine", _ACCOUNT) == 1
    # OFF : x-p1 compte seule en plus de y
    assert publish.auto_series_capacity("ma_chaine", _ACCOUNT, together=False) == 2


def test_auto_series_capacity_is_zero_without_any_validated_clip(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)  # pret, jamais approuve

    assert publish.auto_series_capacity("ma_chaine", _ACCOUNT) == 0


def test_preview_series_auto_together_false_picks_individual_parts_by_score(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "y", score=90)
    publish.approve("vid1", "x-p1", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)  # x-p2 jamais valide
    publish.approve("vid1", "y", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)

    preview = _auto_preview(isolated_cwd, count=2, together=False)

    # x-p1 (score 95) et y (score 90) : x-p2, jamais valide, n'est jamais pris (pas de repli silencieux)
    assert [it["clip_id"] for it in preview["items"]] == ["x-p1", "y"]
    assert preview["ok"] is True and preview["insufficient"] is False


def test_create_series_together_false_tags_entries_parts_together_false(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=95, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=95, part=2, parts_total=2)
    publish.approve("vid1", "x-p1", "ma_chaine", now=SERIES_NOW, account=_ACCOUNT)
    start = SERIES_NOW + timedelta(hours=1)

    created = publish.create_series(
        mode="auto", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=2,
        start_at=start, count=1, settings=_series_settings(), now=SERIES_NOW, together=False)

    assert [e["clip_id"] for e in created] == ["x-p1"]
    assert created[0]["parts_together"] is False


def test_create_series_default_tags_entries_parts_together_true(isolated_cwd):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _approve_all(isolated_cwd, "vid1", ["a"])
    start = SERIES_NOW + timedelta(hours=1)

    created = publish.create_series(
        mode="auto", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=2,
        start_at=start, count=1, settings=_series_settings(), now=SERIES_NOW)

    assert created[0]["parts_together"] is True


def test_create_post_outside_a_series_leaves_parts_together_unset(isolated_cwd):
    """Hors formulaire série (« Nouvelle publication »), le champ reste absent : comportement ON
    inchangé pour les entrées existantes et les publications a l'unite (criterion clause 2)."""
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)

    entry = publish.create_post(
        "vid1", "a", "ma_chaine", account=_ACCOUNT, mode="immediate", settings=_series_settings(), now=SERIES_NOW)

    assert "parts_together" not in entry


# --------------------------------------------------------------------------
# TASK-2456 (revue r-publication I4) : mark_in_progress = prise en main atomique sous verrou
# --------------------------------------------------------------------------


def test_taking_an_entry_returns_the_entry_read_under_the_lock(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    snapshot = _post(isolated_cwd)

    taken = publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW, expected=snapshot)

    assert taken["in_progress_since"] == NOW.isoformat() and taken["account"] == "compte1"
    assert _read_state(isolated_cwd, "ma_chaine")[0]["in_progress_since"] == NOW.isoformat()


@pytest.mark.parametrize("field, value", [
    ("account", "compte2"),
    ("slot_at", "2026-10-01T18:00:00+00:00"),
    ("publish_mode", "scheduled"),
    ("post_options", {"visibility": "friends"}),
])
def test_an_entry_changed_since_the_snapshot_is_not_taken(isolated_cwd, field, value):
    from clipper import publish

    _setup(isolated_cwd)
    snapshot = _post(isolated_cwd)
    stale = {**snapshot, field: value}  # l'instantane du worker ne correspond plus a l'entree relue

    with pytest.raises(publish.PublishError, match="modifiée"):
        publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW, expected=stale)

    assert not _read_state(isolated_cwd, "ma_chaine")[0].get("in_progress_since")


@pytest.mark.parametrize("status", ["approved", "published", "failed", "rejected"])
def test_only_a_scheduled_entry_is_taken(isolated_cwd, status):
    from clipper import publish

    _setup(isolated_cwd)
    snapshot = _post(isolated_cwd)
    path = isolated_cwd / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    entries[0]["status"] = status
    path.write_text(json.dumps(entries), encoding="utf-8")

    with pytest.raises(publish.PublishError, match=status):
        publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW, expected=snapshot)

    assert _read_state(isolated_cwd, "ma_chaine")[0]["status"] == status
    assert not _read_state(isolated_cwd, "ma_chaine")[0].get("in_progress_since")


def test_an_entry_already_in_progress_is_not_taken_twice(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    snapshot = _post(isolated_cwd)
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW, expected=snapshot)
    later = NOW + timedelta(minutes=5)

    with pytest.raises(publish.PublishError, match="en cours"):
        publish.mark_in_progress("vid1", "03", "ma_chaine", now=later, expected=snapshot)

    assert _read_state(isolated_cwd, "ma_chaine")[0]["in_progress_since"] == NOW.isoformat()


# --------------------------------------------------------------------------
# TASK-f66a4166f966 : garde-fous de statut et créneaux (revues r-publication
# et r-comptes, partie publish.py)
# --------------------------------------------------------------------------


def _seed_full_entry(cwd: Path, channel: str, status: str, **extra) -> dict:
    _write_config(cwd)
    if not (cwd / "presets" / f"{channel}.toml").exists():
        _write_preset(cwd, channel, _SLOT_PRESET)
    _write_sidecar(cwd, "vid1", "03")
    state = _state_file(cwd, channel)
    state.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "video_id": "vid1", "clip_id": "03", "series_id": None, "part": None,
        "status": status, "slot_at": "2026-10-05T09:00:00+00:00" if status in ("scheduled", "published") else None,
        "decided_at": "2026-09-28T00:00:00+00:00", "published_at": None, "error": None, "account": _ACCOUNT,
    }
    entry.update(extra)
    state.write_text(json.dumps([entry]), encoding="utf-8")
    return entry


# ---------- (1) approve refuse published / scheduled / en cours, sans rien ecrire ----------


@pytest.mark.parametrize("status", ["published", "scheduled"])
def test_approve_refuses_a_published_or_scheduled_clip_and_writes_nothing(isolated_cwd, status):
    from clipper import publish

    entry = _seed_full_entry(isolated_cwd, "ma_chaine", status)

    with pytest.raises(publish.PublishError, match=status):
        publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)

    assert _read_state(isolated_cwd, "ma_chaine") == [entry]  # rien n'est reecrit


def test_approve_refuses_a_clip_in_progress_and_writes_nothing(isolated_cwd):
    from clipper import publish

    entry = _seed_full_entry(isolated_cwd, "ma_chaine", "scheduled",
                             in_progress_since="2026-10-01T00:00:00+00:00")

    with pytest.raises(publish.PublishError, match="en cours"):
        publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT)

    assert _read_state(isolated_cwd, "ma_chaine") == [entry]


def test_approve_is_still_allowed_on_an_approved_or_a_failed_entry(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_failed("vid1", "01", "ma_chaine", "raison")

    entry = publish.approve("vid1", "01", "ma_chaine", account=_ACCOUNT)

    assert entry["status"] == "approved"  # ni erreur ni republication automatique


# ---------- (2) reject refuse published / en cours, y compris une partie sœur, avant toute ecriture ----------


def test_reject_refuses_a_published_clip_and_writes_nothing(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_published("vid1", "01", "ma_chaine", now=_MON)
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="published"):
        publish.reject("vid1", "01", "ma_chaine")

    assert _read_state(isolated_cwd, "ma_chaine") == before


def test_reject_refuses_a_clip_in_progress_and_writes_nothing(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_in_progress("vid1", "01", "ma_chaine", now=_MON)
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="en cours"):
        publish.reject("vid1", "01", "ma_chaine")

    assert _read_state(isolated_cwd, "ma_chaine") == before


def test_reject_refuses_a_series_part_when_a_sibling_is_already_published_and_writes_nothing(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)
    _write_sidecar(isolated_cwd, "vid1", "03-p1", part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "03-p2", part=2, parts_total=2)
    publish.approve("vid1", "03-p1", "ma_chaine", now=_MON, account=_ACCOUNT, schedule=_MON9)
    publish.mark_published("vid1", "03-p1", "ma_chaine", now=_MON)
    publish.approve("vid1", "03-p2", "ma_chaine", now=_MON, account=_ACCOUNT)
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="published"):
        publish.reject("vid1", "03-p2", "ma_chaine")  # refuse la partie 2 elle-meme : la sœur p1 est publiee

    assert _read_state(isolated_cwd, "ma_chaine") == before  # aucune ecriture, meme sur p2


# ---------- (3) unschedule refuse rejected et published-avec-tiktok_state, garde l'annulation manuelle ----------


def test_unschedule_refuses_a_rejected_entry(isolated_cwd):
    from clipper import publish

    entry = _seed_full_entry(isolated_cwd, "ma_chaine", "rejected")

    with pytest.raises(publish.PublishError, match="rejected"):
        publish.unschedule("vid1", "03", "ma_chaine")

    assert _read_state(isolated_cwd, "ma_chaine") == [entry]


def test_unschedule_refuses_a_published_entry_with_a_tiktok_state(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_published("vid1", "01", "ma_chaine", now=_MON, tiktok_state="published",
                           post_url="https://example.invalid/@ma_chaine/video/1", publish_at=_MON.isoformat(),
                           account=_ACCOUNT)
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="published"):
        publish.unschedule("vid1", "01", "ma_chaine")

    assert _read_state(isolated_cwd, "ma_chaine") == before


def test_unschedule_still_cancels_a_manually_declared_publication(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_published("vid1", "01", "ma_chaine", now=_MON)  # declaration manuelle : aucun tiktok_state

    entry = publish.unschedule("vid1", "01", "ma_chaine")

    assert entry["status"] == "approved" and entry["slot_at"] is None


# ---------- (4) la partie N s'approuve quand la partie N-1 est published ----------


def test_approve_part_n_accepts_when_previous_part_is_published(isolated_cwd):
    from clipper import publish

    _series(isolated_cwd)
    publish.approve("vid1", "03-p1", "ma_chaine", now=_MON, account=_ACCOUNT, schedule=_MON9)
    publish.mark_published("vid1", "03-p1", "ma_chaine", now=_MON)

    entry = publish.approve("vid1", "03-p2", "ma_chaine", account=_ACCOUNT)

    assert entry["part"] == 2


# ---------- (5) update_post passe une entree approved a scheduled ----------


def test_update_post_moves_an_approved_entry_to_scheduled(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine", _SLOT_PRESET)
    _write_sidecar(isolated_cwd, "vid1", "03")
    entry = publish.approve("vid1", "03", "ma_chaine", now=NOW, account=_ACCOUNT)
    assert entry["status"] == "approved"  # compte sans creneau

    updated = publish.update_post("vid1", "03", "ma_chaine", mode="scheduled",
                                  publish_at=NOW + timedelta(days=1), now=NOW)

    assert updated["status"] == "scheduled"  # sinon le worker (qui ne prend que 'scheduled') l'ignore pour toujours


# ---------- (6) creneaux pris = instants du compte, toutes chaines, dans _next_free_slot/move/postpone ----------


def test_approve_skips_an_instant_already_taken_by_another_style_of_the_same_account(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "style_a", _TWO_SLOTS)
    _write_preset(isolated_cwd, "style_b", _TWO_SLOTS)
    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid2", "02")
    schedule_one_slot = _sched(("mon", "09:00"))

    first = publish.approve("vid1", "01", "style_a", now=_MON, account=_ACCOUNT, schedule=schedule_one_slot)
    second = publish.approve("vid2", "02", "style_b", now=_MON, account=_ACCOUNT, schedule=schedule_one_slot)

    assert first["slot_at"] == datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("UTC")).isoformat()
    # meme compte, autre style : l'instant du 28 est deja pris, 02 saute a la semaine suivante
    assert second["slot_at"] == datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC")).isoformat()


def test_move_refuses_a_slot_taken_by_another_style_of_the_same_account(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "style_a", _TWO_SLOTS)
    _write_preset(isolated_cwd, "style_b", _TWO_SLOTS)
    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid2", "02")
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # monday, past 09:00
    taken_slot = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)
    publish.approve("vid1", "01", "style_a", now=now, account=_ACCOUNT, schedule=_MON9)  # -> 2026-10-05T09:00
    publish.approve("vid2", "02", "style_b", now=now, account=_ACCOUNT)  # approved, sans creneau

    with pytest.raises(publish.PublishError, match="vid2/02"):
        publish.move("vid2", "02", "style_b", taken_slot, schedule=_MON9)


def test_move_sees_the_same_instant_with_a_different_utc_offset_as_taken(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid1", "02")
    schedule = {"slots": [{"day": "mon", "time": "18:00"}], "timezone": "Europe/Paris"}
    paris_slot = datetime(2026, 10, 5, 18, 0, tzinfo=ZoneInfo("Europe/Paris"))  # +02:00 (CEST)
    # publication manuelle du formulaire, enregistree en UTC (le navigateur envoie toISOString())
    publish.create_post("vid1", "01", "ma_chaine", account=_ACCOUNT, mode="scheduled",
                        publish_at=paris_slot.astimezone(timezone.utc), now=_MON)
    publish.approve("vid1", "02", "ma_chaine", now=_MON, account=_ACCOUNT)  # approved, sans creneau

    with pytest.raises(publish.PublishError, match="vid1/02"):
        publish.move("vid1", "02", "ma_chaine", paris_slot, schedule=schedule)  # meme instant que 01, offset different


def test_postpone_sees_a_slot_taken_by_another_style_of_the_same_account(isolated_cwd):
    from clipper import publish

    _write_config(isolated_cwd)
    _write_preset(isolated_cwd, "style_a", _TWO_SLOTS)
    _write_preset(isolated_cwd, "style_b", _TWO_SLOTS)
    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid2", "02")
    publish.approve("vid1", "01", "style_a", now=_MON, account=_ACCOUNT, schedule=_TWO_SCHED)  # -> lun 09:00
    publish.approve("vid2", "02", "style_b", now=_MON, account=_ACCOUNT, schedule=_TWO_SCHED)  # -> lun 18:00

    moved = publish.postpone(
        "vid1", "01", "style_a", "plafond atteint",
        allowed=lambda slot: None, now=_MON, schedule=_TWO_SCHED)

    # le 18:00 de style_b (meme compte) est vu comme pris : le prochain creneau libre est le lundi suivant
    assert moved["slot_at"] == "2026-10-05T09:00:00+00:00"


# ---------- (7) preview_series refuse chaque item si les reglages seraient refuses a la creation ----------


def test_preview_series_refuses_every_item_when_the_default_visibility_would_be_refused_at_creation(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])

    preview = _auto_preview(isolated_cwd, count=2, settings=_series_settings(visibility="private"))

    assert preview["ok"] is False
    assert all(it["refusal"] is not None and "privée" in it["refusal"] for it in preview["items"])


# ---------- (8) create_series : annulation tout-ou-rien, journalisee, couvre toute Exception ----------


def test_create_series_lists_and_logs_the_entries_the_worker_already_took_and_covers_any_exception(
    isolated_cwd, monkeypatch, caplog,
):
    from clipper import publish

    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "a", score=90)
    _write_sidecar(isolated_cwd, "vid1", "b", score=80)
    _approve_all(isolated_cwd, "vid1", ["a", "b"])
    start = SERIES_NOW + timedelta(hours=1)

    real_create_post = publish.create_post

    def fake_create_post(video_id, clip_id, channel, **kwargs):
        if clip_id == "b":
            raise ValueError("sidecar JSON corrompu")  # pas un PublishError/ChannelError/ConfigError
        entry = real_create_post(video_id, clip_id, channel, **kwargs)
        # le worker prend la main sur "a" avant que la boucle n'atteigne "b"
        publish.mark_in_progress(video_id, clip_id, channel or publish.NO_CHANNEL, now=SERIES_NOW)
        return entry

    monkeypatch.setattr(publish, "create_post", fake_create_post)

    with pytest.raises(publish.PublishError, match="vid1/a"):
        publish.create_series(
            mode="auto", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=4,
            start_at=start, count=2, settings=_series_settings(), now=SERIES_NOW)

    assert "vid1/a" in caplog.text  # journalise (ADR-ad2e), pas avale en silence
    entries = {e["clip_id"]: e for e in _read_state(isolated_cwd, "ma_chaine")}
    assert entries["a"]["in_progress_since"] is not None  # l'entree prise par le worker reste intacte, pas annulee


# --------------------------------------------------------------------------
# Revue Fable (TASK-4c3d) : fable-comptes 3, 4, 6 ; fable-publication I2, I3, M4
# --------------------------------------------------------------------------


@pytest.mark.parametrize("form", [
    {"manual": True},
    {"publish_mode": "immediate"},
    {"post_options": {"visibility": "friends", "allow_comments": False}},
])
def test_approve_refuses_an_entry_carrying_form_settings_and_writes_nothing(isolated_cwd, form):
    """fable-comptes 3 / fable-publication I3 : « Approuver » reconstruisait l'entree et jetait en silence le mode,
    la visibilite... d'une publication du formulaire ; elle se reprend par « Réessayer » ou « Modifier »."""
    from clipper import publish

    entry = _seed_full_entry(isolated_cwd, "ma_chaine", "failed", error="Chrome introuvable",
                             slot_at="2026-10-02T18:00:00+00:00", service="tiktok", **form)

    with pytest.raises(publish.PublishError, match="Réessayer"):
        publish.approve("vid1", "03", "ma_chaine", account=_ACCOUNT, schedule=_MON9, now=_MON)

    assert _read_state(isolated_cwd, "ma_chaine") == [entry]


def test_approve_part_two_never_gets_a_slot_before_part_one(isolated_cwd):
    """fable-comptes 6 : la partie 2 prenait le prochain creneau libre depuis « maintenant », avant la partie 1."""
    from clipper import publish

    _series(isolated_cwd)
    part_one_at = "2026-10-08T12:00:00+02:00"  # jeudi : le lundi 5 etait pris par un clip annule depuis
    _state_file(isolated_cwd, "ma_chaine").parent.mkdir(parents=True, exist_ok=True)
    _state_file(isolated_cwd, "ma_chaine").write_text(json.dumps([{
        "video_id": "vid1", "clip_id": "03-p1", "series_id": "vid1:03", "part": 1, "status": "scheduled",
        "slot_at": part_one_at, "decided_at": None, "published_at": None, "error": None, "account": _ACCOUNT,
    }]), encoding="utf-8")
    schedule = {"slots": [{"day": "mon", "time": "18:30"}, {"day": "thu", "time": "12:00"}], "timezone": "Europe/Paris"}

    entry = publish.approve("vid1", "03-p2", "ma_chaine", now=datetime(2026, 10, 3, 10, tzinfo=timezone.utc),
                            account=_ACCOUNT, schedule=schedule)

    assert datetime.fromisoformat(entry["slot_at"]) > datetime.fromisoformat(part_one_at)
    assert datetime.fromisoformat(entry["slot_at"]) == datetime(2026, 10, 12, 18, 30, tzinfo=ZoneInfo("Europe/Paris"))


def test_a_deleted_style_still_counts_for_the_account_and_the_publications(isolated_cwd):
    """fable-comptes 4 : le fichier de publication d'un style supprime n'etait plus lu par personne : plafonds du
    compte sous-comptes, publications invisibles."""
    from clipper import publish

    entry = _seed_full_entry(isolated_cwd, "ma_chaine", "published", published_at="2026-10-01T10:00:00+00:00",
                             tiktok_state="published", tiktok_publish_at="2026-10-01T10:00:00+00:00")
    (isolated_cwd / "presets" / "ma_chaine.toml").unlink()  # le style est supprime, sa file reste

    assert publish.account_publish_times(_ACCOUNT) == [datetime(2026, 10, 1, 10, tzinfo=timezone.utc)]
    assert publish.all_entries() == [("ma_chaine", entry)]


def test_mark_published_by_hand_is_refused_while_the_worker_drives_the_entry(isolated_cwd):
    """fable-publication I2 : « Déclarer publié » pendant le pilotage effacait l'entree en cours ; le vrai post du
    worker n'etait plus enregistre (URL, etat, sidecar perdus)."""
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_in_progress("vid1", "01", "ma_chaine", now=_MON)
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="en cours"):
        publish.mark_published("vid1", "01", "ma_chaine", now=_MON)
    assert _read_state(isolated_cwd, "ma_chaine") == before

    # le worker, lui, enregistre son post (tiktok_state donne) pendant qu'il pilote
    entry = publish.mark_published("vid1", "01", "ma_chaine", now=_MON, tiktok_state="published",
                                   post_url="https://example.invalid/@x/video/1", publish_at=_MON.isoformat(),
                                   account=_ACCOUNT)
    assert entry["post_url"] == "https://example.invalid/@x/video/1" and entry["in_progress_since"] is None


@pytest.mark.parametrize("case", ["in_progress", "published", "rejected"])
def test_set_mode_refuses_an_entry_in_progress_published_or_rejected(isolated_cwd, case):
    """fable-publication M4 : le mode d'une entree pilotee, publiee ou refusee ne change plus."""
    publish = _tiktok_env(isolated_cwd, ("01",))
    if case == "in_progress":
        publish.mark_in_progress("vid1", "01", "ma_chaine", now=_MON)
    elif case == "published":
        publish.mark_published("vid1", "01", "ma_chaine", now=_MON)
    else:
        publish.reject("vid1", "01", "ma_chaine")
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError, match="mode"):
        publish.set_mode("vid1", "01", "ma_chaine", "immediate")

    assert _read_state(isolated_cwd, "ma_chaine") == before


# --------------------------------------------------------------------------
# TASK-7f582251f6c5 : clip refuse par TikTok a la verification de contenu
# --------------------------------------------------------------------------


def test_mark_refused_by_platform_records_reason_and_capture_without_halting_the_account(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))

    entry = publish.mark_refused_by_platform(
        "vid1", "01", "ma_chaine", "vérification de contenu : problème signalé par TikTok",
        capture="state/browser/ab12cd/captures/x.png")

    assert entry["status"] == "refused_by_platform"
    assert entry["error"] == "vérification de contenu : problème signalé par TikTok"
    assert entry["capture"] == "state/browser/ab12cd/captures/x.png"
    assert entry["halted"] is False and entry["slot_at"] is None and entry["refused_at"]
    assert publish.halted_account(_ACCOUNT) is None  # le compte continue
    assert "refused_by_platform" not in publish.UNFINISHED_STATUSES  # le clip est clos, rien à republier
    assert [e["clip_id"] for _, e in publish.refused_by_platform()] == ["01"]


def test_a_clip_refused_by_the_platform_is_never_republished_moved_edited_or_approved_again(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    publish.mark_refused_by_platform("vid1", "01", "ma_chaine", "problème")

    with pytest.raises(publish.PublishError, match="refused_by_platform"):
        publish.retry("vid1", "01", "ma_chaine")
    with pytest.raises(publish.PublishError, match="refused_by_platform"):
        publish.unschedule("vid1", "01", "ma_chaine")
    with pytest.raises(publish.PublishError, match="refused_by_platform"):
        publish.create_post("vid1", "01", "ma_chaine", account=_ACCOUNT, mode="immediate", now=NOW)
    assert publish.approval_refusal(publish.list_entries("ma_chaine")[0]) is not None
    assert publish.list_entries("ma_chaine")[0]["status"] == "refused_by_platform"


# --------------------------------------------------------------------------
# TASK-fa00f90a735a : coche « Heure par clip » du formulaire série (mode manuel) --
# ``clip_dates`` : une date par clip, validée clip par clip, jamais calculée.
# --------------------------------------------------------------------------


def _per_clip_preview(cwd, clip_dates, selection, **kwargs):
    kwargs.setdefault("mode", "manual")
    kwargs.setdefault("selection", selection)
    kwargs["clip_dates"] = clip_dates
    kwargs.setdefault("interval_hours", None)
    kwargs.setdefault("start_at", None)
    return _auto_preview(cwd, **kwargs)


def _two_clips(cwd):
    _series_env(cwd)
    _write_video_channel(cwd, "vid1", "ma_chaine")
    _write_sidecar(cwd, "vid1", "a", score=50)
    _write_sidecar(cwd, "vid1", "b", score=60)


def test_per_clip_dates_are_used_as_given_in_any_order(isolated_cwd):
    _two_clips(isolated_cwd)
    late = SERIES_NOW + timedelta(hours=9)
    early = SERIES_NOW + timedelta(hours=2)

    preview = _per_clip_preview(
        isolated_cwd, {("vid1", "a"): late, ("vid1", "b"): early}, [("vid1", "a"), ("vid1", "b")])

    assert [(it["clip_id"], it["publish_at"], it["refusal"]) for it in preview["items"]] == [
        ("a", late.isoformat(), None), ("b", early.isoformat(), None)]
    assert preview["ok"] is True


def test_per_clip_dates_refuse_each_clip_with_its_own_explicit_reason(isolated_cwd):
    _two_clips(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "c", score=40)
    settings = _series_settings()
    far = SERIES_NOW + timedelta(days=int(settings["schedule_max_days"]) + 5)

    preview = _per_clip_preview(
        isolated_cwd,
        {("vid1", "a"): SERIES_NOW + timedelta(minutes=1), ("vid1", "b"): far,
         ("vid1", "c"): SERIES_NOW + timedelta(hours=3)},
        [("vid1", "a"), ("vid1", "b"), ("vid1", "c")], settings=settings)

    refusals = {it["clip_id"]: it["refusal"] for it in preview["items"]}
    assert "avance minimale" in refusals["a"]
    assert "hors fenêtre" in refusals["b"]
    assert refusals["c"] is None
    assert preview["ok"] is False


def test_per_clip_dates_refuse_two_clips_at_the_same_time(isolated_cwd):
    _two_clips(isolated_cwd)
    same = SERIES_NOW + timedelta(hours=3)

    preview = _per_clip_preview(
        isolated_cwd, {("vid1", "a"): same, ("vid1", "b"): same}, [("vid1", "a"), ("vid1", "b")])

    assert preview["items"][0]["refusal"] is None
    assert "même heure" in preview["items"][1]["refusal"]


def test_per_clip_dates_refuse_a_slot_already_taken_on_the_account(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "z", score=10)
    taken = SERIES_NOW + timedelta(hours=3)
    publish.create_post("vid1", "z", "ma_chaine", account=_ACCOUNT, mode="scheduled", publish_at=taken,
                        now=SERIES_NOW, settings=_series_settings())

    preview = _per_clip_preview(
        isolated_cwd, {("vid1", "a"): taken, ("vid1", "b"): taken + timedelta(hours=1)},
        [("vid1", "a"), ("vid1", "b")])

    assert "déjà pris" in preview["items"][0]["refusal"]
    assert preview["items"][1]["refusal"] is None


def test_per_clip_dates_respect_the_daily_cap(isolated_cwd):
    _two_clips(isolated_cwd)
    base = SERIES_NOW + timedelta(hours=1)

    preview = _per_clip_preview(
        isolated_cwd, {("vid1", "a"): base, ("vid1", "b"): base + timedelta(minutes=10)},
        [("vid1", "a"), ("vid1", "b")], settings=_series_settings(max_posts_per_day=1))

    assert preview["items"][0]["refusal"] is None
    assert "plafond de 1 publication" in preview["items"][1]["refusal"]


def test_per_clip_dates_refuse_a_part_dated_before_the_previous_part(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=50, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=50, part=2, parts_total=2)

    preview = _per_clip_preview(
        isolated_cwd,
        {("vid1", "x-p1"): SERIES_NOW + timedelta(hours=5), ("vid1", "x-p2"): SERIES_NOW + timedelta(hours=2)},
        [("vid1", "x-p1")])

    assert [it["clip_id"] for it in preview["items"]] == ["x-p1", "x-p2"]
    assert preview["items"][0]["refusal"] is None
    assert "partie" in preview["items"][1]["refusal"] and "x-p1" in preview["items"][1]["refusal"]
    assert preview["ok"] is False


def test_per_clip_dates_accept_parts_in_order(isolated_cwd):
    _series_env(isolated_cwd)
    _write_video_channel(isolated_cwd, "vid1", "ma_chaine")
    _write_sidecar(isolated_cwd, "vid1", "x-p1", score=50, part=1, parts_total=2)
    _write_sidecar(isolated_cwd, "vid1", "x-p2", score=50, part=2, parts_total=2)

    preview = _per_clip_preview(
        isolated_cwd,
        {("vid1", "x-p1"): SERIES_NOW + timedelta(hours=2), ("vid1", "x-p2"): SERIES_NOW + timedelta(hours=5)},
        [("vid1", "x-p2")])

    assert preview["ok"] is True


def test_per_clip_dates_need_a_date_for_every_selected_clip(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)

    with pytest.raises(publish.PublishError, match="vid1/b"):
        _per_clip_preview(isolated_cwd, {("vid1", "a"): SERIES_NOW + timedelta(hours=2)},
                          [("vid1", "a"), ("vid1", "b")])


def test_per_clip_dates_are_manual_only_and_need_a_timezone(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)
    with pytest.raises(publish.PublishError, match="manuel"):
        _per_clip_preview(isolated_cwd, {("vid1", "a"): SERIES_NOW + timedelta(hours=2)}, None,
                          mode="auto", count=1)
    with pytest.raises(publish.PublishError, match="fuseau"):
        _per_clip_preview(isolated_cwd, {("vid1", "a"): datetime(2026, 10, 2, 9, 0)}, [("vid1", "a")])


def test_without_clip_dates_a_missing_start_is_still_refused(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)
    with pytest.raises(publish.PublishError, match="début"):
        _auto_preview(isolated_cwd, mode="manual", selection=[("vid1", "a")], start_at=None)


def test_create_series_per_clip_dates_schedules_each_clip_at_its_date(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)
    late = SERIES_NOW + timedelta(hours=9)
    early = SERIES_NOW + timedelta(hours=2)

    created = publish.create_series(
        mode="manual", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=None, start_at=None,
        selection=[("vid1", "a"), ("vid1", "b")], clip_dates={("vid1", "a"): late, ("vid1", "b"): early},
        settings=_series_settings(), now=SERIES_NOW)

    assert [(e["clip_id"], e["slot_at"]) for e in created] == [("a", late.isoformat()), ("b", early.isoformat())]


def test_create_series_per_clip_dates_is_all_or_nothing(isolated_cwd):
    from clipper import publish

    _two_clips(isolated_cwd)
    same = SERIES_NOW + timedelta(hours=3)

    with pytest.raises(publish.PublishError, match="vid1/b"):
        publish.create_series(
            mode="manual", style="ma_chaine", account=_ACCOUNT, service="tiktok", interval_hours=None,
            start_at=None, selection=[("vid1", "a"), ("vid1", "b")],
            clip_dates={("vid1", "a"): same, ("vid1", "b"): same}, settings=_series_settings(), now=SERIES_NOW)

    assert _read_state(isolated_cwd, "ma_chaine") == []


# ---- TASK-0c97 : sidecar réécrit avec réessais sous Windows (revue r-publish I2)


def _lock_replace(monkeypatch, suffix: str, times: int | None):
    """Simule un lecteur qui tient le fichier ouvert : ``os.replace`` vers ``*suffix`` leve PermissionError
    ``times`` fois (``None`` : toujours), puis laisse passer."""
    import os

    real, seen = os.replace, {"n": 0}

    def replace(src, dst, *args, **kwargs):
        if str(dst).endswith(suffix) and (times is None or seen["n"] < times):
            seen["n"] += 1
            raise PermissionError(13, "Acces refuse (simule)")
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    return seen


def test_mark_published_rewrites_a_sidecar_locked_twice_by_a_reader(isolated_cwd, monkeypatch):
    publish = _tiktok_env(isolated_cwd, ("01",))
    seen = _lock_replace(monkeypatch, "01.json", 2)

    publish.mark_published("vid1", "01", "ma_chaine", post_url="https://example.invalid/v/42", post_id="42",
                           tiktok_state="published", publish_at="2026-09-28T09:00:00+00:00", account=_ACCOUNT)

    assert seen["n"] == 2
    assert _read_sidecar(isolated_cwd, "vid1", "01")["tiktok_post"]["id"] == "42"
    assert not list((isolated_cwd / "output" / "vid1").glob("*.tmp"))


def test_mark_published_keeps_the_entry_published_when_the_sidecar_stays_locked(isolated_cwd, monkeypatch, caplog):
    import logging

    publish = _tiktok_env(isolated_cwd, ("01",))
    _lock_replace(monkeypatch, "01.json", None)

    with caplog.at_level(logging.ERROR):
        entry = publish.mark_published(
            "vid1", "01", "ma_chaine", post_url="https://example.invalid/v/42", post_id="42",
            tiktok_state="published", publish_at="2026-09-28T09:00:00+00:00", account=_ACCOUNT)

    stored = _read_state(isolated_cwd, "ma_chaine")[0]
    assert entry["status"] == stored["status"] == "published"
    assert stored["post_url"] == "https://example.invalid/v/42" and stored["in_progress_since"] is None
    assert any(r.levelname == "ERROR" and "https://example.invalid/v/42" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# TASK-5a7b750462c4 : post « supprimé de la plateforme » (Clipper n'efface rien, il l'enregistre)
# --------------------------------------------------------------------------


def _published_on_tiktok(publish, clip, state):
    at = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
    return publish.mark_published(
        "vid1", clip, "ma_chaine", now=at, post_url=f"https://example.invalid/@x/video/{clip}", post_id=f"7{clip}",
        tiktok_state=state, publish_at=at.isoformat(), account=_ACCOUNT)


@pytest.mark.parametrize("state", ["scheduled_on_tiktok", "published"])
def test_mark_removed_from_platform_records_state_reason_and_sidecar(isolated_cwd, state):
    publish = _tiktok_env(isolated_cwd, ("01", "02"))
    _published_on_tiktok(publish, "01", state)
    when = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)

    entry = publish.mark_removed_from_platform("vid1", "01", "ma_chaine", "mal cadré", now=when)

    assert entry["status"] == "removed_from_platform"
    assert entry["removed_at"] == when.isoformat() and entry["removed_reason"] == "mal cadré"
    assert entry["slot_at"] is None and entry["tiktok_state"] == state  # l'état d'avant reste lisible
    assert _read_state(isolated_cwd, "ma_chaine")[0]["status"] == "removed_from_platform"
    assert _read_sidecar(isolated_cwd, "vid1", "01")["removed_from_platform"] == {"at": when.isoformat(), "reason": "mal cadré"}
    assert [e["clip_id"] for _, e in publish.removed_from_platform()] == ["01"]
    assert "removed_from_platform" not in publish.UNFINISHED_STATUSES


def test_mark_removed_from_platform_reason_is_optional(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    _published_on_tiktok(publish, "01", "scheduled_on_tiktok")

    entry = publish.mark_removed_from_platform("vid1", "01", "ma_chaine")

    assert entry["removed_reason"] is None
    assert _read_sidecar(isolated_cwd, "vid1", "01")["removed_from_platform"]["reason"] is None


@pytest.mark.parametrize("case", ["approved", "scheduled", "in_progress", "failed", "rejected", "refused", "removed", "missing"])
def test_mark_removed_from_platform_refuses_anything_but_a_published_entry(isolated_cwd, case):
    publish = _tiktok_env(isolated_cwd, ("01",))
    clip = "01"
    if case == "approved":
        _write_sidecar(isolated_cwd, "vid1", "02")
        publish.approve("vid1", "02", "ma_chaine", now=_MON, account=_ACCOUNT)
        clip = "02"
    elif case == "in_progress":
        publish.mark_in_progress("vid1", "01", "ma_chaine", now=_MON)
    elif case == "failed":
        publish.mark_failed("vid1", "01", "ma_chaine", "boum", now=_MON)
    elif case == "rejected":
        publish.reject("vid1", "01", "ma_chaine")
    elif case == "refused":
        publish.mark_refused_by_platform("vid1", "01", "ma_chaine", "problème")
    elif case == "removed":
        _published_on_tiktok(publish, "01", "published")
        publish.mark_removed_from_platform("vid1", "01", "ma_chaine")
    elif case == "missing":
        clip = "99"
    before = _read_state(isolated_cwd, "ma_chaine")

    with pytest.raises(publish.PublishError):
        publish.mark_removed_from_platform("vid1", clip, "ma_chaine", "x")

    assert _read_state(isolated_cwd, "ma_chaine") == before


def test_removed_post_frees_slot_and_caps_and_is_never_republished(isolated_cwd):
    publish = _tiktok_env(isolated_cwd, ("01",))
    _published_on_tiktok(publish, "01", "scheduled_on_tiktok")
    assert publish.account_publish_times(_ACCOUNT) != []

    publish.mark_removed_from_platform("vid1", "01", "ma_chaine", "mal cadré")

    assert publish.account_publish_times(_ACCOUNT) == []  # plus un post programmé/publié pour les plafonds
    assert publish._account_taken_slots(_ACCOUNT, None, "presets", "config.toml") == set()
    for call in (lambda: publish.retry("vid1", "01", "ma_chaine"),
                 lambda: publish.unschedule("vid1", "01", "ma_chaine"),
                 lambda: publish.set_mode("vid1", "01", "ma_chaine", "immediate"),
                 lambda: publish.cancel_post("vid1", "01", "ma_chaine"),
                 lambda: publish.create_post("vid1", "01", "ma_chaine", account=_ACCOUNT, mode="immediate", now=NOW)):
        with pytest.raises(publish.PublishError, match="removed_from_platform|supprimé"):
            call()
    assert publish.approval_refusal(publish.list_entries("ma_chaine")[0]) is not None
    assert publish.list_entries("ma_chaine")[0]["status"] == "removed_from_platform"


# --------------------------------------------------------------------------
# TASK-4ca998e97789 (audit 10/10, publication-I2) : l'entree « en cours » porte le pid du worker qui la pilote ;
# un redemarrage ne passe en echec qu'une entree dont ce processus est mort
# --------------------------------------------------------------------------


def _dead_process():
    import subprocess
    import sys

    from clipper import worker

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    created = worker._process_created_at(proc.pid)
    proc.wait()
    return proc.pid, created


def test_mark_in_progress_writes_the_pid_and_creation_time_of_the_holder(isolated_cwd):
    import os

    from clipper import publish, worker

    _setup(isolated_cwd)
    _post(isolated_cwd)

    entry = publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)

    assert entry["in_progress_pid"] == os.getpid()
    assert entry["in_progress_pid_created_at"] == worker._process_created_at(os.getpid())
    stored = _read_state(isolated_cwd, "ma_chaine")[0]
    assert stored["in_progress_pid"] == os.getpid()


def test_fail_interrupted_leaves_an_entry_driven_by_a_live_process_alone(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)  # pilotee par CE processus, vivant

    assert publish.fail_interrupted("ma_chaine", now=NOW) == 0

    entry = _read_state(isolated_cwd, "ma_chaine")[0]
    assert entry["status"] == "scheduled" and entry["in_progress_since"] == NOW.isoformat()


def test_fail_interrupted_fails_an_entry_whose_holder_is_dead_or_unknown(isolated_cwd):
    import os

    from clipper import publish, worker

    _setup(isolated_cwd)
    _write_sidecar(isolated_cwd, "vid1", "01")
    _write_sidecar(isolated_cwd, "vid1", "02")
    _post(isolated_cwd, clip="03")
    _post(isolated_cwd, clip="01", now=NOW + timedelta(days=1))  # plafond : une publication par jour
    _post(isolated_cwd, clip="02", now=NOW + timedelta(days=2))
    dead_pid, dead_created = _dead_process()
    path = isolated_cwd / "state" / "publish" / "ma_chaine.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    for entry in entries:
        entry["in_progress_since"] = NOW.isoformat()
        if entry["clip_id"] == "03":  # processus mort
            entry.update(in_progress_pid=dead_pid, in_progress_pid_created_at=dead_created)
        elif entry["clip_id"] == "01":  # pid reattribue : notre pid, mais une autre heure de creation
            entry.update(in_progress_pid=os.getpid(), in_progress_pid_created_at=12345)
        # "02" : ancienne entree sans pid (worker d'avant la version) : interrompue
    path.write_text(json.dumps(entries), encoding="utf-8")

    assert publish.fail_interrupted("ma_chaine", now=NOW) == 3

    for entry in _read_state(isolated_cwd, "ma_chaine"):
        assert entry["status"] == "failed" and "interrompue" in entry["error"]
        assert entry["in_progress_since"] is None and entry["in_progress_pid"] is None
        assert entry["in_progress_pid_created_at"] is None
    assert worker.process_alive(os.getpid(), 12345) is False


def test_release_and_publish_clear_the_holder_pid(isolated_cwd):
    from clipper import publish

    _setup(isolated_cwd)
    _post(isolated_cwd)
    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    publish.release_in_progress("vid1", "03", "ma_chaine", "pause", state_dir=None)
    entry = _read_state(isolated_cwd, "ma_chaine")[0]
    assert entry["in_progress_pid"] is None and entry["in_progress_pid_created_at"] is None

    publish.mark_in_progress("vid1", "03", "ma_chaine", now=NOW)
    publish.mark_failed("vid1", "03", "ma_chaine", "captcha", halted=False)
    entry = _read_state(isolated_cwd, "ma_chaine")[0]
    assert entry["in_progress_pid"] is None and entry["in_progress_pid_created_at"] is None
