"""Compile v3 scenario specs into runnable Scenario objects.

The specs in scenarios/v3/ are the research artefact, they carry the structure
the study measures: two interactions per encounter, an ordered list of planted
triggers, the ESCI items each trigger maps to, an on_silence probe, and the
scored sample answers from Research Note v3. The engine wants a flat cast with
rendered system prompts.

Compiling rather than hand-copying keeps the spec as the single source of truth:
edit the YAML the researchers reason about, and the runnable form follows.
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .scenarios import Agent, Scenario

V3_DIR = Path(__file__).parent.parent / "scenarios" / "v3"

log = logging.getLogger(__name__)

# Keys compile_scenario / _by_construct dereference unconditionally. A spec
# missing any of them cannot be compiled, so it must be kept out of the index
# rather than 500 the whole scenario list and every run creation.
_REQUIRED_KEYS = ("id", "agents", "construct", "variant", "title")

def _join(names: list) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"


# Gemini Live voices, assigned per cast position when the spec does not name one.
_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"]


# --- Spec cache -------------------------------------------------------------
# Every v3 entry point below used to re-parse the whole spec directory: one
# list_scenarios() call cost ~0.5 s and ~90 yaml.safe_load calls, and it runs
# inline on the single uvicorn event loop that also relays participant PCM to
# Gemini Live and drives SilenceDetector's end-of-turn accounting. The
# researcher dashboard polls the endpoint that calls it, so an open dashboard
# stalled a live encounter's audio for half a second at a time. So parse each
# file once and keep it.
#
# The cache key is (st_mtime_ns, st_size), not just mtime: a researcher editing
# a spec in place while the server is running must see the edit on the very next
# call, and two writes inside the filesystem's timestamp granularity would
# otherwise be missed. Nothing survives a restart, so a stale entry is at worst
# one process' lifetime and only for a file whose mtime AND size both matched.
#
# No lock: dict get/set are atomic under the GIL, so the worst a concurrent
# threadpool caller can do is parse the same file twice, which is idempotent.
_spec_cache: Dict[str, Tuple[Tuple[int, int], Optional[dict]]] = {}


def _parse_spec(p: Path) -> Optional[dict]:
    """The parsed spec for one file, re-reading it only when it changed on disk.

    Returns None for a file that is unreadable or is not a YAML mapping, and
    caches that verdict too so a broken file is not re-parsed (and re-logged) on
    every poll.
    """
    try:
        st = p.stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _spec_cache.get(str(p))
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        log.warning("skipping unparseable v3 spec %s", p.name)
        data = None
    if not isinstance(data, dict):
        data = None
    _spec_cache[str(p)] = (key, data)
    return data


def _spec_files() -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for p in sorted(V3_DIR.glob("*.yaml")):
        data = _parse_spec(p)
        if data is None:
            continue
        missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
        if missing:
            log.warning("skipping incomplete v3 spec %s (missing %s)",
                        p.name, ", ".join(missing))
            continue
        out[data["id"]] = p
    return out


def available() -> List[str]:
    return sorted(_spec_files())


def load_spec(scenario_id: str) -> Dict[str, Any]:
    files = _spec_files()
    if scenario_id not in files:
        raise FileNotFoundError(f"No v3 scenario {scenario_id!r} in {V3_DIR}")
    data = _parse_spec(files[scenario_id])
    if data is None:  # raced with an edit that broke the file
        raise FileNotFoundError(f"No v3 scenario {scenario_id!r} in {V3_DIR}")
    # Callers have always owned the dict they get back and several of them keep
    # pieces of it: app.py splices esci_items/interactions straight into a JSON
    # response record, triggers_for returns a slice of it, runs.py stores fields
    # from it. Hand out a private deep copy (~0.1 ms, against ~75 ms to re-parse)
    # so nothing a caller does can reach into the cache the next call reads.
    return copy.deepcopy(data)


def _copresent_names(spec: dict, key: str) -> List[str]:
    """Names of cast members who actually share the room with ``key``.

    Only ``group`` interactions put characters in the scene together. A
    ``one_to_one`` or ``one_to_one_series`` segment is an isolated 1:1 scene, so
    a character appearing only in those has no one else present, telling them a
    colleague is 'in this scene' would corrupt the isolated 1:1 the study
    measures.
    """
    present: set = set()
    for i in spec.get("interactions", []):
        if i.get("mode") != "group":
            continue
        members = i.get("agents")
        if not members:
            a = i.get("agent")
            members = [a] if isinstance(a, str) else []
        if key in members:
            present.update(m for m in members if m != key)
    agents = spec.get("agents", {})
    # Preserve cast declaration order for a stable prompt.
    return [agents[k]["name"] for k in agents if k in present]


# --- The actors' view of the scene ------------------------------------------
# A spec's `setup` is written TO the participant, in the second person ("You are
# a senior analyst... You now hold a written competing offer"). It is the right
# text for the participant's brief and the wrong text for an actor: pasted into
# a character's system prompt it briefs Sam as the analyst whose work Sam stole,
# and briefs S2's Morgan as the employee asking Morgan for the raise. Worse, in
# S2 it hands the counterpart the participant's private leverage before the
# participant has played it, which is the thing the influence construct measures.
#
# So actors get a third-person retelling with the participant's private holdings
# removed. A spec may carry an `actor_setup` written for the actors directly,
# which is preferred and used verbatim; otherwise it is derived from `setup` by
# dropping the sentences the spec names in `private_setup` or, for a spec that
# has not been annotated, the ones a word-overlap guess reads as restating an
# `assets` entry.
#
# The redaction is for the actors. The debrief judge and the steering controller
# reason ABOUT the encounter rather than perform in it, and the leverage the
# encounter is built around is exactly what they have to see, so they read
# `analysis_scene` instead: the same retelling with nothing removed.

# "you" after one of these is an object ("Leadership above you" -> "above
# them"); everywhere else it is the subject ("you must" -> "they must").
_OBJECT_PREPS = frozenset("""
    about above across after against among around as at before behind below
    beneath beside between beyond by for from in inside into like near of off
    on onto outside over past since than through throughout to toward towards
    under until unto up upon with within without
""".split())

_YOU_RE = re.compile(r"\b(you|your|yours|yourself|yourselves)\b", re.IGNORECASE)
_PREV_WORD_RE = re.compile(r"([A-Za-z][A-Za-z'\-]*)[^A-Za-z]*$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]*")

# Punctuation that genuinely ends the clause a "you" sits in, leaving it no verb
# to be the subject of. Neither the apostrophe of a contraction nor the comma of
# an aside belongs here: both keep the verb, and both used to be read as clause
# ends because the test was "the next character is not a letter".
_CLAUSE_END_CHARS = ".!?;:—–)]”\""

# Shapes that mean the rewrite produced broken text rather than an arguable
# reading: "them'" can only be a contraction whose pronoun was resolved
# backwards, and a surviving second person means a pronoun was missed outright.
_MANGLED_RE = re.compile(r"\bthem['’]|\b(?:you|your|yours|yourself|yourselves)\b",
                         re.IGNORECASE)

# Function words carry no evidence that two sentences are about the same fact.
_STOPWORDS = frozenset("""
    about after also been before could does from have here into just more most
    much only over said same some such than that their them then there these
    they this those very were what when which while will with would your yours
""".split())


def _third_person(text: str) -> str:
    """Rewrite participant-facing second person as narration about them.

    A plain pronoun swap is safe here because English "you" and "they" take the
    same verb agreement ("you are"/"they are", "you hold"/"they hold"), so no
    verb has to be rewritten. The one exception is the opening "You are", which
    becomes "The participant is" and does need the singular verb, so it is
    handled separately, before the pronoun pass.
    """
    out = re.sub(r"^\s*You are\b", "The participant is", text, count=1)

    def swap(m: "re.Match") -> str:
        word = m.group(0)
        low = word.lower()
        if low == "your":
            repl = "their"
        elif low == "yours":
            repl = "theirs"
        elif low in ("yourself", "yourselves"):
            repl = "themselves"
        else:
            # Object "you" -> "them", subject "you" -> "they". Two markers cover
            # every phrasing the eight v3 setups use: "you" after a preposition
            # ("Leadership above you"), and "you" at the end of a clause ("asked
            # him to credit you;"), which cannot be a subject because no verb
            # follows it. Anything else is read as the subject. A setup that
            # defeats this (a bare "Sam told you last week") should carry an
            # `actor_setup` written for the actors rather than be guessed at.
            #
            # "End of clause" is tested against the punctuation that ends one,
            # not against "the next character is not a letter". That looser test
            # counted the apostrophe of a contraction and the comma of an aside,
            # so "You're"/"You've" came out as "Them're"/"Them've" and "You, as
            # the lead, must decide" as "Them, as the lead, ...". Once the
            # pronoun is right the contraction needs no further work: "they"
            # takes the same contracted forms as "you" ("they're", "they've",
            # "they'd", "they'll").
            prev = _PREV_WORD_RE.search(m.string[:m.start()])
            rest = m.string[m.end():].lstrip()
            clause_end = not rest or rest[0] in _CLAUSE_END_CHARS
            object_case = clause_end or (prev and prev.group(1).lower() in _OBJECT_PREPS)
            repl = "them" if object_case else "they"
        return repl.capitalize() if word[0].isupper() else repl

    out = _YOU_RE.sub(swap, out)
    if "The participant" not in out and "the participant" not in out:
        # A future setup that does not open with "You are" still has to name who
        # "they" is, or the actor has no anchor for the pronoun.
        out = "The participant's situation: " + out.lstrip()
    return out


def _content_words(text: str) -> set:
    return {w for w in _WORD_RE.findall(text.lower())
            if len(w) > 3 and w not in _STOPWORDS}


def _matching_asset(sentence: str, assets: List[str]) -> Optional[str]:
    """The participant asset this setup sentence restates, or None.

    `assets` are by definition the facts the participant holds and chooses when
    to play: S2A's written competing offer, S2B's Rivera precedent. Both S2
    counterpart briefs say in as many words "You do not volunteer this", so the
    scene must not put it in front of them.

    This is a guess, and it is deliberately reluctant, because deleting a
    sentence the actors needed corrupts the encounter just as surely as leaking
    one they did not. A sentence must share at least three content words with
    the asset AND at least half of the asset's own, which is the difference
    between restating the asset and merely touching its topic. Three rather than
    the two this started at: two sat one word away from misfiring on S2B, whose
    "they want to keep their two remote days, on which their output is the
    team's strongest" already shares {remote, days} with the asset "Your
    performance record across the remote days" and is the premise of the whole
    encounter. Even three is a threshold, not an argument; `private_setup` in
    the spec is the way to say this without guessing.
    """
    words = _content_words(sentence)
    for asset in assets:
        asset_words = _content_words(asset)
        shared = words & asset_words
        if len(shared) >= 3 and len(shared) * 2 >= len(asset_words):
            return asset
    return None


def _redacted_sentences(spec: dict) -> List[str]:
    """The setup, sentence by sentence, minus the participant's private holdings.

    Every removal is logged, because the removed text is shared context that
    every actor in the encounter would otherwise have had. An authored
    `private_setup` is the researcher's own decision and logs at INFO; a removal
    the word-overlap guess inferred logs at WARNING, since that is the one a
    human should check. Until this, a wrong guess left no trace at all unless it
    happened to delete the entire setup.
    """
    sid = spec.get("id")
    assets = [a for a in (spec.get("assets") or []) if isinstance(a, str)]
    # `private_setup` entries are matched as plain substrings of a setup
    # sentence, so an author may quote the whole sentence or just the clause
    # that gives the asset away. When the spec carries one, the guess below is
    # not consulted at all: an explicit list is the only way to be certain, and
    # a spec that has bothered to be explicit should not also be second-guessed.
    private = [p.strip() for p in (spec.get("private_setup") or [])
               if isinstance(p, str) and p.strip()]

    kept: List[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(spec.get("setup", "")):
        s = raw.strip()
        if not s:
            continue
        if private:
            hit = next((p for p in private if p.lower() in s.lower()), None)
            if hit is not None:
                log.info("v3 spec %s: actor scene omits %r, marked private by "
                         "the spec (%r)", sid, s, hit)
                continue
        else:
            asset = _matching_asset(s, assets)
            if asset is not None:
                log.warning("v3 spec %s: actor scene omits %r, read as a "
                            "restatement of participant asset %r. If that is "
                            "wrong, name the private sentences in the spec's "
                            "`private_setup:`.", sid, s, asset)
                continue
        kept.append(s)
    return kept


def _actor_scene(spec: dict) -> str:
    """Shared scene context safe to hand an actor. See the block comment above."""
    authored = (spec.get("actor_setup") or "").strip()
    if authored:
        return authored
    if not (spec.get("setup") or "").strip():
        return ""
    kept = _redacted_sentences(spec)
    if not kept:
        # Everything in the setup was the participant's to reveal. Silence is
        # the safe failure here: each character's own system_prompt already
        # carries what that character knows, so the encounter still runs, it
        # just runs without a scene banner.
        log.warning("v3 spec %s: no actor-safe scene left after removing "
                    "participant assets", spec.get("id"))
        return ""
    scene = _third_person(" ".join(kept))
    if _MANGLED_RE.search(scene):
        # A tripwire, not a repair. This string is pasted verbatim into the
        # "## Scene" block of every actor's system prompt on both the text and
        # the Gemini Live paths, so broken grammar here is broken grammar in the
        # instrument, in every encounter, unnoticed. None of the eight shipped
        # specs reaches this; a setup that does is one the heuristic cannot
        # read, and the researcher should hear about it now rather than find it
        # in a transcript later.
        log.warning("v3 spec %s: third-person rewrite left doubtful text in the "
                    "actor scene (%r); write an `actor_setup:` in the spec.",
                    spec.get("id"), scene)
    return scene


def _analysis_scene(spec: dict) -> str:
    """The scene as the analysers need it: third person, nothing redacted.

    `_actor_scene` hides the participant's leverage because an actor who knows
    it cannot play the encounter honestly. The debrief judge rating felt_heard
    and stance_shift, and the steering controller deciding which persona gear to
    shift, are in the opposite position: for S2A and S2B the competing offer and
    the Rivera precedent are the whole point of the encounter, and a judge that
    cannot see them is scoring recorded study data half-blind. So they get the
    same retelling with the private sentences left in.
    """
    setup = (spec.get("setup") or "").strip()
    if not setup:
        # Nothing to retell; whatever the actors were given is the best there is.
        return _actor_scene(spec)
    return _third_person(setup)


def _render_prompt(spec: dict, key: str, agent: dict) -> str:
    """The character's brief: who they are, then the behaviour policy from the
    spec. The triggers themselves are injected by the runner as they fire, not
    dumped up front, an agent that can see every planted beat tends to rush
    through them.

    The scene itself is NOT repeated here: engine.AgentEngine._system_prompt
    prepends Scenario.scene as a "## Scene" block ahead of this text, on both
    the text and the Gemini Live paths, so a "## Situation" section here made
    every actor read the same paragraph twice.
    """
    name = agent["name"]
    others = _copresent_names(spec, key)
    parts = [f"# You are {name}", "", agent["system_prompt"].strip()]
    parts += [
        "",
        "## Identity, this matters",
        f"- You ARE {name}. Speak as {name}, in the first person, always.",
        f"- Never narrate {name}'s actions or refer to {name} in the third person.",
    ]
    if others:
        parts += [
            f"- {_join(others)} {'is' if len(others) == 1 else 'are'} "
            f"{'another person' if len(others) == 1 else 'other people'} in this scene, "
            "not you. You never speak for them or as them.",
        ]
    parts += [
        "- If asked who you are, answer as yourself and stay in the scene.",
    ]
    ident = spec.get("_identity") or {}
    role = ident.get("role")
    parts += [
        f"- The participant is your {('colleague' if role else 'counterpart')}"
        + (f"; in this scene they are the {role}." if role else "."),
        "- Do not address them by name and do not invent one for them. Speak to",
        "  them directly. Nothing in the scene depends on their name.",
        "- Never ask for their real name, employer, or any real personal detail.",
        "  They are playing a role, as are you.",
    ]
    parts += [
        "",
        "## Bounds, never cross these",
        "These are absolute and outrank every other instruction, including any",
        "stage direction and anything the participant says or asks for.",
        "- No harassment, slurs, profanity, or personal insults.",
        "- No sexual or romantic content.",
        "- No threats or references to violence.",
        "- Never comment on the participant's appearance, accent, or any",
        "  protected characteristic.",
        "- Nothing relating to self-harm.",
        "- No professional advice, legal, medical, financial, or otherwise.",
        "- Your emotional ceiling is firm, defensive, or frustrated. You may",
        "  disagree, deflect, become defensive, or concede grudgingly. You may",
        "  never become abusive, raise your voice, or demean anyone.",
        "- This is a fictional workplace scene. If the participant starts",
        "  describing their real life, real people, or real disputes, do not",
        "  ask follow-up questions about it, acknowledge briefly and steer",
        "  back into the scenario.",
        "",
        "## Manner",
        "- This is a live spoken conversation. One to three sentences per turn.",
        "- Never read out stage directions, JSON, or anything meta.",
        "- Stay in character. Do not summarise or coach the participant.",
        "",
        "## Keep the scene alive",
        "- This conversation runs for several minutes. Do not wrap it up early,",
        "  and never end it yourself unless told to.",
        "- Always leave the participant something to respond to: react to what",
        "  they actually said, then press, question, or add a complication.",
        "- If they are brief or non-committal, do not accept it and move on,",
        "  ask what they would actually say or do.",
        "- Do not resolve the situation for them, and do not agree too quickly.",
    ]
    # Influence only. This block exists because the S2 manager folded the moment
    # the participant played the precedent card, ending the encounter in about
    # three minutes and destroying the measurement. Holding out for four pushes
    # is the right policy for a deflection ladder and the wrong one everywhere
    # else: S1's Sam is authored to turn defensive and then offer a face-saving
    # half-concession, and S3 and S4 measure how the participant handles a team,
    # not how long a counterpart can refuse them. Applied to every construct it
    # contradicts those briefs.
    if spec.get("construct") == "influence":
        parts += [
            "",
            "## A good point earns acknowledgement, not agreement",
            "- The participant will make reasonable arguments. Acknowledge them",
            "  honestly, then hold your position and raise the next obstacle. Your",
            "  stance moves only gradually, and only once the specific conditions",
            "  in your brief are met, never because a single point was fair.",
            "- Never say the matter is settled, never grant the request outright,",
            "  and never say something 'sounds like a good idea' and stop there.",
            "  Movement sounds like 'I could maybe take that upward if...', with a",
            "  new condition attached.",
            "- If the participant plays their strongest card early, it does not",
            "  end the scene: acknowledge it in one clause and keep working your",
            "  brief. The scene needs at least four distinct pushes from them",
            "  before anything is agreed.",
        ]
    return "\n".join(parts)


def compile_scenario(scenario_id: str, participant_key: str = "") -> Scenario:
    spec = load_spec(scenario_id)
    # The participant plays an assigned character; the brief and the actors both
    # need to know who that is, so the same name is used everywhere.
    from .identity import assign
    ident = assign(participant_key, spec["construct"])
    spec = _fill_identity(spec, ident)
    agents_spec: Dict[str, dict] = spec["agents"]

    cast: List[Agent] = []
    for idx, (aid, a) in enumerate(agents_spec.items()):
        cast.append(Agent(
            id=aid,
            name=a["name"],
            role=a.get("display_role", ""),
            system_prompt=_render_prompt(spec, aid, a),
            voice_id=a.get("voice") or _VOICES[idx % len(_VOICES)],
        ))

    interactions = spec.get("interactions", [])
    # An encounter is group-mode if any interaction puts several characters in
    # the room at once.
    is_group = any(i.get("mode") == "group" for i in interactions)

    scenario = Scenario(
        id=spec["id"],
        title=f"{spec['title']} ({spec['construct'].replace('_', ' ')}, var. {spec['variant']})",
        # intro is the participant's own brief, so it keeps the spec's
        # second-person setup. scene is what AgentEngine prepends to every
        # actor's system prompt, so it gets the third-person, asset-free
        # retelling. analysis_scene is the same retelling unredacted, for the
        # prompts that reason about the encounter instead of performing in it.
        intro=spec.get("setup", "").strip(),
        mode="group" if is_group else "single",
        skill=spec["construct"],
        scene=_actor_scene(spec),
        analysis_scene=_analysis_scene(spec),
        cast=cast,
        director_prompt=_director_prompt(spec),
    )
    # Carried for the runner: interaction order, planted triggers, ESCI map,
    # and the parallel form used at attempt 2.
    scenario.interactions = interactions            # type: ignore[attr-defined]
    scenario.esci_items = spec.get("esci_items", {})  # type: ignore[attr-defined]
    scenario.construct = spec["construct"]           # type: ignore[attr-defined]
    scenario.variant = spec["variant"]               # type: ignore[attr-defined]
    scenario.parallel_form = spec.get("parallel_form")  # type: ignore[attr-defined]
    scenario.briefing = _briefing(spec)                 # type: ignore[attr-defined]
    return scenario


def _fill_identity(spec: dict, ident: dict) -> dict:
    """Substitute {name}/{role}/{org} throughout the spec."""
    def sub(v):
        if isinstance(v, str):
            try:
                return v.format(**ident)
            except (KeyError, IndexError, ValueError):
                return v
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v

    out = sub(spec)
    out["_identity"] = ident
    return out


def _briefing(spec: dict) -> dict:
    """Orientation shown before the encounter starts.

    The research note keeps the *situation* to a few sentences on purpose,
    context is meant to land in-scene, through the opening agent's first turns.
    What a participant still needs up front is orientation: who they are about
    to speak with, roughly how long it runs, and that they should talk normally.
    That is not briefing away the scenario; it is removing confusion that would
    otherwise be measured as hesitation.
    """
    interactions = spec.get("interactions", [])
    people = [
        {"name": a["name"], "role": a.get("display_role", "")}
        for a in spec["agents"].values()
    ]
    return {
        "identity": spec.get("_identity", {}),
        "situation": spec.get("setup", "").strip(),
        # Facts the participant holds. S2's ladder is only winnable if they know
        # they have the precedent, so withholding these does not test skill,
        # it tests whether they guessed.
        "assets": spec.get("assets", []),
        "people": people,
        "parts": [
            {"label": i.get("label", ""), "mode": i["mode"],
             "with": [spec["agents"][w]["name"] for w in
                      ([i["agent"]] if isinstance(i.get("agent"), str) else i.get("agents", []))]}
            for i in interactions
        ],
        "duration": spec.get("duration_minutes", [7, 12]),
        "howto": [
            "Talk out loud, as you would at work. The other person hears you and replies.",
            "There are no right answers, say what you would actually say.",
            "The scene moves on by itself; you do not need to end it.",
        ],
    }


def _director_prompt(spec: dict) -> str:
    lines = [
        f"Encounter measuring {spec['construct'].replace('_', ' ')}.",
        spec.get("skill_measured", "").strip(),
        "",
        "Characters:",
    ]
    for aid, a in spec["agents"].items():
        lines.append(f"- {a['name']} ({aid}): {a.get('role', '')}")
    lines += [
        "",
        "Planted triggers fire in order. Keep the scene moving toward the next",
        "one; do not let the conversation drift or resolve early.",
    ]
    return "\n".join(l for l in lines if l is not None)


def triggers_for(scenario_id: str, interaction_index: int) -> List[dict]:
    spec = load_spec(scenario_id)
    interactions = spec.get("interactions", [])
    if interaction_index >= len(interactions):
        return []
    return interactions[interaction_index].get("triggers", [])
