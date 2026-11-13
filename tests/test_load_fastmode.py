"""Amended fast-iteration contracts; no live gateway or Docker by default."""
import asyncio
from collections import Counter
import copy
import json
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest

from tools.load import fast, measure, run, smoke, probe
from tools.load.search import Search
from test_load_measure import valid_metrics, REFERENCE
from test_load_run import FakeRuntime


@pytest.mark.parametrize('value', ['0','-1','5','bad'])
def test_k_rejects_invalid_values(value):
    with pytest.raises(SystemExit): run.parse_args(['--encounters-per-participant',value])


def test_defaults_and_k_and_search_parsing():
    args = run.parse_args([])
    assert args.encounters_per_participant == 'all' and args.ramp == [1,5,10,20,30]
    assert run.parse_args(['--encounters-per-participant','1']).encounters_per_participant == 1
    assert run.parse_args(['--ramp-search']).ramp_search == 20
    assert run.parse_args(['--ramp-search','15']).ramp_search == 15
    with pytest.raises(SystemExit): run.parse_args(['--ramp-search','--ramp','1,5'])
    with pytest.raises(SystemExit): run.parse_args(['--ramp-search','30','--gateway','live','--allow-live'])


@pytest.mark.parametrize('n', [4,5,20])
def test_k1_follows_assigned_first_encounter_balanced_mix_and_advances_once(monkeypatch,tmp_path,n):
    client = httpx.AsyncClient
    assigned, advanced, counters = {}, [], []
    order = ['S3A','S1A','S4A','S2A']  # Deliberately different from index rotation.
    def handler(request):
        if request.url.path == '/test':
            index = len(counters); counters.append(index)
            assigned[f'r{index}'] = order[index % 4]
            return httpx.Response(307, headers={'location':f'/v2?participant_id=p{index}&run=r{index}'})
        rid = request.url.path.split('/')[3]
        if request.method == 'POST': advanced.append(rid)
        return httpx.Response(200,json={'current':{'id':assigned[rid]}})
    monkeypatch.setattr(run.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handler),**kw))
    class P:
        def __init__(self,base,scenario,steps,**kwargs):
            self.active=False
            self.tl={'session_id':f's{len(counters)}_{len(advanced)}_{kwargs["index"]}', 'scenario':scenario,
                     'lines':[], 'completed':True,'hangup_requested':0}
        async def run(self): return self.tl
    async def exercise():
        rt=FakeRuntime(tmp_path);await rt.start()
        args=run.parse_args(['--encounters-per-participant','1','--warmup','0'])
        return await run.run_step(rt,args,n,1,600,participant_factory=P)
    step=asyncio.run(exercise())
    expected=dict(Counter(order[i%4] for i in range(n)))
    assert step['metrics']['scenario_mix']==expected and len(advanced)==n
    assert step['metrics']['encounter_positions']==[1]*n
    assert step['full_length'] and step['metrics']['encounters_per_participant']==1


def sample_step(n=1,scenario='S1A',k=1):
    m=valid_metrics();m['achieved_concurrency']=n
    m.update(encounters_per_participant=k,by_scenario={scenario:copy.deepcopy(m)},
             scenario_mix={scenario:1},measurement_window={'start':10,'end':600})
    return {'concurrency':n,'repeat':1,'metrics':m,'errors':[],'server_errors':[],
            'fake_errors':[],'full_length':True,'live_stop':None,'early_stop':None,'profile':None}


def test_k_mismatch_is_invalid_even_with_a_server_failure():
    s=sample_step();ref=fast.reference([s])
    s['metrics']['encounters_per_participant']=2
    s['server_errors']=['HTTP 500']
    assert fast.evaluate(s,ref)['verdict']=='INVALID'
    assert 'K mismatch' in ';'.join(fast.evaluate(s,ref)['reasons'])


def test_reference_weights_match_measured_mix_not_balanced_baseline():
    a=sample_step(scenario='S1A');b=sample_step(scenario='S2A')
    b['metrics']['latency_samples_s']=[10]
    b['metrics']['by_scenario']['S2A']['latency_samples_s']=[10]
    ref=fast.reference([a,b])
    m=sample_step()['metrics'];m['by_scenario']={'S1A':{'participant_turns':99},'S2A':{'participant_turns':1}}
    m['scenario_mix']={'S1A':4,'S2A':1}
    matched,errors=fast.matched_reference(ref,m)
    assert not errors and matched['p90_s']==1 and matched['scenario_weights']=={'S1A':99,'S2A':1}


def test_each_baseline_unit_validated_with_its_own_window():
    units=[sample_step(scenario=s) for s in run.SCENARIOS]
    combined=fast.combine_units(units,run.SCENARIOS,1)
    ref=fast.reference([combined])
    assert fast.evaluate(combined,ref)['verdict']=='PASS'
    assert len(combined['metrics']['measurement_windows'])==4
    units[-1]['metrics']['generator_loop_lag_ms']['p99']=51
    assert fast.evaluate(combined,ref)['verdict']=='INVALID'


@pytest.mark.parametrize('results,path,capacity', [
    ({1:'PASS',20:'PASS',30:'FAIL',25:'PASS'},[1,20,30,25],25),
    ({1:'PASS',20:'FAIL',10:'PASS',15:'PASS'},[1,20,10,15],15),
    ({1:'PASS',20:'PASS',30:'PASS',40:'PASS'},[1,20,30,40],40),
    ({1:'INVALID'},[1],None), ({1:'FAIL'},[1],None),
])
def test_search_decisions(results,path,capacity):
    planner=Search();n=1;steps=[]
    while n is not None:
        state=results[n];steps.append({'concurrency':n,'verdict':state})
        n=planner.observe(n,state)
    assert [r['n'] for r in planner.path]==path
    assert measure.capacity(steps,1)==capacity
    assert planner.untested()


def test_search_invalid_retry_never_bisects_and_custom_bounds_terminate():
    search=Search();assert search.observe(1,'PASS')==20
    assert search.observe(20,'INVALID')==20
    assert search.observe(20,'INVALID') is None
    custom=Search(target=12,ceiling=40,live_cap=20)
    assert custom.observe(1,'PASS')==12
    assert custom.observe(12,'PASS')==20
    assert custom.observe(20,'PASS') is None
    with pytest.raises(ValueError): Search(target=21,live_cap=20)


def test_sorted_prefix_retains_out_of_order_and_mixed_repeat_failures():
    steps=[{'concurrency':n,'verdict':v} for n,v in [(1,'PASS'),(1,'PASS'),(20,'FAIL'),(20,'INVALID'),(10,'PASS'),(10,'PASS'),(30,'PASS'),(30,'PASS')]]
    assert measure.capacity(steps,2)==10
    assert measure.non_monotonic(steps)


@pytest.mark.parametrize('kwargs', [
    {'server_errors':['HTTP 500']}, {'fake_errors':[{'kind':'protocol_error'}]},
    {'resources':[{'rss_bytes':900000001}]}, {'lag':[{'p99_ms':251}]},
    {'events':[{'type':'voice_error','where':'new'}],'allowed_kinds':set()},
])
def test_irreversible_causes(kwargs): assert fast.irreversible(**kwargs)


def test_no_early_voice_failure_until_baseline_frozen_and_rates_not_inputs():
    assert fast.irreversible(events=[{'type':'voice_error','where':'new'}],allowed_kinds=None) is None
    assert fast.irreversible(events=[{'type':'turn_timing','vad_speech_end':0,'first_audio_played':500},
                                    {'type':'reply_missing'}],allowed_kinds=set()) is None


def test_early_failure_excuses_only_missing_measurements_not_generator_evidence():
    m=valid_metrics();m.update(steady_sample_count=0,close_burst_s=None)
    m['speech_end_to_first_played_s']['p90']=None
    cause={'reason':'RSS > 900 MB'}
    assert measure.verdict(5,m,REFERENCE,early_stop=cause)['verdict']=='FAIL'
    m['generator_loop_lag_ms']['p99']=51
    assert measure.verdict(5,m,REFERENCE,early_stop=cause)['verdict']=='INVALID'
    m['generator_loop_lag_ms']['p99']=1;m['achieved_concurrency']=1;m['steady_sample_count']=1
    assert measure.verdict(5,m,REFERENCE,early_stop=cause)['verdict']=='INVALID'


def healthy_smoke():
    step=sample_step(n=5)
    step['metrics'].update(scenario_mix={s:1 for s in run.SCENARIOS},settled_artifacts=True,
        by_scenario={s:{'latency_samples_s':[1], 'lines_said':1,'lines_answered':1} for s in run.SCENARIOS})
    step['timelines']=[{'session_id':str(i),'completed':True,'completion_reason':'ceiling','video_chunks':1,
        'completed_at':60,'post_complete_close':[]} for i in range(5)]
    step['metrics']['encounters']=[{'session_id':str(i),'lines_answered':1,
        'speech_end_to_first_played_s':{'n':1}} for i in range(5)]
    return step


def test_smoke_preset_is_capacity_ineligible_and_classifies_closes_not_counts():
    args=smoke.preset(run.parse_args([]))
    assert args.encounters_per_participant==1 and args.encounter_seconds==60 and args.webcam
    assert args.duration is None and args.ramp==[5] and args.smoke_workload
    step=healthy_smoke();probe_result={'passed':True,'same_socket':True}
    assert smoke.diagnostic(step,probe_result)['verdict']=='PASS'
    step['timelines'][0]['post_complete_close']=[{'t':61,'code':1006}]*3
    assert smoke.diagnostic(step,probe_result)['verdict']=='PASS'
    assert not smoke.diagnostic(step,probe_result)['capacity_eligible']
    step['server_errors']=['close before completion']
    assert smoke.diagnostic(step,probe_result)['verdict']=='FAIL'


@pytest.mark.parametrize('field', ['video_chunks','completion_reason'])
def test_smoke_requires_real_completion_and_webcam(field):
    step=healthy_smoke();step['timelines'][0].pop(field)
    assert smoke.diagnostic(step,{'passed':True,'same_socket':True})['verdict']=='FAIL'


def test_smoke_gate_stops_before_ramp_and_skip_is_explicit(tmp_path):
    calls=[]
    async def gate(args,directory):calls.append('smoke');return {'ran':True,'verdict':'FAIL'}
    async def ramp(args):calls.append('ramp');return {'verdict':'PASS'},args.report_dir
    args=run.parse_args(['--report-dir',str(tmp_path)])
    result,_=asyncio.run(smoke.run_with_smoke(args,smoke_runner=gate,experiment=ramp))
    assert calls==['smoke'] and result['verdict']=='FAIL' and not result['capacity_eligible']
    args.skip_smoke=True
    asyncio.run(smoke.run_with_smoke(args,smoke_runner=gate,experiment=ramp))
    assert calls==['smoke','ramp']


def test_same_socket_probe_has_no_reconnect_and_idles_six_seconds(monkeypatch):
    client,server=socket.socketpair();requests=[];sleeps=[]
    def serve():
        try:
            for _ in range(2):
                data=b''
                while b'\r\n\r\n' not in data:data+=server.recv(4096)
                requests.append(data)
                body=b'{"data":[{"id":"fake"}]}'
                server.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: keep-alive\r\n\r\n'+body)
        finally:server.close()
    thread=threading.Thread(target=serve);thread.start()
    connections=[]
    def connect(*args,**kwargs):connections.append(args);return client
    monkeypatch.setattr(probe.socket,'create_connection',connect)
    monkeypatch.setattr(probe.ssl,'create_default_context',lambda **kw:SimpleNamespace(wrap_socket=lambda s,**kw:s))
    monkeypatch.setattr(probe,'time',SimpleNamespace(sleep=sleeps.append))
    result=probe.probe('https://localhost:8443','unused',160)
    thread.join(1)
    assert result['same_socket'] and len(connections)==1 and len(requests)==2 and sleeps==[6]
    from server.voice.cascade import GATEWAY_KEEPALIVE_S
    with pytest.raises(ValueError):probe.probe('https://localhost','unused',GATEWAY_KEEPALIVE_S)


def test_k1_baseline_four_separate_units_and_search_metadata(monkeypatch,tmp_path):
    calls=[];windows=[]
    async def step(rt,args,n,repeat,ceiling,**kwargs):
        calls.append((n,repeat,kwargs.get('label'),kwargs.get('reference')))
        if n==1:
            scenario=run.SCENARIOS[(len([x for x in calls if x[0]==1])-1)%4]
            s=sample_step(1,scenario)
            s['metrics']['measurement_window']={'start':len(calls)*600+30,'end':(len(calls)+1)*600}
            windows.append(s['metrics']['measurement_window'])
            return s
        s=sample_step(n)
        s['metrics']['by_scenario']={scenario:copy.deepcopy(sample_step()['metrics']['by_scenario']['S1A']) for scenario in run.SCENARIOS}
        s['metrics']['scenario_mix']={scenario:n//4 for scenario in run.SCENARIOS}
        return s
    monkeypatch.setattr(run,'run_step',step)
    args=run.parse_args(['--ramp-search','--encounters-per-participant','1','--report-dir',str(tmp_path)])
    report,_=asyncio.run(run.run_experiment(args,runtime_factory=FakeRuntime))
    assert [x[0] for x in calls]==[1,1,1,1,20,30,40]
    assert len(set(x[2] for x in calls[:4]))==4 and len(windows)==4
    assert all(x[3] is None for x in calls[:4]) and all(x[3] is not None for x in calls[4:])
    assert report['capacity']==40 and report['ramps'][0]['tested_ns']==[1,20,30,40]
    assert report['ramps'][0]['search_path'][-1]['next_n'] is None
    assert 'different workload' in report['rule'] and report['configuration']['encounters_per_participant']==1
    assert 'Tested Ns' in (tmp_path/'report.md').read_text(encoding='utf-8')


def test_smoke_runtime_is_stopped_before_ramp_and_each_factor_is_fresh(monkeypatch,tmp_path):
    lifecycle=[]
    class SmokeRuntime(FakeRuntime):
        async def start(self):lifecycle.append(('start',str(self.directory)));return await super().start()
        async def stop(self):lifecycle.append(('stop',str(self.directory)))
        async def keepalive_probe(self):lifecycle.append(('probe',str(self.directory)));return {'passed':True,'same_socket':True}
    async def step(*args,**kwargs):return healthy_smoke()
    monkeypatch.setattr(run,'run_step',step)
    async def gate(args,directory):return await smoke.run_smoke(args,directory,runtime_factory=SmokeRuntime)
    async def ramp(args):
        lifecycle.append(('ramp',str(args.report_dir)))
        assert args.smoke_results['verdict']=='PASS'
        return {'verdict':'PASS'},args.report_dir
    args=run.parse_args(['--tts-factors','1,3','--report-dir',str(tmp_path)])
    asyncio.run(smoke.run_with_smoke(args,smoke_runner=gate,experiment=ramp))
    assert [kind for kind,_ in lifecycle]==['start','probe','stop','start','probe','stop','ramp']
    assert lifecycle[0][1]!=lifecycle[3][1]


def test_incremental_voice_error_stops_pending_worker_and_preserves_invalid_precedence(monkeypatch,tmp_path):
    import time
    client=httpx.AsyncClient
    monkeypatch.setattr(run.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(
        lambda request:httpx.Response(307,headers={'location':'/v2?participant_id=p&run=r'})),**kw))
    rt=FakeRuntime(tmp_path)
    emitted=[]
    def rows():
        if emitted:return []
        emitted.append(True)
        return [{'type':'voice_error','where':'new-kind','wall':time.time()}]
    rt.event_rows=rows
    class Waiting:
        def __init__(self,*a,**kw):
            self.active=False;self.tl={'session_id':'s','scenario':'S2A','completed':False,'lines':[],
                'hangup_requested':None,'server_errors':[]}
        async def run(self):
            self.active=True
            try:await asyncio.Event().wait()
            finally:self.tl['hangup_requested']=time.time();self.active=False
    async def exercise():
        await rt.start()
        args=run.parse_args(['--scenarios','S2A','--warmup','0.1'])
        return await asyncio.wait_for(run.run_step(rt,args,1,1,600,participant_factory=Waiting,reference=REFERENCE),2)
    step=asyncio.run(exercise())
    assert step['early_stop'] and 'new voice_error' in step['early_stop']['reason']
    assert not step['errors']


def test_incremental_server_worker_exception_does_not_wait_for_other_workers(monkeypatch,tmp_path):
    import time
    client=httpx.AsyncClient
    async def handler(request):
        if request.url.params['name'].endswith('_0'):
            await asyncio.sleep(0.15)
            return httpx.Response(503)
        return httpx.Response(307,headers={'location':'/v2?participant_id=p&run=r'})
    monkeypatch.setattr(run.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handler),**kw))
    class Waiting:
        def __init__(self,*a,**kw):
            self.active=True;self.tl={'session_id':'s','scenario':'S2A','completed':False,'lines':[],
                'hangup_requested':None}
        async def run(self):
            try:await asyncio.Event().wait()
            finally:self.tl['hangup_requested']=time.time()
    async def exercise():
        rt=FakeRuntime(tmp_path);await rt.start()
        args=run.parse_args(['--scenarios','S2A','--warmup','0.1'])
        return await asyncio.wait_for(run.run_step(rt,args,2,1,600,participant_factory=Waiting),2)
    step=asyncio.run(exercise())
    assert step['early_stop'] and step['server_errors'] and not step['errors']


def test_docker_same_socket_probe_against_real_fake_is_opt_in(tmp_path):
    import os
    if os.getenv('RF_LOAD_DOCKER_TESTS')!='1':pytest.skip('requires RF_LOAD_DOCKER_TESTS=1; real Docker isolated fake')
    from tools.load.runtime import Runtime
    async def exercise():
        rt=Runtime(tmp_path)
        try:
            await rt.start()
            result=await rt.keepalive_probe()
            assert result['same_socket'] and result['idle_s']==6 and result['responses']==2
        finally:await rt.stop()
    asyncio.run(exercise())


def test_search_retry_and_frozen_baseline_after_all_repeats(monkeypatch,tmp_path):
    calls=[];attempts=Counter()
    async def step(rt,args,n,repeat,ceiling,**kw):
        calls.append((n,repeat,kw.get('reference')))
        s=sample_step(n,k='all')
        if n==1:
            s['metrics']['voice_error_kinds']={f'baseline-{repeat}':1}
        elif n==20:
            attempts[(n,repeat)]+=1
            if attempts[(n,repeat)]==1:s['errors']=['generator test failure']
        return s
    monkeypatch.setattr(run,'run_step',step)
    args=run.parse_args(['--ramp-search','--search-ceiling','20','--repeat','2','--report-dir',str(tmp_path)])
    report,_=asyncio.run(run.run_experiment(args,runtime_factory=FakeRuntime))
    assert [n for n,_,_ in calls]==[1,1,20,20,20,20]
    for n,_,ref in calls:
        if n>1:assert set(ref['voice_error_kinds'])=={'baseline-1','baseline-2'}
    assert report['capacity']==20 and report['verdict']=='PASS'
    assert [p['verdict'] for p in report['ramps'][0]['search_path']]==['PASS','INVALID','PASS']
    assert len([s for s in report['steps'] if s.get('superseded_invalid_attempt')])==2


def test_probe_failure_cannot_reconnect_after_first_response(monkeypatch):
    client,server=socket.socketpair();connections=[]
    def serve():
        server.recv(4096)
        body=b'{"data":[{"id":"fake"}]}'
        server.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        server.close()
    thread=threading.Thread(target=serve);thread.start()
    def connect(*a,**kw):connections.append(True);return client
    monkeypatch.setattr(probe.socket,'create_connection',connect)
    monkeypatch.setattr(probe.ssl,'create_default_context',lambda **kw:SimpleNamespace(wrap_socket=lambda s,**kw:s))
    monkeypatch.setattr(probe,'time',SimpleNamespace(sleep=lambda _:None))
    with pytest.raises(OSError):probe.probe('https://localhost','unused',160)
    thread.join(1)
    assert len(connections)==1


def test_observer_streams_error_events_and_retires_closed_sessions(tmp_path):
    from tools.load.observer import EncounterEvents
    folder=tmp_path/'s';folder.mkdir()
    events=folder/'events.jsonl'
    events.write_text('\n'.join(json.dumps(row) for row in [
        {'type':'turn_timing','large':'body'}, {'type':'voice_error','where':'new'}])+'\n',encoding='utf-8')
    reader=EncounterEvents(tmp_path)
    rows=reader.read()
    assert len(rows)==1 and rows[0]['event']['type']=='voice_error'
    assert reader.read()==[]
    (folder/'manifest.json').write_text('{"status":"closed"}',encoding='utf-8')
    assert reader.read()==[] and not reader.tails and events in reader.finished


def test_deliberate_stop_does_not_excuse_preexisting_invalid_baseline():
    result=measure.verdict(5,valid_metrics(),REFERENCE,baseline_valid=False,early_stop={'reason':'RSS > 900 MB'})
    assert result['verdict']=='INVALID'
