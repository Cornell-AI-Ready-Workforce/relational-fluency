"""Diagnostic smoke gate in fresh runtimes, separate from capacity verdicts."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
import datetime as dt
from http.client import HTTPException
import json
from pathlib import Path
import secrets

from tools.load import measure
from tools.load.fake_upstream import FakeConfig, load_config
from tools.load.runtime import ROOT, Runtime


def preset(args):
    smoke = copy.copy(args)
    smoke.encounters_per_participant = 1
    smoke.encounter_seconds = 60.0
    smoke.duration, smoke.steps = None, None
    smoke.scenarios = ['S1A', 'S2A', 'S3A', 'S4A']
    smoke.arrival, smoke.warmup, smoke.webcam, smoke.fail_fast = 0, 0, True, False
    smoke.repeat, smoke.ramp = 1, [5]
    smoke.ramp_search = None
    smoke.smoke_workload = True
    return smoke


def diagnostic(step, probe, expected=('S1A', 'S2A', 'S3A', 'S4A')):
    m = step['metrics']
    reasons = list(step['errors']) + list(step.get('server_errors', []))
    if not probe.get('passed') or not probe.get('same_socket'): reasons.append('same-socket keep-alive probe failed')
    if step['fake_errors']: reasons.append('fake route/protocol error')
    if m.get('whole_step', m).get('voice_error'): reasons.append('non-benign voice_error during smoke')
    if not m.get('settled_artifacts'): reasons.append('closed manifests or rendered mixes missing')
    if m['achieved_concurrency'] < 4.5: reasons.append('smoke achieved concurrency < 90% of N=5')
    if m['generator_loop_lag_ms']['p99'] is None or m['generator_loop_lag_ms']['p99'] > 50:
        reasons.append('generator lag missing or >50 ms')
    if m['server_loop_lag_ms']['p99'] is None or m['server_loop_lag_ms']['p99'] > 250:
        reasons.append('server loop lag missing or >250 ms')
    if m['server_rss_bytes']['max'] is None or m['server_rss_bytes']['max'] > 900_000_000:
        reasons.append('server RSS missing or >900 MB')
    if set(m.get('scenario_mix', {})) != set(expected): reasons.append('smoke did not cover all four scenarios')
    timelines = step.get('timelines', [])
    encounters = {e['session_id']: e for e in m.get('whole_step', m).get('encounters', [])}
    if len(timelines) != 5: reasons.append('smoke needs five single-encounter participants')
    for t in timelines:
        if not t.get('completed') or t.get('completion_reason') != 'ceiling' or t.get('duration_limit_reached'):
            reasons.append('smoke did not end through the server ceiling')
        if not t.get('video_chunks'): reasons.append('webcam upload evidence missing')
        encounter = encounters.get(t.get('session_id'), {})
        if not encounter.get('lines_answered') or not encounter.get('speech_end_to_first_played_s', {}).get('n'):
            reasons.append('per-participant playback evidence missing')
        if t.get('server_errors') or t.get('generator_errors') or t.get('unexpected_disconnect'):
            reasons.append('pre-completion/worker failure')
        for event in t.get('post_complete_close', []):
            if not t.get('completed_at') or event['t'] < t['completed_at']:
                reasons.append('close classification inconsistent with completion')
    for scenario in expected:
        members = [encounters.get(t.get('session_id'), {}) for t in timelines if t.get('scenario') == scenario]
        member = m.get('by_scenario', {}).get(scenario, {})
        evidence = (any(e.get('lines_said') and e.get('lines_answered') and
                        e.get('speech_end_to_first_played_s', {}).get('n') for e in members)
                    if 'whole_step' in m else
                    bool(member.get('latency_samples_s') and member.get('lines_said') and member.get('lines_answered')))
        if not evidence:
            reasons.append(f'{scenario}: speech/playback evidence missing')
    return {'verdict': 'FAIL' if reasons else 'PASS', 'reasons': sorted(set(reasons)),
            'diagnostic': True, 'capacity_eligible': False, 'keepalive_probe': probe}


async def run_smoke(args, directory, *, runtime_factory=Runtime, participant_factory=None):
    from tools.load.run import run_step, Participant, load_key, production_environment
    config = load_config(args.fake_config, profile=args.fake_tts_profile)
    outcomes, gateway_errors = [], []
    if args.gateway == 'live' and args.max_concurrency < 5:
        return {'ran': False, 'verdict': 'FAIL', 'capacity_eligible': False,
                'reasons': ['N=5 smoke exceeds live concurrency cap'], 'outcomes': []}
    for factor in args.tts_factors or [config.tts_realtime_factor]:
        runtime = runtime_factory(directory / f'smoke-tts-{factor:g}', backend=args.backend, gateway=args.gateway,
            image=args.image, profile_image=args.profile_image, fake_config=replace(config, tts_realtime_factor=factor),
            live_key=load_key() if args.gateway == 'live' else None,
            live_url=args.live_url or production_environment().get('LLM_BASE_URL'), cpu_scale=args.cpu_scale,
            fargate_reference=args.fargate_reference, encounter_seconds=60)
        try:
            await runtime.start()
            probe = await runtime.keepalive_probe()
            step = await run_step(runtime, preset(args), 5, 1, 60, participant_factory=participant_factory or Participant)
            outcome = {**diagnostic(step, probe), 'tts_factor': factor, 'step': step}
        except (RuntimeError, OSError, ValueError, HTTPException) as exc:
            outcome = {'verdict': 'FAIL', 'reasons': [type(exc).__name__ + ': ' + str(exc)],
                       'diagnostic': True, 'capacity_eligible': False}
        finally:
            await runtime.stop()
            rows = measure.JsonlTail(runtime.data / "upstream-attempts.jsonl").read()
            gateway_errors.extend({"phase": "smoke", "tts_realtime_factor": factor, **row}
                                  for row in measure.gateway_errors(rows))
        outcomes.append(outcome)
        print('Smoke: ' + outcome['verdict'] + ' ' + '; '.join(outcome['reasons']), flush=True)
        if outcome['verdict'] != 'PASS': break
    return {'ran': True, 'fake_tts_profile': config.tts_profile, 'fake_configuration': asdict(config),
            'gateway_errors': gateway_errors,
            'verdict': 'PASS' if all(o['verdict'] == 'PASS' for o in outcomes) else 'FAIL',
            'capacity_eligible': False, 'outcomes': outcomes}


async def run_with_smoke(args, *, smoke_runner=run_smoke, experiment=None):
    from tools.load.run import run_experiment, _write_report
    args = copy.copy(args)
    directory = (args.report_dir or ROOT / 'tools/load/reports' /
                 (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(3))).resolve()
    args.report_dir = directory
    gate = args.smoke or (not args.skip_smoke and args.duration is None and args.encounter_seconds is None and not args.isolation_only)
    if gate:
        args.smoke_results = await smoke_runner(args, directory)
        if args.smoke or args.smoke_results['verdict'] != 'PASS':
            report = {'rule': 'Diagnostic smoke; no capacity estimate.', 'configuration': {
                      'smoke': args.smoke_results, 'live_guard_rule': measure.LIVE_GUARD_RULE,
                      'live_guard_counted_purposes': list(measure.LIVE_RATE_PURPOSES)},
                      'gateway_errors': args.smoke_results.get('gateway_errors', []),
                      'smoke': args.smoke_results, 'capacity_eligible': False, 'capacity': None,
                      'verdict': args.smoke_results['verdict'], 'steps': []}
            _write_report(directory, report)
            return report, directory
    else:
        args.smoke_results = {'ran': False, 'reason': '--skip-smoke or diagnostic/isolation mode'}
    return await (experiment or run_experiment)(args)
