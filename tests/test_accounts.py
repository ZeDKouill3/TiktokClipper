"""Tests de clipper.accounts et des routes /api/accounts* (SPEC-6fa4, R1-R7).

Aucun test ne touche le vrai coffre : un backend keyring en memoire est
branche (accounts.use_backend). Aucun reseau.
"""

from __future__ import annotations

import json
import logging
import re
import string
import subprocess
import sys
import time
from datetime import datetime as _datetime
from datetime import timezone as _timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from keyring.backends import fail as keyring_fail
from keyring.backends import null as keyring_null
from keyring.errors import PasswordDeleteError

from clipper import accounts
from clipper.config import Config
from clipper.web import create_app

STATIC = Path(__file__).resolve().parent.parent / "clipper" / "web" / "static"
PWD = "Sup3r-Secret-Exemple!"
LOCAL = "http://127.0.0.1:8000"


class MemoryKeyring:
    """Duck-typing volontaire : un sous-classe de KeyringBackend s'enregistrerait dans keyring
    (ChainerBackend) et polluerait le vrai coffre vu par les autres tests du processus."""

    priority = 1

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def get_password(self, service, username):
        return self.store.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self.store:
            raise PasswordDeleteError("absent")
        del self.store[(service, username)]


class PlaintextKeyring(MemoryKeyring):
    pass


class LeakyKeyring(MemoryKeyring):
    """Backend dont les erreurs recopient le mot de passe : il ne doit jamais ressortir."""

    def set_password(self, service, username, password):
        raise RuntimeError(f"echec avec {password}")


@pytest.fixture
def vault():
    backend = MemoryKeyring()
    accounts.use_backend(backend)
    yield backend
    accounts.use_backend(None)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return Config(mode="review", workspace_dir=tmp_path / "workspace", output_dir=tmp_path / "output")


def local_client(config) -> TestClient:
    return TestClient(create_app(config=config), base_url=LOCAL, client=("127.0.0.1", 50000))


NEW = {"label": "Compte exemple", "platform": "TikTok", "username": "exemple@example.com",
       "notes": "ma_chaine", "password": PWD}


# --------------------------------------------------------------------------
# R1 / R2 : stockage, coffre
# --------------------------------------------------------------------------


def test_add_stores_password_in_the_vault_and_never_in_the_file(config, vault):
    account = accounts.add_account(config, NEW)

    assert vault.store == {("clipper-accounts", account["id"]): PWD}
    raw = Path("state/accounts.json").read_text(encoding="utf-8")
    assert PWD not in raw
    on_disk = json.loads(raw)["accounts"][0]
    assert on_disk["label"] == "Compte exemple" and on_disk["has_password"] is True
    assert "password" not in on_disk
    assert "password" not in account and account["has_password"] is True


def test_accounts_section_is_a_valid_config_table():
    assert accounts.CONFIG_DEFAULTS["password_length"] == 20
    assert "state_file" in accounts.CONFIG_DEFAULTS


def test_write_is_atomic_and_leaves_no_temp_file(config, vault):
    accounts.add_account(config, NEW)
    accounts.add_account(config, {"label": "Autre"})
    # accounts.json.lock : le verrou inter-processus (ADR-35b7), jamais un fichier temporaire de l'ecriture.
    assert {p.name for p in Path("state").iterdir()} == {"accounts.json", "accounts.json.lock"}
    assert len(accounts.list_accounts(config)) == 2


def test_write_failure_keeps_the_previous_file_and_cleans_up(config, vault, monkeypatch):
    accounts.add_account(config, {"label": "Premier"})
    before = Path("state/accounts.json").read_bytes()

    def boom(src, dst):
        raise OSError("disque plein")

    real_replace = accounts.os.replace
    monkeypatch.setattr(accounts.os, "replace", boom)
    with pytest.raises(accounts.AccountsError):
        accounts.add_account(config, NEW)
    monkeypatch.setattr(accounts.os, "replace", real_replace)

    assert Path("state/accounts.json").read_bytes() == before
    assert {p.name for p in Path("state").iterdir()} == {"accounts.json", "accounts.json.lock"}
    assert vault.store == {}  # pas de secret orphelin dans le coffre


def test_update_changes_fields_and_password(config, vault):
    account = accounts.add_account(config, NEW)
    updated = accounts.update_account(config, account["id"], {"label": "Renommé", "password": "Autre-mdp-12345"})

    assert updated["label"] == "Renommé" and updated["username"] == "exemple@example.com"
    assert vault.store[("clipper-accounts", account["id"])] == "Autre-mdp-12345"
    assert "Autre-mdp-12345" not in Path("state/accounts.json").read_text(encoding="utf-8")


def test_update_without_password_keeps_it_and_empty_password_removes_it(config, vault):
    account = accounts.add_account(config, NEW)
    accounts.update_account(config, account["id"], {"notes": "x"})
    assert vault.store[("clipper-accounts", account["id"])] == PWD

    cleared = accounts.update_account(config, account["id"], {"password": ""})
    assert cleared["has_password"] is False and vault.store == {}


def test_delete_removes_the_account_and_its_vault_entry(config, vault):
    account = accounts.add_account(config, NEW)
    keep = accounts.add_account(config, {"label": "Reste"})

    accounts.delete_account(config, account["id"])

    assert vault.store == {}
    assert [a["id"] for a in accounts.list_accounts(config)] == [keep["id"]]


def test_unknown_account_is_an_explicit_error(config, vault):
    with pytest.raises(accounts.AccountNotFound, match="introuvable"):
        accounts.update_account(config, "nope", {"label": "x"})
    with pytest.raises(accounts.AccountNotFound):
        accounts.delete_account(config, "nope")
    with pytest.raises(accounts.AccountNotFound):
        accounts.get_password(config, "nope")


@pytest.mark.parametrize("backend", [keyring_fail.Keyring(), keyring_null.Keyring(), PlaintextKeyring()],
                         ids=["fail", "null", "plaintext"])
def test_unsafe_vault_refuses_writing_a_password_without_fallback(config, backend):
    accounts.use_backend(backend)
    try:
        with pytest.raises(accounts.VaultUnavailable, match="coffre"):
            accounts.add_account(config, NEW)
    finally:
        accounts.use_backend(None)
    assert not Path("state/accounts.json").exists()  # rien d'ecrit nulle part
    assert getattr(backend, "store", {}) == {}


def test_unsafe_vault_still_allows_accounts_without_password(config):
    accounts.use_backend(keyring_fail.Keyring())
    try:
        account = accounts.add_account(config, {"label": "Sans mot de passe"})
        with pytest.raises(accounts.VaultUnavailable):
            accounts.update_account(config, account["id"], {"password": PWD})
    finally:
        accounts.use_backend(None)


def test_default_backend_is_the_os_one_and_fail_is_refused(config):
    # Sans coffre sur Linux (CI, cloud) : keyring.get_keyring() est le backend fail, refus explicite.
    accounts.use_backend(None)
    import keyring

    if not isinstance(keyring.get_keyring(), keyring_fail.Keyring):
        pytest.skip("un vrai coffre est present : on n'y touche pas dans les tests")
    with pytest.raises(accounts.VaultUnavailable):
        accounts.add_account(config, NEW)


def test_get_password_returns_it_and_flags_a_lost_vault_entry(config, vault):
    account = accounts.add_account(config, NEW)
    assert accounts.get_password(config, account["id"]) == PWD
    vault.store.clear()
    with pytest.raises(accounts.AccountsError, match="ne contient plus"):
        accounts.get_password(config, account["id"])
    nopass = accounts.add_account(config, {"label": "Sans"})
    with pytest.raises(accounts.AccountNotFound, match="aucun mot de passe"):
        accounts.get_password(config, nopass["id"])


def test_validation_errors_are_explicit_and_never_echo_the_password(config, vault):
    for bad, match in [({}, "libellé"), ({"label": 3}, "chaîne"), ({"label": "x", "zzz": 1}, "zzz"),
                       ({"label": "x", "password": 12}, "password"),
                       ({"label": "x", "password": "p" * 600}, "512")]:
        with pytest.raises(accounts.AccountsError, match=match) as err:
            accounts.add_account(config, bad)
        assert "ppp" not in str(err.value)
    with pytest.raises(accounts.AccountsError, match="objet"):
        accounts.add_account(config, ["x"])


def test_corrupt_accounts_file_is_an_explicit_error(config, vault):
    Path("state").mkdir()
    Path("state/accounts.json").write_text("{pas du json", encoding="utf-8")
    with pytest.raises(accounts.AccountsError, match="illisible"):
        accounts.list_accounts(config)


def test_accounts_read_write_cycle_is_locked_across_processes(config, vault):
    """Revue r-comptes 3 : preuve a deux processus (comme research/reviews/scratch-comptes/accounts_race.py)
    que le cycle lecture-ecriture d'accounts.py est protege par un verrou de FICHIER inter-processus, pas
    seulement le threading.Lock de ce processus : un vrai second processus (le worker), lance exactement entre
    la lecture et l'ecriture du web, n'efface pas l'arret R4 qu'il vient d'ecrire."""
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    root = Path(accounts.__file__).resolve().parents[1]
    cwd = Path.cwd()
    worker_code = (
        "import sys\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "from clipper import accounts\n"
        "from clipper.config import Config\n"
        "from pathlib import Path\n"
        "cfg = Config(mode='review', workspace_dir=Path('w'), output_dir=Path('o'))\n"
        f"accounts.uncheck_ready(cfg, {account['id']!r}, 'arret de publication : captcha')\n"
    )
    real_write = accounts._write
    spawned: list[subprocess.Popen] = []

    def write_after_spawning_worker(cfg, accs):
        # Le worker est lance alors que ce process tient encore le verrou (il est dans son cycle
        # lecture-ecriture) : sans verrou de FICHIER, le worker ecrit avant nous et sa R4 est effacee par
        # notre propre ecriture, batie sur une lecture plus ancienne que la sienne.
        spawned.append(subprocess.Popen([sys.executable, "-c", worker_code], cwd=cwd))
        time.sleep(0.3)
        return real_write(cfg, accs)

    accounts._write = write_after_spawning_worker
    try:
        accounts.update_account(config, account["id"], {"slots": [{"day": "mon", "time": "18:00"}]})
    finally:
        accounts._write = real_write

    assert spawned[0].wait(timeout=10) == 0
    stored = accounts.list_accounts(config)[0]
    assert stored["slots"] == [{"day": "mon", "time": "18:00"}]  # l'ecriture du web n'est pas perdue
    assert stored["r4_halt"] is not None                          # l'arret R4 du worker non plus
    assert stored["ready_to_publish"] is False


# --------------------------------------------------------------------------
# R4 : jamais le mot de passe hors de la route dediee
# --------------------------------------------------------------------------


def test_list_has_no_password_only_has_password(config, vault):
    accounts.add_account(config, NEW)
    listed = accounts.list_accounts(config)
    assert PWD not in json.dumps(listed)
    assert listed[0]["has_password"] is True and "password" not in listed[0]


def test_vault_errors_and_logs_never_contain_the_password(config, caplog):
    caplog.set_level(logging.DEBUG)
    accounts.use_backend(LeakyKeyring())
    try:
        with pytest.raises(accounts.AccountsError) as err:
            accounts.add_account(config, NEW)
    finally:
        accounts.use_backend(None)
    assert PWD not in str(err.value) and PWD not in repr(err.value)
    assert PWD not in caplog.text
    import traceback

    assert PWD not in "".join(traceback.format_exception(err.value))


def test_http_flow_never_leaks_the_password_except_on_the_dedicated_route(config, vault, caplog):
    caplog.set_level(logging.DEBUG)
    c = local_client(config)

    created = c.post("/api/accounts", json=NEW)
    assert created.status_code == 201 and PWD not in created.text
    account_id = created.json()["id"]

    listed = c.get("/api/accounts")
    assert listed.status_code == 200 and PWD not in listed.text
    assert listed.json()[0]["has_password"] is True

    shown = c.get(f"/api/accounts/{account_id}/password")
    assert shown.status_code == 200 and shown.json() == {"password": PWD}
    assert shown.headers["cache-control"] == "no-store"

    bad = c.put(f"/api/accounts/{account_id}", json={"label": 12, "password": PWD})
    assert bad.status_code == 422 and PWD not in bad.text
    bad2 = c.post("/api/accounts", content=f'{{"password": "{PWD}", ', headers={"content-type": "application/json"})
    assert bad2.status_code == 422 and PWD not in bad2.text
    assert PWD not in caplog.text


def test_leaky_vault_over_http_returns_a_clean_error(config):
    accounts.use_backend(LeakyKeyring())
    try:
        resp = local_client(config).post("/api/accounts", json=NEW)
    finally:
        accounts.use_backend(None)
    assert resp.status_code == 422 and PWD not in resp.text


def test_unsafe_vault_over_http_is_503_in_french(config):
    accounts.use_backend(keyring_fail.Keyring())
    try:
        resp = local_client(config).post("/api/accounts", json=NEW)
    finally:
        accounts.use_backend(None)
    assert resp.status_code == 503 and "coffre" in resp.json()["detail"]
    assert PWD not in resp.text


def test_crud_over_http(config, vault):
    c = local_client(config)
    account_id = c.post("/api/accounts", json=NEW).json()["id"]
    changed = c.put(f"/api/accounts/{account_id}", json={"label": "Nouveau"})
    assert changed.status_code == 200 and changed.json()["label"] == "Nouveau"
    assert c.put("/api/accounts/inconnu", json={"label": "x"}).status_code == 404
    assert c.request("DELETE", f"/api/accounts/{account_id}", json={}).status_code == 204
    assert c.get("/api/accounts").json() == [] and vault.store == {}
    assert c.get(f"/api/accounts/{account_id}/password").status_code == 404


# --------------------------------------------------------------------------
# R3 : PC seulement
# --------------------------------------------------------------------------

ROUTES = [
    ("get", "/api/accounts"), ("post", "/api/accounts"), ("post", "/api/accounts/generate"),
    ("put", "/api/accounts/abc"), ("delete", "/api/accounts/abc"), ("get", "/api/accounts/abc/password"),
]


def _call(c, method, path, **kw):
    if method == "get":
        return c.get(path, **kw)
    kw.setdefault("json", {})
    return c.request(method.upper(), path, **kw)


@pytest.mark.parametrize("method,path", ROUTES)
def test_remote_client_address_is_refused_even_with_a_valid_token(tmp_path, monkeypatch, vault, method, path):
    monkeypatch.chdir(tmp_path)
    cfg = Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o",
                 _sections={"web": {"host": "0.0.0.0", "token": "jeton-exemple"}})
    c = TestClient(create_app(config=cfg), base_url=LOCAL, client=("192.168.1.20", 50000))
    resp = _call(c, method, path, headers={"x-clipper-token": "jeton-exemple"})
    assert resp.status_code == 403
    assert "PC" in resp.json()["detail"] and "bouclage" in resp.json()["detail"]


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8000", "127.0.0.1.evil.com", "127.0.0.1:80:80", ""])
def test_foreign_host_header_is_refused_even_from_loopback(config, vault, method, path, host):
    c = TestClient(create_app(config=config), client=("127.0.0.1", 50000))
    resp = _call(c, method, path, headers={"host": host})
    assert resp.status_code == 403
    assert "Host" in resp.json()["detail"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "localhost:8123", "127.0.0.1:8000"])
def test_loopback_host_headers_are_accepted(config, vault, host):
    c = TestClient(create_app(config=config), client=("127.0.0.1", 50000))
    assert c.get("/api/accounts", headers={"host": host}).status_code == 200


def test_ipv6_loopback_client_is_accepted_and_missing_client_refused(config, vault):
    c6 = TestClient(create_app(config=config), base_url=LOCAL, client=("::1", 50000))
    assert c6.get("/api/accounts").status_code == 200
    other = TestClient(create_app(config=config), base_url=LOCAL, client=("not-an-ip", 1))
    assert other.get("/api/accounts").status_code == 403


def test_other_routes_are_not_affected_by_the_local_only_rule(config):
    c = TestClient(create_app(config=config), client=("192.168.1.20", 50000))
    assert c.get("/api/queue").status_code == 200


# --------------------------------------------------------------------------
# R4 : CORS, ecritures JSON seulement
# --------------------------------------------------------------------------


def test_no_cors_headers_and_preflight_is_refused(config, vault):
    c = local_client(config)
    resp = c.get("/api/accounts", headers={"origin": "https://evil.example.com"})
    assert not [h for h in resp.headers if h.lower().startswith("access-control-")]
    pre = c.options("/api/accounts", headers={"origin": "https://evil.example.com",
                                              "access-control-request-method": "POST",
                                              "access-control-request-headers": "content-type"})
    assert pre.status_code >= 400
    assert not [h for h in pre.headers if h.lower().startswith("access-control-")]


@pytest.mark.parametrize("method,path", [("post", "/api/accounts"), ("put", "/api/accounts/abc"),
                                         ("delete", "/api/accounts/abc"), ("post", "/api/accounts/generate")])
def test_writes_without_a_json_body_are_refused(config, vault, method, path):
    c = local_client(config)
    for kwargs in ({}, {"content": "label=x", "headers": {"content-type": "application/x-www-form-urlencoded"}},
                   {"content": '{"label": "x"}', "headers": {"content-type": "text/plain"}}):
        resp = c.request(method.upper(), path, **kwargs)
        assert resp.status_code == 415
        assert "JSON" in resp.json()["detail"]
    assert c.get("/api/accounts").json() == []


# --------------------------------------------------------------------------
# R5 : generateur
# --------------------------------------------------------------------------


def test_default_length_comes_from_config_defaults(config):
    assert len(accounts.generate_password(config)) == accounts.CONFIG_DEFAULTS["password_length"] == 20


@pytest.mark.parametrize("length", [12, 20, 64])
def test_generated_password_has_every_class_and_the_right_length(config, length):
    for _ in range(50):
        pw = accounts.generate_password(config, length)
        assert len(pw) == length
        assert any(c in string.ascii_lowercase for c in pw)
        assert any(c in string.ascii_uppercase for c in pw)
        assert any(c in string.digits for c in pw)
        assert all(c in string.ascii_letters + string.digits for c in pw)


def test_symbols_option_adds_a_symbol_class(config):
    for _ in range(50):
        pw = accounts.generate_password(config, 12, symbols=True)
        assert any(not c.isalnum() for c in pw)
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_avoid_ambiguous_removes_lookalike_characters(config):
    for _ in range(100):
        pw = accounts.generate_password(config, 64, symbols=True, avoid_ambiguous=True)
        assert not set(pw) & set("Il1O0o|")


def test_generator_uses_secrets_not_random(config):
    src = Path(accounts.__file__).read_text(encoding="utf-8")
    assert "import secrets" in src and not re.search(r"^import random|^from random", src, re.M)


@pytest.mark.parametrize("length", [11, 65, 0, -5, 12.5, "20", True])
def test_out_of_bounds_length_is_refused(config, length):
    with pytest.raises(accounts.AccountsError, match="longueur invalide"):
        accounts.generate_password(config, length)


def test_generate_route(config, vault):
    c = local_client(config)
    ok = c.post("/api/accounts/generate", json={"length": 30, "symbols": True, "avoid_ambiguous": True})
    assert ok.status_code == 200 and len(ok.json()["password"]) == 30
    assert len(c.post("/api/accounts/generate", json={}).json()["password"]) == 20
    assert c.post("/api/accounts/generate", json={"length": 5}).status_code == 422
    assert c.post("/api/accounts/generate", json={"length": 70}).status_code == 422
    assert c.post("/api/accounts/generate", json={"symbols": "oui"}).status_code == 422
    assert c.post("/api/accounts/generate", json={"x": 1}).status_code == 422


# --------------------------------------------------------------------------
# R6 : ecran Comptes (test statique)
# --------------------------------------------------------------------------


def test_accounts_screen_is_wired_in_navigation_and_scripts():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    nav = page[page.index('<nav class="nav"'):page.index("</nav>", page.index('<nav class="nav"'))]

    assert 'href="#/accounts"' in nav and 'data-screen="accounts"' in nav
    assert nav.index("Configuration") < nav.index('href="#/accounts"')
    assert 'id="screen-accounts"' in page
    assert "/static/screens/accounts.js" in page
    assert page.index("/static/screens.js") < page.index("/static/screens/accounts.js") < page.index("/static/app.js")
    assert '"accounts"' in app_js


def test_accounts_screen_has_the_spec_features():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")

    assert "Screens.accounts" in js
    for route in ("/api/accounts", "/password", "/api/accounts/generate"):
        assert route in js
    assert "••••" in js and "has_password" in js                      # liste masquée
    for label in ("Copier l'identifiant", "Copier le mot de passe", "Afficher", "Ajouter", "Modifier", "Supprimer"):
        assert label in js
    assert "navigator.clipboard" in js and "Copié" in js
    assert re.search(r"30\s*\*\s*1000|30000", js)                     # re-masqué après 30 s
    assert "confirmDialog" in js                                      # suppression confirmée
    assert "location.hostname" in js and "à distance" in js           # avis console distante
    assert "toastError" in js
    assert "ma_chaine" in js or "exemple@example.com" in js           # exemples neutres


def test_accounts_screen_never_persists_secrets_in_the_browser():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")
    assert "localStorage" not in js and "sessionStorage" not in js


# --------------------------------------------------------------------------
# SPEC-00d1 R3, R6 : case « pret a publier », connexion verifiee, decochage automatique
# --------------------------------------------------------------------------

CONNECTED = {"state": "connected", "checked_at": "2026-10-01T10:00:00+00:00", "expires_at": None}


def _login(state, at="2026-10-01T11:00:00+00:00"):
    return {"state": state, "checked_at": at, "expires_at": None}


def test_a_new_account_is_never_connected_and_not_ready(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})

    assert account["ready_to_publish"] is False and account["login"] is None and account["ready_note"] is None
    assert "has_password" in account  # les champs d'avant restent


def test_ready_is_computed_from_the_verified_connection_with_the_reason(config, vault, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    for state, expected in (("never", "non vérifiée"), ("expired", "expirée")):
        out = accounts.record_login(config, account["id"], _login(state))
        assert out["ready_to_publish"] is False
        assert expected in accounts.ready_blocked_reason(out)
    with caplog.at_level(logging.INFO):
        out = accounts.record_login(config, account["id"], CONNECTED)
    assert out["ready_to_publish"] is True and out["auto_checked"] is True and out["ready_note"] is None
    assert accounts.list_accounts(config)[0]["ready_to_publish"] is True
    assert "coché automatiquement" in caplog.text and "connexion TikTok vérifiée" in caplog.text


def test_there_is_no_manual_way_to_tick_ready(config, vault):
    assert not hasattr(accounts, "set_ready")
    account = accounts.add_account(config, {"label": "Compte"})
    with pytest.raises(accounts.AccountsError, match="champ"):
        accounts.update_account(config, account["id"], {"ready_to_publish": True})
    assert accounts.list_accounts(config)[0]["ready_to_publish"] is False


def test_an_already_connected_profile_is_ready_without_any_action(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})  # profil connecte avant l'existence de la case

    out = accounts.record_login(config, account["id"], CONNECTED)

    assert out["ready_to_publish"] is True and out["r4_halt"] is None


def test_the_ready_flag_cannot_be_set_by_the_generic_update(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    with pytest.raises(accounts.AccountsError, match="champ"):
        accounts.update_account(config, account["id"], {"ready_to_publish": True})
    with pytest.raises(accounts.AccountsError, match="champ"):
        accounts.update_account(config, account["id"], {"login": CONNECTED})


def test_an_expired_session_unchecks_ready_by_itself_logged_and_shown(config, vault, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    with caplog.at_level(logging.WARNING):
        result = accounts.record_login(config, account["id"], _login("expired"))

    assert result["auto_unchecked"] is True and result["ready_to_publish"] is False
    assert "décoché automatiquement" in result["ready_note"] and "session TikTok expirée" in result["ready_note"]
    assert any("prêt à publier" in r.message and account["id"] in r.message for r in caplog.records)
    stored = accounts.list_accounts(config)[0]
    assert stored["ready_to_publish"] is False and stored["ready_note"] == result["ready_note"]
    assert stored["login"]["state"] == "expired"


def test_a_vanished_session_cookie_of_a_connected_account_counts_as_expired(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    result = accounts.record_login(config, account["id"], _login("never"))  # Chrome a purge le cookie expire

    assert result["login"]["state"] == "expired" and result["auto_unchecked"] is True


def test_a_still_connected_check_keeps_ready_and_unchecking_is_not_repeated(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    again = accounts.record_login(config, account["id"], _login("connected", "2026-10-01T12:00:00+00:00"))
    assert again["ready_to_publish"] is True and again["auto_unchecked"] is False
    assert again["login"]["checked_at"] == "2026-10-01T12:00:00+00:00"

    accounts.record_login(config, account["id"], _login("expired"))
    second = accounts.record_login(config, account["id"], _login("expired"))
    assert second["auto_unchecked"] is False  # deja decoche : pas de nouveau decochage


def test_record_login_does_not_write_the_file_for_a_checked_at_only_refresh(config, vault, monkeypatch):
    """Revue r-comptes 3 : l'ouverture de l'ecran Comptes revverifie la connexion a chaque fois, mais rien ne
    doit etre ecrit sur disque quand seul checked_at change (sinon course avec le worker a chaque ouverture)."""
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    calls = []
    real_write = accounts._write
    monkeypatch.setattr(accounts, "_write", lambda cfg, accs: (calls.append(1), real_write(cfg, accs))[-1])

    again = accounts.record_login(config, account["id"], _login("connected", "2026-10-01T12:00:00+00:00"))
    assert again["login"]["checked_at"] == "2026-10-01T12:00:00+00:00"  # rendu a l'appelant malgre tout
    assert calls == []

    changed = accounts.record_login(config, account["id"], _login("expired", "2026-10-01T13:00:00+00:00"))
    assert changed["login"]["state"] == "expired"
    assert calls == [1]  # l'etat a change : ecriture


def test_an_r4_stop_unchecks_ready_until_the_user_says_the_problem_is_fixed(config, vault, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    with caplog.at_level(logging.WARNING):
        assert accounts.uncheck_ready(config, account["id"], "arrêt de publication : captcha détecté") is True
    stored = accounts.list_accounts(config)[0]
    assert stored["ready_to_publish"] is False and "captcha détecté" in stored["ready_note"]
    assert "captcha détecté" in caplog.text
    assert accounts.uncheck_ready(config, account["id"], "encore") is False  # deja decoche
    assert stored["r4_halt"]["reason"] == "arrêt de publication : captcha détecté"
    # la connexion revérifiée ne suffit pas : l'arrêt R4 reste en attente
    again = accounts.record_login(config, account["id"], _login("connected", "2026-10-01T12:00:00+00:00"))
    assert again["ready_to_publish"] is False and "captcha détecté" in accounts.ready_blocked_reason(again)
    # « J'ai réglé le problème » : efface l'arrêt, la case suit la connexion
    resolved = accounts.clear_halt(config, account["id"])
    assert resolved["r4_halt"] is None and resolved["ready_to_publish"] is True and resolved["ready_note"] is None


def test_record_login_rejects_an_unknown_state(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    with pytest.raises(accounts.AccountsError, match="état de connexion invalide"):
        accounts.record_login(config, account["id"], {"state": "peut-etre"})


def test_ready_and_login_never_carry_a_password(config, vault):
    account = accounts.add_account(config, NEW)
    accounts.record_login(config, account["id"], CONNECTED)

    assert PWD not in Path("state/accounts.json").read_text(encoding="utf-8")
    assert PWD not in json.dumps(accounts.list_accounts(config))


# ---- routes : connexion verifiee a l'ouverture, case refusee, ecran Comptes


@pytest.fixture
def cookies():
    """Lecture de cookies simulee : ``cookies.set(account, [...])`` ; aucun profil, aucun navigateur."""
    from clipper import browser

    class Reader:
        def __init__(self):
            self.by_account, self.error = {}, None

        def set(self, account, rows):
            self.by_account[account] = rows
            profile = Path("state/browser") / account
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "Local State").write_text("{}", encoding="utf-8")

        def __call__(self, account):
            if self.error:
                raise browser.BrowserError(self.error)
            return self.by_account.get(account, [])

    reader = Reader()
    browser.use_cookie_reader(reader)
    yield reader
    browser.use_cookie_reader(None)


def _session(days=30, name="sessionid"):
    from datetime import datetime, timedelta, timezone

    return {"name": name, "domain": ".tiktok.com",
            "expires": (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()}


def test_the_accounts_list_verifies_each_connection_on_opening(config, vault, cookies):
    connected = accounts.add_account(config, {"label": "Connecté"})
    never = accounts.add_account(config, {"label": "Jamais"})
    expired = accounts.add_account(config, {"label": "Expiré"})
    cookies.set(connected["id"], [_session()])
    cookies.set(expired["id"], [_session(days=-2)])

    rows = {a["label"]: a for a in local_client(config).get("/api/accounts").json()}

    assert rows["Connecté"]["login"]["state"] == "connected" and rows["Connecté"]["ready_blocked_reason"] is None
    assert rows["Connecté"]["login"]["expires_at"] and rows["Connecté"]["login"]["checked_at"]
    assert rows["Jamais"]["login"]["state"] == "never" and "non vérifiée" in rows["Jamais"]["ready_blocked_reason"]
    assert rows["Expiré"]["login"]["state"] == "expired" and "expirée" in rows["Expiré"]["ready_blocked_reason"]
    assert never["id"] == rows["Jamais"]["id"]
    assert accounts.list_accounts(config)[0]["login"]["state"] == "connected"  # enregistre


def test_clear_halt_keeps_ready_off_while_the_connection_is_not_verified(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)
    accounts.uncheck_ready(config, account["id"], "captcha")
    accounts.record_login(config, account["id"], _login("expired"))

    out = accounts.clear_halt(config, account["id"])

    assert out["r4_halt"] is None and out["ready_to_publish"] is False
    assert "expirée" in accounts.ready_blocked_reason(out)
    with pytest.raises(accounts.AccountNotFound):
        accounts.clear_halt(config, "inconnu")


def test_an_already_connected_profile_shows_ready_on_opening_the_screen(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])

    row = local_client(config).get("/api/accounts").json()[0]

    assert row["ready_to_publish"] is True and row["ready_blocked_reason"] is None
    assert accounts.list_accounts(config)[0]["ready_to_publish"] is True  # enregistre


def test_a_profile_without_session_is_not_ready_and_says_why(config, vault, cookies):
    accounts.add_account(config, {"label": "Compte"})

    row = local_client(config).get("/api/accounts").json()[0]

    assert row["ready_to_publish"] is False and "non vérifiée" in row["ready_blocked_reason"]


def test_the_manual_ready_route_is_gone_405_with_a_message(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)

    for body in ({"ready": True}, {"ready": False}):
        resp = c.put(f"/api/accounts/{account['id']}/ready", json=body)
        assert resp.status_code == 405 and "automatique" in resp.json()["detail"]
    assert accounts.list_accounts(config)[0]["ready_to_publish"] is False  # rien n'a bougé


def test_closing_the_login_window_rechecks_and_ticks_ready(config, vault, cookies):
    from clipper import web
    from clipper.web import app as web_app

    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])  # l'utilisateur s'est connecte pendant que la fenetre etait ouverte

    web_app._verify_login(config, {"id": account["id"]})  # ce que start_login appelle a la fermeture (on_close)

    assert accounts.list_accounts(config)[0]["ready_to_publish"] is True


def test_reopening_after_the_session_expired_unticks_with_a_logged_reason(config, vault, cookies, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    assert c.get("/api/accounts").json()[0]["ready_to_publish"] is True
    cookies.set(account["id"], [_session(days=-1)])

    with caplog.at_level(logging.WARNING):
        row = c.get("/api/accounts").json()[0]

    assert row["ready_to_publish"] is False and "décoché automatiquement" in row["ready_note"]
    assert "décoché" in caplog.text


def test_the_resolve_route_clears_the_r4_stop_and_rechecks_the_connection(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.get("/api/accounts")
    accounts.uncheck_ready(config, account["id"], "arrêt de publication : captcha détecté")
    row = c.get("/api/accounts").json()[0]
    assert row["ready_to_publish"] is False and row["r4_halt"]["reason"].endswith("captcha détecté")
    assert "captcha détecté" in row["ready_blocked_reason"]

    resp = c.post(f"/api/accounts/{account['id']}/resolve", json={})

    assert resp.status_code == 200
    assert resp.json()["r4_halt"] is None and resp.json()["ready_to_publish"] is True
    assert accounts.list_accounts(config)[0]["ready_to_publish"] is True


def test_the_resolve_route_does_not_tick_when_the_connection_is_gone(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.get("/api/accounts")
    accounts.uncheck_ready(config, account["id"], "captcha")
    cookies.set(account["id"], [_session(days=-1)])

    resp = c.post(f"/api/accounts/{account['id']}/resolve", json={})

    assert resp.status_code == 200
    assert resp.json()["r4_halt"] is None and resp.json()["ready_to_publish"] is False
    assert "expirée" in resp.json()["ready_note"] or "expirée" in accounts.ready_blocked_reason(resp.json())


def test_the_resolve_route_with_unreadable_cookies_keeps_the_stop_and_says_why(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.get("/api/accounts")
    accounts.uncheck_ready(config, account["id"], "captcha")
    cookies.error = "cookies du profil illisibles : ferme la fenêtre Chrome de ce compte"

    resp = c.post(f"/api/accounts/{account['id']}/resolve", json={})

    assert resp.status_code == 409 and "ferme la fenêtre Chrome" in resp.json()["detail"]
    assert accounts.list_accounts(config)[0]["r4_halt"] is not None


def test_the_resolve_route_validates_the_account_and_is_local_only(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    assert local_client(config).post("/api/accounts/inconnu/resolve", json={}).status_code == 404
    remote = TestClient(create_app(config=config), base_url="http://exemple.invalid", client=("203.0.113.5", 50000))
    assert remote.post(f"/api/accounts/{account['id']}/resolve", json={}).status_code == 403


def test_opening_the_screen_unticks_ready_when_the_session_expired_and_notifies_the_console(config, vault, cookies):
    from clipper import tiktok

    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    assert c.get("/api/accounts").json()[0]["ready_to_publish"] is True
    cookies.set(account["id"], [_session(days=-1)])

    row = c.get("/api/accounts").json()[0]

    assert row["ready_to_publish"] is False and "session TikTok expirée" in row["ready_note"]
    assert row["ready_blocked_reason"] and "expirée" in row["ready_blocked_reason"]
    event = tiktok.read_events(config=config)[-1]
    assert event["level"] == "warn" and event["account"] == account["id"] and "décoché" in event["reason"]


def test_unreadable_cookies_on_opening_keep_the_known_state_and_say_so(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.get("/api/accounts")
    cookies.error = "cookies du profil illisibles : ferme la fenêtre Chrome de ce compte"

    row = c.get("/api/accounts").json()[0]

    assert row["ready_to_publish"] is True  # pas de verification = pas de decochage
    assert "ferme la fenêtre Chrome" in row["login_error"] and "non vérifiable" in row["ready_blocked_reason"]


# Preuve TASK-b778469259de : le fuseau par defaut d'un compte est Europe/Paris (test_a_new_account_has_no_slots...),
# donc _account_overview decide du "jour" des posts en heure de Paris. Rejoue a l'horloge reelle puis a 23:58/00:02
# Paris, ete (UTC+2) et hiver (UTC+1).
_PARIS_BOUNDARY_CASES = [
    pytest.param(None, id="horloge-reelle"),
    pytest.param(_datetime(2026, 6, 14, 21, 58, tzinfo=_timezone.utc), id="23h58-paris-ete"),
    pytest.param(_datetime(2026, 6, 14, 22, 2, tzinfo=_timezone.utc), id="00h02-paris-ete"),
    pytest.param(_datetime(2025, 12, 31, 22, 58, tzinfo=_timezone.utc), id="23h58-paris-hiver"),
    pytest.param(_datetime(2025, 12, 31, 23, 2, tzinfo=_timezone.utc), id="00h02-paris-hiver"),
]


@pytest.mark.parametrize("frozen_at", _PARIS_BOUNDARY_CASES)
def test_the_accounts_screen_rows_carry_posts_of_the_day_cap_and_last_failure(config, vault, cookies, monkeypatch, frozen_at):
    from datetime import datetime, timezone

    from clipper.web import app as web_app

    account = accounts.add_account(config, {"label": "Compte"})  # fuseau par defaut : Europe/Paris
    now = frozen_at or datetime.now(timezone.utc)
    # TASK-b778469259de : _account_overview lit datetime.now(tz) (Europe/Paris) une seconde fois, a quelques
    # ms de ``now`` ci-dessus, pour decider du jour calendaire des entrees ; pres de minuit a Paris les deux
    # lectures peuvent tomber de part et d'autre de la frontiere. On fige la lecture du serveur sur ``now``.
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now

    monkeypatch.setattr(web_app, "datetime", _Frozen)
    Path("presets").mkdir()
    Path("presets/ma_chaine.toml").write_text(
        f'[channel]\ntimezone = "UTC"\ntiktok_account = "{account["id"]}"\n', encoding="utf-8")
    Path("config.toml").write_text('mode = "review"\n', encoding="utf-8")
    entries = [
        {"video_id": "aaaaaaaaaaa", "clip_id": "01", "series_id": None, "part": None, "status": "published",
         "slot_at": None, "decided_at": None, "published_at": now.isoformat(), "error": None,
         "tiktok_publish_at": now.isoformat(), "account": account["id"]},
        {"video_id": "aaaaaaaaaaa", "clip_id": "02", "series_id": None, "part": None, "status": "failed",
         "slot_at": None, "decided_at": None, "published_at": None, "error": "captcha détecté",
         "capture": "state/browser/x/captures/a.png", "failed_at": now.isoformat(), "account": account["id"]},
    ]
    Path("state/publish").mkdir(parents=True)
    Path("state/publish/ma_chaine.json").write_text(json.dumps(entries), encoding="utf-8")

    row = local_client(config).get("/api/accounts").json()[0]

    assert (row["posts_today"], row["max_posts_per_day"]) == (1, 1)
    failure = row["last_failure"]
    assert failure["reason"] == "captcha détecté" and failure["clip_id"] == "02" and failure["channel"] == "ma_chaine"
    assert failure["capture_url"] == "/api/publish/aaaaaaaaaaa/02/capture"


def test_the_accounts_screen_is_wired_for_publication_accounts():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")

    assert "data-acc-ready" in js and "Prêt à publier" in js
    assert "/ready" not in js and "accSetReady" not in js                # plus aucune coche manuelle
    assert "ready_blocked_reason" in js and "aria-readonly" not in js    # case cliquable (SPEC-f348 R5), avec la raison
    assert "preventDefault" in js and "accBrowserLogin" in js            # cliquer sur la case non prête = Se connecter
    assert "/resolve" in js and "J'ai réglé le problème" in js and "r4_halt" in js
    assert "login.state" in js or "a.login" in js                       # état de connexion
    for label in ("jamais connecté", "connecté", "session expirée"):
        assert label in js, label
    assert "posts_today" in js and "max_posts_per_day" in js            # posts du jour / plafond
    assert "last_failure" in js and "capture_url" in js                 # dernier échec avec capture
    assert "Se connecter" in js and "/browser/login" in js
    assert "Relever les stats" in js and "/api/stats/tiktok/refresh" in js
    assert "ready_note" in js                                           # décochage automatique affiché


# --------------------------------------------------------------------------
# SPEC-6076 R2 : creneaux de publication reguliers portes par le compte
# --------------------------------------------------------------------------

SLOTS = [{"day": "mon", "time": "18:30"}, {"day": "fri", "time": "09:00"}]


def test_a_new_account_has_no_slots_and_the_default_timezone(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})

    assert account["slots"] == [] and account["timezone"] == "Europe/Paris"
    assert accounts.schedule_of(account) == {"slots": [], "timezone": "Europe/Paris"}


def test_an_account_of_an_older_file_reads_without_slots_then_with_the_default_timezone(config, vault):
    Path("state").mkdir()
    Path("state/accounts.json").write_text(json.dumps({"accounts": [{"id": "ab12cd", "label": "Ancien"}]}), encoding="utf-8")

    listed = accounts.list_accounts(config)[0]

    assert listed["slots"] == [] and listed["timezone"] == "Europe/Paris"


def test_slots_and_timezone_are_saved_on_the_account_and_listed(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})

    updated = accounts.update_account(config, account["id"], {"slots": SLOTS, "timezone": "UTC"})

    assert updated["slots"] == SLOTS and updated["timezone"] == "UTC"
    on_disk = json.loads(Path("state/accounts.json").read_text(encoding="utf-8"))["accounts"][0]
    assert on_disk["slots"] == SLOTS and on_disk["timezone"] == "UTC"
    assert accounts.list_accounts(config)[0]["slots"] == SLOTS
    # une mise a jour sans slots ne les efface pas, `slots: []` les retire (suppression d'un creneau)
    assert accounts.update_account(config, account["id"], {"label": "Renommé"})["slots"] == SLOTS
    assert accounts.update_account(config, account["id"], {"slots": SLOTS[:1]})["slots"] == SLOTS[:1]
    assert accounts.update_account(config, account["id"], {"slots": []})["slots"] == []


def test_slots_are_validated_and_deduplicated_with_explicit_errors(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})

    for bad in ([{"day": "someday", "time": "18:30"}], [{"day": "mon", "time": "25:99"}], [{"day": "mon"}],
                ["mon 18:30"], "mon 18:30", [{"day": "mon", "time": "18:30", "extra": 1}]):
        with pytest.raises(accounts.AccountsError, match="créneau|slots"):
            accounts.update_account(config, account["id"], {"slots": bad})
    with pytest.raises(accounts.AccountsError, match="fuseau horaire inconnu"):
        accounts.update_account(config, account["id"], {"timezone": "Mars/Olympus"})
    assert accounts.list_accounts(config)[0]["slots"] == []
    twice = accounts.update_account(config, account["id"], {"slots": SLOTS + SLOTS[:1]})
    assert twice["slots"] == SLOTS


def test_migrate_slots_fills_an_account_once_and_never_overwrites(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})

    assert accounts.migrate_slots(config, account["id"], SLOTS, "UTC") is True
    assert accounts.migrate_slots(config, account["id"], [{"day": "tue", "time": "10:00"}], "Asia/Tokyo") is False

    after = accounts.list_accounts(config)[0]
    assert after["slots"] == SLOTS and after["timezone"] == "UTC"
    with pytest.raises(accounts.AccountNotFound):
        accounts.migrate_slots(config, "zz99", SLOTS)


def test_slots_over_http_are_saved_by_the_accounts_screen_and_refused_when_invalid(config, vault):
    client = local_client(config)
    created = client.post("/api/accounts", json={"label": "Compte exemple", "slots": SLOTS, "timezone": "UTC"})
    assert created.status_code == 201 and created.json()["slots"] == SLOTS
    account_id = created.json()["id"]

    saved = client.put(f"/api/accounts/{account_id}", json={"slots": SLOTS[:1]})
    assert saved.status_code == 200 and saved.json()["slots"] == SLOTS[:1]
    assert client.get("/api/accounts").json()[0]["slots"] == SLOTS[:1]
    refused = client.put(f"/api/accounts/{account_id}", json={"slots": [{"day": "xx", "time": "09:00"}]})
    assert refused.status_code == 422 and "créneau invalide" in refused.json()["detail"]


def test_accounts_screen_edits_slots_like_the_old_channel_form():
    js = (STATIC / "screens" / "accounts.js").read_text(encoding="utf-8")

    for marker in ("accSlotsEditor", "accReadSlots", "data-slot-add", "data-slot-del", "data-acc-slots",
                   "slots: accReadSlots(slotsBox)", "timezone:", "Créneaux de publication"):
        assert marker in js


# ---- pause manuelle d'un compte (SPEC-f348 R3 c, R7.1-R7.5)


def test_pause_sets_paused_at_unticks_ready_and_says_so(config, vault, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)

    with caplog.at_level(logging.INFO):
        out = accounts.pause(config, account["id"])

    assert out["ready_to_publish"] is False and out["auto_unchecked"] is True
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", out["paused_at"])
    reloaded = accounts.list_accounts(config)[0]
    assert reloaded["paused_at"] == out["paused_at"] and reloaded["ready_to_publish"] is False
    assert "En pause (manuel) depuis le " in accounts.ready_blocked_reason(reloaded)
    assert "En pause (manuel) depuis le " in reloaded["ready_note"]
    assert "pause" in caplog.text and account["id"] in caplog.text


def test_a_second_pause_changes_nothing(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    first = accounts.pause(config, account["id"])
    before = Path("state/accounts.json").read_text(encoding="utf-8")

    second = accounts.pause(config, account["id"])

    assert second["paused_at"] == first["paused_at"] and second["auto_unchecked"] is False
    assert Path("state/accounts.json").read_text(encoding="utf-8") == before


def test_resume_ticks_ready_when_connected_without_halt(config, vault, caplog):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)
    accounts.pause(config, account["id"])

    with caplog.at_level(logging.INFO):
        out = accounts.resume(config, account["id"])

    assert out["paused_at"] is None and out["ready_to_publish"] is True and out["auto_checked"] is True
    assert accounts.list_accounts(config)[0]["paused_at"] is None
    assert "reprise" in caplog.text or "resume" in caplog.text


def test_resume_leaves_ready_off_with_the_connection_or_halt_reason(config, vault):
    a = accounts.add_account(config, {"label": "A"})
    accounts.pause(config, a["id"])
    out = accounts.resume(config, a["id"])
    assert out["ready_to_publish"] is False and "non vérifiée" in accounts.ready_blocked_reason(out)
    assert "En pause" not in (out["ready_note"] or "")

    b = accounts.add_account(config, {"label": "B"})
    accounts.record_login(config, b["id"], CONNECTED)
    accounts.uncheck_ready(config, b["id"], "arrêt de publication : captcha détecté")
    accounts.pause(config, b["id"])
    out = accounts.resume(config, b["id"])
    assert out["ready_to_publish"] is False and "captcha détecté" in accounts.ready_blocked_reason(out)
    assert "En pause" not in (out["ready_note"] or "")


def test_resume_without_pause_changes_nothing(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.record_login(config, account["id"], CONNECTED)
    before = Path("state/accounts.json").read_text(encoding="utf-8")

    out = accounts.resume(config, account["id"])

    assert out["ready_to_publish"] is True and out["auto_checked"] is False and out["paused_at"] is None
    assert Path("state/accounts.json").read_text(encoding="utf-8") == before
    with pytest.raises(accounts.AccountNotFound):
        accounts.pause(config, "inconnu")
    with pytest.raises(accounts.AccountNotFound):
        accounts.resume(config, "inconnu")


def test_during_a_pause_login_and_halt_are_recorded_but_ready_stays_off(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    accounts.pause(config, account["id"])

    out = accounts.record_login(config, account["id"], CONNECTED)
    assert out["ready_to_publish"] is False and out["auto_checked"] is False
    assert out["login"]["state"] == "connected" and "En pause (manuel)" in accounts.ready_blocked_reason(out)
    assert accounts.connection_blocked_reason(out) is None

    accounts.uncheck_ready(config, account["id"], "captcha")
    out = accounts.clear_halt(config, account["id"])
    assert out["ready_to_publish"] is False and out["r4_halt"] is None and out["auto_checked"] is False
    assert "En pause (manuel)" in accounts.ready_blocked_reason(out)

    accounts.uncheck_ready(config, account["id"], "captcha 2")
    halted = accounts.list_accounts(config)[0]
    assert halted["r4_halt"]["reason"] == "captcha 2" and halted["login"]["state"] == "connected"
    assert "captcha 2" in accounts.connection_blocked_reason(halted)


def test_paused_at_and_ready_cannot_be_set_by_update(config, vault):
    account = accounts.add_account(config, {"label": "Compte"})
    for field in ("paused_at", "ready_to_publish"):
        with pytest.raises(accounts.AccountsError, match="champ"):
            accounts.update_account(config, account["id"], {field: "2026-10-07T10:00:00+00:00"})
    c = local_client(config)
    for field in ("paused_at", "ready_to_publish"):
        assert c.put(f"/api/accounts/{account['id']}", json={field: True}).status_code == 422


# ---- routes pause / resume (SPEC-f348 R7.1, R7.2, R7.4)


def test_the_pause_route_unticks_notifies_the_console_and_shows_in_both_listings(config, vault, cookies):
    from clipper import tiktok

    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    assert c.get("/api/accounts").json()[0]["ready_to_publish"] is True

    resp = c.post(f"/api/accounts/{account['id']}/pause", json={})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ready_to_publish"] is False and body["paused_at"] and "auto_unchecked" not in body
    event = tiktok.read_events(config=config)[-1]
    assert event["account"] == account["id"] and "En pause (manuel)" in event["reason"]
    row = c.get("/api/accounts").json()[0]  # la connexion reste verifiee, la case ne se recoche pas toute seule
    assert row["paused_at"] == body["paused_at"] and row["ready_to_publish"] is False
    assert row["login"]["state"] == "connected" and "En pause (manuel)" in row["ready_blocked_reason"]
    published = c.get("/api/publish/accounts").json()["accounts"][0]
    assert published["paused_at"] == body["paused_at"] and published["ready_to_publish"] is False


def test_the_resume_route_rechecks_the_connection_before_giving_the_state_back(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.get("/api/accounts")
    c.post(f"/api/accounts/{account['id']}/pause", json={})
    cookies.set(account["id"], [_session(days=-1)])  # la session a expire pendant la pause

    resp = c.post(f"/api/accounts/{account['id']}/resume", json={})

    assert resp.status_code == 200
    assert resp.json()["paused_at"] is None and resp.json()["ready_to_publish"] is False
    assert "expirée" in accounts.ready_blocked_reason(resp.json())
    cookies.set(account["id"], [_session()])
    c.post(f"/api/accounts/{account['id']}/pause", json={})
    resumed = c.post(f"/api/accounts/{account['id']}/resume", json={})
    assert resumed.json()["paused_at"] is None and resumed.json()["ready_to_publish"] is True


def test_the_resume_route_with_unreadable_cookies_keeps_the_pause(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    cookies.set(account["id"], [_session()])
    c = local_client(config)
    c.post(f"/api/accounts/{account['id']}/pause", json={})
    cookies.error = "cookies du profil illisibles : ferme la fenêtre Chrome de ce compte"

    resp = c.post(f"/api/accounts/{account['id']}/resume", json={})

    assert resp.status_code == 409 and "ferme la fenêtre Chrome" in resp.json()["detail"]
    assert accounts.list_accounts(config)[0]["paused_at"] is not None


def test_pause_and_resume_routes_are_local_only_and_404_for_an_unknown_account(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})
    remote = TestClient(create_app(config=config), base_url="http://exemple.invalid", client=("203.0.113.5", 50000))
    for action in ("pause", "resume"):
        assert local_client(config).post(f"/api/accounts/inconnu/{action}", json={}).status_code == 404
        assert remote.post(f"/api/accounts/{account['id']}/{action}", json={}).status_code == 403
    assert accounts.list_accounts(config)[0]["paused_at"] is None


def test_the_manual_ready_route_stays_405_and_names_pause_and_resume(config, vault, cookies):
    account = accounts.add_account(config, {"label": "Compte"})

    resp = local_client(config).put(f"/api/accounts/{account['id']}/ready", json={"ready": True})

    assert resp.status_code == 405 and "pause" in resp.json()["detail"] and "resume" in resp.json()["detail"]


# --------------------------------------------------------------------------
# TASK-ef6e6c31363f : pays attendu de l'IP par compte
# --------------------------------------------------------------------------


def test_expected_country_is_stored_normalised_and_exposed(config, vault):
    account = accounts.add_account(config, {"label": "UK", "expected_country": " gb "})
    assert account["expected_country"] == "GB"
    assert accounts.list_accounts(config)[0]["expected_country"] == "GB"
    assert accounts.expected_country_of(config, account["id"]) == "GB"


def test_account_without_expected_country_is_unchanged(config, vault):
    account = accounts.add_account(config, {"label": "FR"})
    assert account["expected_country"] == ""
    assert accounts.expected_country_of(config, account["id"]) is None
    assert accounts.expected_country_of(config, "inconnu") is None


def test_expected_country_can_be_changed_and_cleared(config, vault):
    account = accounts.add_account(config, {"label": "UK", "expected_country": "GB"})
    assert accounts.update_account(config, account["id"], {"expected_country": "fr"})["expected_country"] == "FR"
    assert accounts.update_account(config, account["id"], {"label": "Autre"})["expected_country"] == "FR"
    assert accounts.update_account(config, account["id"], {"expected_country": ""})["expected_country"] == ""
    assert accounts.expected_country_of(config, account["id"]) is None


@pytest.mark.parametrize("bad", ["GBR", "G", "1F", "G B", 5, None])
def test_expected_country_must_be_two_letters(config, vault, bad):
    with pytest.raises(accounts.AccountsError, match="expected_country"):
        accounts.add_account(config, {"label": "UK", "expected_country": bad})


def test_expected_country_over_the_api(config, vault):
    client = local_client(config)
    created = client.post("/api/accounts", json={"label": "UK", "expected_country": "gb"})
    assert created.status_code == 201 and created.json()["expected_country"] == "GB"
    bad = client.put(f"/api/accounts/{created.json()['id']}", json={"expected_country": "GBR"})
    assert bad.status_code == 422 and "expected_country" in bad.text
