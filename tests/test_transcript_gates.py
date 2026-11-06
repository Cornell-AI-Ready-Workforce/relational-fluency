"""The confidence gate and the English gate (pipeline 2026-11-06a).

Built on top of the speech check (tests/test_speech_check.py), which still
lets a few phantoms through in production. The confidence gate reads the
transcriber's own per-token logprobs; the logprobs below are the live ones
from gpt-realtime-2.1 / gpt-4o-transcribe on 2026-10-12 (macOS `say` for the
real lines; room tone, bursts and digital silence for the inventions). The
English gate reads the words. Everything is offline.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from server import llm  # noqa: E402
from server import speech_check as sc  # noqa: E402
from server.voice import realtime as R  # noqa: E402
from server.voice import transcript_gates as G  # noqa: E402
from test_participant_turn_integrity import (  # noqa: E402
    GEMINI, GPT, LOUD, bridge, in_a_loop, next_transcript, runner_for,
)
from test_speech_check import Answers, gpt_runner  # noqa: E402,F401

PCM = LOUD * 50


def toks(*pairs):
    return [{"token": t, "logprob": lp, "bytes": []} for t, lp in pairs]


# Measured live (see transcript_gates.py).
REAL = {
    "No.": toks(("No", -0.001), (".", -0.129)),
    "Okay.": toks(("Okay", -0.253), (".", -0.209)),
    "Um, yeah.": toks(("Um", -0.002), (",", -0.023), (" yeah", -0.0), (".", -0.024)),
    "Hi Morgan.": toks(("Hi", -0.0), (" Morgan", -0.049), (".", -0.116)),
    "Yes.": toks(("Yes", -0.0), (".", -0.391)),
    "Hi Riley, I think": toks(("Hi", -0.0), (" Riley", -0.046), (",", -0.006),
                              (" I", -0.0), (" think", 0.0)),
}
INVENTED = {
    "Sure.": toks(("Sure", -3.512), (".", -0.444)),
    "Yes.": toks(("Yes", -3.579), (".", -0.876)),
    "Ok": toks(("Ok", -4.585)),
    "What?": toks(("What", -5.715), ("?", -0.98)),
    "Foul language": toks(("F", -5.484), ("oul", -6.408), (" language", -8.315)),
    "치료": toks(("치", -9.062), ("료", -2.009)),
    "啵": toks(("�", -8.552), ("�", -1.128)),
    "Zzz": toks(("Z", -6.954), ("zz", -0.604)),
}


@pytest.fixture
def answers(monkeypatch):
    def install(text="[no speech]", delay=0.0):
        a = Answers(text, delay)
        monkeypatch.setattr(sc, "_post", a)
        return a
    return install


# --------------------------------------------------------------------------
# 1. Word scores and the confidence gate
# --------------------------------------------------------------------------

def test_tokens_join_into_words_and_punctuation_is_not_a_word():
    assert G.word_logprobs(INVENTED["Foul language"]) == pytest.approx(
        [-11.892, -8.315])
    assert G.word_logprobs(REAL["Um, yeah."]) == pytest.approx([-0.002, 0.0])
    # A character split across two tokens is one word, not two marks.
    assert G.word_logprobs(INVENTED["啵"]) == pytest.approx([-9.68])
    assert G.word_logprobs(toks(("。", -3.7))) == []
    assert G.word_logprobs(None) == [] and G.confidence(None) is None
    assert G.word_logprobs([{"token": "x"}, "junk"]) == []


@pytest.mark.parametrize("text", sorted(REAL))
def test_every_real_line_is_confident(text):
    assert not G.unsure(G.confidence(REAL[text]))


@pytest.mark.parametrize("text", sorted(INVENTED))
def test_every_invention_is_not(text):
    assert G.unsure(G.confidence(INVENTED[text]))


def test_the_bar_is_a_knob(monkeypatch):
    sure = G.confidence(INVENTED["Sure."])
    monkeypatch.setenv("PARTICIPANT_MIN_WORD_LOGPROB", "-4")
    assert not G.unsure(sure)
    monkeypatch.setenv("PARTICIPANT_MIN_WORD_LOGPROB", "off")
    assert not G.unsure(G.confidence(INVENTED["Foul language"]))
    monkeypatch.setenv("PARTICIPANT_MIN_WORD_LOGPROB", "nonsense")
    assert G.min_word_logprob() == G.DEFAULT_MIN_WORD_LOGPROB
    assert not G.unsure(None) and not G.unsure({})


# --------------------------------------------------------------------------
# 2. The English gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Tuurlijk.", "լավ.", "치료", "啵", "Привет", "Merci beaucoup.",
    "Danke schön.", "Natuurlijk, dat klopt.", "Gracias.", "Oui."])
def test_a_short_line_with_no_english_in_it_is_not_english(text):
    assert G.not_english(text) is not None


@pytest.mark.parametrize("text", [
    # The rest of s_1790781273_b8b7cc's phantoms are English: this gate is
    # not for them (the confidence gate and the speech check are).
    "Democrat", "Post.", "Hi.", "Sexuality", "Ehh", "The", "Good afternoon.",
    # Real lines, and the ways a word list goes wrong.
    "Okay.", "Yeah, totally.", "We're gonna miss the deadlines.", "Um",
    "Mhm.", "Hmmm.", "Texted you.", "Stopped.", "Emailed it.", "Kinda.",
    "Nope.", "HR?", "OK", "Hi Barley", "I'm organ.", "Yikes", "résumé", "Two.",
    "7", "Riley?", "Gotcha.", "Welp.", "Meh.", "Hmph.",
])
def test_english_lines_are_kept(text):
    assert G.not_english(text, ["Riley", "Morgan"]) is None


def test_names_count_as_english():
    assert G.not_english("Priyanka?") is not None
    assert G.not_english("Priyanka?", ["Priyanka"]) is None


def test_names_only_a_brief_mentions_count_too():
    # Review of #81, point 3: "Priya" is cast only in S4 but a brief in S3A
    # mentions her leaving, and a participant saying "Priya?" was suppressed.
    brief = "Jordan has stopped his extra review pass since Priya left. The team is tired."
    names = G.names_in(brief)
    assert {"Jordan", "Priya", "The"} <= names
    assert G.not_english("Priya?", names) is None
    assert G.not_english("Priya?") is not None


def test_only_listed_initialisms_pass_as_letters():
    # Point 7: "SP" was one of the measured phantoms and any short all-caps
    # token used to count as an initialism.
    assert G.not_english("SP") is not None
    assert G.not_english("HR?") is None and G.not_english("OK") is None
    assert G.not_english("TC?") is not None and G.not_english("TC?", ["TC"]) is None


def test_the_word_list_is_loaded_at_import():
    # Point 6: not on the event loop, on the first participant line.
    assert G._lexicon.cache_info().currsize == 1


def test_a_long_line_is_left_alone_and_the_knobs_move_it(monkeypatch):
    long = "Natuurlijk klopt helemaal zij hebben gelijk gehad echt"
    assert G.not_english(long) is None
    monkeypatch.setenv("PARTICIPANT_LANGUAGE_GATE_MAX_WORDS", "0")
    assert G.not_english(long) is not None
    monkeypatch.setenv("PARTICIPANT_LANGUAGE_GATE", "0")
    assert G.not_english("Tuurlijk.") is None


# --------------------------------------------------------------------------
# 3. The bridge asks for logprobs on the gpt route only, and hands them on
# --------------------------------------------------------------------------

def test_only_the_gpt_session_asks_for_logprobs(monkeypatch):
    gpt = R.RealtimeVoiceSession("x", model=GPT, voice="", api_key="k")
    assert gpt._session_payload()["include"] == [
        "item.input_audio_transcription.logprobs"]
    gem = R.RealtimeVoiceSession("x", model=GEMINI, voice="", api_key="k")
    assert "include" not in gem._session_payload()
    monkeypatch.setenv("TRANSCRIPTION_LOGPROBS", "0")
    assert "include" not in gpt._session_payload()
    monkeypatch.delenv("TRANSCRIPTION_LOGPROBS")
    # Not probed with whisper-1, so not asked for on it.
    monkeypatch.setenv("INPUT_TRANSCRIPTION_MODEL", "whisper-1")
    assert "include" not in gpt._session_payload()


@in_a_loop
async def test_a_transcript_carries_its_word_scores():
    rt = bridge(GPT)
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="i1", transcript="Sure.", logprobs=INVENTED["Sure."])
    ev = await next_transcript(rt)
    assert ev["confidence"] == {"mean_word_logprob": -3.512,
                                "min_word_logprob": -3.512, "words": 1}
    rt.ws.feed(type="conversation.item.input_audio_transcription.completed",
               item_id="i2", transcript="Hi.")
    assert (await next_transcript(rt))["confidence"] is None


def test_provenance_says_which_gates_ran():
    gpt = llm.provenance(GPT)["turn_gate"]["participant_transcript_gates"]
    assert gpt["transcription_logprobs"] is True
    assert gpt["participant_language_gate"] is True
    gem = llm.provenance(GEMINI)["turn_gate"]["participant_transcript_gates"]
    assert gem["transcription_logprobs"] is False
    assert llm.PIPELINE_VERSION >= "2026-11-06a"


# --------------------------------------------------------------------------
# 4. What the runner does
# --------------------------------------------------------------------------

def withdrawals(runner):
    out = []

    async def withdraw(text, reason, committed_at, voiced_ms):
        out.append((text, reason))
    runner._withdraw_reply = withdraw
    return out


@in_a_loop
async def test_an_unsure_line_the_check_hears_nothing_in_is_suppressed(answers):
    # Two independent readings agree: the transcriber was guessing and the
    # second model hears no speech. The check decides first (no_speech), and
    # the transcriber's scores are on the record with it.
    a = answers("[no speech]")
    runner, session, ws = gpt_runner()
    withdrawn = withdrawals(runner)
    await runner._await_participant("start")
    await runner._record_user_turn(
        "Sure.", item_id="i1", voiced_ms=900, pcm=PCM,
        confidence=G.confidence(INVENTED["Sure."]))
    assert not session.store.of("user_turn")
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech" and sup["text"] == "Sure."
    assert withdrawn == [("Sure.", "no_speech")]
    assert len(a.payloads) == 1, "the hearing ran; the gate did not pre-empt it"
    assert not ws.frames("user_transcript")
    assert runner._awaiting_participant


@in_a_loop
async def test_an_unsure_line_the_check_hears_words_in_is_kept_and_flagged(answers):
    # Review of #81, point 4: a confident four-token name sums to about -2.2
    # and was suppressed with the hearing that could rescue it cancelled.
    answers("Priyanka.")
    runner, session, ws = gpt_runner()
    withdrawn = withdrawals(runner)
    conf = G.confidence(toks(("Pri", -0.6), ("yan", -0.6), ("k", -0.6), ("a", -0.6), (".", -0.3)))
    assert G.unsure(conf)
    await runner._record_user_turn("Priyanka.", voiced_ms=700, pcm=PCM, confidence=conf)
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Priyanka." and turn["speech_check"] == "heard"
    assert turn["transcriber_unsure"] is True and turn["transcriber_confidence"] == conf
    assert turn["language_unknown"] == ["Priyanka"]  # not in the list, not in S2A's text
    assert withdrawn == [] and not session.store.of("user_turn_suppressed")
    assert [f["text"] for f in ws.frames("user_transcript")] == ["Priyanka."]


@pytest.mark.parametrize("answer", [RuntimeError("gateway down")])
def test_an_unsure_line_the_check_cannot_decide_on_is_suppressed(answers, answer):
    answers(answer)
    runner, session, _ = gpt_runner()
    withdrawn = withdrawals(runner)
    asyncio.run(runner._record_user_turn(
        "Sure.", voiced_ms=900, pcm=PCM, confidence=G.confidence(INVENTED["Sure."])))
    assert not session.store.of("user_turn")
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "low_transcriber_confidence"
    assert sup["mean_word_logprob"] == -3.512 and sup["speech_check"] == "error"
    assert withdrawn == [("Sure.", "low_transcriber_confidence")]


@in_a_loop
async def test_an_unsure_line_with_no_hearing_for_it_is_suppressed(answers):
    # Four words or more, not over playback: the check does not select it,
    # so the transcriber's own doubt is all there is.
    a = answers("whatever")
    runner, session, _ = gpt_runner()
    withdrawn = withdrawals(runner)
    conf = G.confidence(toks(("Foul", -6.0), (" language", -8.0), (" in", -5.0),
                             (" here", -4.0), (" now", -6.0)))
    await runner._record_user_turn("Foul language in here now", voiced_ms=1500,
                                   pcm=PCM, confidence=conf)
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "low_transcriber_confidence" and "speech_check" not in sup
    assert a.payloads == [] and withdrawn == [("Foul language in here now",
                                               "low_transcriber_confidence")]


@in_a_loop
async def test_a_hearing_begun_at_the_commit_is_awaited_not_cancelled():
    runner, session, _ = gpt_runner()
    withdrawn = withdrawals(runner)

    async def hearing():
        return {"outcome": "heard", "text": "What?", "model": "m", "ms": 5, "audio_ms": 900}
    task = asyncio.ensure_future(hearing())
    await runner._record_user_turn(
        "What?", voiced_ms=900, pcm=PCM, check={"task": task, "started": 0},
        confidence=G.confidence(INVENTED["What?"]))
    assert task.done() and not task.cancelled()
    (turn,) = session.store.of("user_turn")
    assert turn["transcriber_unsure"] is True and withdrawn == []


@in_a_loop
async def test_a_confident_line_is_written_with_its_scores(answers):
    answers("Hi, Morgan.")
    runner, session, _ = gpt_runner()
    conf = G.confidence(REAL["Hi Morgan."])
    await runner._record_user_turn("Hi Morgan.", voiced_ms=800, pcm=PCM,
                                   confidence=conf)
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Hi Morgan." and turn["speech_check"] == "heard"
    assert turn["transcriber_confidence"] == conf


@in_a_loop
async def test_not_english_is_written_as_the_english_the_check_heard(answers):
    answers("Totally.")
    runner, session, ws = gpt_runner()
    await runner._record_user_turn("Tuurlijk.", voiced_ms=900, pcm=PCM)
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Totally."
    assert turn["transcript_original"] == "Tuurlijk."
    assert [f["text"] for f in ws.frames("user_transcript")] == ["Totally."]


@pytest.mark.parametrize("answer", ["Natuurlijk.", "Vanzelfsprekend."])
def test_not_english_where_the_check_hears_other_non_english_is_suppressed(answers, answer):
    # Two readings of the same audio, neither English and not the same word:
    # the transcriber's line is not something anybody said.
    answers(answer)
    runner, session, ws = gpt_runner()
    withdrawn = withdrawals(runner)
    asyncio.run(runner._record_user_turn("Tuurlijk.", voiced_ms=900, pcm=PCM))
    assert not session.store.of("user_turn")
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "not_english" and sup["unknown"] == ["Tuurlijk"]
    assert sup["speech_check"] == "heard"
    assert withdrawn == [("Tuurlijk.", "not_english")]
    assert not ws.frames("user_transcript")


def test_the_same_word_heard_back_is_kept_and_flagged(answers):
    # Two models wrote the same word from the same audio: it was said, and
    # the list is what is short (a name, a loanword). Kept, flagged.
    answers("Tuurlijk.")
    runner, session, _ = gpt_runner()
    withdrawn = withdrawals(runner)
    asyncio.run(runner._record_user_turn("Tuurlijk.", voiced_ms=900, pcm=PCM))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Tuurlijk." and turn["language_unknown"] == ["Tuurlijk"]
    assert withdrawn == [] and not session.store.of("user_turn_suppressed")


def test_not_english_where_the_check_could_not_decide_is_kept_and_flagged(answers):
    # Review of #81, point 2: a word list alone must not silence anybody.
    answers(RuntimeError("gateway down"))
    runner, session, ws = gpt_runner()
    withdrawn = withdrawals(runner)
    asyncio.run(runner._record_user_turn("Tuurlijk.", voiced_ms=900, pcm=PCM))
    (turn,) = session.store.of("user_turn")
    assert turn["text"] == "Tuurlijk." and turn["language_unknown"] == ["Tuurlijk"]
    assert turn["speech_check"] == "error"
    assert withdrawn == [] and not session.store.of("user_turn_suppressed")
    assert [f["text"] for f in ws.frames("user_transcript")] == ["Tuurlijk."]


@in_a_loop
async def test_the_speech_check_still_decides_first(answers):
    answers("[no speech]")
    runner, session, _ = gpt_runner()
    withdrawals(runner)
    await runner._record_user_turn("Tuurlijk.", voiced_ms=900, pcm=PCM)
    (sup,) = session.store.of("user_turn_suppressed")
    assert sup["reason"] == "no_speech"


@in_a_loop
async def test_without_a_speech_check_the_english_gate_flags_and_keeps(answers):
    # The cascade route, production since 2026-10-20: no check can hear the
    # line again, so "լավ." is written and flagged, not silenced, and the
    # participant's "Gotcha." is never at the mercy of a word list.
    a = answers()
    runner, session, _ = runner_for("S2A")
    runner.rt = bridge(GEMINI)
    withdrawn = withdrawals(runner)
    await runner._record_user_turn("լավ.", voiced_ms=900)
    await runner._record_user_turn("Okay, sounds good.", voiced_ms=900)
    turns = session.store.of("user_turn")
    assert [t["text"] for t in turns] == ["լավ.", "Okay, sounds good."]
    assert turns[0]["language_unknown"] == ["լավ"] and "language_unknown" not in turns[1]
    assert not session.store.of("user_turn_suppressed") and withdrawn == []
    assert a.payloads == []
