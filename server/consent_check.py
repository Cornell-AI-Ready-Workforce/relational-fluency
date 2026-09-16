"""Refuse to field a consent form that is still the shipped template.

config/consent.yaml arrives as a template with nobody's name in it, a
placeholder version, and a body that describes one conversation and never says
that the participant's live microphone audio is transmitted to a model
provider. Every one of those is invisible at run time: the app serves the
template happily, the page renders it, participants tick the box, and
consent_text_version records that they agreed to "v0.1-2026-06". The failure
only becomes apparent when somebody asks to have their data deleted and there
is nobody named to ask.

So the check is here rather than in a reviewer's head. `consent_fielding_blocker`
answers one question — is this configuration fit to put in front of a real
participant — and returns the reason when it is not, so that server/app.py can
turn a silent IRB violation into a loud refusal to start.

The checks are deliberately about *unfilledness*, not about wording quality: no
program can tell approved language from a plausible draft, which is why
`irb_status.reviewed` exists as an explicit human act.
"""

from __future__ import annotations

import re
from typing import Any

# The version string this repository ships with. A wave collected under it
# cannot be distinguished afterwards from a wave collected under any other
# unedited copy of the template.
TEMPLATE_VERSIONS = {"v0.1-2026-06"}

# The shapes a "somebody still has to type here" marker takes in this file:
# config/consent.yaml's own "[FILL IN: ...]" convention plus the usual suspects.
_PLACEHOLDER = re.compile(
    r"\bfill[ _-]?in\b|\bTBD\b|\bTODO\b|\bXXX+\b|\bplaceholder\b", re.I)

# Stricter for prose, because approved consent text may legitimately contain the
# words "fill in" ("fill in the survey") and refusing to field over that would
# make the guard something a researcher edits around rather than answers.
_PROSE_PLACEHOLDER = re.compile(
    r"\[\s*FILL[ _-]?IN\b[^\]]*\]|\bFILL IN\b|\[\s*TBD\s*\]|\bTODO\b")

# A version that still reads like a draft is as ambiguous in the record as the
# template's own string, so it fails for the same reason.
_DRAFT_VERSION = re.compile(
    r"draft|template|example|placeholder|unapproved|tbd|todo|fill", re.I)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

# Providers whose models this project can be pointed at. The body has to name
# whichever one receives participant speech; naming none is the state the
# template shipped in.
_PROVIDERS = re.compile(
    r"\bgemini\b|\bgoogle\b|\bopenai\b|\bgpt\b|\banthropic\b|\bclaude\b"
    r"|\bazure\b|\bmistral\b|\bllama\b|\bmeta\b", re.I)

_SPEECH = re.compile(r"\baudio\b|\bvoice\b|\bmicrophone\b|\bspeech\b|\bwhat you say\b", re.I)
_TRANSMISSION = re.compile(
    r"\btransmit|\bsent\b|\bsends\b|\bsend\b|\bstream|\bshared with\b|\bgoes to\b|\bpassed to\b", re.I)
# "never", "not" and "no" flip the meaning of the sentence they appear in. The
# template's ONLY mention of a model provider was a negative one — webcam video
# "is never transmitted to any AI model provider" — and reading it as disclosure
# is precisely the mistake that let the missing audio statement go unnoticed for
# a whole template. A negated sentence therefore does not satisfy this check.
_NEGATION = re.compile(r"\bnever\b|\bnot\b|\bno\b|\bnothing\b", re.I)


def _unfilled(value: Any) -> bool:
    """True when this field is blank or still carries a template placeholder."""
    text = str(value if value is not None else "").strip()
    if not text:
        return True
    if _PLACEHOLDER.search(text):
        return True
    # A bare "<the PI's name>" or "[study email]" is the other shape a
    # placeholder takes once somebody has replaced the FILL IN wording but not
    # the value.
    return bool(re.fullmatch(r"[<\[].*[>\]]", text, re.S))


def _discloses_audio_transmission(body: str) -> bool:
    """Does the body affirmatively say participant speech reaches a provider?

    Sentence by sentence, because the claim has to be made in one place to be
    readable as a claim: a body that names Google in one paragraph and mentions
    microphone audio in another has told the participant nothing about where
    their voice goes.
    """
    for sentence in re.split(r"(?<=[.!?])\s+|\n\n+", body or ""):
        if _NEGATION.search(sentence):
            continue
        if (_SPEECH.search(sentence)
                and _PROVIDERS.search(sentence)
                and _TRANSMISSION.search(sentence)):
            return True
    return False


def consent_fielding_blocker(cfg: dict) -> str | None:
    """Why this consent config must not be shown to a participant, or None.

    None means every field a human has to supply has been supplied, the text
    has been marked reviewed, and the body says where the participant's voice
    goes. It does NOT mean the wording is approved — only a person can say
    that, and `irb_status.reviewed` is where they say it.
    """
    if not isinstance(cfg, dict) or not cfg:
        return ("config/consent.yaml did not load as a consent form "
                "(no mapping of settings was found in it)")

    reasons: list[str] = []

    version = str(cfg.get("version") or "").strip()
    if not version:
        reasons.append(
            "carries no `version`, so nothing records which wording a participant agreed to")
    elif version in TEMPLATE_VERSIONS:
        reasons.append(
            f"still carries the shipped template's version {version!r}, so consent_text_version "
            "cannot distinguish this wave from an unedited copy of the template")
    elif _DRAFT_VERSION.search(version):
        reasons.append(
            f"carries a draft version string ({version!r}); bump it to the approved text's own version")

    for field in ("title", "body", "confirm_checkbox"):
        if not str(cfg.get(field) or "").strip():
            reasons.append(f"has no `{field}`")

    irb_status = cfg.get("irb_status")
    reviewed = irb_status.get("reviewed") if isinstance(irb_status, dict) else None
    # A string is accepted because YAML quoting is easy to get wrong and a
    # reviewer who typed `reviewed: "true"` has still done the reviewing.
    if not (reviewed is True or str(reviewed).strip().lower() in {"true", "yes"}):
        reasons.append(
            "is not marked reviewed (`irb_status.reviewed` is not true), so this is still draft "
            "text that no one has confirmed matches the approved protocol")

    contact = cfg.get("contact")
    if not isinstance(contact, dict):
        reasons.append(
            "has no `contact:` block, and the withdrawal and decline cards have no one to name")
    else:
        missing = [k for k in ("pi_name", "email", "irb_protocol") if _unfilled(contact.get(k))]
        if missing:
            reasons.append(
                "has unfilled contact details (" + ", ".join(missing) + "), so a participant "
                "asking for their data to be deleted is pointed at a blank")
        email = str(contact.get("email") or "").strip()
        if email and not _unfilled(email) and not _EMAIL.match(email):
            reasons.append(f"has a study contact email that is not an address ({email!r})")

    body = str(cfg.get("body") or "")
    if _PROSE_PLACEHOLDER.search(body) or _PROSE_PLACEHOLDER.search(str(cfg.get("title") or "")):
        reasons.append(
            "still contains FILL IN markers, which a participant would read as part of the form")
    if body and not _discloses_audio_transmission(body):
        reasons.append(
            "never says that the participant's live microphone audio is transmitted to the "
            "model provider, which is the one disclosure this platform's own design requires")

    if not reasons:
        return None
    return "config/consent.yaml is not fit to field: " + "; ".join(reasons) + "."
