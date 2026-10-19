"""Premier envoi après un profil « Crashed » (TASK-3c307527aa76) : Chrome restaure puis referme des onglets
au démarrage ; ``context.pages[0]`` était cet onglet, d'où « Target page, context or browser has been closed ».
YouTube doit ouvrir un onglet neuf (``browser.fresh_page``), comme TikTok (TASK-08c8c051855c)."""
from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from clipper import youtube
from clipper.config import Config
from test_youtube import FakeStudio

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
CLOSED = "Target page, context or browser has been closed"


class RestoredTab:
    """Onglet restauré par Chrome sur un profil Crashed : toute utilisation échoue, il se referme tout seul."""

    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise RuntimeError(CLOSED)


class CrashedProfileContext:
    """Contexte dont le seul onglet présent au démarrage est l'onglet restauré ; ``new_page`` ouvre la page neuve."""

    def __init__(self, page, restored):
        self.page, self.pages = page, [restored]
        self.opened_new = 0

    def new_page(self):
        self.opened_new += 1
        return self.page


def _config(tmp_path) -> Config:
    return Config(mode="review", workspace_dir=tmp_path / "w", output_dir=tmp_path / "o")


@pytest.fixture
def crashed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    restored, page = RestoredTab(), FakeStudio()
    contexts = []

    @contextmanager
    def opener(account, *, headless):
        context = CrashedProfileContext(page, restored)
        contexts.append(context)
        yield context

    return {"restored": restored, "page": page, "contexts": contexts, "opener": opener, "tmp_path": tmp_path}


def test_the_first_login_check_after_a_crash_does_not_use_the_restored_tab(crashed):
    result = youtube.verify_login("ma_chaine", config=_config(crashed["tmp_path"]), now=NOW,
                                  opener=crashed["opener"], sleep=lambda s: None, rng=random.Random(1))

    assert result["ready"] is True
    assert crashed["contexts"][0].opened_new == 1
    assert crashed["restored"].close_calls == 1
    assert ("goto", youtube.load_selectors()["urls"]["studio"]) in crashed["page"].calls


def test_the_first_publish_after_a_crash_does_not_use_the_restored_tab(crashed):
    tmp_path = crashed["tmp_path"]
    mp4 = tmp_path / "04.mp4"
    mp4.write_bytes(b"mp4")
    clip = {"video_path": mp4, "caption": "Une légende", "hashtags": ["#jeu"], "screen_title": "Le titre"}

    with pytest.raises(youtube.YouTubeStop) as caught:  # le faux Studio ne va pas jusqu'au bout de l'envoi
        youtube.publish(clip, "ma_chaine", mode="immediate", config=_config(tmp_path), now=NOW,
                        opener=crashed["opener"], sleep=lambda s: None, rng=random.Random(1))

    assert CLOSED not in str(caught.value)
    assert crashed["contexts"][0].opened_new == 1
    assert crashed["restored"].close_calls == 1
    assert crashed["page"].calls, "la page neuve doit être celle qui pilote l'envoi"
