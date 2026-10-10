"""Jury de juges IA pour les decisions de jugement du mode auto (ADR-ff87).

Bibliotheque, pas une etape : les etapes l'appellent, elle n'importe aucune
etape (ADR-b16b) et passe par clipper.llm pour chaque appel (ADR-b1c1).

    from clipper import jury
    result = jury.deliberate(
        [{"id": "m3", "text": "...", "context": "[812.4-851.0] s, single"}],
        rubric,                        # {"criteria": {nom: {"weight", "question"}}, "trend_keywords"?}
        context="Titre, chaine, signaux de la video...",
    )

Deroulement :
1. Tour 1, a l'aveugle : chaque juge configure note tous les candidats en un
   seul appel (usage ``jury_<nom>``). Les candidats sont anonymises (C1, C2...
   dans l'ordre ou le juge les voit) et melanges de facon deterministe, avec
   une graine partagee par tous les juges d'un meme ``model`` configure
   (``seed``) : ils voient donc les candidats dans le meme ordre. Un prompt
   de juge place le bloc commun (intro, grille, contexte, candidats,
   consignes generiques) EN PREMIER et les consignes propres au role
   (perspective, veto) EN DERNIER ; ce bloc commun est alors identique octet
   pour octet entre juges d'un meme modele, et transmis a clipper.llm comme
   ``cache_prefix`` (voir ``_ask``) pour qu'un backend qui sait relire un
   cache de blocs le marque comme cacheable (TASK-2cbb) -- un prefixe
   textuel identique seul ne suffit pas, le fournisseur ne relit que des
   blocs, jamais un prefixe de caracteres a l'interieur d'un bloc unique.
   Le backend claude-cli (defaut) ignore ce marquage depuis TASK-b384 : un
   400 "A maximum of 4 blocks with cache_control" intermittent et hors de
   notre controle lui etait imputable (voir clipper/llm/claude_cli.py).
   Les appels d'un tour partent en 2 vagues : un juge par modele d'abord (le
   "leader"), attendu jusqu'au bout, puis les autres juges de ce tour ;
   chaque vague en parallele.
2. Desaccord : un candidat dont les scores par juge (0-100, grille ponderee)
   s'ecartent de plus de ``threshold``, ou dont au moins un juge a une
   confiance < ``debate_confidence_below`` (SPEC-73d0, R2), passe au debat.
   Chaque juge donne, pour chaque candidat et a chaque tour, une confiance
   entiere de 0 (au hasard) a 100 (certain), exigee par le schema (R1).
3. Tour 2 (un seul) sur ces candidats : chaque juge relit ses notes et son
   argument, puis les arguments anonymes des autres, et peut reviser.
4. Agregation : mediane par critere des notes finales de chaque juge, score
   = moyenne ponderee des medianes x10. La mediane est ponderee par
   (poids de calibration du juge, 1 s'il est absent du fichier de poids de
   clipper.jury_calibration, ``weights_path``, defaut state/jury_weights.json)
   x max(confiance/100, ``min_confidence_weight``) (SPEC-73d0, R3) : a
   confiances egales, c'est la mediane d'avant. Un juge a veto doit valoir 1
   dans le fichier de poids, et un fichier illisible est une JuryError
   (ADR-1cf0, ADR-ad2e).
   Le veto motive d'un juge ``veto`` (tour final) rejette le candidat : c'est
   a l'etape d'en tirer la consequence (ici on ne supprime rien).

Retour (serialisable en JSON, a ecrire dans le JSON de l'etape) :

    {"judges": [{"name", "usage", "model", "veto"}], "seed", "threshold",
     "debate_confidence_below", "min_confidence_weight",
     "quorum", "weights": None | {nom: poids},
     "failed": [{"judge", "round", "error"}], "debated": [id],
     "candidates": [{"id", "scores": {critere: mediane}, "score",
                     "confidence", "veto": None | {"judge", "reason"}, "debated",
                     "trace": {"rounds": [{"round", "judges": {nom: {"scores",
                               "score", "argument", "confidence",
                               ("veto", "veto_reason")}}}],
                               "revisions": [{"judge", "criterion", "from",
                                              "to", "argument"}],
                               "dissent": [{"judge", "score", "median"}]}}]}

``candidates`` garde l'ordre d'entree. ``confidence`` d'un candidat : mediane
des confiances finales des juges. ``dissent`` : juges dont le score
final s'ecarte de la mediane des scores de plus de ``threshold``.

Echecs (ADR-ad2e) : une reponse de juge invalide (JSON, schema, candidat
manquant ou en double, veto sans raison) leve llm.SchemaError. Avec
``quorum`` configure, un juge invalide est ecarte (trace dans ``failed``)
tant qu'au moins ``quorum`` juges repondent correctement a ce tour ; sous le
quorum, JuryError. Un juge ``veto`` reste toujours obligatoire (sinon la
conformite sauterait en silence), et les erreurs transitoires (quota,
reseau) ou d'appel remontent toujours, quel que soit le quorum : la video
repart en file d'attente. Au tour 2, un juge ecarte garde ses notes du
tour 1, sans revision.

Configuration ([jury], fusionnee en profondeur avec CONFIG_DEFAULTS) :

    [jury]
    threshold = 20          # ecart de score (0-100) qui declenche le debat
    debate_confidence_below = 40  # confiance (0-100) d'un juge qui declenche le debat
    min_confidence_weight = 0.2   # plancher du poids de confiance (jamais zero)
    quorum = 4              # facultatif : juges valides minimum par tour
    seed = 0

    [jury.judges.retention]
    model = "sonnet"        # niveau (strong|fast) ou nom de modele

    [jury.judges.avocat]
    enabled = false

    [jury.judges.historien] # juge ajoute : perspective obligatoire
    perspective = "..."

``model`` d'un juge prime sur [llm.usages.jury_<nom>] ; sans ``model``,
c'est [llm] qui decide pour cet usage.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from clipper import llm

_RETENTION = (
    "Retention. Tu raisonnes comme l'algorithme de TikTok : un clip vit ou meurt sur le temps "
    "de visionnage. Demande-toi : la toute premiere phrase (les 3 premieres secondes) cree-t-elle "
    "une tension, une question ou une promesse qui oblige a rester ? Le rythme tient-il sans temps "
    "mort ni tunnel d'explication ? La fin donne-t-elle envie de revoir, de commenter ou de "
    "partager ? Un debut lent ou une chute molle font decrocher : sanctionne-les nettement, meme "
    "si le fond est interessant."
)

_SPECTATEUR = (
    "Spectateur cible. Tu as 16-30 ans, tu scrolles ton fil TikTok en France le soir, le pouce pret "
    "a passer. Tu suis le sujet de la video sans en etre expert et tu ne connais ni la chaine ni la "
    "personne qui parle. Lis chaque candidat comme s'il apparaissait dans ton fil : je m'arrete ou "
    "je scrolle, et a quel mot je decroche ? Est-ce que je like, je commente, je l'envoie a un "
    "pote ? Juge avec tes reactions de spectateur, pas avec un regard de professionnel : ce qui "
    "t'ennuie ou te perd ennuie et perd le public."
)

_MONTEUR = (
    "Monteur. Tu dois publier ce passage tel quel, coupe au debut et a la fin, sans voix off ni "
    "carton d'explication. Verifie : se comprend-il seul (aucun \"comme je disais\", aucun "
    "\"il\" ou \"ca\" dont on ignore a quoi il renvoie, aucune reference a ce qui precede) ? "
    "Commence-t-il directement sur l'accroche plutot que sur une mise en contexte ? Finit-il sur "
    "une phrase complete et une chute nette (punchline, revelation, conclusion), pas au milieu "
    "d'une idee ? Un contexte indispensable qui manque doit se voir dans standalone et payoff."
)

_AVOCAT = (
    "Avocat du diable. Ton role est de trouver ce qui fera echouer le clip, pas de l'aimer. Cherche "
    "le defaut le plus grave : contexte manquant, accroche qui promet plus que la suite ne donne, "
    "info deja vue partout, blague qui ne marche que si l'on connait le createur, passage mou au "
    "milieu, fin qui tombe a plat. Ne mets une note haute que si tu n'as rien trouve de serieux "
    "apres avoir vraiment cherche, et nomme toujours le principal defaut dans ton argument, meme "
    "pour un bon candidat."
)

_CONFORMITE = (
    "Conformite. Tu verifies que le clip peut etre publie sans risque reel de ban ou de perte de "
    "portee sur TikTok en France, pas que le sujet est consensuel. Un propos clivant, polemique ou "
    "choquant tenu par une personnalite publique ou un invite dans un debat, une interview, une "
    "emission ou un discours public, rapporte tel quel, N'EST PAS un motif de veto : c'est le contenu "
    "recherche, meme s'il vise une religion, une origine ou un groupe. \"Haine\" seule n'est plus un "
    "motif : nomme le risque precis ci-dessous.\n\n"
    "Pose un veto seulement pour un risque reel et precis, que tu cites dans veto_reason (le passage, "
    "le risque), parmi : appel explicite a la violence ou au harcelement contre une personne ou un "
    "groupe hors debat contradictoire ; mineur identifiable ; contenu sexuel ; violence graphique "
    "gratuite ; incitation a un acte dangereux ; diffamation (accusation precise et non etayee contre "
    "une personne reelle identifiable) ; clip qui repose sur une oeuvre protegee (musique, film, "
    "extrait d'une autre chaine). Un sujet sensible traite normalement, un gros mot ou une pique "
    "legere ne suffisent pas. Note aussi la grille avec ton regard : un clip qui frole une vraie "
    "limite perd de sa valeur."
)

CONFIG_DEFAULTS: dict[str, object] = {
    # Composition du jury (ADR-ff87). Chaque juge : usage LLM, modele
    # (niveau strong|fast ou nom, prime sur [llm.usages.<usage>]), veto,
    # perspective (le prompt propre au juge). Modeles varies : diversite.
    "judges": {
        "retention": {"usage": "jury_retention", "model": "strong", "veto": False, "perspective": _RETENTION},
        "spectateur": {"usage": "jury_spectateur", "model": "fast", "veto": False, "perspective": _SPECTATEUR},
        "monteur": {"usage": "jury_monteur", "model": "strong", "veto": False, "perspective": _MONTEUR},
        "avocat": {"usage": "jury_avocat", "model": "strong", "veto": False, "perspective": _AVOCAT},
        "conformite": {"usage": "jury_conformite", "model": "fast", "veto": True, "perspective": _CONFORMITE},
    },
    # Ecart (points sur 100) entre le score le plus haut et le plus bas des
    # juges au-dela duquel un candidat passe au debat.
    "threshold": 20,
    # Un candidat passe aussi au debat si un juge a une confiance (0-100)
    # strictement inferieure a ce seuil (SPEC-73d0, R2).
    "debate_confidence_below": 40,
    # Plancher du poids de confiance (confiance/100) dans la mediane ponderee
    # (SPEC-73d0, R3) : aucun juge n'est jamais reduit a zero. ]0, 1].
    "min_confidence_weight": 0.2,
    # Nombre minimal de juges valides par tour ; absent : tous obligatoires.
    "quorum": None,
    # Graine du melange des candidats (propre a chaque juge).
    "seed": 0,
    # Appels LLM simultanes (un par juge et par tour).
    "parallel": 5,
}

_JUDGE_KEYS = {"enabled", "usage", "model", "veto", "perspective"}
_MIN_JUDGES = 3
_ARGUMENT_CHARS = 500


class JuryError(Exception):
    """Configuration du jury ou candidats invalides, ou quorum non atteint."""


# --------------------------------------------------------------------------
# Reglages
# --------------------------------------------------------------------------


def _deep_merge(base: Mapping[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in top.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def perspective_sha(perspective: str) -> str:
    """Empreinte d'une perspective : 12 premiers caracteres hexa du sha1 du texte exact (UTF-8).

    Permet de dire, apres coup, quelle version d'un juge a note un moment (TASK-4e58554d15d3)."""
    return hashlib.sha1(perspective.encode("utf-8")).hexdigest()[:12]


def _judges(settings: dict[str, Any]) -> list[dict[str, Any]]:
    judges = []
    for name, entry in settings["judges"].items():
        unknown = set(entry) - _JUDGE_KEYS
        if unknown:
            raise JuryError(f"[jury.judges.{name}] : cle(s) inconnue(s) {sorted(unknown)}")
        if not entry.get("enabled", True):
            continue
        perspective = entry.get("perspective")
        if not isinstance(perspective, str) or not perspective.strip():
            raise JuryError(f"[jury.judges.{name}] : perspective manquante")
        judges.append(
            {
                "name": name,
                "usage": entry.get("usage", f"jury_{name}"),
                "model": entry.get("model"),
                "veto": bool(entry.get("veto", False)),
                "perspective": perspective,
            }
        )
    if len(judges) < _MIN_JUDGES:
        raise JuryError(f"il faut au moins {_MIN_JUDGES} juges actifs, {len(judges)} configure(s)")
    if not any(j["veto"] for j in judges):
        raise JuryError("aucun juge actif n'a le veto : le juge conformite (veto = true) est obligatoire (ADR-ff87)")
    quorum = settings["quorum"]
    if quorum is not None and not (isinstance(quorum, int) and 1 <= quorum <= len(judges)):
        raise JuryError(f"[jury] quorum invalide : {quorum!r} (entier de 1 a {len(judges)})")
    return judges


def _confidence_settings(settings: Mapping[str, Any]) -> tuple[float, float]:
    """(debate_confidence_below, min_confidence_weight), valides (ADR-ad2e)."""
    below = settings["debate_confidence_below"]
    if isinstance(below, bool) or not isinstance(below, (int, float)) or not 0 <= below <= 100:
        raise JuryError(f"[jury] debate_confidence_below invalide : {below!r} (nombre de 0 a 100)")
    floor = settings["min_confidence_weight"]
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 < floor <= 1:
        raise JuryError(f"[jury] min_confidence_weight invalide : {floor!r} (nombre dans ]0, 1])")
    return float(below), float(floor)


class _JudgeConfig:
    """Vue de la config pour un juge : son ``model`` remplace celui de
    [llm.usages.<usage>], le reste est la config d'origine."""

    def __init__(self, config: Any, usage: str, model: str | None):
        self._config, self._usage, self._model = config, usage, model

    def section(self, name: str) -> dict[str, Any]:
        table = self._config.section(name)
        if name != "llm" or not self._model:
            return table
        usages = dict(table.get("usages", {}))
        usages[self._usage] = {**usages.get(self._usage, {}), "model": self._model}
        return {**table, "usages": usages}

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._config, attr)


def _check_candidates(candidates: Sequence[Mapping[str, Any]]) -> None:
    seen = set()
    for n, c in enumerate(candidates):
        cid = c.get("id")
        if not isinstance(cid, str) or not cid:
            raise JuryError(f"candidat {n} : id manquant")
        if cid in seen:
            raise JuryError(f"candidat {n} : id {cid!r} en double")
        seen.add(cid)
        if not isinstance(c.get("text"), str) or not c["text"].strip():
            raise JuryError(f"candidat {cid!r} : texte manquant")


# --------------------------------------------------------------------------
# Prompts et schemas
# --------------------------------------------------------------------------


def _schema(criteria: Mapping[str, Any], refs: list[str], veto: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "ref": {"type": "string", "enum": refs},
        "argument": {
            "type": "string", "minLength": 1, "maxLength": _ARGUMENT_CHARS,
            "description": "Une ou deux phrases concretes, qui citent le passage decisif.",
        },
        "confidence": {
            "type": "integer", "minimum": 0, "maximum": 100,
            "description": "Ta confiance dans ces notes : 0 = au hasard, 100 = certain.",
        },
        "scores": {
            "type": "object",
            "description": "Note entiere de 0 a 10 par critere de la grille.",
            "properties": {
                name: {"type": "integer", "minimum": 0, "maximum": 10, "description": c["question"]}
                for name, c in criteria.items()
            },
            "required": list(criteria),
            "additionalProperties": False,
        },
    }
    if veto:
        item["veto"] = {"type": "boolean", "description": "true seulement pour un risque reel et precis."}
        item["veto_reason"] = {
            "type": "string", "maxLength": _ARGUMENT_CHARS,
            "description": "Si veto : le passage et le risque ; sinon chaine vide.",
        }
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": len(refs),
                "maxItems": len(refs),
                "items": {
                    "type": "object",
                    "properties": item,
                    "required": list(item),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _grid_text(rubric: Mapping[str, Any]) -> str:
    criteria = "\n".join(f"- {name} : {c['question']}" for name, c in rubric["criteria"].items())
    keywords = rubric.get("trend_keywords")
    trend = f"Mots-cles tendance : {', '.join(keywords)}\n" if keywords else ""
    return (
        "## Grille : une note entiere de 0 a 10 par critere\n"
        f"{criteria}\n{trend}"
        "Echelle : 0-2 absent, 3-4 faible, 5-6 correct, 7-8 fort, 9-10 exceptionnel (rare). "
        "Une note gonflee fait publier un mauvais clip, une note ecrasee en fait perdre un bon : "
        "sers-toi de toute l'echelle.\n"
    )


def _common(n_judges: int, rubric: Mapping[str, Any], context: str) -> str:
    """Bloc commun a tous les juges (intro, grille, contexte) : aucune donnee
    propre a un juge, pour que ce bloc soit identique octet pour octet entre
    juges (memes candidats a la suite : voir ``_round1_prompt``)."""
    return (
        f"Tu fais partie d'un jury de {n_judges} juges qui decide quels extraits d'une video longue "
        "deviennent des clips TikTok pour un public francophone. Chaque juge a sa perspective ; les "
        "notes sont agregees par mediane. Reste strictement dans ta perspective : c'est elle qui rend "
        "le jury utile.\n\n"
        + _grid_text(rubric)
        + (f"\n## Contexte de la video\n{context}\n" if context else "")
    )


def _role(judge: dict[str, Any]) -> str:
    """Consignes propres au juge (perspective, veto) : placees en fin de
    prompt (voir ``_round1_prompt``/``_round2_prompt``) pour que le bloc
    commun qui precede reste inchange d'un juge a l'autre."""
    veto = (
        "Tu es le seul juge a pouvoir poser un veto : veto = true rejette le candidat quelles que "
        "soient les notes, veto_reason dit pourquoi.\n\n"
        if judge["veto"]
        else ""
    )
    return f"## Ta perspective\n{judge['perspective']}\n\n" + veto


def _block(ref: str, candidate: Mapping[str, Any]) -> str:
    ctx = candidate.get("context")
    return f"### {ref}\n" + (f"Contexte : {ctx}\n" if ctx else "") + f"Texte : « {candidate['text']} »"


def _schema_block(schema: Mapping[str, Any]) -> str:
    """Consigne de format place avant le role (voir _round1_prompt et
    _round2_prompt) : le schema (--json-schema) fait ainsi partie du
    prefixe identique entre juges d'un meme modele (TASK-b0fa), pour que le
    fournisseur du modele puisse relire son cache de prompt au lieu de le
    reecrire a chaque juge. llm.ask ajoute de toute facon sa propre consigne
    de schema a la toute fin du prompt (ADR-b1c1) : redondant mais sans
    consequence, puisque cette fin vient apres le role, deja divergent."""
    return (
        "\n## Format de reponse\nReponds avec un JSON conforme a ce schema, sans texte ni balise "
        f"autour :\n{json.dumps(schema, ensure_ascii=False)}\n\n"
    )


def _round1_prompt(
    common: str, shown: list[tuple[str, Mapping[str, Any]]], role: str, schema: Mapping[str, Any]
) -> str:
    return (
        common
        + f"\n## Candidats ({len(shown)}), dans un ordre aleatoire\n\n"
        + "\n\n".join(_block(ref, c) for ref, c in shown)
        + "\n\n## Consignes\n"
        "Note chaque candidat sur son seul texte, independamment des autres et de sa place dans la "
        "liste. Pour chacun : ref, puis argument (une ou deux phrases concretes qui citent entre "
        "guillemets le passage decisif et disent ce qui marche ou bloque de ton point de vue, sans "
        "formule generique), puis confidence (entier de 0 a 100 : 0 = tu notes au hasard, 100 = tu "
        "es certain ; sois honnete, un candidat ambigu ou hors de ta competence merite une confiance "
        "basse), puis les notes. Un element par candidat, sans en omettre.\n"
        + _schema_block(schema)
        + role
    )


def _round2_prompt(
    common: str,
    shown: list[tuple[str, Mapping[str, Any]]],
    own: dict[str, dict[str, Any]],
    others: dict[str, list[str]],
    role: str,
    veto: bool,
    schema: Mapping[str, Any],
) -> str:
    blocks = []
    for ref, c in shown:
        mine = own[ref]
        notes = ", ".join(f"{k} {v}" for k, v in mine["scores"].items())
        heard = "\n".join(f"- Avis {n} : {arg}" for n, arg in enumerate(others[ref], 1))
        vetoed = f"Ton veto au tour 1 : {mine['veto_reason']}\n" if veto and mine["veto"] else ""
        blocks.append(
            _block(ref, c)
            + f"\nTes notes au tour 1 : {notes}\nTa confiance au tour 1 : confiance {mine['confidence']}/100\n"
            f"Ton argument : {mine['argument']}\n"
            + vetoed
            + f"Autres avis :\n{heard}"
        )
    return (
        common
        + "\n## Debat\nLe jury diverge sur les candidats ci-dessous. Pour chacun, tu retrouves tes "
        "notes et ton argument du premier tour, puis les arguments des autres juges, anonymes et "
        "dans un ordre aleatoire. Lis-les honnetement en restant dans ta perspective. Revise une note "
        "seulement si un argument t'apporte un fait precis que tu avais manque ou mal lu dans le "
        "texte ; ne t'aligne jamais pour faire consensus ni parce qu'un avis revient souvent.\n\n"
        + "\n\n".join(blocks)
        + "\n\n## Consignes\nPour chaque candidat : ref, puis argument (ce qui a change et pourquoi, "
        "ou pourquoi tu maintiens, en une ou deux phrases), puis ta confidence (0 a 100) apres "
        "debat, puis toutes tes notes, revisees ou non."
        + (" Redonne aussi veto et veto_reason, maintenus ou leves." if veto else "")
        + "\n"
        + _schema_block(schema)
        + role
    )


# --------------------------------------------------------------------------
# Appels
# --------------------------------------------------------------------------


def _ask(
    judge: dict[str, Any],
    prompt: str,
    cache_prefix: str,
    schema: Mapping[str, Any],
    config: Any,
) -> dict[str, dict[str, Any]]:
    """ref -> {"scores", "argument", "confidence", ("veto", "veto_reason")} ; toute reponse
    incomplete ou incoherente est une llm.SchemaError. ``schema`` peut
    imposer veto/veto_reason meme a un juge sans veto (partage par son
    modele, TASK-b0fa) : seul ``judge["veto"]`` decide si on en tient
    compte. ``cache_prefix`` (le prompt prive de son role, identique entre
    juges d'un meme modele) va a clipper.llm pour qu'un backend qui le
    supporte le marque comme bloc cacheable (TASK-2cbb) : ignore par
    claude-cli depuis TASK-b384 (voir clipper/llm/claude_cli.py)."""
    def check(answer: dict[str, Any]) -> None:
        # Controles que le schema ne sait pas exprimer : renvoyes au meme juge
        # pour correction (repair_attempts) au lieu d'echouer l'etape.
        seen: set[str] = set()
        for item in answer["candidates"]:
            ref = item["ref"]
            if ref in seen:
                raise llm.SchemaError(f"juge {judge['name']} : {ref} note deux fois")
            seen.add(ref)
            confidence = item.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
                raise llm.SchemaError(
                    f"juge {judge['name']} : confiance invalide sur {ref} : {confidence!r} (entier de 0 a 100)"
                )
            if judge["veto"] and item["veto"] and not item["veto_reason"].strip():
                raise llm.SchemaError(f"juge {judge['name']} : veto sans raison sur {ref}")

    answer = llm.ask(
        judge["usage"],
        prompt,
        [],
        schema,
        config=_JudgeConfig(config, judge["usage"], judge["model"]),
        cache_prefix=cache_prefix,
        check=check,
    )
    out: dict[str, dict[str, Any]] = {}
    for item in answer["candidates"]:
        ref = item["ref"]
        entry = {"scores": dict(item["scores"]), "argument": item["argument"], "confidence": item["confidence"]}
        if judge["veto"]:
            entry["veto"] = item["veto"]
            entry["veto_reason"] = item["veto_reason"] if item["veto"] else ""
        out[ref] = entry
    return out


def _model_veto_flags(judges: list[dict[str, Any]]) -> dict[Any, bool]:
    """``judge["model"]`` -> True si un juge au moins de ce modele a
    veto=True. Sert a batir un schema identique pour tout juge d'un meme
    modele (TASK-b0fa, prefixe de prompt commun) : un juge sans veto dont le
    modele en compte un repond quand meme sur veto/veto_reason, ignores par
    _ask (seul son propre judge["veto"] compte pour l'exploiter)."""
    flags: dict[Any, bool] = {}
    for j in judges:
        flags[j["model"]] = flags.get(j["model"], False) or j["veto"]
    return flags


def _waves(judges: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Noms des juges en 2 vagues : un "leader" par ``model`` configure
    (premiere occurrence, dans l'ordre de ``judges``), puis le reste, pour
    ne pas envoyer tous les juges d'un tour en une seule salve parallele."""
    seen: set[Any] = set()
    leaders, others = [], []
    for judge in judges:
        key = judge["model"]
        if key not in seen:
            seen.add(key)
            leaders.append(judge["name"])
        else:
            others.append(judge["name"])
    return leaders, others


def _run_round(
    rnd: int,
    tasks: dict[str, Any],
    judges: list[dict[str, Any]],
    quorum: int | None,
    parallel: int,
    failed: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Lance ``tasks`` (nom de juge -> appel sans argument) en 2 vagues (voir
    ``_waves``), chaque vague en parallele, la 2e attendant la fin complete
    de la 1re ; renvoie nom -> reponse pour les juges valides, selon la regle
    du quorum."""
    present = [j for j in judges if j["name"] in tasks]
    by_name = {j["name"]: j for j in present}
    answers: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def _run_wave(names: list[str]) -> None:
        if not names:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(names)))) as executor:
            futures = {name: executor.submit(tasks[name]) for name in names}
        for name in names:
            try:
                answers[name] = futures[name].result()
            except llm.SchemaError as exc:
                if quorum is None or by_name[name]["veto"]:
                    raise
                errors[name] = exc

    for wave in _waves(present):
        _run_wave(wave)

    for name, exc in errors.items():
        failed.append({"judge": name, "round": rnd, "error": str(exc)})
    if errors and len(answers) < quorum:
        raise JuryError(
            f"tour {rnd} : quorum non atteint ({len(answers)} juge(s) valide(s) sur {len(tasks)}, "
            f"quorum {quorum}) : "
            + " ; ".join(f"{name} : {exc}" for name, exc in errors.items())
        )
    return answers


# --------------------------------------------------------------------------
# Agregation
# --------------------------------------------------------------------------


def _weights(config: Any, judges: list[dict[str, Any]]) -> dict[str, float] | None:
    """Poids par juge actif du fichier ecrit par clipper.jury_calibration
    (lu via sa section de config, sans l'importer), None s'il n'existe pas."""
    path = Path(config.section("jury_calibration")["weights_path"])
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        loaded = {name: float(entry["weight"]) for name, entry in data["judges"].items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise JuryError(f"poids du jury : {path} illisible : {exc}") from exc
    weights = {j["name"]: loaded.get(j["name"], 1.0) for j in judges}
    cal = config.section("jury_calibration")
    lo, hi = float(cal["min_weight"]), float(cal["max_weight"])
    for j in judges:
        w = weights[j["name"]]
        if not (math.isfinite(w) and w > 0):
            raise JuryError(f"poids du jury : {path} : poids invalide pour {j['name']} : {w!r}")
        if not lo <= w <= hi:
            raise JuryError(
                f"poids du jury : {path} : poids {w} de {j['name']} hors des bornes [{lo}, {hi}] (ADR-1cf0)"
            )
        if (j["veto"] or j["name"] == "conformite") and w != 1.0:
            raise JuryError(
                f"poids du jury : {j['name']} vaut {w} dans {path}, "
                "le juge conformite (ou a veto) garde le poids 1 (ADR-1cf0)"
            )
    return weights


def _weighted_median(values: Sequence[tuple[float, float]]) -> float:
    """Mediane de (valeur, poids) : premiere valeur ou le poids cumule
    atteint la moitie du total, moyenne avec la suivante si la moitie tombe
    pile sur la frontiere (egale a statistics.median a poids egaux)."""
    ordered = sorted(values)
    half = sum(w for _, w in ordered) / 2
    cumul = 0.0
    for n, (value, weight) in enumerate(ordered):
        cumul += weight
        if math.isclose(cumul, half):
            return (value + ordered[n + 1][0]) / 2
        if cumul > half:
            return value
    return ordered[-1][0]


def _score(scores: Mapping[str, float], criteria: Mapping[str, Any]) -> float:
    total = sum(c["weight"] for c in criteria.values())
    return round(sum(scores[name] * c["weight"] for name, c in criteria.items()) / total * 10, 1)


def _spread(notes: Mapping[str, dict[str, Any]], criteria: Mapping[str, Any]) -> float:
    values = [_score(n["scores"], criteria) for n in notes.values()]
    return max(values) - min(values)


def _round_record(rnd: int, notes: Mapping[str, dict[str, Any]], criteria: Mapping[str, Any]) -> dict[str, Any]:
    judges = {}
    for name, n in notes.items():
        entry = {
            "scores": n["scores"],
            "score": _score(n["scores"], criteria),
            "argument": n["argument"],
            "confidence": n["confidence"],
        }
        if "veto" in n:
            entry["veto"] = n["veto"]
            entry["veto_reason"] = n["veto_reason"]
        judges[name] = entry
    return {"round": rnd, "judges": judges}


def deliberate(
    candidates: Sequence[Mapping[str, Any]],
    rubric: Mapping[str, Any],
    *,
    context: str = "",
    config: Any = None,
) -> dict[str, Any]:
    """Fait juger ``candidates`` (id, text, context facultatif) par le jury
    configure, sur la grille ``rubric`` ; voir la docstring du module."""
    if config is None:
        from clipper.config import load_config

        config = load_config()
    settings = _deep_merge(CONFIG_DEFAULTS, config.section("jury"))
    judges = _judges(settings)
    model_veto = _model_veto_flags(judges)
    weights = _weights(config, judges)
    _check_candidates(candidates)
    criteria = rubric["criteria"]
    threshold = float(settings["threshold"])
    conf_below, conf_floor = _confidence_settings(settings)
    quorum = settings["quorum"]
    seed = settings["seed"]
    parallel = int(settings["parallel"])
    failed: list[dict[str, Any]] = []
    by_id = {c["id"]: c for c in candidates}
    common = _common(len(judges), rubric, context)

    # Tour 1 : ordre et refs propres a chaque juge (ADR-ff87 : pas de biais
    # de position partage entre juges d'un meme modele).
    views: dict[str, dict[str, str]] = {}  # juge -> ref -> id
    tasks = {}
    for judge in judges:
        ids = [c["id"] for c in candidates]
        random.Random(f"{seed}:{judge['name']}").shuffle(ids)
        refs = {f"C{n}": cid for n, cid in enumerate(ids, 1)}
        views[judge["name"]] = refs
        shown = [(ref, by_id[cid]) for ref, cid in refs.items()]
        schema = _schema(criteria, list(refs), model_veto[judge["model"]])
        role_text = _role(judge)
        prompt = _round1_prompt(common, shown, role_text, schema)
        cache_prefix = prompt.removesuffix(role_text)
        tasks[judge["name"]] = (
            lambda j=judge, p=prompt, c=cache_prefix, s=schema: _ask(j, p, c, s, config)
        )
    answers = _run_round(1, tasks, judges, quorum, parallel, failed) if candidates else {}
    active = [j for j in judges if j["name"] in answers]

    # roundN[id][juge] = {"scores", "argument", ("veto", "veto_reason")}
    round1 = {cid: {j["name"]: answers[j["name"]][_ref(views[j["name"]], cid)] for j in active} for cid in by_id}
    debated = [
        cid for cid in by_id
        if _spread(round1[cid], criteria) > threshold
        or any(n["confidence"] < conf_below for n in round1[cid].values())
    ]

    # Tour 2 : debat sur les seuls desaccords.
    round2: dict[str, dict[str, dict[str, Any]]] = {cid: {} for cid in debated}
    if debated:
        tasks = {}
        for judge in active:
            name = judge["name"]
            ids = list(debated)
            rng = random.Random(f"{seed}:{name}:2")
            rng.shuffle(ids)
            shown, own, others = [], {}, {}
            for cid in ids:
                ref = _ref(views[name], cid)
                shown.append((ref, by_id[cid]))
                own[ref] = round1[cid][name]
                heard = [round1[cid][o["name"]]["argument"] for o in active if o["name"] != name]
                rng.shuffle(heard)
                others[ref] = heard
            schema = _schema(criteria, [s[0] for s in shown], model_veto[judge["model"]])
            role_text = _role(judge)
            prompt = _round2_prompt(common, shown, own, others, role_text, judge["veto"], schema)
            cache_prefix = prompt.removesuffix(role_text)
            tasks[name] = (
                lambda j=judge, p=prompt, c=cache_prefix, s=schema: _ask(j, p, c, s, config)
            )
        answers2 = _run_round(2, tasks, active, quorum, parallel, failed)
        for name, answer in answers2.items():
            for ref, entry in answer.items():
                round2[views[name][ref]][name] = entry

    results = []
    for cid in by_id:
        final = {**round1[cid], **round2.get(cid, {})}
        # Poids = calibration (1 sans fichier) x confiance plancher (SPEC-73d0, R3).
        pond = {
            j: (1.0 if weights is None else weights[j]) * max(n["confidence"] / 100, conf_floor)
            for j, n in final.items()
        }
        scores = {
            name: _weighted_median([(n["scores"][name], pond[j]) for j, n in final.items()])
            for name in criteria
        }
        judge_scores = {name: _score(n["scores"], criteria) for name, n in final.items()}
        median = statistics.median(judge_scores.values())
        veto = next(
            ({"judge": j["name"], "reason": final[j["name"]]["veto_reason"]}
             for j in active if j["veto"] and final[j["name"]]["veto"]),
            None,
        )
        rounds = [_round_record(1, round1[cid], criteria)]
        revisions = []
        if cid in round2:
            rounds.append(_round_record(2, round2[cid], criteria))
            for name, entry in round2[cid].items():
                for crit in criteria:
                    before, after = round1[cid][name]["scores"][crit], entry["scores"][crit]
                    if before != after:
                        revisions.append(
                            {"judge": name, "criterion": crit, "from": before, "to": after, "argument": entry["argument"]}
                        )
        dissent = [
            {"judge": name, "score": s, "median": median}
            for name, s in judge_scores.items()
            if abs(s - median) > threshold
        ]
        results.append(
            {
                "id": cid,
                "scores": scores,
                "score": _score(scores, criteria),
                "confidence": statistics.median(n["confidence"] for n in final.values()),
                "veto": veto,
                "debated": cid in round2,
                "trace": {"rounds": rounds, "revisions": revisions, "dissent": dissent},
            }
        )

    return {
        "judges": [
            {**{k: j[k] for k in ("name", "usage", "model", "veto")}, "perspective_sha": perspective_sha(j["perspective"])}
            for j in judges
        ],
        "seed": seed,
        "threshold": threshold,
        "debate_confidence_below": conf_below,
        "min_confidence_weight": conf_floor,
        "quorum": quorum,
        "weights": weights,
        "failed": failed,
        "debated": debated,
        "candidates": results,
    }


def _ref(view: Mapping[str, str], cid: str) -> str:
    return next(ref for ref, c in view.items() if c == cid)
