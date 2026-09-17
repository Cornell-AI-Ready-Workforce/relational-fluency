"""What a fetch of a participant entry link is allowed to create.

GET /start mints a study-cohort run and a participant record. That is right for
the person the link was handed to and wrong for everything else that fetches a
URL — and the link's whole life is spent in places that fetch URLs nobody
clicked: the Qualtrics survey body, the recruitment email, the Slack or Teams
channel where the team pastes it to check it, a scanner's crawl, a browser
prefetching a link somebody only hovered.

Two runs were created on production in one day by exactly that, one of them by a
bare GET carrying no participant key at all. Each is a row in the study cohort
with no human behind it, and the link is about to be pasted into a live survey
where every unfurl, every link checker and every prefetch would add another.

The rule these tests pin, in both directions:

  * nothing that is fetching the link may enrol anybody, and
  * nobody who is following it may be turned away.

The second half is why there is no "require POST" here and why half of this file
is positive controls. A participant with a broken survey pipe still gets their
run, on a plain GET, exactly as before; a participant whose link lost its key
parameter entirely gets one button and then their run. The entry surface's one
unbreakable rule is that a real arrival is never refused, and a fix for stray
runs that cost one encounter would be a worse bug than the one it closed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as appmod

LINKS = ["/start"]

#: The user agents that actually did this. Slack and Teams unfurl a pasted link,
#: Twitter/Facebook/Discord preview one, and a monitor fetches it on a timer.
UNFURLERS = [
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "Mozilla/5.0 (compatible; Twitterbot/1.0)",
    "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Microsoft Office Word 2014 (link preview)",
    "Mozilla/5.0 (compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)",
]

#: How a browser says it is fetching speculatively rather than because somebody
#: asked. A prefetched link used to mint the run before the click that never came.
PREFETCH_HEADERS = [
    {"Sec-Purpose": "prefetch;prerender"},
    {"Purpose": "prefetch"},
    {"X-Purpose": "preview"},
    {"X-Moz": "prefetch"},
]

#: A real participant's browser, for the controls. Nothing in it is a signal.
BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


@pytest.fixture()
def runs_mod(tmp_path, monkeypatch):
    """server.runs writing into a temp directory (see tests/test_links.py)."""
    from server import runs

    monkeypatch.setattr(runs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    return runs


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """server.storage writing into the same temp directory."""
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


# --- what a fetch must not create --------------------------------------------

@pytest.mark.parametrize("path", LINKS)
def test_a_bare_fetch_of_an_entry_link_enrols_nobody(client, runs_mod, store, path):
    """The production case, verbatim: a GET with no participant key at all.

    It used to answer 307 and leave behind a run in cohort `unattributed`, a
    participant record, and a completion code — a study row created by whatever
    had fetched the URL. Nothing at the other end of that request could consent,
    speak or be paid.
    """
    r = client.get(path, headers={"User-Agent": BROWSER}, follow_redirects=False)

    assert r.status_code == 200, r.text
    assert _runs(runs_mod) == [], "a fetch with nobody behind it created a run"
    assert _records(store) == [], "and a participant record"
    # Never a dead end: the page a person lands on carries the way in.
    assert "<form method=\"post\"" in r.text and "Continue" in r.text


@pytest.mark.parametrize("path", LINKS)
@pytest.mark.parametrize("ua", UNFURLERS)
def test_an_unfurler_holding_a_real_participant_key_still_enrols_nobody(
        client, runs_mod, store, path, ua):
    """The commoner case, and the one the key does not save you from.

    A participant's own link pasted into Slack carries their key, so the unfurl
    minted THEIR run — before they had clicked, in their name, and with the
    first encounter's clock started. The key in the URL is not evidence that a
    person is holding it.
    """
    r = client.get(path, params={"pid": "RFUNFURL1", "qid": "R_0123456789abcd"},
                   headers={"User-Agent": ua}, follow_redirects=False)

    assert r.status_code == 200, r.text
    assert _runs(runs_mod) == [], f"{ua} enrolled a participant"
    assert _records(store) == []


@pytest.mark.parametrize("headers", PREFETCH_HEADERS)
def test_a_browser_prefetch_enrols_nobody(client, runs_mod, store, headers):
    """Hovering a link is not clicking it. A prefetch that enrolled the
    participant also started their run, and if they then did not click, the run
    stayed: a study row for somebody who never arrived."""
    r = client.get("/start", params={"pid": "RFPREFETCH"},
                   headers=dict(headers, **{"User-Agent": BROWSER}),
                   follow_redirects=False)

    assert r.status_code == 200, r.text
    assert _runs(runs_mod) == []
    assert _records(store) == []


def test_the_page_a_probe_gets_says_nothing_happened(client):
    """An unfurl card is read by people. It must not imply a session is open.

    It must also not tell the reader they are not a person. Some participants'
    browsers match this denylist and always will — Slack's iOS in-app browser
    carries the token "Slack" — so the sentence has to be true whoever reads it:
    nothing has been created, and there is a way in from here.
    """
    body = client.get("/start", headers={"User-Agent": UNFURLERS[0]}).text
    assert "Nothing has been created yet." in body
    assert "If you are here to take part" in body
    assert "was opened by something other than" not in body
    # And it must not be indexed or crawled onward from there.
    assert 'content="noindex, nofollow"' in body


def test_a_thousand_fetches_are_still_zero_runs(client, runs_mod, store):
    """Idempotence is the property, not "fewer runs". A link checker retries, a
    monitor runs on a timer, and a scanner walks every arm."""
    for _ in range(25):
        for path in LINKS:
            client.get(path, headers={"User-Agent": UNFURLERS[0]})
            client.get(path, headers={"Sec-Purpose": "prefetch"})
            client.get(path, headers={"User-Agent": BROWSER})
    assert _runs(runs_mod) == []
    assert _records(store) == []


# --- and what a real arrival still gets --------------------------------------

@pytest.mark.parametrize("path", LINKS)
def test_a_real_participant_still_arrives_on_a_plain_get(client, runs_mod, path):
    """THE CONTROL THIS WHOLE FILE IS MEASURED AGAINST.

    The entry link is a GET in a Qualtrics redirect. A participant who clicks it
    must get their run on that GET, with no button, no second request and no
    refusal — the fix for stray runs is not allowed to cost one encounter.
    """
    r = client.get(path, params={"pid": "RFREAL01", "qid": "R_0123456789abcd"},
                   headers={"User-Agent": BROWSER}, follow_redirects=False)

    assert r.status_code == 307, r.text
    run = runs_mod.get(_run_id(r))
    assert run["participant_id"] == "RFREAL01"
    assert run["cohort"] == "study"
    assert run["qualtrics_id"] == "R_0123456789abcd"
    assert len(run["scenarios"]) == 4


def test_a_returning_participant_resumes_the_same_run(client, runs_mod):
    """The other half of idempotence, and the half that already worked. Two hits
    of the same link are one run, so a refresh, a dropped connection or a second
    tab is not a second half-finished record."""
    first = client.get("/start", params={"pid": "RFREAL02"},
                       headers={"User-Agent": BROWSER}, follow_redirects=False)
    second = client.get("/start", params={"pid": "RFREAL02"},
                        headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert _run_id(first) == _run_id(second)
    assert len(_runs(runs_mod)) == 1


@pytest.mark.parametrize("path", LINKS)
def test_the_continue_button_enrols_the_person_who_pressed_it(
        client, runs_mod, store, path):
    """The way back in for whoever the check page stopped.

    A POST of the same URL is what the page's one button sends, and it does
    exactly what the GET would have done — including for somebody whose link
    carried no key at all, who is waved through as `unattributed` rather than
    refused. 303, not 307: a preserved POST would be re-sent to /v2, which
    serves GET only.
    """
    r = client.post(path, headers={"User-Agent": BROWSER}, follow_redirects=False)

    assert r.status_code == 303, r.text
    run = runs_mod.get(_run_id(r))
    assert run["cohort"] == "unattributed"
    assert run["participant_id"].startswith("unattributed_")
    assert len(run["scenarios"]) == 4
    assert len(_records(store)) == 1, "the run got its stable participant record"


def test_the_continue_button_carries_the_query_string_through(client, runs_mod):
    """The check page posts to action="", so ?qid= and the rest survive the
    click. Losing the response id there would turn a rescued participant into
    one whose consent can never be recorded."""
    r = client.post("/start", params={"pid": "RFREAL03", "qid": "R_abcdef0123456"},
                    headers={"User-Agent": UNFURLERS[0]}, follow_redirects=False)
    run = runs_mod.get(_run_id(r))
    assert run["participant_id"] == "RFREAL03"
    assert run["qualtrics_id"] == "R_abcdef0123456"
    assert run["cohort"] == "study"


@pytest.mark.parametrize("path", LINKS)
@pytest.mark.parametrize("broken", ["", "${e://Field/ParticipantKey}",
                                    "ParticipantKey"])
def test_a_broken_survey_pipe_is_never_refused_and_still_gets_its_run(
        client, runs_mod, path, broken):
    """THE LINE THIS FIX WAS NOT ALLOWED TO CROSS.

    A survey whose piping has broken sends the parameter with nothing usable in
    it, and that participant is mid-study with a link somebody else got wrong.
    They are never refused: no 400, no 404, no error page. They reach their run,
    marked unattributed, with the raw value kept for the hand-join.

    WHAT CHANGED, AND WHY IT HAD TO. This used to be a plain 307 on the GET, on
    the argument that the KEY PARAMETER'S PRESENCE marked a real participant
    because every link the study hands out carries it. The raw template link
    carries it too — ?pid=${e://Field/ParticipantKey} is the exact string the
    team pastes into Slack, Teams and the recruitment email — so an Outlook
    preview, a Defender or Proofpoint rewrite, a Zoom unfurl and a request with
    no user agent at all each minted a run off it, with a user-agent denylist as
    the only thing deciding. The presence of the parameter distinguished
    nothing. The value being unusable now means the entry check page, and the
    participant pays one click.

    One click, and never more than one: the status is 200 and not an error, the
    page carries a Continue, and pressing it produces exactly the run the GET
    used to.
    """
    r = client.get(path, params={"pid": broken}, headers={"User-Agent": BROWSER},
                   follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "Continue" in r.text
    # Not blamed and not told a machine fetched it — this is the note written
    # for the participant whose link lost its identifier.
    assert "still take part" in r.text
    assert _runs(runs_mod) == []

    r = client.post(path, params={"pid": broken},
                    headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 303, r.text
    run = runs_mod.get(_run_id(r))
    assert run["cohort"] == "unattributed"
    assert run["participant_key_status"] != "ok"
    assert run["participant_id"].startswith("unattributed_")
    assert run["raw_participant_key"] == (broken or None)


def test_two_broken_arrivals_are_still_two_runs(client, runs_mod):
    """The failure the unattributed path exists for: an unreplaced placeholder is
    the same string for everybody, so taking it at face value put arrival two
    inside arrival one's half-finished run.

    No ?qid= on either, so there is nothing that could honestly join them and
    two people must stay two runs. The case where there IS something is
    test_one_person_pressing_continue_twice_is_still_one_run below.
    """
    a = client.post("/start", params={"pid": "${e://Field/ParticipantKey}"},
                    headers={"User-Agent": BROWSER}, follow_redirects=False)
    b = client.post("/start", params={"pid": "${e://Field/ParticipantKey}"},
                    headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert _run_id(a) != _run_id(b)


def test_an_operators_own_check_of_the_link_creates_nothing(client, runs_mod, store):
    """curl is how the link is checked before a wave is fielded, and checking the
    link used to be how the first run of the study got made."""
    r = client.get("/start", params={"pid": "RFCURL1"},
                   headers={"User-Agent": "curl/8.4.0"}, follow_redirects=False)
    assert r.status_code == 200
    assert _runs(runs_mod) == [] and _records(store) == []
