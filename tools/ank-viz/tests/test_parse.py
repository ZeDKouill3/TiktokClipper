"""Parsing des sorties enregistrées de `ank ... --json` et de git."""
import json
from pathlib import Path

import ankviz

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


def test_find_keeps_tasks_adrs_specs_and_drops_logs():
    entities = ankviz.parse_find(fixture("find.json"))
    kinds = {e["kind"] for e in entities}
    assert kinds == {"task", "adr", "spec"}
    assert len([e for e in entities if e["kind"] == "task"]) == 19
    assert len([e for e in entities if e["kind"] == "adr"]) == 5
    assert len([e for e in entities if e["kind"] == "spec"]) == 2


def test_find_reads_claim_holder_from_state():
    entities = {e["id"]: e for e in ankviz.parse_find(fixture("find.json"))}
    viz = entities["TASK-7aca619df8f4"]
    assert viz["status"] == "in_progress"
    assert viz["claimed_by"] == "UP60041549@wl0023729"
    assert viz["short"] == "TASK-7aca"
    assert entities["ADR-49cdd4316d6e"]["short"] == "ADR-49cd"
    assert entities["TASK-7291d843d843"]["claimed_by"] is None


def test_find_corpus_hash():
    assert ankviz.parse_corpus(fixture("find.json")) == "7b7540274850aa4345191a1a5a0189cbd1f31665"


def test_show_extracts_done_criteria_block():
    crit = ankviz.parse_criterion(fixture("show_TASK-7291.json"))
    assert crit.startswith("Pour chaque clip, l'étape construit")
    assert crit.rstrip().endswith("durée attendue +/-0,1 s.")
    crit2 = ankviz.parse_criterion(fixture("show_TASK-7aca.json"))
    assert crit2.startswith("'python tools/ank-viz/server.py' lance un serveur local")
    assert crit2.rstrip().endswith("testé sur des sorties enregistrées.")


def test_show_without_criterion_gives_empty_string():
    assert ankviz.parse_criterion(fixture("show_ADR-09ad.json")) == ""


def test_graph_edges():
    edges = ankviz.parse_edges(fixture("graph.json"))
    assert ("TASK-0caecdbe981e", "TASK-4ca09185579f") in edges
    assert ("TASK-634e1b914db5", "TASK-155751618fa8") in edges
    assert all(len(e) == 2 for e in edges)


def test_status_default_branch():
    st = ankviz.parse_status(fixture("status.json"))
    assert st["default_branch"] == "main"
    assert st["branch"] == "task/TASK-7aca-ank-viz"


def test_for_each_ref_local_and_remote():
    refs = ankviz.parse_refs(fixture("for-each-ref.txt"))
    names = {(r["name"], r["remote"]) for r in refs}
    assert names == {("main", False), ("task/TASK-7aca-ank-viz", False), ("origin/main", True)}
    main = next(r for r in refs if r["name"] == "main")
    assert main["sha"] == "ec791ef"
    assert main["subject"] == "ratify SPEC-08c28e46a632"
    assert main["date"] == "2026-09-25T10:44:16+01:00"
    assert main["ref"] == "refs/heads/main"


def test_origin_head_symref_is_skipped():
    text = "refs/remotes/origin/HEAD\tec791ef\t2026-09-25T10:44:16+01:00\tx\tratify\n"
    assert ankviz.parse_refs(text) == []


def test_rev_list_left_right_is_behind_then_ahead():
    assert ankviz.parse_ahead_behind(fixture("rev-list.txt")) == {"ahead": 0, "behind": 0}
    assert ankviz.parse_ahead_behind("3\t5\n") == {"ahead": 5, "behind": 3}
