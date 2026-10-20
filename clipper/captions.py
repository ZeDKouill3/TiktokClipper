"""Etape captions : titre, legende, hashtags et texte d'accroche par clip
(SPEC-6127), demandes a clipper.llm (usage ``captions``) dans la langue de
la video.

Entrees (workspace/<video_id>/) :
- parts.json (parts) : chaque moment retenu, decoupe en un clip unique ou en
  Part 1/2/.../N (``id``, ``format``, ``parts_total``,
  ``parts``: [{"part","start","end","duration","hook_text","suspense"}]) ;
- moments.json (moments) : ``justification`` et accroche de selection de
  chaque moment retenu, par ``id`` ;
- transcript.json (transcribe) : ``language`` et les mots horodates, pour
  donner a l'IA le texte exact prononce dans chaque partie ;
- meta.json (download), facultatif : titre de la video et chaine (hashtags
  plus pertinents ; mots-cles de la premiere phrase de la legende,
  [captions] caption_keywords_first, TASK-b8a0).

Sortie : workspace/<video_id>/captions.json

    {"video_id",
     "clips": [{"id", "moment_id", "part", "parts_total", "start", "end",
                "duration", "language", "title", "caption", "hashtags",
                "hook_text", "screen_title"}]}

``id`` = ``<moment_id>`` sur 2 chiffres (clip unique) ou
``<moment_id>-p<part>`` (multipart), ex. ``03-p2`` (SPEC-6127).

Pour chaque clip, l'IA recoit le texte prononce dans la partie, l'accroche
et la justification du moment, et rend titre, legende (``caption``, ton
sobre, aucun mot d'emphase clickbait, aucun emoji sauf ``[captions]
caption_allow_emoji`` explicite qui en autorise au plus deux, jamais
obligatoires, TASK-a844), hashtags (chacun commencant par #, sans doublon),
texte d'accroche (``hook_text``, memes regles de sobriete que caption,
8 mots au plus, valeur par defaut) et titre d'ecran ``screen_title``
(SPEC-6a86 : 6 mots au plus,
valeur par defaut, ton sobre sans mot d'emphase clickbait, aucun emoji sauf
``[captions] screen_title_allow_emoji`` explicite, qui autorise au plus un
emoji simple sans le rendre obligatoire) dans la
langue de la video ; ces regles sont revalidees ici et passees a llm.ask
comme controle : une reponse qui les enfreint est renvoyee au modele avec
l'erreur pour correction ([llm] repair_attempts), puis traitee comme une
reponse invalide si elle les enfreint encore. Reponse invalide ou Claude
indisponible : l'erreur remonte, rien n'est ecrit, aucune legende de secours
n'est inventee (ADR-ad2e).

Un moment multipart garde le meme screen_title dans toutes ses parties :
il est demande a l'IA une seule fois, avec la partie 1 (schema et prompt) ;
les parties suivantes ne le redemandent pas (le prompt cite celui deja
choisi comme contexte) et captions le recopie tel quel.

Le titre de publication (``title``, poste sur TikTok, distinct de
``screen_title``) suit la meme mecanique : le titre de base est demande a
l'IA une seule fois, avec la partie 1 ; les parties suivantes ne le
redemandent pas (le prompt cite celui deja choisi comme contexte). Chaque
clip recoit ``title`` = titre de base + " (Partie N)" (la partie 1 aussi) ;
pour un clip unique (parts_total = 1), aucun suffixe. La longueur maximale
demandee a l'IA pour le titre de base tient compte du suffixe le plus long
du moment, pour que le title final respecte title_max_chars.

Les moments sont independants et traites en parallele, au plus ``parallel``
a la fois (CONFIG_DEFAULTS, defaut 4 ; 1 = sequentiel) ; a l'interieur d'un
moment, ses parties restent traitees dans l'ordre, sequentiellement (la
partie 1 fixe title et screen_title, repris par les suivantes). La sortie
(ordre des clips) est celle de parts.json (moments, puis parties), quel que
soit l'ordre d'arrivee des reponses. ``parallel`` < 1 est refuse avec une
erreur explicite.

Appel a l'abonnement (SPEC-6a47, config, vide par defaut) : ``cta_line``
(chaine) ajoutee a la fin de ``caption`` sur une nouvelle ligne quand non
vide ; ``cta_hashtags`` (liste) ajoutes a ``hashtags`` (sans doublon avec
ceux deja choisis par l'IA). Applique une fois tous les clips generes,
independamment de l'IA ; un ``cta_hashtags`` qui ne commence pas par # est
une erreur explicite (ADR-ad2e).
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from clipper import llm

CONFIG_DEFAULTS: dict[str, object] = {
    "title_max_chars": 100,
    # Suffixe du title d'une serie ; {n} = numero de partie (preset EN : " (Part {n})").
    "part_title_suffix": " (Partie {n})",
    "caption_max_chars": 300,
    "hashtags_max": 8,
    "hook_words_max": 8,
    # TASK-35a1 : cible demandee au modele, plus courte que la limite (le
    # modele depasse sinon d'un mot) ; la validation reste hook_words_max.
    # Plafonnee a hook_words_max si le reglage de la limite est plus bas.
    "hook_words_target": 6,
    "screen_title_words_max": 6,
    "screen_title_words_target": 5,
    # Etude des vues 13/10 : les titres de plus de 35 caracteres font mediane 80
    # vues contre 251 (correlation). 0 = pas de limite ; sinon un titre d'ecran
    # plus long est refuse (renvoye au modele) et la limite est dite au prompt.
    "screen_title_max_chars": 0,
    # TASK-3268 : essais de reparation de l'appel qui produit titre d'ecran et
    # legende, propres a captions (un titre trop long ne doit pas arreter la
    # video apres une seule reparation). Les autres usages gardent
    # [llm] repair_attempts. Aucun titre de secours : apres les essais, la
    # SchemaError remonte.
    "title_repair_attempts": 3,
    # SPEC-6a86 : sans configuration explicite, un screen_title avec un
    # emoji est refuse. Activer l'option ne rend pas l'emoji obligatoire,
    # elle permet seulement d'en accepter un (au plus un).
    "screen_title_allow_emoji": False,
    # SPEC-08c2 : "citation" (defaut, de preference une phrase prononcee dans
    # le clip), "promesse" (annonce l'enjeu ou la chute sans la reveler) ou
    # "promesse_emoji" (promesse + « ... » + UN emoji d'emotion, sans qu'il
    # faille activer screen_title_allow_emoji).
    # Valeur inconnue : CaptionsError qui nomme la cle, jamais de repli.
    "screen_title_style": "citation",
    # SPEC-6a86 : ton sobre, aucun superlatif ni mot d'emphase clickbait.
    # Insensible a la casse et aux accents, comparaison mot entier. Liste
    # non exhaustive, reglable par preset de chaine.
    "screen_title_forbidden_words": [
        "pur", "total", "explose", "choc", "incroyable", "fou", "dingue",
        "glaçant", "assourdissant", "dévoilé",
    ],
    # TASK-a844 : meme sobriete que screen_title pour caption et hook_text.
    # Sans configuration explicite, le moindre emoji est refuse. Activer
    # l'option autorise jusqu'a _CAPTION_ALLOWED_EMOJI_MAX emojis simples,
    # sans jamais les rendre obligatoires.
    "caption_allow_emoji": False,
    # Nombre de moments traites en parallele (leurs parties restant
    # sequentielles entre elles) ; 1 = sequentiel, comme avant.
    "parallel": 4,
    # Appel a l'abonnement (SPEC-6a47), vide par defaut : sans configuration
    # explicite, caption et hashtags restent inchanges par cette spec.
    # cta_line : ajoutee a la fin de caption. cta_hashtags : ajoutes a
    # hashtags, memes regles que les autres (commencent par #, sans doublon).
    "cta_line": "",
    "cta_hashtags": [],
    # TASK-b8a0 : la premiere phrase de la legende porte les mots-cles de
    # recherche reels du clip (jeu, streamer/chaine, moment), la legende
    # servant de metadonnee de recherche TikTok. A false, consigne strictement
    # identique a celle d'avant ce reglage.
    "caption_keywords_first": True,
    # Moderation TikTok : un clip a ete restreint aux plus de 18 ans a cause de
    # termes lies a la triche ou aux sanctions dans la legende et les
    # hashtags. Insensible a la casse et aux accents, mot entier (une entree
    # de plusieurs mots est une suite de mots). Une reponse qui en contient
    # est renvoyee au modele, jamais corrigee en silence. Le titre d'ecran
    # n'est pas concerne.
    "moderation_risk_terms": [
        "ban", "bans", "bannissement", "bannissements",
        "banni", "bannie", "bannis", "bannies",
        "triche", "tricher", "tricheur", "tricheurs", "tricheuse", "tricheuses",
        "cheat", "cheats", "cheater", "cheaters", "cheating",
        "hack", "hacks", "hacker", "hackers", "hacking",
        "macro", "macros", "exploit", "exploits",
        "contourner",
        "logiciel tiers", "logiciels tiers", "logiciel externe", "logiciels externes",
    ],
}

_EDGE = 0.1
_EPS = 1e-6

# TASK-a844 : nombre d'emojis simples au plus dans caption/hook_text quand
# [captions] caption_allow_emoji est actif.
_CAPTION_ALLOWED_EMOJI_MAX = 2

# --------------------------------------------------------------------------
# screen_title : un seul emoji Extended_Pictographic (SPEC-6127).
# --------------------------------------------------------------------------

_ZWJ = "‍"
_VS16 = "️"
_SKIN_TONE_MODIFIERS = range(0x1F3FB, 0x1F400)
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)

# Approximation des plages Extended_Pictographic d'Unicode : les blocs
# d'emojis usuels, hors indicateurs regionaux (drapeaux, geres a part).
_PICTOGRAPHIC_RANGES = (
    (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328),
    (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA),
    (0x24C2, 0x24C2),
    (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x27BF),
    (0x2934, 0x2935),
    (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
    (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299),
    (0x1F000, 0x1F0FF),
    (0x1F200, 0x1F2FF),
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F700, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
)


def _is_pictographic(codepoint: int) -> bool:
    return any(lo <= codepoint <= hi for lo, hi in _PICTOGRAPHIC_RANGES)


class CaptionsError(Exception):
    """Entree manquante, ou parts.json et moments.json incoherents."""


# --------------------------------------------------------------------------
# Texte de la partie
# --------------------------------------------------------------------------


def _part_text(transcript: dict[str, Any], start: float, end: float) -> str:
    """Mots prononces dans [start, end] (bornes de la partie), dans l'ordre,
    tels qu'ecrits par l'etape transcribe."""
    words = [
        w
        for seg in transcript.get("segments", [])
        for w in (seg.get("words") or [])
        if w["start"] >= start - _EDGE - _EPS and w["end"] <= end + _EDGE + _EPS
    ]
    return "".join(w["word"] for w in words).strip()


# --------------------------------------------------------------------------
# Schema et prompt
# --------------------------------------------------------------------------


def response_schema(
    settings: dict[str, Any], *, include_screen_title: bool = True, include_title: bool = True
) -> dict[str, Any]:
    """Ce que le LLM renvoie pour un clip. ``include_screen_title`` est faux
    pour les parties d'un moment multipart apres la premiere : le titre
    d'ecran n'est alors plus redemande (recopie de la partie 1).
    ``include_title`` suit la meme regle pour le titre de publication."""
    caption_allow_emoji = bool(settings["caption_allow_emoji"])
    caption_emoji_desc = (
        f"au plus {_CAPTION_ALLOWED_EMOJI_MAX} emojis simples, jamais obligatoires"
        if caption_allow_emoji
        else "AUCUN emoji"
    )
    properties: dict[str, Any] = {
        "caption": {
            "type": "string", "minLength": 1, "maxLength": int(settings["caption_max_chars"]),
            "description": (
                "Legende publiee sous le clip, dans la langue de la video ; ton sobre, "
                f"aucun mot d'emphase clickbait ; {caption_emoji_desc}."
            ),
        },
        "hashtags": {
            "type": "array",
            "minItems": 1,
            "maxItems": int(settings["hashtags_max"]),
            "items": {
                "type": "string", "minLength": 2, "maxLength": 30,
                "description": "Un hashtag, en commencant par #.",
            },
            "description": "Hashtags pertinents pour ce clip precis, sans doublon.",
        },
        "hook_text": {
            "type": "string", "minLength": 1, "maxLength": 80,
            "description": (
                f"Texte d'accroche affiche a l'ecran les 2 premieres secondes, "
                f"viser {_words_target(settings, 'hook_words_target', 'hook_words_max')} mots, "
                f"{settings['hook_words_max']} au plus ; si une citation du transcript depasse, "
                "ne la recopie pas telle quelle : raccourcis-la ou reformule ; "
                f"ton sobre, aucun mot d'emphase clickbait ; {caption_emoji_desc}."
            ),
        },
    }
    required = ["caption", "hashtags", "hook_text"]
    if include_title:
        properties["title"] = {
            "type": "string", "minLength": 1, "maxLength": int(settings["title_max_chars"]),
            "description": "Titre accrocheur du clip, dans la langue de la video.",
        }
        required.append("title")
    if include_screen_title:
        allow_emoji = bool(settings["screen_title_allow_emoji"])
        style = screen_title_style(settings)
        title_hint = (
            _PROMESSE_SCHEMA_HINT
            if style in _PROMESSE_STYLES
            else "si une citation depasse, ne la recopie pas telle quelle : raccourcis-la ou reformule ;"
        )
        if style == "promesse_emoji":
            title_hint += " " + _PROMESSE_EMOJI_RULE + " ;"
        emoji_desc = (
            "un emoji final obligatoire (voir plus haut)"
            if style == "promesse_emoji"
            else "au plus un emoji simple, jamais obligatoire"
            if allow_emoji
            else "AUCUN emoji"
        )
        properties["screen_title"] = {
            "type": "string", "minLength": 1, "maxLength": 60,
            "description": (
                "Titre d'ecran affiche en haut, sur un encadre blanc, pendant tout le "
                f"clip : viser {_words_target(settings, 'screen_title_words_target', 'screen_title_words_max')} "
                f"mots, {settings['screen_title_words_max']} au plus (l'emoji ne compte pas) ; "
                f"{title_hint} ton sobre (jamais de superlatif ni de mot d'emphase "
                f"clickbait) ; {emoji_desc}."
            ),
        }
        required.append("screen_title")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# SPEC-08c2 : consignes du style "promesse", reprises a l'identique dans le
# prompt et dans la description du champ du schema (le schema prime).
_PROMESSE_SCHEMA_HINT = (
    "annonce l'enjeu du clip, jamais reveler ni resumer l'issue ; varier la forme : au plus 1 titre "
    "sur 2 en question, sinon une situation en suspens (« Il tente le boss seul », "
    "« Dernière carte, tout ou rien ») ; la promesse se realise dans le clip : "
    "aucun fait, nom ou chiffre absent du transcript ou de la justification du moment ;"
)
# Consigne ajoutee par "promesse_emoji", identique dans le prompt et dans la
# description du champ du schema. Les « ? » sont proscrits avec « ... ».
_PROMESSE_EMOJI_RULE = (
    "le titre se termine TOUJOURS par des points de suspension « ... » suivis d'un espace et d'UN seul emoji "
    "qui traduit l'emotion du moment (jamais 🤔, jamais deux emojis) ; jamais de « ? » avec « ... » "
    "(pas de « ?... ») ; ne recopie pas une citation en ajoutant « ... » : reformule en promesse ; "
    "rappel : jamais reveler ni resumer l'issue ; varie l'emoji d'un clip a l'autre au sein de la reponse "
    "(😬 😱 😤 😅 🔥 😮 😂 🙏 selon l'emotion, pas toujours le meme)"
)
_PROMESSE_PROMPT_RULE = (
    "il tient le spectateur en haleine : il annonce l'enjeu du clip, jamais reveler ni resumer l'issue "
    "(un titre qui dit ce qui s'est passe, ex. « Il refuse de répondre aux stats », est refuse) ; "
    "varier la forme : au plus 1 titre sur 2 en question, sinon une situation en suspens "
    "(« Il tente le boss seul », « Dernière carte, tout ou rien »), pas une citation ; "
    "la promesse se realise dans le clip : "
    "aucun fait, nom ou chiffre absent du transcript ou de la justification du moment"
)


def _keywords_rule(channel: str) -> str:
    """TASK-b8a0 : consigne de la premiere phrase de ``caption`` (mots-cles de
    recherche reels). Jeu ou streamer inconnu : la consigne le dit au modele,
    rien n'est invente (ADR-ad2e)."""
    streamer = (
        "le nom du streamer ou de la chaine de la source (voir Contexte)"
        if channel
        else "le streamer n'est pas connu (voir Contexte) : n'invente aucun nom de streamer"
    )
    return (
        "caption, mots-cles de recherche : la PREMIERE phrase de la legende contient les mots-cles "
        "reels sous lesquels on chercherait ce clip, dans cet ordre : le nom du jeu, tel qu'il est "
        f"ecrit officiellement ; {streamer} ; puis le moment lui-meme. Reste naturelle : une vraie "
        "phrase, pas une liste de mots-cles, pas une liste de noms ni de bourrage. Le jeu se lit dans "
        "le titre de la video source, la transcription ou le contexte ; si le jeu n'est identifiable "
        "ni dans le titre, ni dans la transcription, ni dans le contexte, ecris la phrase sans "
        "nom de jeu et n'invente aucun nom de jeu."
    )


def _game_rule(category: str) -> str:
    """Le jeu en tete de la legende est la categorie Twitch du chapitre du clip
    (etude des vues 13/10 : des legendes citaient un jeu devine, jamais le bon)."""
    return (
        f"caption, jeu : la categorie Twitch du clip est « {category} » (voir Contexte). Si c'est un jeu, "
        "le jeu en tete de la legende est CELUI-CI, ecrit avec son nom officiel ; jamais un autre jeu devine "
        "d'apres le titre de la video. Si le streamer parle d'un autre jeu, n'ecris jamais « Sur <autre jeu> » "
        "mais une tournure comme « <Streamer> parle de <autre jeu> ». Si la categorie n'est pas un jeu "
        "(Just Chatting, IRL...), n'ecris pas « Sur <jeu> »."
    )


def _prompt(
    language: str,
    video_title: str,
    moment: dict[str, Any],
    part: dict[str, Any],
    parts_total: int,
    text: str,
    settings: dict[str, Any],
    *,
    screen_title: str | None = None,
    title: str | None = None,
    channel: str = "",
    category: str | None = None,
) -> str:
    context = ""
    if parts_total > 1:
        context = (
            f"\nCe clip est la partie {part['part']}/{parts_total} d'une histoire plus longue : "
            "la legende peut le mentionner, mais l'accroche doit donner envie sans avoir vu les "
            "parties precedentes.\n"
        )

    caption_allow_emoji = bool(settings["caption_allow_emoji"])
    caption_emoji_rule = (
        f"au plus {_CAPTION_ALLOWED_EMOJI_MAX} emojis simples, jamais obligatoires"
        if caption_allow_emoji
        else "aucun emoji"
    )
    rules = [f"Reponds entierement dans la langue de la video ({language or 'celle de la transcription'})."]
    if _foreign_language(language):
        rules.append(
            f"screen_title et hook_text sont ecrits dans la langue de la video ({language}), jamais en "
            "francais, meme si les consignes et les exemples ci-dessous sont en francais : ces exemples "
            f"sont des illustrations de la forme attendue, transpose-les en {language}."
        )
    if title is None:
        rules.append("title : court, accrocheur, sans hashtag ni exces d'emoji.")
    rules.append(
        "caption : la legende publiee sous le clip, qui donne envie de regarder en entier ; ton sobre, "
        f"aucun mot d'emphase clickbait ; {caption_emoji_rule}."
    )
    keywords_first = bool(settings["caption_keywords_first"])
    if keywords_first:
        rules.append(_keywords_rule(channel))
        if category:
            rules.append(_game_rule(category))
    rules.append(
        "hashtags : chacun commence par #, jamais deux fois le meme, pertinents pour ce clip precis "
        f"(pas de generique inutile), {int(settings['hashtags_max'])} au plus."
    )
    risk_terms = ", ".join(str(t) for t in settings["moderation_risk_terms"])
    if risk_terms:
        rules.append(
            "moderation TikTok : la caption et les hashtags n'emploient jamais ces termes "
            f"(ni un hashtag qui les reprend) : {risk_terms} ; reformule autrement."
        )
    rules.append(
        f"hook_text : le texte affiche a l'ecran des le debut, viser "
        f"{_words_target(settings, 'hook_words_target', 'hook_words_max')} mots "
        f"({int(settings['hook_words_max'])} au plus, limite stricte), qui arrete le scroll ; "
        "si une citation du transcript depasse, ne recopie pas une citation telle quelle : "
        f"raccourcis-la ou reformule ; ton sobre, aucun mot d'emphase clickbait ; {caption_emoji_rule}."
    )
    if screen_title is None:
        allow_emoji = bool(settings["screen_title_allow_emoji"])
        style = screen_title_style(settings)
        emoji_rule = (
            "un seul emoji final obligatoire (voir plus haut), pas de sequence composee, pas de drapeau"
            if style == "promesse_emoji"
            else "au plus un emoji simple, jamais obligatoire (pas de sequence composee, pas de drapeau)"
            if allow_emoji
            else "aucun emoji"
        )
        forbidden_words = ", ".join(str(w) for w in settings["screen_title_forbidden_words"])
        max_chars = screen_title_max_chars(settings)
        max_chars_rule = (
            f" Longueur : {max_chars} caracteres au plus (espaces et emoji compris), limite stricte."
            if max_chars
            else ""
        )
        content_rule = (
            _PROMESSE_PROMPT_RULE + (" ; " + _PROMESSE_EMOJI_RULE if style == "promesse_emoji" else "")
            if style in _PROMESSE_STYLES
            else "de preference une phrase reellement prononcee dans ce clip (citation courte entre "
            "« »), sinon un fait concret et precis du clip, jamais un contenu absent du clip"
        )
        rules.append(
            f"screen_title : titre, viser "
            f"{_words_target(settings, 'screen_title_words_target', 'screen_title_words_max')} mots "
            f"({int(settings['screen_title_words_max'])} au plus, limite stricte), affiche dans un encadre blanc au-dessus de la video, pendant tout le clip ; ton "
            f"sobre, {content_rule} ; "
            f"{emoji_rule} ; aucun superlatif ni mot d'emphase clickbait, notamment : {forbidden_words}."
            f"{max_chars_rule}"
        )
    rules.append(
        "Comptage des mots pour hook_text et screen_title : tout groupe separe par des espaces "
        "qui contient une lettre ou un chiffre compte pour un mot ('à', '3', « n'ai », "
        "« l'égorger » comptent chacun pour 1 mot) ; l'emoji et la ponctuation isolee ne "
        "comptent pas."
    )
    rules_text = "".join(f"{i}. {rule}\n" for i, rule in enumerate(rules, start=1))

    title_context = ""
    if title is not None:
        title_context = (
            f"\nTitre deja choisi pour ce moment, le meme dans toutes ses parties (ne pas le "
            f"redemander) : {title}\n"
        )
    if screen_title is None:
        screen_title_context = ""
    else:
        screen_title_context = (
            f"\nTitre d'ecran deja choisi pour ce moment, le meme dans toutes ses parties (ne pas "
            f"le redemander) : {screen_title}\n"
        )
    channel_line = (
        f"Chaine / streamer de la source : {channel or 'inconnu'}\n" if keywords_first else ""
    )
    if keywords_first and category:
        channel_line += f"Categorie Twitch du clip : {category}\n"
    return (
        "Tu ecris les metadonnees d'un clip vertical TikTok tire d'une video plus longue.\n\n"
        "## Regles\n"
        f"{rules_text}"
        f"{context}"
        f"{title_context}"
        f"{screen_title_context}\n"
        "## Contexte\n"
        f"Video source : {video_title or '(sans titre)'}\n"
        f"{channel_line}"
        f"Pourquoi ce moment a ete retenu : {moment.get('justification', '')}\n"
        f"Accroche du moment : {moment.get('hook_text', '')}\n\n"
        "## Texte prononce dans ce clip\n"
        f"{text}\n"
    )


# --------------------------------------------------------------------------
# Validation au-dela du schema
# --------------------------------------------------------------------------


_HASHTAG_SEPARATOR = re.compile(r"[\s,]")


def _validate_hashtags(hashtags: list[str]) -> None:
    seen: set[str] = set()
    for tag in hashtags:
        if not tag.startswith("#") or not tag[1:].strip():
            raise llm.SchemaError(f"hashtag invalide (doit commencer par #) : {tag!r}")
        if _HASHTAG_SEPARATOR.search(tag):
            raise llm.SchemaError(f"hashtag invalide (espace ou virgule interdits) : {tag!r}")
        key = tag.lower()
        if key in seen:
            raise llm.SchemaError(f"hashtag en double : {tag!r}")
        seen.add(key)


def _counted_words(text: str) -> list[str]:
    """Groupes separes par des espaces qui comptent pour un mot chacun
    (contiennent au moins une lettre ou un chiffre) : l'emoji et la
    ponctuation isolee sont exclus."""
    return [token for token in text.split() if any(c.isalnum() for c in token)]


def _count_words(text: str) -> int:
    return len(_counted_words(text))


def _numbered_words(text: str) -> str:
    return ", ".join(f"{i}. {word}" for i, word in enumerate(_counted_words(text), start=1))


def _words_target(settings: dict[str, Any], target_key: str, max_key: str) -> int:
    """Cible de mots demandee au modele, jamais au-dessus de la limite."""
    return min(int(settings[target_key]), int(settings[max_key]))


def _excess_message(n: int, target: int) -> str:
    excess = n - target
    return f"{n} mots, il en faut {target} au plus : supprime {excess} mot{'s' if excess > 1 else ''}"


def _validate_hook_text(hook_text: str, max_words: int, target_words: int | None = None) -> None:
    n = _count_words(hook_text)
    if n > max_words:
        target = max_words if target_words is None else min(target_words, max_words)
        raise llm.SchemaError(
            f"texte d'accroche de {_excess_message(n, target)} : {_numbered_words(hook_text)}"
        )


def _validate_screen_title_emoji(screen_title: str, allow_emoji: bool) -> None:
    """SPEC-6a86 : par defaut (``allow_emoji`` faux), le moindre emoji est
    refuse. Si autorise, au plus un emoji Extended_Pictographic simple, avec
    au plus un VS16 et un modificateur de teint ; sequence ZWJ et drapeau
    toujours refuses."""
    codepoints = [ord(c) for c in screen_title]
    n = len(codepoints)
    emoji_count = 0
    i = 0
    while i < n:
        cp = codepoints[i]
        if cp in _REGIONAL_INDICATORS and i + 1 < n and codepoints[i + 1] in _REGIONAL_INDICATORS:
            if not allow_emoji:
                raise llm.SchemaError(
                    f"aucun emoji autorise par defaut dans screen_title (SPEC-6a86, "
                    f"[captions] screen_title_allow_emoji) : {screen_title!r}"
                )
            raise llm.SchemaError(f"emoji de type drapeau refuse dans screen_title : {screen_title!r}")
        if _is_pictographic(cp):
            if not allow_emoji:
                raise llm.SchemaError(
                    f"aucun emoji autorise par defaut dans screen_title (SPEC-6a86, "
                    f"[captions] screen_title_allow_emoji) : {screen_title!r}"
                )
            j = i + 1
            if j < n and codepoints[j] == ord(_VS16):
                j += 1
            if j < n and codepoints[j] in _SKIN_TONE_MODIFIERS:
                j += 1
            if j < n and codepoints[j] == ord(_ZWJ):
                raise llm.SchemaError(
                    f"emoji compose (sequence ZWJ) refuse dans screen_title : {screen_title!r}"
                )
            emoji_count += 1
            i = j
            continue
        i += 1
    if emoji_count > 1:
        raise llm.SchemaError(
            f"screen_title doit contenir au plus un emoji, {emoji_count} trouve(s) : {screen_title!r}"
        )


def _validate_sober_text_emoji(text: str, field_name: str, allow_emoji: bool) -> None:
    """TASK-a844 : meme detecteur d'emoji que screen_title (Extended_Pictographic,
    VS16, modificateur de teint, sequence ZWJ, drapeau regional), applique a
    caption/hook_text. Sans option (``allow_emoji`` faux), le moindre emoji
    est refuse. Si autorise, au plus ``_CAPTION_ALLOWED_EMOJI_MAX`` emoji(s)
    simples, jamais obligatoires ; sequence ZWJ et drapeau toujours refuses."""
    codepoints = [ord(c) for c in text]
    n = len(codepoints)
    emoji_count = 0
    i = 0
    while i < n:
        cp = codepoints[i]
        if cp in _REGIONAL_INDICATORS and i + 1 < n and codepoints[i + 1] in _REGIONAL_INDICATORS:
            if not allow_emoji:
                raise llm.SchemaError(
                    f"aucun emoji autorise par defaut dans {field_name} (TASK-a844, "
                    f"[captions] caption_allow_emoji) : {text!r}"
                )
            raise llm.SchemaError(f"emoji de type drapeau refuse dans {field_name} : {text!r}")
        if _is_pictographic(cp):
            if not allow_emoji:
                raise llm.SchemaError(
                    f"aucun emoji autorise par defaut dans {field_name} (TASK-a844, "
                    f"[captions] caption_allow_emoji) : {text!r}"
                )
            j = i + 1
            if j < n and codepoints[j] == ord(_VS16):
                j += 1
            if j < n and codepoints[j] in _SKIN_TONE_MODIFIERS:
                j += 1
            if j < n and codepoints[j] == ord(_ZWJ):
                raise llm.SchemaError(
                    f"emoji compose (sequence ZWJ) refuse dans {field_name} : {text!r}"
                )
            emoji_count += 1
            i = j
            continue
        i += 1
    if emoji_count > _CAPTION_ALLOWED_EMOJI_MAX:
        raise llm.SchemaError(
            f"{field_name} doit contenir au plus {_CAPTION_ALLOWED_EMOJI_MAX} emoji(s), "
            f"{emoji_count} trouve(s) : {text!r}"
        )


_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _normalize_word(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def _validate_screen_title_forbidden_words(screen_title: str, forbidden_words: list[str]) -> None:
    """SPEC-6a86 : aucun mot de la liste, insensible a la casse et aux
    accents, comparaison mot entier (« choquant » n'est pas « choc »)."""
    forbidden = {_normalize_word(w) for w in forbidden_words}
    for token in _WORD_RE.findall(screen_title):
        if _normalize_word(token) in forbidden:
            raise llm.SchemaError(
                f"screen_title contient un mot interdit (ton sobre, SPEC-6a86) : "
                f"{token!r} dans {screen_title!r}"
            )


def _normalized_words(text: str) -> list[str]:
    return [_normalize_word(w) for w in re.findall(r"[^\W_]+", text, re.UNICODE)]


def _validate_moderation_risk_terms(answer: dict[str, Any], risk_terms: list[str]) -> None:
    """Caption et hashtags sans terme a risque de moderation TikTok : mot
    entier (ou suite de mots), insensible a la casse et aux accents, le # d'un
    hashtag etant ignore. Le titre d'ecran n'est pas controle."""
    terms = [t for t in (_normalized_words(str(term)) for term in risk_terms) if t]
    fields = [("caption", answer["caption"])] + [("hashtag", tag) for tag in answer["hashtags"]]
    for name, text in fields:
        words = _normalized_words(text)
        for term in terms:
            n = len(term)
            if any(words[i:i + n] == term for i in range(len(words) - n + 1)):
                raise llm.SchemaError(
                    f"{name} contient un terme a risque de moderation TikTok "
                    f"({' '.join(term)!r}) : {text!r} ; reformule sans ce terme "
                    "ni hashtag equivalent"
                )


_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}
# Categories Twitch qui ne sont pas un jeu : « Sur <ce nom> » n'a pas de sens.
_NON_GAME_CATEGORIES = frozenset({
    "just chatting", "irl", "talk shows & podcasts", "music", "art", "special events", "sports",
})
# « Sur la chaine de ... », « Sur le stream de ... » : pas un nom de jeu.
_NOT_A_GAME_LEAD = re.compile(r"^(la cha[iî]ne|le stream|le live|le canal|le direct)\b", re.IGNORECASE)
_SUR_RE = re.compile(r"^\s*Sur\s+(?P<game>[^,:;.\n]+?)\s*[,:;]")
_VU_PAR_RE = re.compile(r"^\s*(?P<game>[^,:;.\n]+?)\s+vu par\b", re.IGNORECASE)


def _game_tokens(text: str) -> list[str]:
    """Nom de jeu normalise : casse, accents, ponctuation ; ``II`` = ``2``."""
    return [_ROMAN.get(w, w) for w in _normalized_words(text)]


def _contains_seq(haystack: list[str], needle: list[str]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _cited_game(caption: str) -> str | None:
    """Jeu cite en tete par « Sur X, ... » ou « X vu par ... » ; None si la
    legende n'a pas cette forme ou cite la chaine/le stream."""
    match = _SUR_RE.match(caption) or _VU_PAR_RE.match(caption)
    if match is None:
        return None
    game = match.group("game").strip()
    if not game or _NOT_A_GAME_LEAD.match(game):
        return None
    return game


def caption_wrong_game(caption: str, category: str | None, video_title: str) -> str | None:
    """Jeu cite par « Sur X, ... » (ou « X vu par ») qui n'est ni la categorie Twitch du chapitre
    du clip ni dans le titre de la VOD ; None si la legende est conforme. Sans categorie (YouTube,
    pas de chapitres) : aucun controle."""
    if not category:
        return None
    game = _cited_game(caption)
    if game is None:
        return None
    cited = _game_tokens(game)
    if _contains_seq(_game_tokens(video_title), cited):
        return None
    if category.strip().lower() not in _NON_GAME_CATEGORIES:
        cat = _game_tokens(category)
        if _contains_seq(cat, cited) or _contains_seq(cited, cat):
            return None
    return game


def _validate_caption_game(caption: str, category: str | None, video_title: str) -> None:
    """Etude des vues 13/10 : legende sur le mauvais jeu, renvoyee au modele (ADR-ad2e)."""
    game = caption_wrong_game(caption, category, video_title)
    if game is not None:
        raise llm.SchemaError(
            f"la legende cite le jeu {game!r} mais la categorie Twitch du clip est {category!r} et le titre "
            f"de la video {video_title!r} ne le contient pas : ecris le jeu de la categorie (nom officiel) ou, "
            f"si le streamer parle d'un autre jeu, « <Streamer> parle de {game} » ; legende : {caption!r}"
        )


def _validate_screen_title_length(screen_title: str, max_chars: int) -> None:
    if max_chars and len(screen_title) > max_chars:
        raise llm.SchemaError(
            f"titre d'ecran de {len(screen_title)} caracteres, {max_chars} au plus "
            f"([captions] screen_title_max_chars) : raccourcis-le : {screen_title!r}"
        )


_PROMESSE_EMOJI_RE = re.compile(r"^(?P<body>.*\S)\.\.\. (?P<emoji>\S+)$", re.DOTALL)
_FORBIDDEN_FINAL_EMOJI = "🤔"


def _validate_promesse_emoji(screen_title: str) -> None:
    """Style ``promesse_emoji`` : fin « ... » + espace + UN seul emoji (🤔
    refuse), aucun « ? », aucun autre emoji dans le corps. Rien n'est corrige."""
    match = _PROMESSE_EMOJI_RE.match(screen_title)
    if match is None:
        raise llm.SchemaError(
            "screen_title doit finir par des points de suspension « ... », un espace, "
            f"puis UN seul emoji : {screen_title!r}"
        )
    body, token = match.group("body"), match.group("emoji")
    if "?" in screen_title:
        raise llm.SchemaError(
            f"screen_title ne doit jamais contenir « ? » (pas de « ?... ») : {screen_title!r}"
        )
    if any(_is_pictographic(ord(c)) for c in body):
        raise llm.SchemaError(
            f"screen_title : l'emoji va uniquement apres les points de suspension : {screen_title!r}"
        )
    count = sum(1 for c in token if _is_pictographic(ord(c)))
    if count != 1:
        raise llm.SchemaError(
            f"screen_title doit finir par un seul emoji, {count} trouve(s) : {screen_title!r}"
        )
    if _FORBIDDEN_FINAL_EMOJI in token:
        raise llm.SchemaError(
            f"emoji {_FORBIDDEN_FINAL_EMOJI} refuse dans screen_title : choisis une autre emotion : "
            f"{screen_title!r}"
        )
    _validate_screen_title_emoji(screen_title, True)  # ZWJ et drapeau refuses


_FR_WORDS = frozenset(
    "le la les un une des du de et est il elle ils elles lui je tu nous vous sans pour dans avec sur "
    "pas ne ce cette qui que mais ou ses leur leurs tout tous fait veut peut mon ma mes ton ta tes "
    "notre votre comme entre apres avant jamais toujours encore meme deja en au aux sa tres".split()
)
_EN_WORDS = frozenset(
    "the a an and is are was were he she they his her their with without for to of in on it its this "
    "that no not can just why what how who you we i my your our be been has have but or at by from "
    "after before never always still even already".split()
)
_FR_ONLY_LETTERS = frozenset("éèêëàâùûçôîï")
# Elision en debut de mot (n'obeit, l'arme, qu'il) : francaise, vaut deux indices ; « don't »,
# « it's » ne comptent pas (l'apostrophe y suit une lettre interieure), « c'mon » non plus.
_FR_ELISION_RE = re.compile(r"\b(?!c['’]mon\b)(?:n|l|d|qu|j|c|s|m|t)['’][^\W\d_]", re.IGNORECASE)


def _foreign_language(language: str) -> bool:
    """Vrai si la video n'est pas en francais (langue connue du transcript)."""
    return bool(language) and language.lower() != "fr"


def _validate_screen_title_language(screen_title: str, language: str) -> None:
    """TASK-fcb56296766e : sur une video non francaise, un titre d'ecran
    manifestement francais est refuse (renvoye au modele). Detection locale
    par mots outils ; en cas de doute, rien n'est refuse."""
    if not _foreign_language(language):
        return
    tokens = [_normalize_word(t) for t in _WORD_RE.findall(screen_title)]
    fr = sum(1 for t in tokens if t in _FR_WORDS)
    en = sum(1 for t in tokens if t in _EN_WORDS)
    if any(c in _FR_ONLY_LETTERS for c in screen_title.lower()):
        fr += 1
    if _FR_ELISION_RE.search(screen_title):
        fr += 2
    if fr >= 2 and fr > en:
        raise llm.SchemaError(
            f"screen_title est en francais alors que la langue de la video est {language!r} : "
            f"reecris-le dans la langue de la video : {screen_title!r}"
        )


def _validate_screen_title(
    screen_title: str, max_words: int, forbidden_words: list[str], allow_emoji: bool,
    target_words: int | None = None, style: str = "citation",
) -> None:
    n = _count_words(screen_title)
    if n > max_words:
        target = max_words if target_words is None else min(target_words, max_words)
        raise llm.SchemaError(
            f"titre d'ecran de {_excess_message(n, target)} : {_numbered_words(screen_title)}"
        )
    _validate_screen_title_forbidden_words(screen_title, forbidden_words)
    if style == "promesse_emoji":
        _validate_promesse_emoji(screen_title)
    else:
        _validate_screen_title_emoji(screen_title, allow_emoji)


def _check_answer(
    hook_words_max: int,
    screen_title_words_max: int,
    screen_title_forbidden_words: list[str],
    screen_title_allow_emoji: bool,
    caption_allow_emoji: bool,
    *,
    require_screen_title: bool = True,
    hook_words_target: int | None = None,
    screen_title_words_target: int | None = None,
    moderation_risk_terms: list[str] | None = None,
    screen_title_style: str = "citation",
    screen_title_max_chars: int = 0,
    category: str | None = None,
    video_title: str = "",
    language: str = "",
):
    """Controle passe a llm.ask : ce qui le refuse est renvoye au modele
    pour correction, comme une reponse hors schema. ``require_screen_title``
    faux pour les parties d'un moment multipart apres la premiere : le
    schema ne demande alors plus ce champ."""

    def check(answer: dict[str, Any]) -> None:
        _validate_hashtags(answer["hashtags"])
        _validate_moderation_risk_terms(answer, moderation_risk_terms or [])
        _validate_caption_game(answer["caption"], category, video_title)
        _validate_hook_text(answer["hook_text"], hook_words_max, hook_words_target)
        _validate_sober_text_emoji(answer["caption"], "caption", caption_allow_emoji)
        _validate_sober_text_emoji(answer["hook_text"], "hook_text", caption_allow_emoji)
        if require_screen_title:
            _validate_screen_title_language(answer["screen_title"], language)
            _validate_screen_title_length(answer["screen_title"], screen_title_max_chars)
            _validate_screen_title(
                answer["screen_title"], screen_title_words_max,
                screen_title_forbidden_words, screen_title_allow_emoji,
                screen_title_words_target, screen_title_style,
            )

    return check


# --------------------------------------------------------------------------
# Etape
# --------------------------------------------------------------------------


def _read_json(path: Path, optional: bool = False) -> Any:
    if not path.exists():
        if optional:
            return None
        raise CaptionsError(f"entree absente : {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _settings(config: Any) -> dict[str, Any]:
    if config is None:
        from clipper.config import load_config

        config = load_config()
    return {**CONFIG_DEFAULTS, **config.section("captions")}


def _clip_id(moment_id: int, part: int, parts_total: int) -> str:
    base = f"{moment_id:02d}"
    return base if parts_total == 1 else f"{base}-p{part}"


def _apply_cta_line(clips: list[dict[str, Any]], settings: dict[str, Any]) -> None:
    """Applique ``[captions] cta_line``/``cta_hashtags`` (SPEC-6a47) a chaque
    clip : vide par defaut, donc sans effet sans configuration explicite. Un
    hashtag qui ne commence pas par # est une erreur explicite (ADR-ad2e) ;
    un doublon avec un hashtag deja choisi par l'IA est simplement omis."""
    cta_line = str(settings.get("cta_line") or "").strip()
    cta_hashtags = settings.get("cta_hashtags") or []
    if not cta_line and not cta_hashtags:
        return
    for tag in cta_hashtags:
        if not isinstance(tag, str) or not tag.startswith("#") or not tag[1:].strip():
            raise CaptionsError(f"[captions] cta_hashtags : hashtag invalide (doit commencer par #) : {tag!r}")
        if _HASHTAG_SEPARATOR.search(tag):
            raise CaptionsError(f"[captions] cta_hashtags : hashtag invalide (espace ou virgule interdits) : {tag!r}")
    for clip in clips:
        if cta_line:
            clip["caption"] = f"{clip['caption']}\n{cta_line}"
        if cta_hashtags:
            seen = {h.lower() for h in clip["hashtags"]}
            for tag in cta_hashtags:
                if tag.lower() not in seen:
                    clip["hashtags"].append(tag)
                    seen.add(tag.lower())


SCREEN_TITLE_STYLES = ("citation", "promesse", "promesse_emoji")
_PROMESSE_STYLES = ("promesse", "promesse_emoji")


def screen_title_style(settings: dict[str, Any]) -> str:
    """Style du titre d'ecran (SPEC-08c2) ; valeur inconnue : CaptionsError qui
    nomme la cle et les valeurs permises (ADR-ad2e, aucun repli silencieux)."""
    value = settings["screen_title_style"]
    if not isinstance(value, str) or value not in SCREEN_TITLE_STYLES:
        raise CaptionsError(
            f"[captions] screen_title_style doit valoir {' ou '.join(repr(v) for v in SCREEN_TITLE_STYLES)}, "
            f"recu {value!r}"
        )
    return value


def screen_title_max_chars(settings: dict[str, Any]) -> int:
    """Longueur maximale du titre d'ecran (0 = pas de limite) ; < 0, non entier ou booleen : CaptionsError."""
    value = settings["screen_title_max_chars"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CaptionsError(f"[captions] screen_title_max_chars doit etre un entier >= 0, recu {value!r}")
    return value


def chapter_category(meta: dict[str, Any], start: float) -> str | None:
    """Categorie Twitch du chapitre de meta.json qui contient ``start`` (secondes) ; None sans chapitres
    (YouTube) ou hors de tout chapitre."""
    for chapter in meta.get("chapters") or []:
        begin, end = chapter.get("start_time"), chapter.get("end_time")
        if begin is None or end is None:
            continue
        if begin <= start < end:
            return str(chapter.get("title") or "").strip() or None
    return None


def title_repair_attempts(settings: dict[str, Any]) -> int:
    """Nombre de relances LLM d'un titre ; < 0, non entier ou booleen : CaptionsError qui le nomme (ADR-ad2e)."""
    value = settings["title_repair_attempts"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CaptionsError(f"[captions] title_repair_attempts doit etre un entier >= 0, recu {value!r}")
    return value


def parallel_workers(settings: dict[str, Any]) -> int:
    """Nombre de moments traites en parallele ; < 1 : CaptionsError qui le nomme."""
    value = settings["parallel"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CaptionsError(f"[captions] parallel doit etre un entier >= 1, recu {value!r}")
    return value


def _process_moment(
    moment: dict[str, Any],
    source: dict[str, Any],
    transcript: dict[str, Any],
    language: str,
    video_title: str,
    settings: dict[str, Any],
    hook_words_max: int,
    screen_title_words_max: int,
    config: Any,
    log_path: Path,
    channel: str = "",
    meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Clips d'un moment, ses parties traitees dans l'ordre, sequentiellement
    (la partie 1 fixe title et screen_title, repris par les suivantes)."""
    screen_title: str | None = None
    title: str | None = None
    parts_total = moment["parts_total"]
    clips: list[dict[str, Any]] = []
    for part in moment["parts"]:
        request_screen_title = screen_title is None
        request_title = title is None
        schema_settings = settings
        if request_title and parts_total > 1:
            suffix_len = len(str(settings["part_title_suffix"]).format(n=parts_total))
            schema_settings = {
                **settings,
                "title_max_chars": max(1, int(settings["title_max_chars"]) - suffix_len),
            }
        schema = response_schema(
            schema_settings, include_screen_title=request_screen_title, include_title=request_title
        )
        category = chapter_category(meta or {}, part["start"])
        check = _check_answer(
            hook_words_max, screen_title_words_max,
            list(settings["screen_title_forbidden_words"]), bool(settings["screen_title_allow_emoji"]),
            bool(settings["caption_allow_emoji"]),
            require_screen_title=request_screen_title,
            hook_words_target=_words_target(settings, "hook_words_target", "hook_words_max"),
            screen_title_words_target=_words_target(
                settings, "screen_title_words_target", "screen_title_words_max"),
            moderation_risk_terms=list(settings["moderation_risk_terms"]),
            screen_title_style=globals()["screen_title_style"](settings),
            screen_title_max_chars=screen_title_max_chars(settings),
            category=category, video_title=video_title, language=language,
        )
        text = _part_text(transcript, part["start"], part["end"])
        prompt = _prompt(language, video_title, source, part, parts_total, text, settings,
                          screen_title=screen_title, title=title, channel=channel,
                          category=category)
        answer = llm.ask("captions", prompt, [], schema, config=config, check=check, log_path=log_path,
                         repair_attempts=title_repair_attempts(settings))
        if request_screen_title:
            screen_title = answer["screen_title"]
        if request_title:
            title = answer["title"]
        final_title = title if parts_total == 1 else title + str(settings["part_title_suffix"]).format(n=part["part"])
        clips.append({
            "id": _clip_id(moment["id"], part["part"], parts_total),
            "moment_id": moment["id"],
            "part": part["part"],
            "parts_total": parts_total,
            "start": part["start"],
            "end": part["end"],
            "duration": part["duration"],
            "language": language,
            "title": final_title,
            "caption": answer["caption"],
            "hashtags": answer["hashtags"],
            "hook_text": answer["hook_text"],
            "screen_title": screen_title,
        })
    return clips


def run(
    video_id: str,
    workspace_dir: str | Path = "workspace",
    *,
    config: Any = None,
    force: bool = False,
) -> Path:
    """Ecrit titre, legende, hashtags et texte d'accroche de chaque clip de
    parts.json dans workspace/<video_id>/captions.json, dont le chemin est
    renvoye. Un resultat deja present n'est pas refait, sauf ``force``. Les
    moments sont traites en parallele ([captions] parallel, defaut 4), leurs
    parties restant sequentielles entre elles ; l'echec d'un moment fait
    remonter l'erreur sans rien ecrire."""
    video_dir = Path(workspace_dir) / video_id
    out = video_dir / "captions.json"
    if out.exists() and not force:
        return out

    parts_data = _read_json(video_dir / "parts.json")
    moments_data = _read_json(video_dir / "moments.json")
    transcript = _read_json(video_dir / "transcript.json")
    meta = _read_json(video_dir / "meta.json", optional=True) or {}
    settings = _settings(config)
    title_repair_attempts(settings)  # refusé ici, avant tout appel LLM
    screen_title_style(settings)  # idem (SPEC-08c2)
    screen_title_max_chars(settings)  # idem
    workers = parallel_workers(settings)
    language = transcript.get("language") or ""
    video_title = meta.get("title") or ""
    channel = meta.get("channel") or ""
    moments_by_id = {m["id"]: m for m in moments_data["moments"]}
    hook_words_max = int(settings["hook_words_max"])
    screen_title_words_max = int(settings["screen_title_words_max"])
    log_path = video_dir / "llm_refusals.jsonl"

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for moment in parts_data["moments"]:
        source = moments_by_id.get(moment["id"])
        if source is None:
            raise CaptionsError(f"moment {moment['id']} de parts.json absent de moments.json")
        pairs.append((moment, source))

    def process(pair: tuple[dict[str, Any], dict[str, Any]]) -> list[dict[str, Any]]:
        moment, source = pair
        return _process_moment(
            moment, source, transcript, language, video_title, settings,
            hook_words_max, screen_title_words_max, config, log_path, channel, meta,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        grouped = list(executor.map(process, pairs))

    clips = [clip for group in grouped for clip in group]
    _apply_cta_line(clips, settings)

    result = {"video_id": video_id, "clips": clips}
    video_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out
