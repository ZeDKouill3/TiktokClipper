from __future__ import annotations

import sys
import types

import cv2
import pytest

# OpenCV lance par defaut autant de threads internes (Sobel, cvtColor, resize,
# imread/imencode) que de coeurs logiques. Sous pytest-xdist, chaque worker
# fait la meme chose : N workers x jusqu'a cv2.getNumThreads() threads natifs
# sur une machine a 12 coeurs logiques sature largement le CPU, ce qui degrade
# le passage a l'echelle et starve l'ordonnancement des threads Python des
# tests bases sur le temps reel (ConcurrencyTracker). Un thread OpenCV par
# worker laisse la parallelisation reelle a pytest-xdist (process-level).
cv2.setNumThreads(1)


@pytest.fixture(autouse=True)
def _no_real_geolocation():
    """Aucun test ne lit le vrai service de géolocalisation (TASK-30cc) : la garde du navigateur piloté
    voit une IP française simulée. Les tests de clipper.network branchent leur propre lecture."""
    from clipper import network

    network.reset()
    network.use_fetcher(lambda url: {"ip": "192.0.2.1", "country": "FR", "city": "Test", "org": "test"})
    yield
    network.use_fetcher(None)
    network.reset()


@pytest.fixture(autouse=True)
def _journal_in_tmp(tmp_path, monkeypatch):
    """Le journal global (clipper.journal) ne s'ecrit jamais dans le vrai logs/ du depot (TASK-8f03) :
    le handler installe pendant un test (clipper.__main__, TestClient, worker) par ``journal.install``
    avec un dossier relatif (le "logs" par defaut, resolu contre le cwd du depot) est redirige sous
    tmp_path. Un dossier absolu (tests qui choisissent leur dossier) reste tel quel ; CONFIG_DEFAULTS
    n'est pas modifie (test_config verifie la valeur reelle)."""
    from pathlib import Path

    from clipper import journal

    real_install = journal.install

    class _TmpJournalConfig:
        def __init__(self, config):
            self._config = config

        def section(self, name):
            values = self._config.section(name)
            if name == "journal" and not Path(values["dir"]).is_absolute():
                values = {**values, "dir": str(tmp_path / values["dir"])}
            return values

    monkeypatch.setattr(journal, "install", lambda kind, config: real_install(kind, _TmpJournalConfig(config)))


@pytest.fixture(autouse=True)
def _learning_memos_reset(monkeypatch):
    """Les memoires « dernier calcul » de clipper.learning (bilan de veille, alertes « 0 vue ») sont des dicts de
    module indexes par chemin de fichier d'etat. Avec ``tmp_path_retention_policy = "failed"``, deux tests dont le nom
    partage ses 30 premiers caracteres recoivent le meme tmp_path l'un apres l'autre : sans remise a zero, le second
    herite du « dernier calcul » du premier (test rouge selon l'ordre, suite complete du 19/11)."""
    from clipper import learning

    monkeypatch.setattr(learning, "_veille_report_computed", {})
    monkeypatch.setattr(learning, "_zero_views_computed", {})


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run a test with cwd set to an empty temp dir, so config/workspace
    lookups never touch the real repo's config.toml or workspace/."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def fake_ctranslate2(monkeypatch):
    """Install a fake ``ctranslate2`` module in sys.modules so clipper.gpu
    can be tested against CUDA-available, CUDA-unavailable and erroring
    cases without the real (heavy, GPU-requiring) dependency."""

    def _install(cuda_device_count=None, raises=None):
        module = types.ModuleType("ctranslate2")

        def _get_cuda_device_count():
            if raises is not None:
                raise raises
            return cuda_device_count

        module.get_cuda_device_count = _get_cuda_device_count
        monkeypatch.setitem(sys.modules, "ctranslate2", module)
        return module

    return _install


@pytest.fixture
def no_ctranslate2(monkeypatch):
    """Ensure ``import ctranslate2`` raises ImportError, simulating an
    environment where ctranslate2 isn't installed at all."""
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
