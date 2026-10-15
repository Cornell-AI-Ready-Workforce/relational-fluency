"""Two more participant-transcript gates: the transcriber's own confidence,
and whether a line has any English in it (pipeline 2026-10-12a).

The speech check (server/speech_check.py, 30a) hears a suspect line again on
a second model. It still lets a few phantoms through in production: a check
that fails or runs out of time keeps the line as it came; a line it does not
select (four words or more, not over a character's playback) is never heard
again; and a line it hears words in keeps the LIVE text, so "Tuurlijk." stays
"Tuurlijk." whatever the check heard. These two gates ask different questions
of different evidence, and neither calls a model.

THE CONFIDENCE GATE (gpt route). gpt-4o-transcribe returns a log-probability
for every token it wrote when the session asks for them (`include:
["item.input_audio_transcription.logprobs"]`; accepted by the Cornell gateway
on gpt-realtime-2.1, 2026-10-12, and every completed transcript then carries
`logprobs`). A word's score is the sum of its tokens' logprobs (the joint
probability the transcriber gave the whole word, so a name split into
confident sub-word pieces is judged as one word), punctuation tokens left out.
A line whose MEAN word score is under PARTICIPANT_MIN_WORD_LOGPROB is the
transcriber guessing, and is suppressed as `low_transcriber_confidence`.

  Measured live (scratchpad probe/, gpt-realtime-2.1, two runs each):
    real short lines (macOS `say`): "No." "Okay." "Um, yeah." "Hi Morgan."
      "Good." "Sure." "Maybe." "Right" "Yes."         worst word -0.25 (Okay)
    inventions over room tone, bursts and digital silence: "Sure." -3.51,
      "Yes." -3.58/-3.58, "Ok" -4.59, "What?" -5.72, "Zzz" -7.56, "SP" -9.71,
      "phhh" -10.7, "Foul language" mean -10.1, "치료" -11.1, "(Drumroll)"
      -4.44, "[Applause]" -2.95                       best -2.95
    a character's voice leaking back at -24 dB: "than they should right
      now." mean -0.53 -- confident. Echo is the speech check's job, not this.
    clearly spoken Dutch: "Tuurlijk, dat is goed." every token ~0.0 --
      confident. Language is the second gate's job, not this.
  -2.0 sits between the real worst (-0.25) and the phantoms' best (-2.95),
  nearer the phantoms, because a real microphone scores real speech lower
  than synthetic speech does and dropping a line someone said is the worse
  error. Every user_turn records its scores so the bar can be re-read off
  production.

THE ENGLISH GATE (every route). A line of up to PARTICIPANT_LANGUAGE_GATE_MAX_WORDS
words of which NOT ONE is English (a word list, the cast's names, and a few
conversational words; see is_english_word) is not something an English-speaking
participant said: "Tuurlijk.", "լավ.", "치료". Where the speech check heard
English in the same audio, that is written instead (as the check already does
for another script); otherwise the line is suppressed as `not_english`. One
English word anywhere keeps a line, so a name the list lacks, slang or a
misspelt word never sinks a sentence; and longer lines are left alone, since
those are far likelier to be the participant's own English written in the
wrong script than an invention (script_mismatch still flags them).

  The word list is Webster's Second (1934, public domain; /usr/share/dict/web2
  on BSD and macOS) plus its propernames list, lowercased, letters only:
  234,454 words, english_words.txt.gz beside this file. It has no inflections
  and nothing modern, so a word is also tried without its common endings
  ("talked" -> "talk", "deadlines" -> "deadline", "we're" -> "we"), with
  a held sound squeezed ("Ehh" -> "eh"), and against _EXTRA; a short
  all-capitals word ("TC") is taken as an initialism.

Knobs, read per call:
  TRANSCRIPTION_LOGPROBS              default on; "0" stops asking for them
                                      (and so turns the confidence gate off)
  PARTICIPANT_MIN_WORD_LOGPROB        default -2.0; "off" turns the gate off
  PARTICIPANT_LANGUAGE_GATE           default on; "0" turns it off
  PARTICIPANT_LANGUAGE_GATE_MAX_WORDS default 6; 0 applies it at any length
"""
from __future__ import annotations

import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

from ..llm import setting

LOGPROBS_INCLUDE = "item.input_audio_transcription.logprobs"
DEFAULT_MIN_WORD_LOGPROB = -2.0
DEFAULT_LANGUAGE_MAX_WORDS = 6

_WORDS_FILE = Path(__file__).with_name("english_words.txt.gz")

# Conversational and modern words Webster's Second does not have, in their
# base form (endings are stripped before the lookup).
_EXTRA = frozenset("""
ok okay alright yeah yep yup nope nah hmm hm mhm mm um umm uh uhh huh ah eh
oh ooh wow whoa oops yikes ugh hey hiya bye lol gonna wanna gotta kinda sorta dunno
gimme lemme ya y'all email e-mail online offline internet website app
smartphone laptop text zoom slack teammate cofounder co-worker coworker
feedback deadline workflow onboard onboarding standup sync async
""".split())

# Endings tried, longest first, with what to put back.
_SUFFIXES = (
    ("n't", ""), ("'re", ""), ("'ve", ""), ("'ll", ""), ("'s", ""),
    ("'d", ""), ("'m", ""), ("iest", "y"), ("ies", "y"), ("ied", "y"),
    ("ier", "y"), ("ily", "y"), ("ing", ""), ("ing", "e"), ("est", ""),
    ("est", "e"), ("ed", ""), ("ed", "e"), ("er", ""), ("er", "e"),
    ("es", ""), ("ly", ""), ("s", ""), ("d", ""),
)

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _flag(name: str, default: str) -> bool:
    return setting(name, default).strip().lower() not in (
        "0", "false", "no", "off", "")


# ── the confidence gate ─────────────────────────────────────────────────────

def logprobs_enabled() -> bool:
    return _flag("TRANSCRIPTION_LOGPROBS", "1")


def min_word_logprob() -> Optional[float]:
    """The bar, or None where PARTICIPANT_MIN_WORD_LOGPROB turns it off."""
    raw = setting("PARTICIPANT_MIN_WORD_LOGPROB", str(DEFAULT_MIN_WORD_LOGPROB))
    if raw.strip().lower() in ("off", "none", ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_WORD_LOGPROB


def _is_punctuation(token: str) -> bool:
    # U+FFFD is half of a character the tokenizer split across two tokens
    # ("啵" arrives as two of them): word content, not a mark.
    return all(c.isspace() or (c != "�" and
                               unicodedata.category(c)[0] in "PS")
               for c in token)


def word_logprobs(tokens: Optional[Iterable[dict]]) -> list:
    """The transcriber's per-token logprobs, joined into words: a token that
    starts with a space, or follows punctuation, starts a new word; a word's
    score is the sum of its tokens'. Punctuation tokens are not words. []
    where there are none (the route sends no logprobs, or the line is all
    punctuation)."""
    words: list = []
    open_word = False
    for tok in tokens or ():
        if not isinstance(tok, dict):
            continue
        text, lp = tok.get("token") or "", tok.get("logprob")
        if not isinstance(lp, (int, float)):
            continue
        if _is_punctuation(text):
            open_word = False
            continue
        if open_word and not text[:1].isspace():
            words[-1] += lp
        else:
            words.append(float(lp))
        open_word = True
    return words


def confidence(tokens: Optional[Iterable[dict]]) -> Optional[dict]:
    """{mean_word_logprob, min_word_logprob, words} for a transcript's
    logprobs, or None where there are no word scores to judge."""
    words = word_logprobs(tokens)
    if not words:
        return None
    return {"mean_word_logprob": round(sum(words) / len(words), 3),
            "min_word_logprob": round(min(words), 3), "words": len(words)}


def unsure(conf: Optional[dict]) -> bool:
    """The confidence gate's verdict: True when the transcriber's own mean
    word score is under the bar. Never where there are no scores (another
    route, a fake bridge, the knob off)."""
    bar = min_word_logprob()
    return (bar is not None and isinstance(conf, dict)
            and isinstance(conf.get("mean_word_logprob"), (int, float))
            and conf["mean_word_logprob"] < bar)


# ── the English gate ────────────────────────────────────────────────────────

def language_gate_enabled() -> bool:
    return _flag("PARTICIPANT_LANGUAGE_GATE", "1")


def language_max_words() -> int:
    try:
        return max(0, int(setting("PARTICIPANT_LANGUAGE_GATE_MAX_WORDS",
                                  str(DEFAULT_LANGUAGE_MAX_WORDS))))
    except ValueError:
        return DEFAULT_LANGUAGE_MAX_WORDS


@lru_cache(maxsize=1)
def _lexicon() -> frozenset:
    with gzip.open(_WORDS_FILE, "rt", encoding="ascii") as f:
        return frozenset(f.read().split())


def _fold(word: str) -> str:
    """Lowercased, curly apostrophe straightened, accents off a Latin letter
    ("café" -> "cafe"). A letter of another script is left as it is, and so
    never matches the list."""
    w = word.lower().replace("’", "'")
    out = []
    for c in w:
        base = unicodedata.normalize("NFKD", c)[0]
        out.append(base if base.isascii() else c)
    return "".join(out)


def is_english_word(word: str, names: Iterable[str] = ()) -> bool:
    if word.isupper() and len(word) <= 4 and word.isascii():
        # An initialism or a name said as letters ("TC", "HR", "OK").
        return True
    w = _fold(word)
    known = _lexicon()
    extra = _EXTRA | {_fold(n) for n in names if n}
    if w in known or w in extra:
        return True
    # A sound held on: "Ehh", "Hmmm", "Sooo" -> "eh", "hm", "so".
    squeezed = re.sub(r"(.)\1+", r"\1", w)
    if squeezed != w and (squeezed in known or squeezed in extra):
        return True
    for end, put in _SUFFIXES:
        if w.endswith(end) and len(w) - len(end) >= 2:
            stem = w[: len(w) - len(end)] + put
            if stem in known or stem in extra:
                return True
            # "stopped" -> "stopp" -> "stop"
            if (put == "" and len(stem) >= 3 and stem[-1] == stem[-2]
                    and stem[:-1] in known):
                return True
    return False


def not_english(text: str, names: Iterable[str] = ()) -> Optional[dict]:
    """The English gate's verdict on one line: None to keep it, else
    {words, unknown} for a line of at most language_max_words() words of
    which none is English. A line with a digit in it, or with no words at
    all (the word-less gates' business), is kept."""
    if not language_gate_enabled() or not text or re.search(r"\d", text):
        return None
    words = _WORD.findall(text)
    cap = language_max_words()
    if not words or (cap and len(words) > cap):
        return None
    names = list(names or ())
    if any(is_english_word(w, names) for w in words):
        return None
    return {"words": len(words), "unknown": words}


def provenance() -> dict:
    return {"transcription_logprobs": logprobs_enabled(),
            "participant_min_word_logprob": min_word_logprob(),
            "participant_language_gate": language_gate_enabled(),
            "participant_language_gate_max_words": language_max_words()}
