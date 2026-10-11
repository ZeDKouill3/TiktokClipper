"""Lance ``clipper serve`` sur l'espace de démonstration (cwd = dossier temporaire), avec un coffre de mots de
passe en mémoire : le vrai coffre de l'OS n'est ni lu ni écrit, et une IP publique simulée (aucun réseau).
Appelé par capture.py, jamais à la main."""

from __future__ import annotations

import sys

import keyring.backend

from clipper import accounts, network
from clipper.__main__ import main


class MemoryVault(keyring.backend.KeyringBackend):
    """Coffre en mémoire du processus : le mot de passe factice disparaît avec lui."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._items[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._items.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self._items.pop((service, username), None)


def fake_geo(url: str) -> dict[str, str]:
    """IP de documentation (RFC 5737) en France : la pastille réseau est verte et rien ne sort de la machine."""
    return {"ip": "203.0.113.7", "city": "Paris", "country": "FR", "org": "AS64500 Exemple"}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage : serve_demo.py <compte> <mot_de_passe_factice> <port>")
    vault = MemoryVault()
    vault.set_password(accounts.SERVICE, sys.argv[1], sys.argv[2])
    accounts.use_backend(vault)
    network.use_fetcher(fake_geo)
    raise SystemExit(main(["serve", "--port", sys.argv[3]]))
