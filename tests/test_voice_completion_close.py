"""The participant route must finish the WebSocket closing handshake.

Use Uvicorn's production SansIO protocol over socketpairs: no listener, DNS,
provider, or credentials. The optional byte relay has the same two stream
pumps as a TCP relay; tools/load/relay.py is absent on this base revision.
Only the gateway and opening setup are stood in. The runner's run(), ceiling,
client reader, finalizer, and cleanup paths are real.
"""
from __future__ import annotations

import ast
import asyncio
import itertools
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager
import json
from functools import wraps
import socket
import time

import pytest
import uvicorn
from starlette.websockets import WebSocket, WebSocketState
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from uvicorn.server import ServerState
from websockets.asyncio.client import connect

from server import app as appmod, realtime_voice_session as rvs, storage
from server.session import Session, SessionRegistry
from server.voice import realtime
from test_final_runner_voice import FakeSession


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(test(*args, **kwargs), 20))
    return run


async def bounded(awaitable, timeout=5):
    return await asyncio.wait_for(awaitable, timeout)


async def noop(*args, **kwargs):
    pass


async def until(predicate):
    async def poll():
        while not predicate():
            await bounded(asyncio.sleep(0.001))
    await asyncio.wait_for(poll(), 4)


class EventStore:
    # Use the real closed-store guard, including in the delayed-finalizer test.
    event = storage.SessionStore.event

    def __init__(self, path, participant_id):
        self.events_path = path
        self.events_fh = path.open("w", encoding="utf-8", buffering=1)
        self.started_at = time.time()
        self.participant_id = participant_id
        self.closed = False

    def close(self, **kwargs):
        self.events_fh.close()
        self.closed = True

    def events(self):
        return [json.loads(line) for line in self.events_path.read_text(encoding="utf-8").splitlines()]


class CaptureSession(FakeSession):
    close_participant_socket = Session.close_participant_socket
    cancel_auto_steer = Session.cancel_auto_steer

    def __init__(self, sid, path, participant_id):
        super().__init__("S1A")
        self.id = sid
        self.store = EventStore(path, participant_id)
        self.log = self.store
        self.run_id = "run_one"
        self.participant_ws = None
        self.researcher_wss = set()
        self._auto_steer_tasks = set()
        self._closed = False
        self.complete = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.cleaned = False
        self.cleanup_error = False


class Gateway:
    model = realtime.MODEL
    voice = "Puck"

    def __init__(self, session):
        self.session = session
        self.ws = object()

    connect = noop

    async def close(self):
        self.session.cleanup_started.set()
        await bounded(self.session.cleanup_release.wait())
        if self.session.cleanup_error:
            raise RuntimeError("gateway cleanup failed")
        self.session.cleaned = True


class CeilingRunner(rvs.RealtimeVoiceSessionRunner):
    def __init__(self, session, ws):
        super().__init__(session, ws)
        session.runner = self
        self._new_session = lambda **kwargs: Gateway(session)
        self._fold_opening = lambda *args, **kwargs: None
        self._instructions = lambda: ""
        self._voice = lambda: "Puck"
        self._announce_opening = noop
        self._announce_clock = noop
        self._await_participant = noop
        self._maybe_turn_open = noop
        self._encounter_elapsed = lambda: 1_000_000

    async def _model_to_client(self):
        await bounded(asyncio.Future())

    async def _silence_watchdog(self):
        await bounded(self.session.complete.wait())
        assert await bounded(self._at_ceiling())


@pytest.fixture
def encounter(monkeypatch, tmp_path):
    registry = SessionRegistry()
    sessions = []

    def create(*args, participant_id=None, **kwargs):
        sid = f"s_1_{len(sessions):06x}"
        session = CaptureSession(sid, tmp_path / f"{sid}.jsonl", participant_id)
        registry._sessions[sid] = session
        sessions.append(session)
        return session

    monkeypatch.setattr(registry, "create", create)
    monkeypatch.setattr(appmod, "registry", registry)
    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", CeilingRunner)
    monkeypatch.setattr(appmod, "PARTICIPANT_KEY_REQUIRED", False)
    monkeypatch.setattr(appmod, "_participant_may_capture", lambda pid: True)
    monkeypatch.setattr(appmod, "_run_context", lambda *args: None)
    yield registry, sessions
    for session in sessions:
        if not session.store.closed:
            session.store.close()


@asynccontextmanager
async def wire(*, relay=False, participant_id="p_one", block_close=False, close_release=None):
    loop = asyncio.get_running_loop()
    server_sock, client_sock = socket.socketpair()
    sockets = [server_sock, client_sock]
    pumps = []
    if relay:
        relay_up = client_sock
        client_sock, relay_down = socket.socketpair()
        sockets.extend([client_sock, relay_down])
        for sock in (relay_up, relay_down):
            sock.setblocking(False)

        async def pump(source, target):
            while data := await bounded(loop.sock_recv(source, 65536)):
                await bounded(loop.sock_sendall(target, data))
            target.shutdown(socket.SHUT_WR)

        pumps = [asyncio.create_task(pump(relay_up, relay_down)),
                 asyncio.create_task(pump(relay_down, relay_up))]

    messages = []
    close_blocked = asyncio.Event()
    close_cancelled = asyncio.Event()

    async def app(scope, receive, send):
        async def observed_send(message):
            messages.append(message)
            if message["type"] == "websocket.close":
                close_blocked.set()
                if close_release is not None:
                    await bounded(close_release.wait())
            if block_close and message["type"] == "websocket.close":
                try:
                    await bounded(asyncio.Future())
                finally:
                    close_cancelled.set()
            await bounded(send(message))
        await bounded(appmod.app(scope, receive, observed_send))

    config = uvicorn.Config(app, lifespan="off", ws="websockets-sansio",
                            ws_ping_interval=None, log_config=None)
    config.load()
    state = ServerState()
    protocol = WebSocketsSansIOProtocol(config, state, {})
    protocol.messages = messages
    protocol.close_blocked = close_blocked
    protocol.close_cancelled = close_cancelled
    transport, _ = await bounded(loop.connect_accepted_socket(lambda: protocol, server_sock))
    try:
        async with connect(
            f"ws://localhost/ws/participant/voice?scenario=S1A&participant_id={participant_id}",
            sock=client_sock, proxy=None, ping_interval=None, open_timeout=5, close_timeout=1,
        ) as client:
            yield client, protocol
        await bounded(until(lambda: not state.tasks))
    finally:
        transport.close()
        for task in list(state.tasks):
            task.cancel()
        await bounded(asyncio.gather(*state.tasks, return_exceptions=True))
        for task in pumps:
            task.cancel()
        await bounded(asyncio.gather(*pumps, return_exceptions=True))
        for sock in sockets:
            sock.close()


async def started(client, sessions):
    first = json.loads(await asyncio.wait_for(client.recv(), 3))
    assert first["type"] == "session"
    return next(session for session in sessions if session.id == first["session_id"])


def assert_handshake(client, protocol, code=1000):
    for endpoint in (client.protocol, protocol.conn):
        assert endpoint.close_sent is not None
        assert endpoint.close_rcvd is not None
        assert endpoint.close_sent.code == endpoint.close_rcvd.code == code
    assert client.close_code == code


@run_async
@pytest.mark.parametrize("relay", [False, True], ids=["direct", "relay"])
@pytest.mark.parametrize("eager", [False, True], ids=["passive", "eager"])
async def test_ceiling_exchanges_both_close_frames(encounter, relay, eager, caplog, capsys):
    registry, sessions = encounter
    async with wire(relay=relay) as (client, protocol):
        session = await bounded(started(client, sessions))
        session.cleanup_release.clear()
        session.complete.set()
        done = json.loads(await bounded(client.recv()))
        assert done == {"type": "encounter_complete", "reason": "ceiling"}
        await bounded(session.cleanup_started.wait())
        assert session.id in registry.list_ids()
        assert not any(m["type"] == "websocket.close" for m in protocol.messages)
        closing = asyncio.create_task(client.close(code=1000)) if eager else None
        session.cleanup_release.set()
        await bounded(client.wait_closed())
        if closing is not None:
            await bounded(closing)
        await bounded(until(lambda: session.store.closed))
        assert_handshake(client, protocol)
        assert session.cleaned and session.participant_ws is None
        assert registry.list_ids() == []
        assert len([m for m in protocol.messages if m["type"] == "websocket.close"]) <= 1
    assert not caplog.records
    assert "WARNING" not in capsys.readouterr().out


@run_async
async def test_completion_keeps_the_same_participants_second_tab_open(encounter):
    registry, sessions = encounter
    async with wire() as (first, p1), wire() as (second, p2):
        one = await bounded(started(first, sessions))
        two = await bounded(started(second, sessions))
        one.complete.set()
        assert json.loads(await bounded(first.recv()))["type"] == "encounter_complete"
        await bounded(first.wait_closed())
        await bounded(until(lambda: one.store.closed))
        assert_handshake(first, p1)
        assert registry.list_ids() == [two.id]
        assert not two.store.closed and two.participant_ws is not None
        assert p2.conn.close_sent is None and p2.conn.close_rcvd is None
        await bounded(second.close())


@run_async
@pytest.mark.parametrize("winner", ["withdrawal", "completion"])
async def test_withdrawal_races_completion_and_still_closes_all_tabs(encounter, winner, caplog, capsys):
    registry, sessions = encounter
    close_release = asyncio.Event()
    async with wire(close_release=close_release) as (first, p1), wire() as (second, p2), \
            wire(participant_id="p_other") as (other, p3):
        one = await bounded(started(first, sessions))
        two = await bounded(started(second, sessions))
        stranger = await bounded(started(other, sessions))
        one.cleanup_release.clear()
        one.complete.set()
        assert json.loads(await bounded(first.recv()))["type"] == "encounter_complete"
        await bounded(one.cleanup_started.wait())
        if winner == "completion":
            one.cleanup_release.set()
            await bounded(p1.close_blocked.wait())
        # Real withdrawal teardown: every session matching this participant,
        # even a second tab, and no session owned by somebody else.
        withdrawal = asyncio.create_task(appmod._stop_live_sessions({"p_one"}, set()))
        if winner == "withdrawal":
            await bounded(p1.close_blocked.wait())
            one.cleanup_release.set()
            await bounded(until(lambda: one.cleaned))
        else:
            await bounded(until(lambda: one.store.closed))
        close_release.set()
        await bounded(withdrawal)
        await bounded(first.wait_closed())
        await bounded(second.wait_closed())
        await bounded(until(lambda: one.cleaned and two.cleaned))
        assert_handshake(first, p1)
        assert_handshake(second, p2)
        assert one.store.closed and two.store.closed
        assert registry.list_ids() == [stranger.id]
        assert p3.conn.close_sent is None
        for protocol in (p1, p2):
            assert len([m for m in protocol.messages if m["type"] == "websocket.close"]) == 1
        await bounded(other.close())
    assert not caplog.records
    assert "WARNING" not in capsys.readouterr().out


@run_async
async def test_disconnected_client_is_not_closed_again(encounter, caplog, capsys):
    registry, sessions = encounter
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        await bounded(client.close())
        await bounded(until(lambda: session.store.closed))
        assert session.cleaned and registry.list_ids() == []
        assert not any(m["type"] == "websocket.close" for m in protocol.messages)
        assert_handshake(client, protocol)
    assert not caplog.records
    assert "WARNING" not in capsys.readouterr().out


@run_async
async def test_close_write_is_bounded_and_registry_cleanup_still_runs(encounter):
    registry, sessions = encounter
    async with wire(block_close=True) as (client, protocol):
        session = await bounded(started(client, sessions))
        session.complete.set()
        assert json.loads(await bounded(client.recv()))["type"] == "encounter_complete"
        await bounded(protocol.close_blocked.wait())
        assert session.cleaned and not session.store.closed
        assert session.participant_ws is None
        # Keep the real two-second bound; the blocked ASGI send must be cancelled.
        await asyncio.wait_for(protocol.close_cancelled.wait(), 3)
        await bounded(until(lambda: session.store.closed))
        assert registry.list_ids() == []
        assert len([m for m in protocol.messages if m["type"] == "websocket.close"]) == 1
        await bounded(client.wait_closed())
        assert client.close_code == 1006  # the write never reached the protocol


@run_async
async def test_registry_cleanup_failure_happens_after_the_close(encounter, monkeypatch, caplog):
    registry, sessions = encounter
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))

        def broken_close(**kwargs):
            raise RuntimeError("registry cleanup failed")

        with monkeypatch.context() as patch:
            patch.setattr(session.store, "close", broken_close)
            session.complete.set()
            assert json.loads(await bounded(client.recv()))["type"] == "encounter_complete"
            await bounded(client.wait_closed())
            await bounded(until(lambda: "registry cleanup failed" in caplog.text))
            assert_handshake(client, protocol)
            # Registry removal precedes the failing store cleanup. The socket
            # handshake has already started and can still finish normally.
            assert registry.list_ids() == []
            assert not session.store.closed
        session.store.close()
    assert "registry cleanup failed" in caplog.text


@run_async
async def test_runner_cleanup_failure_is_not_normal_completion(encounter):
    registry, sessions = encounter
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        session.cleanup_error = True
        session.complete.set()
        assert json.loads(await bounded(client.recv()))["type"] == "encounter_complete"
        assert json.loads(await bounded(client.recv())) == {
            "type": "error", "message": "gateway cleanup failed"}
        await bounded(client.wait_closed())
        assert session.runner.completed
        assert_handshake(client, protocol, code=1011)
        assert [m["code"] for m in protocol.messages if m["type"] == "websocket.close"] == [1011]
        assert session.store.closed and registry.list_ids() == []
        errors = [e for e in session.store.events() if e["type"] == "error"]
        assert errors[0]["where"] == "voice_ws"


@run_async
async def test_close_error_still_drops_the_session(encounter, monkeypatch, capsys):
    registry, sessions = encounter
    monkeypatch.setenv("METRICS_EMF", "1")

    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        ws = session.participant_ws
        original_send = ws.send

        async def fail(message):
            if message["type"] == "websocket.close":
                raise OSError("close write failed")
            await bounded(original_send(message))

        monkeypatch.setattr(ws, "send", fail)
        session.complete.set()
        assert json.loads(await bounded(client.recv()))["type"] == "encounter_complete"
        await bounded(client.wait_closed())
        await bounded(until(lambda: session.store.closed))
        assert session.participant_ws is None
        assert registry.list_ids() == [] and session.cleaned
    output = capsys.readouterr().out
    assert "close write failed" in output
    documents = [json.loads(line) for line in output.splitlines() if line.startswith('{"_aws":')]
    ended, = [doc for doc in documents if "VoiceRouteEnded" in doc]
    assert (ended["ExitCause"], ended["CloseOutcome"]) == ("completed", "close_failed")


@run_async
async def test_delayed_finalizer_outlives_route_and_its_store_event_is_dropped(encounter, monkeypatch):
    """Pin the existing teardown gap; D2 does not cancel overdue finalizers."""
    registry, sessions = encounter
    release = asyncio.Event()
    entered = asyncio.Event()
    original_await = rvs._await_transcript

    async def delayed_transcript(*args, **kwargs):
        entered.set()
        await bounded(release.wait())
        await bounded(original_await(*args, **kwargs))

    monkeypatch.setattr(rvs, "_await_transcript", delayed_transcript)
    monkeypatch.setenv("TRANSCRIPT_GRACE_SECONDS", "0")
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        await bounded(until(lambda: hasattr(session, "runner") and session.runner.rt is not None))
        runner = session.runner
        attempted = []
        original_send = runner._send

        async def observe_send(payload):
            attempted.append(payload)
            await bounded(original_send(payload))

        runner._send = observe_send
        runner._spawn_finalize(runner._finalize_turn(
            runner.agent_id, runner.agent, runner.rt, ["A delayed final line."], None))
        task = runner._finalize_tasks[0]
        try:
            await bounded(entered.wait())
            session.complete.set()
            assert json.loads(await bounded(client.recv()))["type"] == "encounter_complete"
            await bounded(client.wait_closed())
            await bounded(until(lambda: session.store.closed))
            assert_handshake(client, protocol)
            assert not task.done() and not task.cancelled()
            assert registry.list_ids() == []
            before = session.store.events_path.read_text(encoding="utf-8")
            release.set()
            await asyncio.wait_for(asyncio.shield(task), 2)
            # Actual finalizer still appends history and tries assistant_done;
            # the actual SessionStore.event guard silently drops assistant_turn.
            assert session.shared_history[-1]["text"] == "A delayed final line."
            assert any(m["type"] == "assistant_done" for m in attempted)
            assert session.store.events_path.read_text(encoding="utf-8") == before
            assert not any(e["type"] == "assistant_turn" for e in session.store.events())
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await bounded(asyncio.gather(task, return_exceptions=True))


@run_async
@pytest.mark.parametrize("state_name", ["client_state", "application_state"])
async def test_an_already_closed_or_closing_socket_is_a_quiet_noop(state_name, capsys):
    async def receive():
        return {"type": "websocket.connect"}

    sent = []

    async def send(message):
        sent.append(message)

    ws = WebSocket({"type": "websocket"}, receive, send)
    await bounded(ws.accept())
    setattr(ws, state_name, WebSocketState.DISCONNECTED)
    holder = type("Holder", (), {"id": "own", "participant_ws": ws})()
    assert await bounded(Session.close_participant_socket(holder)) is False
    assert holder.participant_ws is None
    assert not any(m["type"] == "websocket.close" for m in sent)
    assert capsys.readouterr().out == ""


@run_async
@pytest.mark.parametrize("route,allowed,key,expected", [
    ("voice", True, None, 4401),
    ("voice", False, "study-key", 4403),
    ("researcher", True, None, 4401),
    ("researcher", True, "study-key", 4404),
])
async def test_rejection_codes_are_unchanged(encounter, monkeypatch, route, allowed, key, expected):
    monkeypatch.setattr(appmod, "SESSION_KEY", "study-key")
    monkeypatch.setattr(appmod, "PARTICIPANT_KEY_REQUIRED", True)
    monkeypatch.setattr(appmod, "_participant_may_capture", lambda pid: allowed)
    messages = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)

    ws = WebSocket({"type": "websocket"}, receive, send)
    if route == "voice":
        await bounded(appmod.ws_participant_voice(
            ws, scenario="S1A", participant_id="p_one", model=None,
            launch=None, key=key, run=None))
    else:
        await bounded(appmod.ws_researcher(ws, session_id="missing", key=key))
    assert [m["code"] for m in messages if m["type"] == "websocket.close"] == [expected]
    assert encounter[0].list_ids() == []


@run_async
@pytest.mark.parametrize("failure", ["never_reconnected", "reconnect_limit"])
async def test_runner_reported_fatal_exit_closes_with_1011(encounter, monkeypatch, failure):
    registry, sessions = encounter

    class LostGatewayRunner(CeilingRunner):
        _model_to_client = rvs.RealtimeVoiceSessionRunner._model_to_client
        _pump = noop

        def __init__(self, session, ws):
            super().__init__(session, ws)
            if failure == "never_reconnected":
                def gateway(**kwargs):
                    rt = Gateway(session)
                    rt.ws = None
                    return rt
                self._new_session = gateway

        async def _silence_watchdog(self):
            await bounded(asyncio.Future())

    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", LostGatewayRunner)
    monkeypatch.setattr(realtime, "RECONNECT_LIMIT", 0)
    if failure == "never_reconnected":
        ticks = itertools.count(100, 20)
        monkeypatch.setattr(rvs, "time", SimpleNamespace(time=lambda: next(ticks)))
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        error = json.loads(await bounded(client.recv()))
        assert error == {"type": "error", "message": "The connection to the conversation was lost."}
        await bounded(client.wait_closed())
        await bounded(until(lambda: session.store.closed))
        assert not session.runner.completed
        assert_handshake(client, protocol, code=1011)
        assert [m["code"] for m in protocol.messages if m["type"] == "websocket.close"] == [1011]
        assert session.cleaned and registry.list_ids() == []
        where = "model_to_client" if failure == "never_reconnected" else "reconnect"
        assert any(e["type"] == "voice_error" and e["where"] == where
                   for e in session.store.events())


@run_async
async def test_runner_exception_before_completion_closes_with_1011(encounter, monkeypatch):
    registry, sessions = encounter

    class FailedRunner(CeilingRunner):
        async def _model_to_client(self):
            raise RuntimeError("gateway pump failed")

    monkeypatch.setattr(appmod, "RealtimeVoiceSessionRunner", FailedRunner)
    async with wire() as (client, protocol):
        session = await bounded(started(client, sessions))
        assert json.loads(await bounded(client.recv())) == {"type": "error", "message": "gateway pump failed"}
        await bounded(client.wait_closed())
        await bounded(until(lambda: session.store.closed))
        assert not session.runner.completed
        assert_handshake(client, protocol, code=1011)
        assert session.cleaned and registry.list_ids() == []


def test_each_await_in_this_module_has_a_deadline():
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        bounded_call = isinstance(value, ast.Call) and (
            isinstance(value.func, ast.Name) and value.func.id == "bounded" or
            isinstance(value.func, ast.Attribute) and
            isinstance(value.func.value, ast.Name) and value.func.value.id == "asyncio" and
            value.func.attr == "wait_for")
        if not bounded_call:
            missing.append(node.lineno)
    assert not missing, f"awaits without deadlines at lines {missing}"


@run_async
@pytest.mark.parametrize("delivered", [True, False])
async def test_completion_outcome_requires_the_completion_frame(encounter, delivered):
    registry, sessions = encounter
    session = registry.create(participant_id="p_one")

    class Page:
        async def send_json(self, payload):
            if not delivered:
                raise OSError("participant disconnected")

    runner = CeilingRunner(session, Page())
    assert not runner.completed
    await bounded(runner._send({"type": "error", "message": "a recoverable error"}))
    assert not runner.completed
    await bounded(runner._send({"type": "encounter_complete"}))
    assert runner.completed is delivered


def test_page_close_1011_preserves_1006_reconnect_and_failure_behaviour(tmp_path):
    import shutil
    import subprocess
    from test_drop_recovery_page import STUB

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; the page harness needs it")
    (tmp_path / "stub.js").write_text(STUB, encoding="utf-8")
    harness = tmp_path / "close.js"
    harness.write_text(r"""
const assert = require('assert');
const { bootV2, vm } = require('./stub.js');
const run = (b, code) => vm.runInContext(code, b.ctx);
async function closePage(code, state) {
  const b = bootV2(process.argv[2], '?run=r_1&participant_id=P1');
  await b.clock.flush();
  run(b, "startCapture = async () => {}; participantId = 'P1';");
  await run(b, 'startSession()');
  run(b, `run = {run_id:'r_1', position:1, total:4, current:{id:'S1A'}};
    sessionId = 's_live'; agentTurns = ${state.turns}; userTurns = 0;
    fatalDrops = ${state.drops}; intentionalClose = ${state.intentional};`);
  if (state.stated) b.ctx.handleServerFrame({data:JSON.stringify({type:'error',message:'gateway lost'})});
  const socket = b.sockets[b.sockets.length - 1];
  assert(socket.listeners.close.length === 1);
  socket.listeners.close[0]({type:'close', code, wasClean:code !== 1006});
  return {
    text: b.dom.$('dropText').innerHTML,
    reconnect: b.dom.$('dropReconnect').textContent,
    display: b.dom.$('dropReconnect').style.display,
    shown: b.dom.$('dropNote').classList.contains('show'),
    fatalDrops: run(b, 'fatalDrops'), started:run(b, 'started'),
    intentional:run(b, 'intentionalClose'),
  };
}
(async () => {
  for (const state of [
    {stated:true, turns:0, drops:0, intentional:false},
    {stated:true, turns:0, drops:1, intentional:false},
    {stated:true, turns:3, drops:0, intentional:false},
    {stated:false, turns:0, drops:0, intentional:false},
    {stated:true, turns:0, drops:0, intentional:true},
  ]) {
    const previous = await closePage(1006, state);
    for (const code of [1011, 1000, 4401, 4403, 4404])
      assert.deepStrictEqual(await closePage(code, state), previous);
    if (state.intentional) assert.strictEqual(previous.shown, false);
    else if (!state.stated || state.turns) assert.strictEqual(previous.reconnect, 'Reconnect');
    else if (!state.drops) assert.strictEqual(previous.reconnect, 'Try once more');
    else assert.strictEqual(previous.display, 'none');
  }
  console.log('CLOSE CODES EQUIVALENT');
})().catch(e => { console.error(e.stack); process.exit(1); });
""", encoding="utf-8")
    result = subprocess.run([node, str(harness), str(Path(__file__).resolve().parents[1] / "static/v2.html")],
                            capture_output=True, text=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLOSE CODES EQUIVALENT" in result.stdout
