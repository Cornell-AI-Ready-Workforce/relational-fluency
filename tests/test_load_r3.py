"""Final-round regressions: live observer loss and batched settle checks."""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from tools.load import measure, observer, run, runtime
from tools.load.settle import SettlePollError, SettlePoller
from test_load_run import FakeRuntime


@pytest.mark.parametrize('fault,detail', [
    ('malformed', 'malformed observer line'),
    ('exit', 'observer log task ended'),
    ('stale', 'observer output stale for more than 2 s'),
    ('error', 'observer disk read failed'),
])
def test_live_observer_fault_kills_server_before_cancellation(monkeypatch, tmp_path, fault, detail):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(run.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(307, headers={'location': '/v2?participant_id=p&run=r'})), **kw))

    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        monitor = runtime.Runtime(tmp_path, gateway='live')
        rt.observer_telemetry = monitor.observer_telemetry
        stream = asyncio.StreamReader()
        monitor.observer_last_output = time.monotonic()
        monitor.observer_log_task = asyncio.create_task(monitor._log(stream, 'observer'))
        started, order = asyncio.Event(), []
        async def kill():
            order.append('kill')
            rt.killed = True
        rt.kill_server = kill

        class Waiting:
            def __init__(self, *args, **kwargs):
                self.active = False
                self.tl = {'session_id': 's', 'scenario': 'S2A', 'lines': [],
                           'completed': False, 'hangup_requested': None}
            async def run(self):
                self.active = True
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    order.append('cancel')
                    self.active = False
                    self.tl['hangup_requested'] = time.time()

        async def break_chain():
            await started.wait()
            if fault == 'malformed':
                stream.feed_data(b'Traceback: not JSON\n{"kind":"resource","sample":{"t":1}}\n')
            elif fault == 'exit':
                stream.feed_eof()
            elif fault == 'stale':
                monitor.observer_last_output = time.monotonic() - 2.01
            else:
                stream.feed_data(b'{"kind":"error","detail":"observer disk read failed"}\n')

        producer = asyncio.create_task(break_chain())
        args = run.parse_args(['--gateway', 'live', '--allow-live', '--ramp', '1',
                               '--scenarios', 'S2A', '--warmup', '0'])
        try:
            result = await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600, participant_factory=Waiting), 3)
        finally:
            await producer
            if not stream.at_eof():
                stream.feed_eof()
            await monitor.observer_log_task
        assert order == ['kill', 'cancel'] and rt.killed
        assert result['live_stop']['reason'] == 'attempt telemetry unavailable'
        assert detail in result['live_stop']['detail']
        assert not result['errors']
        assert measure.verdict(1, result['metrics'], None,
            server_errors=result['server_errors'], errors=result['errors'])['verdict'] == 'FAIL'
        if fault == 'malformed':
            assert result['observer_telemetry']['malformed_lines'] == 1
            assert result['live_stop']['observer']['malformed_lines'] == 1
            assert monitor.resource_rows == [{'t': 1}]  # The parser survives the bad line.
            rows = [json.loads(line) for line in (tmp_path / 'observer-log.jsonl').read_text(encoding='utf-8').splitlines()]
            assert len(rows) == 2 and rows[0]['line'] == 'Traceback: not JSON'
    asyncio.run(exercise())


@pytest.mark.parametrize('payload', ['not json', 'null', '[]', '{}', '{"kind":"unknown"}',
                                      '{"kind":"resource","sample":null}'])
def test_observer_parser_counts_malformed_lines_and_continues(tmp_path, payload):
    async def exercise():
        rt = runtime.Runtime(tmp_path)
        stream = asyncio.StreamReader()
        stream.feed_data((payload + '\n{"kind":"resource","sample":{"t":1}}\n').encode())
        stream.feed_eof()
        await rt._log(stream, 'observer')
        assert rt.observer_malformed_lines == 1 and len(rt.observer_errors) == 1
        assert rt.resource_rows == [{'t': 1}]
    asyncio.run(exercise())


def test_observer_watchdog_uses_strict_two_second_limit_and_tracks_spawned_task(monkeypatch, tmp_path):
    async def exercise():
        rt = runtime.Runtime(tmp_path)
        stream = asyncio.StreamReader()
        async def spawn(*args, **kwargs):
            return SimpleNamespace(stdout=stream)
        monkeypatch.setattr(runtime.asyncio, 'create_subprocess_exec', spawn)
        monkeypatch.setattr(runtime, 'time', SimpleNamespace(monotonic=lambda: 100.0, time=time.time))
        await rt._spawn(['mock-observer'], 'observer')
        assert rt.observer_log_task in rt.tasks and rt.observer_last_output == 100
        rt.observer_last_output = 98
        assert rt.observer_telemetry()['failure'] is None
        rt.observer_last_output = 97.999
        assert 'stale' in rt.observer_telemetry()['failure']
        rt.observer_last_output = 100
        stream.feed_eof()
        await rt.observer_log_task
        assert rt.observer_telemetry()['failure'] == 'observer log task ended'
    asyncio.run(exercise())


def test_observer_tail_read_failure_is_reported_as_structured_error(monkeypatch, capsys):
    class BrokenTail:
        def read(self): raise ValueError('broken attempt JSONL')
    class EndTest(Exception): pass
    def finish(_): raise EndTest
    monkeypatch.setattr(observer, 'JsonlTail', lambda path: BrokenTail())
    monkeypatch.setattr(observer, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=finish))
    with pytest.raises(EndTest):
        observer.main()
    assert json.loads(capsys.readouterr().out) == {'kind': 'error', 'detail': 'broken attempt JSONL'}


def test_thirty_waiting_workers_use_one_exec_per_shared_poll(monkeypatch, tmp_path):
    async def exercise():
        rt = runtime.Runtime(tmp_path)
        rt.observer_name = 'mock-observer'
        ids = [f's_{i}' for i in range(30)]
        calls = []
        second_poll = asyncio.Event()
        release_last_poll = asyncio.Event()
        async def command(*args, **kwargs):
            assert args[:3] == ('docker', 'exec', 'mock-observer')
            at = args.index('settled-sessions')
            batch = args[at + 1:]
            calls.append((time.monotonic(), set(batch)))
            ready = [] if len(calls) == 1 else ids[:15] if len(calls) == 2 else ids[15:]
            if len(calls) == 2:
                second_poll.set()
            elif len(calls) == 3:
                await release_last_poll.wait()
            return 0, json.dumps({'settled_sessions': ready}), ''
        monkeypatch.setattr(runtime, 'command', command)
        poller = SettlePoller(rt)
        workers = [asyncio.create_task(poller.wait(sid, 5)) for sid in ids]
        try:
            await asyncio.wait_for(second_poll.wait(), 3)
            assert all(await asyncio.wait_for(asyncio.gather(*workers[:15]), 3))
            assert all(not task.done() for task in workers[15:])
            release_last_poll.set()
            assert all(await asyncio.wait_for(asyncio.gather(*workers), 3))
            assert len(calls) == 3
            assert calls[0][1] == calls[1][1] == set(ids)
            assert calls[2][1] == set(ids[15:])
            assert all(b[0] - a[0] >= 0.5 for a, b in zip(calls, calls[1:]))
            assert await poller.wait(ids[0], 0)  # Cached close needs no extra exec.
            assert len(calls) == 3
        finally:
            await poller.close()
            await asyncio.gather(*workers, return_exceptions=True)
    asyncio.run(exercise())


def test_shared_settle_timeouts_and_cleanup_do_not_cancel_other_workers():
    class Pending:
        async def settled_sessions(self, ids): return set()
    async def exercise():
        poller = SettlePoller(Pending())
        try:
            assert not await poller.wait('s', 0.01)
            assert not poller.pending['s'].cancelled()
        finally:
            await poller.close()
        assert poller.task.done() and not poller.pending
    asyncio.run(exercise())


def test_shared_settle_poll_error_reaches_all_waiters():
    class Broken:
        async def settled_sessions(self, ids): raise RuntimeError('observer exec failed')
    async def exercise():
        poller = SettlePoller(Broken())
        try:
            results = await asyncio.gather(*(poller.wait(str(i), 1) for i in range(30)), return_exceptions=True)
            assert all(isinstance(result, SettlePollError) for result in results)
        finally:
            await poller.close()
    asyncio.run(exercise())


def test_inspect_batch_reports_each_session_without_all_or_nothing_result(monkeypatch, capsys):
    from tools.load import inspect
    monkeypatch.setattr(inspect, 'settled', lambda data, ids: ids == ['s_closed'])
    monkeypatch.setattr('sys.argv', ['inspect', 'settled-sessions', 's_closed', 's_pending'])
    inspect.main()
    assert json.loads(capsys.readouterr().out) == {'settled_sessions': ['s_closed']}


def test_observer_exit_during_worker_settle_is_live_fail_not_generator_invalid(monkeypatch, tmp_path):
    real_client = httpx.AsyncClient
    def response(request):
        if request.url.path == '/test':
            return httpx.Response(307, headers={'location': '/v2?participant_id=p&run=r'})
        return httpx.Response(200, json={'current': {'id': 'S1A'}})
    monkeypatch.setattr(run.httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(response), **kw))

    async def exercise():
        rt = FakeRuntime(tmp_path)
        await rt.start()
        monitor = runtime.Runtime(tmp_path, gateway='live')
        rt.observer_telemetry = monitor.observer_telemetry
        stream = asyncio.StreamReader()
        monitor.observer_last_output = time.monotonic()
        monitor.observer_log_task = asyncio.create_task(monitor._log(stream, 'observer'))
        async def failed_batch(ids):
            stream.feed_eof()
            await monitor.observer_log_task
            raise RuntimeError('observer container exited')
        rt.settled_sessions = failed_batch
        class Finished:
            def __init__(self, *args, **kwargs):
                self.active = False
                self.tl = {'session_id': 's', 'scenario': 'S1A', 'lines': [], 'completed': True,
                           'hangup_requested': time.time()}
            async def run(self): return self.tl
        args = run.parse_args(['--gateway', 'live', '--allow-live', '--ramp', '1', '--warmup', '0'])
        result = await asyncio.wait_for(run.run_step(rt, args, 1, 1, 600, participant_factory=Finished), 3)
        assert rt.killed and result['live_stop']['detail'] == 'observer log task ended'
        assert result['measurement_errors'] and not result['errors']
        assert measure.verdict(1, result['metrics'], None,
                               server_errors=result['server_errors'], errors=result['errors'])['verdict'] == 'FAIL'
    asyncio.run(exercise())
