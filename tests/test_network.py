"""TASK-30cc : clipper.network, pays de l'IP publique. Service de géolocalisation simulé, aucun réseau."""

from __future__ import annotations

import pytest

from clipper import network
from clipper.config import Config


@pytest.fixture(autouse=True)
def _reset():
    network.reset()
    yield
    network.use_fetcher(None)
    network.use_clock(None)
    network.reset()


def _fr(url=None):
    return {"ip": "1.2.3.4", "city": "Paris", "country": "FR", "org": "AS51207 Free Mobile SAS"}


def _uk(url=None):
    return {"ip": "5.6.7.8", "city": "London", "country": "GB", "org": "AS1 BT"}


def test_defaults():
    d = network.CONFIG_DEFAULTS
    assert d["expected_country"] == "FR"
    assert d["geo_url"].startswith("https://")
    assert d["cache_s"] == 60
    assert d["block_browser"] is True


def test_status_ok_when_country_expected():
    network.use_fetcher(_fr)
    s = network.status()
    assert s["ok"] is True
    assert (s["ip"], s["country"], s["city"], s["isp"]) == ("1.2.3.4", "FR", "Paris", "AS51207 Free Mobile SAS")
    assert s["country_name"] == "France"
    assert s["expected_country"] == "FR"
    assert s["checked_at"]


def test_status_not_ok_in_other_country():
    network.use_fetcher(_uk)
    s = network.status()
    assert s["ok"] is False
    assert s["country"] == "GB"
    assert s["country_name"] == "Royaume-Uni"


def test_expected_country_from_config_is_case_insensitive():
    network.use_fetcher(_uk)
    assert network.status()["ok"] is False  # défaut FR
    config = Config(mode="review", workspace_dir="w", output_dir="o", _sections={"network": {"expected_country": "gb"}})
    assert network.status(config)["ok"] is True


def test_unreachable_service_is_unknown_never_ok():
    def boom(url):
        raise OSError("réseau coupé")

    network.use_fetcher(boom)
    s = network.status()
    assert s["ok"] is None
    assert s["country"] is None
    assert "réseau coupé" in s["error"]


def test_answer_without_country_is_unknown():
    network.use_fetcher(lambda url: {"ip": "1.1.1.1"})
    s = network.status()
    assert s["ok"] is None
    assert "country" in s["error"]


def test_cache_respected_then_expires():
    calls = []
    now = [1000.0]

    def fetch(url):
        calls.append(url)
        return _fr()

    network.use_fetcher(fetch)
    network.use_clock(lambda: now[0])
    network.status()
    now[0] += 59
    network.status()
    assert len(calls) == 1
    now[0] += 2
    network.status()
    assert len(calls) == 2


def test_unknown_not_cached():
    calls = []

    def fetch(url):
        calls.append(1)
        raise OSError("x")

    network.use_fetcher(fetch)
    network.status()
    network.status()
    assert len(calls) == 2


def test_cache_does_not_hide_expected_country_change():
    network.use_fetcher(_uk)
    assert network.status()["ok"] is False
    config = Config(mode="review", workspace_dir="w", output_dir="o", _sections={"network": {"expected_country": "GB"}})
    assert network.status(config)["ok"] is True


def test_require_passes_in_expected_country():
    network.use_fetcher(_fr)
    network.require_expected_country()


def test_require_refuses_other_country():
    network.use_fetcher(_uk)
    with pytest.raises(network.NetworkError) as exc:
        network.require_expected_country()
    assert str(exc.value) == (
        "IP en Royaume-Uni (attendu France) : passe sur le partage de connexion du téléphone"
    )


def test_require_refuses_unknown():
    def boom(url):
        raise OSError("down")

    network.use_fetcher(boom)
    with pytest.raises(network.NetworkError) as exc:
        network.require_expected_country()
    assert "pays de l'IP inconnu" in str(exc.value)
    assert "partage de connexion du téléphone" in str(exc.value)


def test_require_skipped_when_block_disabled():
    network.use_fetcher(lambda url: pytest.fail("aucun appel quand block_browser = false"))
    network.require_expected_country(Config(mode="review", workspace_dir="w", output_dir="o", _sections={"network": {"block_browser": False}}))


def test_require_uses_config_block_and_expected():
    network.use_fetcher(_uk)
    config = Config(mode="review", workspace_dir="w", output_dir="o", _sections={"network": {"block_browser": False}})
    network.require_expected_country(config)  # bloc désactivé : aucun refus
    config = Config(mode="review", workspace_dir="w", output_dir="o", _sections={"network": {"expected_country": "GB"}})
    network.require_expected_country(config)


@pytest.mark.parametrize("path", ["config.example.toml", "clipper/assets/config.example.toml"])
def test_config_example_documents_the_network_table(path):
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / path).read_text(encoding="utf-8")
    assert "# [network]" in text
    for key in network.CONFIG_DEFAULTS:
        assert f"# {key} = " in text, key


def test_unknown_country_is_a_distinct_error_from_a_wrong_country():
    """I2 (revue nuit) : « inconnu » (service injoignable) se distingue de « différent » (arrêt sûr)."""
    def boom(url):
        raise OSError("down")

    network.use_fetcher(boom)
    with pytest.raises(network.NetworkUnknown):
        network.require_expected_country()
    network.use_fetcher(_uk)
    with pytest.raises(network.NetworkError) as exc:
        network.require_expected_country()
    assert not isinstance(exc.value, network.NetworkUnknown)


# ------------------------------------------------------------ TASK-ef6e6c31363f : pays attendu par compte


def test_require_with_account_country_passes_uk_ip_even_if_config_says_fr():
    network.use_fetcher(_uk)
    network.require_expected_country(expected="gb")  # défaut [network] = FR, le compte prime


def test_require_with_account_country_refuses_fr_ip_naming_account_and_both_countries():
    network.use_fetcher(_fr)
    with pytest.raises(network.NetworkError) as exc:
        network.require_expected_country(expected="GB", account="ClipManiaq3")
    assert str(exc.value) == (
        "compte ClipManiaq3 : IP en France (attendu Royaume-Uni) : passe sur le partage de connexion du téléphone"
    )


def test_require_with_account_country_unknown_ip_is_network_unknown():
    def boom(url):
        raise OSError("down")

    network.use_fetcher(boom)
    with pytest.raises(network.NetworkUnknown) as exc:
        network.require_expected_country(expected="GB", account="ClipManiaq3")
    assert "compte ClipManiaq3" in str(exc.value)
    assert "attendu Royaume-Uni" in str(exc.value)


def test_require_without_account_country_keeps_config_value():
    network.use_fetcher(_uk)
    with pytest.raises(network.NetworkError):
        network.require_expected_country(expected=None)
    network.reset()
    network.use_fetcher(_fr)
    network.require_expected_country(expected="")
