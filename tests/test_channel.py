from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


def test_config_defaults_has_exactly_the_spec_keys_and_defaults():
    from clipper.channel import CONFIG_DEFAULTS

    assert CONFIG_DEFAULTS == {
        "display_name": "",
        "source_url": "",
        "watch": False,
        "watch_interval_s": 1800,
        "watch_min_duration_s": 600,
        "mode": "",
        "timezone": "Europe/Paris",
        "logo": "",
    }


def test_list_channels_returns_only_presets_with_channel_table_sorted_by_name(isolated_cwd):
    from clipper.channel import list_channels

    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine_b.toml").write_text('[channel]\ndisplay_name = "B"\n')
    (presets_dir / "ma_chaine_a.toml").write_text('[channel]\ndisplay_name = "A"\n')
    (presets_dir / "cli_only.toml").write_text('mode = "auto"\n')

    assert list_channels(presets_dir) == ["ma_chaine_a", "ma_chaine_b"]


def test_list_channels_refuses_invalid_channel_name(isolated_cwd):
    from clipper.channel import ChannelError, list_channels

    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "Ma-Chaine-Invalide.toml").write_text('[channel]\ndisplay_name = "X"\n')

    with pytest.raises(ChannelError, match="Ma-Chaine-Invalide"):
        list_channels(presets_dir)


def test_load_channel_returns_merged_config_and_channel_dict(isolated_cwd):
    from clipper.channel import load_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\nworkspace_dir = "ws"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text(
        '[channel]\ndisplay_name = "Ma Chaine"\nsource_url = "https://example.invalid/x"\n'
    )

    config, channel = load_channel("ma_chaine")

    assert config.workspace_dir == Path("ws")
    assert channel["display_name"] == "Ma Chaine"
    assert channel["source_url"] == "https://example.invalid/x"


def test_load_channel_defaults_display_name_to_channel_name_when_absent(isolated_cwd):
    from clipper.channel import load_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\nwatch = true\n')

    _, channel = load_channel("ma_chaine")

    assert channel["display_name"] == "ma_chaine"


def test_load_channel_mode_absent_falls_back_to_global_mode(isolated_cwd):
    from clipper.channel import load_channel

    (isolated_cwd / "config.toml").write_text('mode = "auto"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\n')

    config, channel = load_channel("ma_chaine")

    assert config.mode == "auto"
    assert channel["mode"] == "auto"


def test_load_channel_mode_set_on_channel_overrides_global_mode(isolated_cwd):
    from clipper.channel import load_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\nmode = "auto"\n')

    _, channel = load_channel("ma_chaine")

    assert channel["mode"] == "auto"


def test_load_channel_rejects_invalid_mode(isolated_cwd):
    from clipper.config import ConfigError
    from clipper.channel import load_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\nmode = "bogus"\n')

    with pytest.raises(ConfigError, match="bogus"):
        load_channel("ma_chaine")


def test_save_channel_writes_toml_rereadable_by_load_channel(isolated_cwd):
    from clipper.channel import load_channel, save_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    (isolated_cwd / "presets").mkdir()

    save_channel("ma_chaine", {"channel": {"display_name": "Ma Chaine", "watch": True}})

    _, channel = load_channel("ma_chaine")
    assert channel["display_name"] == "Ma Chaine"
    assert channel["watch"] is True


def test_save_channel_leaves_existing_file_intact_on_config_error(isolated_cwd):
    from clipper.config import ConfigError
    from clipper.channel import save_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    original = '[channel]\ndisplay_name = "Avant"\n'
    (presets_dir / "ma_chaine.toml").write_text(original)

    with pytest.raises(ConfigError):
        save_channel("ma_chaine", {"channel": {"not_a_real_key": 1}})

    assert (presets_dir / "ma_chaine.toml").read_text() == original


def test_save_channel_serialises_concurrent_writers_under_the_preset_lock(isolated_cwd, monkeypatch):
    # web-I4 : writer A tient le verrou du preset pendant sa validation (figee). Writer B doit attendre
    # la liberation du verrou au lieu d'entrer dans la section critique ; le dernier a ecrire gagne,
    # sans erreur et sans .tmp restant.
    from clipper import config as config_mod
    from clipper.channel import save_channel

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\ndisplay_name = "Avant"\n')
    real_load = config_mod.load_config
    paused = threading.Event()
    release = threading.Event()

    def gated_load(target, *args, **kwargs):
        if threading.current_thread().name == "writer-a" and str(target).endswith(".tmp"):
            paused.set()
            release.wait(5)
        return real_load(target, *args, **kwargs)

    monkeypatch.setattr(config_mod, "load_config", gated_load)
    errors = []

    def write(display_name):
        try:
            save_channel("ma_chaine", {"channel": {"display_name": display_name}})
        except Exception as exc:
            errors.append(exc)

    writer_a = threading.Thread(target=write, args=("Premier",), name="writer-a")
    writer_b = threading.Thread(target=write, args=("Second",), name="writer-b")
    writer_a.start()
    assert paused.wait(5)
    writer_b.start()
    writer_b.join(0.3)
    blocked_while_a_holds_the_lock = writer_b.is_alive()
    release.set()
    writer_a.join(5)
    writer_b.join(5)

    assert blocked_while_a_holds_the_lock
    assert errors == []
    assert 'display_name = "Second"' in (presets_dir / "ma_chaine.toml").read_text()
    assert list(presets_dir.glob("*.tmp")) == []


def test_delete_channel_removes_the_preset_file(isolated_cwd):
    from clipper.channel import delete_channel

    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ma_chaine.toml").write_text('[channel]\ndisplay_name = "X"\n')

    delete_channel("ma_chaine", presets_dir=presets_dir)

    assert not (presets_dir / "ma_chaine.toml").exists()


def test_delete_channel_refuses_unknown_name(isolated_cwd):
    from clipper.channel import ChannelError, delete_channel

    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()

    with pytest.raises(ChannelError, match="ma_chaine"):
        delete_channel("ma_chaine", presets_dir=presets_dir)


def test_next_slots_returns_empty_list_when_no_slots():
    from clipper.channel import next_slots

    channel = {"slots": [], "timezone": "UTC"}
    after = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    assert next_slots(channel, after, 3) == []


def test_next_slots_returns_same_day_slot_when_still_ahead():
    from clipper.channel import next_slots

    channel = {"slots": [{"day": "mon", "time": "09:00"}], "timezone": "UTC"}
    after = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)  # monday, before 09:00

    result = next_slots(channel, after, 1)

    assert result == [datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("UTC"))]


def test_next_slots_cycles_weekly_past_the_slot_time(isolated_cwd):
    from clipper.channel import next_slots

    channel = {"slots": [{"day": "mon", "time": "09:00"}], "timezone": "UTC"}
    after = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # monday, after 09:00

    result = next_slots(channel, after, 3)

    assert result == [
        datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC")),
        datetime(2026, 10, 12, 9, 0, tzinfo=ZoneInfo("UTC")),
        datetime(2026, 10, 19, 9, 0, tzinfo=ZoneInfo("UTC")),
    ]


def test_next_slots_interleaves_multiple_slots_chronologically():
    from clipper.channel import next_slots

    channel = {
        "slots": [{"day": "mon", "time": "09:00"}, {"day": "wed", "time": "09:00"}],
        "timezone": "UTC",
    }
    after = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)  # monday, after 09:00

    result = next_slots(channel, after, 2)

    assert result == [
        datetime(2026, 9, 30, 9, 0, tzinfo=ZoneInfo("UTC")),
        datetime(2026, 10, 5, 9, 0, tzinfo=ZoneInfo("UTC")),
    ]


def test_next_slots_uses_the_channel_timezone():
    from clipper.channel import next_slots

    channel = {"slots": [{"day": "mon", "time": "09:00"}], "timezone": "Europe/Paris"}
    after = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)

    result = next_slots(channel, after, 1)

    assert result == [datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))]
    assert result[0].utcoffset().total_seconds() == 2 * 3600


def test_list_channels_raises_channel_error_naming_a_malformed_preset(isolated_cwd):
    from clipper.channel import ChannelError, list_channels

    presets_dir = isolated_cwd / "presets"
    presets_dir.mkdir()
    (presets_dir / "ok.toml").write_text("[channel]\n")
    (presets_dir / "casse.toml").write_text("[channel\nmode = ")

    with pytest.raises(ChannelError, match="casse.toml"):
        list_channels(presets_dir)


# --- TASK-31d5 : ni compte ni creneaux dans un style (SPEC-6076 R2) ---

_LEGACY = (
    '[channel]\ndisplay_name = "Ma chaine"\ntimezone = "UTC"\ntiktok_account = "ab12cd"\n'
    '[[channel.slots]]\nday = "mon"\ntime = "18:30"\n[[channel.slots]]\nday = "fri"\ntime = "09:00"\n'
)


def _legacy_env(isolated_cwd, preset=_LEGACY, accounts=None):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    (isolated_cwd / "state").mkdir(exist_ok=True)
    rows = [{"id": "ab12cd", "label": "Compte"}] if accounts is None else accounts
    (isolated_cwd / "state" / "accounts.json").write_text(json.dumps({"accounts": rows}), encoding="utf-8")
    presets = isolated_cwd / "presets"
    presets.mkdir(exist_ok=True)
    path = presets / "ma_chaine.toml"
    path.write_text(preset, encoding="utf-8")
    return load_config("config.toml"), path


def _account(isolated_cwd, account_id="ab12cd"):
    rows = json.loads((isolated_cwd / "state" / "accounts.json").read_text(encoding="utf-8"))["accounts"]
    return next(a for a in rows if a["id"] == account_id)


def _channel_keys(path):
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))["channel"]


@pytest.fixture(autouse=True)
def _forget_legacy_warnings():
    from clipper import channel

    channel._warned_legacy.clear()


def test_a_style_has_no_account_and_no_slots_keys():
    from clipper.channel import CONFIG_DEFAULTS

    assert "tiktok_account" not in CONFIG_DEFAULTS and "slots" not in CONFIG_DEFAULTS


def test_a_legacy_preset_still_loads_never_uses_its_account_and_warns_once(isolated_cwd, caplog):
    from clipper.channel import load_channel

    _legacy_env(isolated_cwd)

    with caplog.at_level(logging.WARNING, logger="clipper.channel"):
        _config, channel = load_channel("ma_chaine")
        load_channel("ma_chaine")

    assert "slots" not in channel and "tiktok_account" not in channel
    warnings = [r.getMessage() for r in caplog.records if "ma_chaine" in r.getMessage()]
    assert len(warnings) == 1 and "slots" in warnings[0] and "tiktok_account" in warnings[0]


def test_migration_copies_the_slots_on_the_account_and_removes_both_keys(isolated_cwd, caplog):
    from clipper.channel import load_channel, migrate_legacy_presets

    config, path = _legacy_env(isolated_cwd)

    with caplog.at_level(logging.INFO, logger="clipper.channel"):
        migrated = migrate_legacy_presets(config)

    assert migrated == ["ma_chaine"]
    account = _account(isolated_cwd)
    assert account["slots"] == [{"day": "mon", "time": "18:30"}, {"day": "fri", "time": "09:00"}]
    assert account["timezone"] == "UTC"
    keys = _channel_keys(path)
    assert "slots" not in keys and "tiktok_account" not in keys
    assert keys["display_name"] == "Ma chaine"  # le reste du preset est conserve
    assert "migré" in caplog.text and "ab12cd" in caplog.text
    caplog.clear()
    load_channel("ma_chaine")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]  # plus d'avertissement ensuite


def test_migration_never_overwrites_the_slots_an_account_already_has(isolated_cwd):
    from clipper.channel import migrate_legacy_presets

    existing = [{"day": "wed", "time": "12:00"}]
    config, path = _legacy_env(isolated_cwd, accounts=[{"id": "ab12cd", "label": "C", "slots": existing, "timezone": "Europe/Paris"}])

    assert migrate_legacy_presets(config) == ["ma_chaine"]

    account = _account(isolated_cwd)
    assert account["slots"] == existing and account["timezone"] == "Europe/Paris"
    assert "slots" not in _channel_keys(path) and "tiktok_account" not in _channel_keys(path)


def test_migration_leaves_a_preset_whose_account_is_unknown_untouched_and_says_so(isolated_cwd, caplog):
    from clipper.channel import migrate_legacy_presets

    config, path = _legacy_env(isolated_cwd, accounts=[])
    before = path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="clipper.channel"):
        assert migrate_legacy_presets(config) == []

    assert path.read_text(encoding="utf-8") == before
    assert "ma_chaine" in caplog.text and "ab12cd" in caplog.text


def test_migration_leaves_slots_without_an_account_untouched_and_says_so(isolated_cwd, caplog):
    from clipper.channel import migrate_legacy_presets

    config, path = _legacy_env(isolated_cwd, preset='[channel]\n[[channel.slots]]\nday = "mon"\ntime = "18:30"\n')
    before = path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="clipper.channel"):
        assert migrate_legacy_presets(config) == []

    assert path.read_text(encoding="utf-8") == before and "sans compte" in caplog.text


def test_migration_refuses_to_copy_an_invalid_slot_and_keeps_the_preset(isolated_cwd, caplog):
    from clipper.channel import migrate_legacy_presets

    config, path = _legacy_env(
        isolated_cwd, preset='[channel]\ntiktok_account = "ab12cd"\n[[channel.slots]]\nday = "someday"\ntime = "25:99"\n')

    with caplog.at_level(logging.WARNING, logger="clipper.channel"):
        assert migrate_legacy_presets(config) == []

    assert "someday" in caplog.text and "slots" in _channel_keys(path)
    assert "slots" not in _account(isolated_cwd)


def test_migration_removes_an_account_key_without_slots_and_touches_no_account(isolated_cwd):
    from clipper.channel import migrate_legacy_presets

    config, path = _legacy_env(isolated_cwd, preset='[channel]\ntiktok_account = "ab12cd"\n')

    assert migrate_legacy_presets(config) == ["ma_chaine"]

    assert "tiktok_account" not in _channel_keys(path)
    assert "slots" not in _account(isolated_cwd)


def test_migration_reports_the_accounts_tiktok_account_onto_entries_without_one(isolated_cwd):
    from clipper.channel import migrate_legacy_presets

    config, _path = _legacy_env(isolated_cwd, preset='[channel]\ntiktok_account = "ab12cd"\n')
    publish_dir = isolated_cwd / "state" / "publish"
    publish_dir.mkdir(parents=True)
    entries = [
        {"video_id": "vid1", "clip_id": "01", "series_id": None, "part": None, "status": "scheduled",
         "slot_at": "2026-01-01T18:00:00+00:00", "decided_at": None, "published_at": None, "error": None},
        {"video_id": "vid1", "clip_id": "02", "series_id": None, "part": None, "status": "scheduled",
         "slot_at": "2026-01-02T18:00:00+00:00", "decided_at": None, "published_at": None, "error": None,
         "account": "compte2"},
    ]
    (publish_dir / "ma_chaine.json").write_text(json.dumps(entries), encoding="utf-8")

    assert migrate_legacy_presets(config) == ["ma_chaine"]

    stored = json.loads((publish_dir / "ma_chaine.json").read_text(encoding="utf-8"))
    assert stored[0]["account"] == "ab12cd"    # sans compte : reporte avant de retirer tiktok_account
    assert stored[1]["account"] == "compte2"   # avait deja un compte : jamais ecrase


def test_migration_does_nothing_on_a_preset_without_legacy_keys(isolated_cwd):
    from clipper.channel import migrate_legacy_presets

    config, path = _legacy_env(isolated_cwd, preset='[channel]\ndisplay_name = "X"\n')
    before = path.read_text(encoding="utf-8")

    assert migrate_legacy_presets(config) == []
    assert path.read_text(encoding="utf-8") == before


def test_saving_a_style_drops_the_legacy_keys(isolated_cwd, caplog):
    from clipper.channel import load_channel, save_channel

    _legacy_env(isolated_cwd)

    with caplog.at_level(logging.INFO, logger="clipper.channel"):
        save_channel("ma_chaine", {"channel": {"display_name": "Nouveau", "tiktok_account": "ab12cd",
                                               "slots": [{"day": "mon", "time": "18:30"}]}})

    keys = _channel_keys(isolated_cwd / "presets" / "ma_chaine.toml")
    assert keys == {"display_name": "Nouveau"}
    assert "retiré" in caplog.text
    load_channel("ma_chaine")
