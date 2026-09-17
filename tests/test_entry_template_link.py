"""The link as it is actually pasted, and the button that was added in front of it.

tests/test_entry_idempotency.py pinned that a link preview, a prefetch and a
scanner cannot enrol anybody. It pinned the wrong link. The one the team pastes
into Slack, Teams, a calendar invite and the recruitment email is the RAW
TEMPLATE:

    https://<host>/start?pid=${e://Field/ParticipantKey}&qid=${e://Field/ResponseID}

That string carries the participant key parameter, and "the parameter is
present" was the whole of the second half of the gate — so every fetcher not
on the user-agent denylist walked straight through it and minted a run.
Measured, against the build before this file existed: an Outlook user agent, a
Google Apps Script fetch, a Proofpoint URL Defense rewrite, a Defender Safe
Links check, a Zoom preview, an iMessage preview, a plain Chrome user agent and
a request with NO user agent header at all. Eight fetchers, eight runs, eight
participant records, eight completion codes, nobody behind any of them — and a
denylist as the only thing standing between a pasted URL and a study row.

The other half of this file is the cost of the fix. Routing every unusable key
through a page with a Continue button puts a re-submittable form in front of
exactly the participants who cannot be de-duplicated afterwards, because the
field that would tell them apart is the field that failed to pipe. Two presses
must not be two people. Their survey response id is what makes that decidable,
and only when it is a real one — an unreplaced ${e://Field/ResponseID} is one
string shared by everybody, and merging on it would rebuild the collision the
unattributed path exists to prevent.

The rule, unchanged in both directions: nothing that is FETCHING the link may
enrol anybody, and nobody who is FOLLOWING it may be turned away.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as appmod

LINKS = ["/start"]

#: The link as it is pasted before Qualtrics has piped anything into it.
TEMPLATE_KEY = "${e://Field/ParticipantKey}"
TEMPLATE_QID = "${e://Field/ResponseID}"

#: Real fetchers that are NOT on the user-agent denylist and never will be:
#: corporate mail security rewrites every link and fetches it, calendar and chat
#: clients render their own previews, and some fetch with no user agent at all.
#: A denylist cannot be completed, which is why it may not be the only gate.
UNLISTED_FETCHERS = [
    # Outlook desktop, which is what actually did this.
    "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko "
    "Microsoft Outlook 16.0.17726",
    "Mozilla/5.0 (compatible; Google-Apps-Script; beanserver; "
    "+https://script.google.com)",
    "Mozilla/5.0 (compatible; ProofpointURLDefense)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/120.0.0.0 Safari/537.36 SafeLinks",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Zoom",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 imessage-preview",
    # The plain browser user agent a link checker copies, and the empty one.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like "
    "Gecko) Chrome/140.0.0.0 Safari/537.36",
    "",
]

#: A real participant's browser, for the controls.
BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

#: Slack's iOS in-app browser. A PARTICIPANT sends this: they tapped the link in
#: a Slack message. It matches the denylist on the token "Slack" and always
#: will, which costs them one click — the accepted trade — but must not cost
#: them a page telling them they are not a person.
SLACK_IN_APP = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                "Slack/24.06.20.0 (iPhone; iOS 17.5; Scale/3.00)")


@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from server import storage

    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(storage, "PARTICIPANTS_DIR", tmp_path / "participants")
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "index.db")
    return storage


@pytest.fixture()
def client(monkeypatch, runs_mod, store):
    monkeypatch.setattr(appmod, "SESSION_KEY", "", raising=False)
    if appmod.ALLOWED_HOSTS and "testserver" not in appmod.ALLOWED_HOSTS:
        monkeypatch.setattr(appmod, "ALLOWED_HOSTS",
                            list(appmod.ALLOWED_HOSTS) + ["testserver"])
    with TestClient(appmod.app) as c:
        yield c


def _runs(runs_mod) -> list:
    d = runs_mod.RUNS_DIR
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _records(store) -> list:
    d = store.PARTICIPANTS_DIR
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def _run_id(response) -> str:
    loc = response.headers["location"]
    assert loc.startswith("/v2?run="), loc
    return loc.split("run=")[1].split("&")[0]


# --- the template link, fetched --------------------------------------------

@pytest.mark.parametrize("ua", UNLISTED_FETCHERS)
@pytest.mark.parametrize("path", LINKS)
def test_the_raw_template_link_enrols_nobody_however_it_is_fetched(
        client, runs_mod, store, path, ua):
    """The production case this gate was written for and did not cover.

    The pasted link, fetched by things no denylist has heard of. Nothing about
    the requester is being trusted here — what decides is that the URL carries
    no participant key this server can use, which is true of the template no
    matter who asks for it.
    """
    r = client.get(path, params={"pid": TEMPLATE_KEY, "qid": TEMPLATE_QID},
                   headers=({"User-Agent": ua} if ua else {}),
                   follow_redirects=False)
    assert r.status_code == 200, r.text
    assert _runs(runs_mod) == []
    assert _records(store) == []


@pytest.mark.parametrize("path", LINKS)
def test_a_wave_of_template_fetches_is_still_zero_runs(client, runs_mod, store,
                                                       path):
    """One paste into a busy channel is many fetches. None of them is a row."""
    for ua in UNLISTED_FETCHERS * 3:
        client.get(path, params={"pid": TEMPLATE_KEY, "qid": TEMPLATE_QID},
                   headers=({"User-Agent": ua} if ua else {}),
                   follow_redirects=False)
    assert _runs(runs_mod) == []
    assert _records(store) == []


@pytest.mark.parametrize("spelling", ["pid", "participant_id", "PROLIFIC_PID"])
def test_every_spelling_of_the_key_is_judged_on_its_value(client, runs_mod,
                                                          spelling):
    """The three spellings are one declaration on purpose, and the gate has to
    read all three the same way — otherwise the template pasted with the other
    field name is a hole nobody tests."""
    r = client.get("/start", params={spelling: TEMPLATE_KEY},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 200
    assert _runs(runs_mod) == []


@pytest.mark.parametrize("useless", ["", "ParticipantKey", "undefined", "null",
                                     "${e://Field/ParticipantKey}",
                                     "e://Field/ParticipantKey"])
def test_no_value_the_server_cannot_use_is_treated_as_a_key(client, runs_mod,
                                                            useless):
    """Every status normalize_participant_key refuses, asked at the door.

    The gate and the enrolment path must agree about what a key is. If they ever
    disagree, the value they disagree about is one a fetch can enrol on.
    """
    r = client.get("/start", params={"pid": useless},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 200, r.text
    assert _runs(runs_mod) == []


# --- and the participant, who is never refused -------------------------------

#: The subset of the above that the user-agent denylist does not claim. Derived
#: from the server's own regex rather than hand-copied, so a token added to the
#: denylist later cannot silently turn a positive control into a false one.
UNLISTED_AND_UNMATCHED = [
    ua for ua in UNLISTED_FETCHERS
    if not appmod._LINK_PROBE_UA_RE.search(ua)
]


@pytest.mark.parametrize("ua", UNLISTED_AND_UNMATCHED)
@pytest.mark.parametrize("path", LINKS)
def test_a_real_key_still_arrives_on_the_first_get(client, runs_mod, path, ua):
    """THE POSITIVE CONTROL, and the half that must not move. The same fetchers,
    the same links, a key that piped: every one of them is an arrival, on the
    plain GET, with no button in the way."""
    key = f"RFT{abs(hash(path + ua)) % 9973:04d}"
    r = client.get(path, params={"pid": key, "qid": "R_0123456789abcd"},
                   headers=({"User-Agent": ua} if ua else {}),
                   follow_redirects=False)
    assert r.status_code == 307, r.text
    run = runs_mod.get(_run_id(r))
    assert run["participant_id"] == key
    assert run["cohort"] == "study"


@pytest.mark.parametrize("path", LINKS)
def test_the_broken_pipe_participant_reaches_their_run_in_one_click(
        client, runs_mod, path):
    """The cost of the fix, paid in full and no more than once: the page, the
    button, the run. No 400, no 404, no error, no dead end."""
    r = client.get(path, params={"pid": TEMPLATE_KEY, "qid": "R_broken0001abc"},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 200
    assert "<form method=\"post\" action=\"\">" in r.text

    r = client.post(path, params={"pid": TEMPLATE_KEY, "qid": "R_broken0001abc"},
                    headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 303
    run = runs_mod.get(_run_id(r))
    assert run["cohort"] == "unattributed"
    assert run["raw_participant_key"] == TEMPLATE_KEY
    assert run["qualtrics_id"] == "R_broken0001abc"


def test_the_page_the_broken_pipe_participant_gets_is_addressed_to_them(client):
    """They are a participant, not a scanner, and the page they get says so —
    including that they can still take part, and that somebody needs telling."""
    r = client.get("/start", params={"pid": TEMPLATE_KEY},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert "still take part" in r.text
    assert "research team" in r.text
    assert "link preview, a scanner" not in r.text


def test_the_probe_page_does_not_tell_a_participant_they_are_not_a_person(
        client, runs_mod):
    """Slack's iOS in-app browser is a PARTICIPANT tapping the link in a Slack
    message, and it matches the denylist on the token "Slack" — as do Discord,
    Telegram, WhatsApp and Skype, which are all in-app-browser vendors and not
    only unfurlers. One click is the accepted trade. Being told, on the one
    screen a stopped participant reads carefully, that the link "was opened by
    something other than a participant's browser" is not: it reads as a refusal,
    and somebody who believes they have been refused closes the tab instead of
    pressing the button that would have let them in.
    """
    r = client.get("/start", params={"pid": "RFSLACK01"},
                   headers={"User-Agent": SLACK_IN_APP}, follow_redirects=False)
    assert r.status_code == 200
    assert "Continue" in r.text
    assert "was opened by something other than" not in r.text
    assert "If you are here to take part" in r.text
    assert _runs(runs_mod) == []

    r = client.post("/start", params={"pid": "RFSLACK01"},
                    headers={"User-Agent": SLACK_IN_APP}, follow_redirects=False)
    assert r.status_code == 303
    assert runs_mod.get(_run_id(r))["participant_id"] == "RFSLACK01"


# --- one person, one run, however many times they press ----------------------

@pytest.mark.parametrize("path", LINKS)
def test_one_person_pressing_continue_twice_is_still_one_run(client, runs_mod,
                                                             store, path):
    """The surface the entry check page created.

    A keyless or broken-key arrival gets a fresh unattributed identity, so there
    was nothing for a second press to resume against and every press was another
    run: one person, two rows, two participant records, two half-finished
    sequences, two partial completion codes — on precisely the arrivals no later
    analysis can de-duplicate. Their Qualtrics response id is the identity that
    survived a broken pipe, and it is on the run document already.
    """
    params = {"qid": "R_twicepressed01"}
    client.get(path, params=params, headers={"User-Agent": BROWSER},
               follow_redirects=False)
    a = client.post(path, params=params, headers={"User-Agent": BROWSER},
                    follow_redirects=False)
    b = client.post(path, params=params, headers={"User-Agent": BROWSER},
                    follow_redirects=False)
    assert _run_id(a) == _run_id(b)
    assert len(_runs(runs_mod)) == 1
    assert len(_records(store)) == 1


def test_back_button_then_continue_again_is_still_one_run(client, runs_mod):
    """The other way a person presses twice: submit, go back, submit again."""
    params = {"pid": TEMPLATE_KEY, "qid": "R_backbutton001"}
    seen = set()
    for _ in range(3):
        client.get("/start", params=params, headers={"User-Agent": BROWSER},
                   follow_redirects=False)
        r = client.post("/start", params=params, headers={"User-Agent": BROWSER},
                        follow_redirects=False)
        seen.add(_run_id(r))
    assert len(seen) == 1
    assert len(_runs(runs_mod)) == 1


def test_an_unpiped_response_id_may_never_merge_two_people(client, runs_mod):
    """THE LINE THE DE-DUPLICATION WAS NOT ALLOWED TO CROSS.

    ${e://Field/ResponseID} is the same string for everybody, exactly as the
    unreplaced participant key is. Joining two arrivals on it would rebuild the
    original defect in its worst form — arrival two inside arrival one's
    half-finished run, sharing their encounters — from the code that was added
    to prevent duplicates. Two template fetches that got as far as pressing
    Continue are two runs.
    """
    params = {"pid": TEMPLATE_KEY, "qid": TEMPLATE_QID}
    a = client.post("/start", params=params, headers={"User-Agent": BROWSER},
                    follow_redirects=False)
    b = client.post("/start", params=params, headers={"User-Agent": BROWSER},
                    follow_redirects=False)
    assert _run_id(a) != _run_id(b)


def test_a_survey_id_cannot_reach_an_attributed_participants_run(client,
                                                                 runs_mod):
    """The resume is only ever onto a run that is already unattributed.

    Otherwise anyone who knows a participant's Qualtrics response id — it rides
    in the survey's own redirect URL — could land inside that participant's run
    by presenting it beside a broken key, and take over the identity their
    encounters are recorded under.
    """
    qid = "R_realperson01"
    theirs = client.get("/start", params={"pid": "RFREALKEY1", "qid": qid},
                        headers={"User-Agent": BROWSER}, follow_redirects=False)
    other = client.post("/start", params={"pid": TEMPLATE_KEY, "qid": qid},
                        headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert _run_id(theirs) != _run_id(other)
    assert runs_mod.get(_run_id(theirs))["participant_id"] == "RFREALKEY1"
    assert runs_mod.get(_run_id(other))["cohort"] == "unattributed"


def test_a_keyed_participant_still_resumes_on_their_key(client, runs_mod):
    """Unmoved, and checked here because the lookup beside it is new: a real key
    resumes on the key and never needs the survey id to do it."""
    params = {"pid": "RFKEYED01", "qid": "R_keyedresume1"}
    a = client.get("/start", params=params, headers={"User-Agent": BROWSER},
                   follow_redirects=False)
    b = client.get("/start", params=params, headers={"User-Agent": BROWSER},
                   follow_redirects=False)
    assert _run_id(a) == _run_id(b)
    assert len(_runs(runs_mod)) == 1


def test_a_withdrawal_survives_the_second_press(client, runs_mod):
    """Somebody who stopped and then opened the link again is handed the run
    that records the stop, not a fresh one. The resume adopts the run's own
    participant identity for exactly this: asking "did this person withdraw?"
    under a synthetic key invented four lines earlier answers "no" every time.
    """
    params = {"pid": TEMPLATE_KEY, "qid": "R_withdrew0001"}
    client.get("/start", params=params, headers={"User-Agent": BROWSER},
               follow_redirects=False)
    first = _run_id(client.post("/start", params=params,
                                headers={"User-Agent": BROWSER},
                                follow_redirects=False))
    runs_mod.withdraw(first, reason="participant pressed stop")

    again = client.post("/start", params=params, headers={"User-Agent": BROWSER},
                        follow_redirects=False)
    assert _run_id(again) == first
    assert runs_mod.get(first).get("withdrawn")
    assert len(_runs(runs_mod)) == 1


# --- the joinability rule itself ---------------------------------------------

@pytest.mark.parametrize("qid,joinable", [
    ("R_0123456789abcd", True),
    ("R_abcdef", True),
    ("${e://Field/ResponseID}", False),
    ("e://Field/ResponseID", False),
    ("", False),
    (None, False),
    ("   ", False),
    ("R_abc", False),          # too short to be a response id
    ("../../etc/passwd", False),
    ("R_abc def", False),
])
def test_what_counts_as_a_survey_response_to_join_on(qid, joinable):
    from server import runs

    assert runs.is_joinable_survey_response(qid) is joinable
