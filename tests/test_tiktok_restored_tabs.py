"""Premier envoi après un profil « Crashed » (TASK-08c8c051855c) : Chrome restaure puis referme des onglets
au démarrage ; ``context.pages[0]`` était cet onglet, d'où « Target page, context or browser has been closed »."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from clipper import browser, tiktok
from test_tiktok import Env, StudioPage, env  # noqa: F401  (fixture et aides réutilisées)

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
    def __init__(self, page, restored):
        self.page, self.pages = page, list(restored)
        self.opened_new = 0

    def new_page(self):
        self.opened_new += 1
        return self.page


def test_fresh_page_opens_a_new_tab_then_closes_the_restored_ones():
    restored = [RestoredTab(), RestoredTab()]
    context = CrashedProfileContext("neuve", restored)

    assert browser.fresh_page(context) == "neuve"
    assert context.opened_new == 1
    assert [t.close_calls for t in restored] == [1, 1]


def test_fresh_page_survives_a_restored_tab_chrome_already_closed():
    class Gone(RestoredTab):
        def close(self):
            raise RuntimeError(CLOSED)

    assert browser.fresh_page(CrashedProfileContext("neuve", [Gone()])) == "neuve"


def test_fresh_page_never_closes_the_page_it_returns():
    class SamePage:
        closed = False

        def close(self):
            self.closed = True

    page = SamePage()

    class Ctx:
        pages = [page]

        def new_page(self):
            return page

    assert browser.fresh_page(Ctx()) is page
    assert page.closed is False


@pytest.fixture
def crashed_env(env):
    restored = RestoredTab()
    contexts = []

    @contextmanager
    def opener(account, *, headless):
        env.opened.append((account, headless))
        context = CrashedProfileContext(env.page, [restored])
        contexts.append(context)
        yield context

    env.opener = opener
    env.restored, env.contexts = restored, contexts
    return env


def test_the_first_publish_after_a_crash_does_not_use_the_restored_tab(crashed_env):
    result = crashed_env.publish()

    assert result["state"] == "published"
    assert crashed_env.contexts[0].opened_new == 1
    assert crashed_env.restored.close_calls == 1


def test_the_first_stats_fetch_after_a_crash_does_not_use_the_restored_tab(crashed_env):
    with pytest.raises(Exception) as caught:  # le faux Studio de publication n'a pas de page de stats
        tiktok.fetch_stats("ma_chaine", config=crashed_env.config, opener=crashed_env.opener)

    assert CLOSED not in str(caught.value)
    assert crashed_env.contexts[0].opened_new == 1
