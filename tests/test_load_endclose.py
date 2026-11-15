"""Completion and transport closure are separate outcomes."""
import asyncio
import json

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from tools.load.participant import Participant
from tools.load.runtime import environment
from tools.load import run, measure
from test_load_measure import valid_metrics, REFERENCE


def abnormal():
    return ConnectionClosedError(None, None)


@pytest.mark.parametrize('completed', [False, True])
def test_close_exception_phase_is_captured_before_done_callback(completed):
    async def exercise():
        p = Participant('http://local', 'S2A', 'tone:1', index=0)
        p.tl['completed'] = completed
        p.ws = type('Socket', (), {'close_code':1006, 'close_reason':''})()
        async def fail(): raise abnormal()
        task = p.spawn(fail())
        await asyncio.gather(task, return_exceptions=True)
        # A later completion must not reclassify an earlier close.
        p.tl['completed'] = True
        await asyncio.sleep(0)
        if completed:
            assert not p.tl['server_errors']
            assert p.tl['post_complete_close'][0]['code'] == 1006
            assert p.tl['post_complete_close'][0]['received'] is None
        else:
            assert p.tl['server_errors'] and p.tl['unexpected_disconnect']
            assert not p.tl['post_complete_close']
    asyncio.run(exercise())


def test_reader_ceiling_completion_then_abnormal_close_is_diagnostic(monkeypatch):
    from tools.sim import stim
    monkeypatch.setattr(stim, 'room_tone', lambda seconds: b'\0' * 3200)
    class Wire:
        close_code = 1006
        close_reason = ''
        def __init__(self): self.queue = asyncio.Queue(); self.closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True
        async def send(self, raw):
            if isinstance(raw, str):
                self.queue.put_nowait(json.dumps({'type':'session', 'session_id':'s_ceiling'}))
                self.queue.put_nowait(json.dumps({'type':'encounter_complete', 'reason':'ceiling'}))
                self.queue.put_nowait(abnormal())
        def __aiter__(self): return self
        async def __anext__(self):
            item = await self.queue.get()
            if isinstance(item, BaseException): raise item
            return item
    async def exercise():
        wire = Wire()
        async with httpx.AsyncClient() as http:
            p = Participant('http://local', 'S2A', 'tone:60', index=0, http=http,
                            identity={'participant_id':'p', 'run':'r'}, connect=lambda *a, **kw: wire)
            tl = await asyncio.wait_for(p.run(), 1)
        assert tl['completed'] and tl['completion_reason'] == 'ceiling'
        assert not tl['server_errors'] and not tl['unexpected_disconnect']
        assert not tl.get('duration_limit_reached') and wire.closed
        assert tl['post_complete_close'][0]['task'] == 'Participant.read'
        assert tl['post_complete_close'][0]['exception'] == 'ConnectionClosedError'
        metrics = valid_metrics()
        metrics['post_complete_close'] = tl['post_complete_close']
        assert measure.verdict(5, metrics, REFERENCE, server_errors=tl['server_errors'])['verdict'] == 'PASS'
    asyncio.run(exercise())


def test_abnormal_reader_close_before_completion_remains_failure():
    async def exercise():
        p = Participant('http://local', 'S2A', 'tone:1', index=0)
        async def frames():
            yield json.dumps({'type':'session', 'session_id':'s'})
            raise abnormal()
        p.ws = frames()
        await asyncio.gather(p.spawn(p.read()), return_exceptions=True)
        assert not p.tl['completed'] and p.tl['unexpected_disconnect']
        assert not p.tl['post_complete_close'] and p.tl['server_errors']
        assert measure.verdict(5, valid_metrics(), REFERENCE, server_errors=p.tl['server_errors'])['verdict'] == 'FAIL'
    asyncio.run(exercise())


def test_short_ceiling_uses_server_env_instead_of_client_duration():
    args = run.parse_args(['--encounter-seconds','5','--scenarios','S2A','--steps','tone:10'])
    assert args.duration is None
    env = environment(gateway='fake', fake_url='https://fake', data_dir='/data', key='test',
                      production={'REALTIME_MODEL':'cascade-argon-elevenlabs', 'ENCOUNTER_MAX_SECONDS':'600'},
                      encounter_seconds=args.encounter_seconds)
    assert env['ENCOUNTER_MIN_SECONDS'] == '0'
    assert env['ENCOUNTER_WRAP_SECONDS'] == '4.0'
    assert env['ENCOUNTER_MAX_SECONDS'] == '5.0'


def test_post_complete_close_is_visible_in_markdown_report(tmp_path):
    m = valid_metrics()
    m['post_complete_close'] = [{'code':1006}]
    run._write_report(tmp_path, {'rule':'test', 'steps':[{'concurrency':1,'repeat':1,'verdict':'PASS',
         'metrics':{**m, 'server_rss_bytes':{'max':1}}, 'reasons':[]}]})
    assert 'Post-completion close diagnostics at N=1: 1' in (tmp_path/'report.md').read_text(encoding='utf-8')
