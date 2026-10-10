from __future__ import annotations

import subprocess
import sys
import threading
import tomllib
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_fake_section_module(monkeypatch, name, config_defaults=None):
    """Install a fake clipper.<name> module in sys.modules, mirroring a real
    pipeline-step module that declares CONFIG_DEFAULTS for its own section."""
    module = types.ModuleType(f"clipper.{name}")
    if config_defaults is not None:
        module.CONFIG_DEFAULTS = config_defaults
    monkeypatch.setitem(sys.modules, f"clipper.{name}", module)
    return module


def test_pyproject_declares_clipper_package_for_py311_with_test_extra():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    assert data["project"]["name"] == "clipper"
    assert data["project"]["requires-python"] == ">=3.11"
    assert "test" in data["project"]["optional-dependencies"]


def test_cli_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "clipper", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0


def test_config_defaults_to_review_mode_when_no_config_file(isolated_cwd):
    from clipper.config import load_config

    config = load_config()

    assert config.mode == "review"


def test_config_raises_when_explicit_path_is_missing(isolated_cwd):
    from clipper.config import ConfigError, load_config

    missing = isolated_cwd / "nope" / "absent.toml"

    with pytest.raises(ConfigError, match=r"absent\.toml"):
        load_config(missing)


def test_config_loads_mode_auto_from_toml_file(isolated_cwd):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text('mode = "auto"\n')

    config = load_config(isolated_cwd / "config.toml")

    assert config.mode == "auto"


def test_config_section_journal_has_the_documented_defaults(isolated_cwd):
    from clipper.config import load_config

    config = load_config()

    assert config.section("journal") == {
        "enabled": True, "dir": "logs", "retention_days": 2,
        "level": "INFO", "exclude_paths": ["/static/", "/media/"],
    }


def test_config_journal_section_accepts_toml_overrides(isolated_cwd):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text(
        '[journal]\nenabled = false\ndir = "mon_journal"\nretention_days = 7\n', encoding="utf-8"
    )

    section = load_config(isolated_cwd / "config.toml").section("journal")

    assert section["enabled"] is False
    assert section["dir"] == "mon_journal"
    assert section["retention_days"] == 7
    assert section["level"] == "INFO"  # cle non redefinie : defaut conserve


def test_config_rejects_unknown_key(isolated_cwd):
    from clipper.config import ConfigError, load_config

    (isolated_cwd / "config.toml").write_text('mode = "review"\nnot_a_real_key = 1\n')

    with pytest.raises(ConfigError):
        load_config(isolated_cwd / "config.toml")


def test_config_example_documents_the_defaults():
    from clipper.config import load_config

    config = load_config(REPO_ROOT / "config.example.toml")

    assert config.mode == "review"


def test_config_example_documents_section_convention():
    text = (REPO_ROOT / "config.example.toml").read_text()

    assert "CONFIG_DEFAULTS" in text


def test_pyproject_discovers_clipper_subpackages():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    find_cfg = data["tool"]["setuptools"]["packages"]["find"]
    include = find_cfg["include"]

    assert "clipper" in include
    assert "clipper.*" in include


def test_pyproject_declares_yt_dlp_and_anthropic_dependencies():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())

    deps = data["project"]["dependencies"]

    assert any(d.split(">")[0].split("=")[0].strip() == "yt-dlp" for d in deps)
    assert any(d.split(">")[0].split("=")[0].strip() == "anthropic" for d in deps)


def test_config_section_merges_module_defaults_with_toml_table(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch, "fake_step", config_defaults={"model": "small", "threshold": 0.5}
    )
    (isolated_cwd / "config.toml").write_text('[fake_step]\nmodel = "large"\n')

    config = load_config(isolated_cwd / "config.toml")

    assert config.section("fake_step") == {"model": "large", "threshold": 0.5}


def test_config_section_returns_only_defaults_when_table_absent(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch, "fake_step", config_defaults={"model": "small", "threshold": 0.5}
    )
    (isolated_cwd / "config.toml").write_text("")

    config = load_config(isolated_cwd / "config.toml")

    assert config.section("fake_step") == {"model": "small", "threshold": 0.5}


def test_config_section_passes_nested_tables_unchanged(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch, "fake_step", config_defaults={"model": "small", "extra": {}}
    )
    (isolated_cwd / "config.toml").write_text(
        '[fake_step]\nmodel = "large"\n[fake_step.extra]\nfoo = "bar"\n'
    )

    config = load_config(isolated_cwd / "config.toml")

    assert config.section("fake_step") == {"model": "large", "extra": {"foo": "bar"}}


def test_config_rejects_unknown_key_in_section(isolated_cwd, monkeypatch):
    from clipper.config import ConfigError, load_config

    _install_fake_section_module(monkeypatch, "fake_step", config_defaults={"model": "small"})
    (isolated_cwd / "config.toml").write_text('[fake_step]\nnot_a_real_key = 1\n')

    with pytest.raises(ConfigError):
        load_config(isolated_cwd / "config.toml")


def test_config_names_missing_dependency_when_module_import_fails(isolated_cwd, monkeypatch, tmp_path):
    import clipper
    from clipper.config import ConfigError, load_config

    module_dir = tmp_path / "broken_module_src"
    module_dir.mkdir()
    (module_dir / "broken_dep_step.py").write_text(
        "import totally_missing_dependency_xyz_abc\n"
        "CONFIG_DEFAULTS = {}\n"
    )
    monkeypatch.setattr(clipper, "__path__", clipper.__path__ + [str(module_dir)])
    (isolated_cwd / "config.toml").write_text('[broken_dep_step]\nfoo = 1\n')

    with pytest.raises(ConfigError) as exc_info:
        load_config(isolated_cwd / "config.toml")

    message = str(exc_info.value)
    assert "totally_missing_dependency_xyz_abc" in message
    assert "clipper.broken_dep_step" in message
    assert "pas de module" not in message


def test_config_rejects_section_without_matching_module(isolated_cwd):
    from clipper.config import ConfigError, load_config

    (isolated_cwd / "config.toml").write_text('[no_such_clipper_module_xyz]\nfoo = 1\n')

    with pytest.raises(ConfigError):
        load_config(isolated_cwd / "config.toml")


def test_config_rejects_section_module_without_config_defaults(isolated_cwd, monkeypatch):
    from clipper.config import ConfigError, load_config

    _install_fake_section_module(monkeypatch, "fake_step_no_defaults", config_defaults=None)
    (isolated_cwd / "config.toml").write_text('[fake_step_no_defaults]\nfoo = 1\n')

    with pytest.raises(ConfigError):
        load_config(isolated_cwd / "config.toml")


def test_config_flat_keys_still_work_alongside_sections(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(monkeypatch, "fake_step", config_defaults={"model": "small"})
    (isolated_cwd / "config.toml").write_text(
        'mode = "auto"\n[fake_step]\nmodel = "large"\n'
    )

    config = load_config(isolated_cwd / "config.toml")

    assert config.mode == "auto"
    assert config.section("fake_step") == {"model": "large"}


def test_config_with_base_merges_flat_keys_preset_wins(isolated_cwd):
    from clipper.config import load_config

    (isolated_cwd / "config.toml").write_text('mode = "review"\nworkspace_dir = "ws"\n')
    (isolated_cwd / "preset.toml").write_text('mode = "auto"\n')

    config = load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")

    assert config.mode == "auto"
    assert config.workspace_dir == Path("ws")


def test_config_with_base_merges_sections_key_by_key_preset_wins(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch, "fake_step", config_defaults={"model": "small", "threshold": 0.5}
    )
    (isolated_cwd / "config.toml").write_text('[fake_step]\nmodel = "large"\nthreshold = 0.9\n')
    (isolated_cwd / "preset.toml").write_text('[fake_step]\nmodel = "xlarge"\n')

    config = load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")

    assert config.section("fake_step") == {"model": "xlarge", "threshold": 0.9}


def test_config_with_base_replaces_subtable_wholesale(isolated_cwd, monkeypatch):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch,
        "fake_step",
        config_defaults={"usages": {"default": {"model": "small", "temperature": 0.1}}},
    )
    (isolated_cwd / "config.toml").write_text(
        '[fake_step.usages.default]\nmodel = "small"\ntemperature = "0.1"\n'
    )
    (isolated_cwd / "preset.toml").write_text(
        '[fake_step.usages.default]\nmodel = "large"\n'
    )

    config = load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")

    assert config.section("fake_step") == {"usages": {"default": {"model": "large"}}}


def test_config_with_base_rejects_unknown_flat_key_in_preset(isolated_cwd):
    from clipper.config import ConfigError, load_config

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    (isolated_cwd / "preset.toml").write_text('not_a_real_key = 1\n')

    with pytest.raises(ConfigError, match="not_a_real_key"):
        load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")


def test_config_with_base_rejects_unknown_section_in_preset(isolated_cwd):
    from clipper.config import ConfigError, load_config

    (isolated_cwd / "config.toml").write_text('mode = "review"\n')
    (isolated_cwd / "preset.toml").write_text('[no_such_clipper_module_xyz]\nfoo = 1\n')

    with pytest.raises(ConfigError, match="no_such_clipper_module_xyz"):
        load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")


def test_config_with_base_rejects_unknown_key_in_known_section_of_preset(isolated_cwd, monkeypatch):
    from clipper.config import ConfigError, load_config

    _install_fake_section_module(monkeypatch, "fake_step", config_defaults={"model": "small"})
    (isolated_cwd / "config.toml").write_text('[fake_step]\nmodel = "large"\n')
    (isolated_cwd / "preset.toml").write_text('[fake_step]\nnot_a_real_key = 1\n')

    with pytest.raises(ConfigError, match=r"fake_step.*not_a_real_key"):
        load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")


def test_config_with_base_full_preset_loads_identically_with_or_without_base(
    isolated_cwd, monkeypatch
):
    from clipper.config import load_config

    _install_fake_section_module(
        monkeypatch, "fake_step", config_defaults={"model": "small", "threshold": 0.5}
    )
    (isolated_cwd / "config.toml").write_text(
        'mode = "review"\nworkspace_dir = "ws1"\noutput_dir = "out1"\n'
        '[fake_step]\nmodel = "base_model"\nthreshold = 0.1\n'
    )
    (isolated_cwd / "preset.toml").write_text(
        'mode = "auto"\nworkspace_dir = "ws2"\noutput_dir = "out2"\n'
        '[fake_step]\nmodel = "preset_model"\nthreshold = 0.9\n'
    )

    with_base = load_config(isolated_cwd / "preset.toml", base=isolated_cwd / "config.toml")
    without_base = load_config(isolated_cwd / "preset.toml")

    assert with_base.mode == without_base.mode == "auto"
    assert with_base.workspace_dir == without_base.workspace_dir == Path("ws2")
    assert with_base.output_dir == without_base.output_dir == Path("out2")
    assert with_base.section("fake_step") == without_base.section("fake_step") == {
        "model": "preset_model",
        "threshold": 0.9,
    }


def test_write_config_serializes_toml_and_is_reread_by_load_config(isolated_cwd, monkeypatch):
    from clipper.config import load_config, write_config

    _install_fake_section_module(monkeypatch, "fake_step", config_defaults={"model": "small"})
    path = isolated_cwd / "config.toml"

    write_config(path, {"mode": "auto", "fake_step": {"model": "large"}})

    assert "mode" in path.read_text()
    config = load_config(path)
    assert config.mode == "auto"
    assert config.section("fake_step") == {"model": "large"}


def test_write_config_with_base_rereads_using_same_base(isolated_cwd, monkeypatch):
    from clipper.config import load_config, write_config

    _install_fake_section_module(monkeypatch, "fake_step", config_defaults={"model": "small"})
    base_path = isolated_cwd / "config.toml"
    base_path.write_text('[fake_step]\nmodel = "base_model"\n')
    preset_path = isolated_cwd / "preset.toml"

    write_config(preset_path, {"mode": "auto"}, base=base_path)

    config = load_config(preset_path, base=base_path)
    assert config.mode == "auto"
    assert config.section("fake_step") == {"model": "base_model"}


def test_write_config_replaces_file_atomically(isolated_cwd):
    from clipper.config import write_config

    path = isolated_cwd / "config.toml"
    path.write_text('mode = "review"\n')

    write_config(path, {"mode": "auto"})

    assert path.read_text().strip() == 'mode = "auto"'
    assert not path.with_suffix(".toml.tmp").exists()


def test_write_config_leaves_original_file_intact_on_config_error(isolated_cwd):
    from clipper.config import ConfigError, write_config

    path = isolated_cwd / "config.toml"
    original = 'mode = "review"\n'
    path.write_text(original)

    with pytest.raises(ConfigError):
        write_config(path, {"mode": "review", "not_a_real_key": 1})

    assert path.read_text() == original
    assert not path.with_suffix(".toml.tmp").exists()


def test_write_config_two_writers_do_not_share_a_tmp_file(tmp_path, monkeypatch):
    # web-I4 : le .tmp au nom fixe etait partage ; le premier os.replace emportait le fichier du second,
    # sa relecture echouait en « fichier de config introuvable ». Writer A est fige pendant sa relecture,
    # writer B (le fil principal) ecrit et remplace entierement, puis A reprend : A doit ecrire sans erreur.
    from clipper import config as config_mod
    from clipper.config import write_config

    path = tmp_path / "config.toml"
    path.write_text('mode = "review"\n')
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

    def writer_a():
        try:
            write_config(path, {"mode": "auto"})
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=writer_a, name="writer-a")
    writer.start()
    assert paused.wait(5)
    write_config(path, {"mode": "review"})
    release.set()
    writer.join(5)

    assert errors == []
    assert path.read_text().strip() == 'mode = "auto"'
    assert list(tmp_path.glob("*.tmp")) == []


def test_action_table_is_valid_and_refuses_unknown_keys(tmp_path):
    from clipper.config import ConfigError, load_config

    ok = tmp_path / "ok.toml"
    ok.write_text('[action]\nenabled = true\nframes_per_passage = 2\n', encoding="utf-8")
    assert load_config(ok).section("action")["frames_per_passage"] == 2
    assert load_config(ok).section("action")["window_seconds"] == 30

    bad = tmp_path / "bad.toml"
    bad.write_text('[action]\nunknown_key = 1\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown_key"):
        load_config(bad)


def test_config_example_documents_every_action_setting():
    from clipper.action import CONFIG_DEFAULTS

    for name in ("config.example.toml", "clipper/assets/config.example.toml"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "[action]" in text, name
        for key in CONFIG_DEFAULTS:
            assert f"# {key} = " in text, (name, key)


def test_defaults_documentation_lit_le_commentaire_au_dessus_de_chaque_cle():
    from clipper.config import _defaults_documentation

    docs = _defaults_documentation("moments")

    assert docs["selection"]["default"] == "single"
    assert docs["selection"]["comment"] == 'Qui note les moments : "single" (le proposeur seul) ou "jury".'
    assert "ADR-ff87" in docs["selection"]["details"]
