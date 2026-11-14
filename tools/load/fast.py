"""Workload comparability, baseline pooling and irreversible observations."""
from __future__ import annotations

from collections import Counter
import copy

from tools.load import measure
from tools.sim import analyze


BASELINE_RULE = ('K=1 is a different workload from K=all. Its N=1 repeat uses one sequential fresh '
                 'single-encounter participant per selected scenario, each with its own warmup/window; '
                 'all selected scenarios must validate. '
                 'The reference is weighted to the measured participant-turn mix of each step. '
                 'K=all retains the server Williams run and encounter transitions. Capacity covers tested points only.')


def weighted_percentile(samples, q):
    rows = sorted((value, weight) for values, total_weight in samples for value, weight in
                  ((v, total_weight / len(values)) for v in values) if weight > 0)
    target = sum(weight for _, weight in rows) * q / 100
    cumulative = 0
    for value, weight in rows:
        cumulative += weight
        if cumulative >= target:
            return value
    return None


def reference(repeats):
    ref = measure.baseline(repeats)
    ref['encounters_per_participant'] = repeats[0]['metrics'].get('encounters_per_participant', 'all')
    scenarios = sorted({s for r in repeats for s in r['metrics'].get('by_scenario', {})})
    ref['by_scenario'] = {}
    for scenario in scenarios:
        metrics = [r['metrics']['by_scenario'][scenario] for r in repeats if scenario in r['metrics'].get('by_scenario', {})]
        item = measure.baseline([{'metrics': m} for m in metrics])
        item['samples'] = [x for m in metrics for x in m['latency_samples_s']]
        ref['by_scenario'][scenario] = item
    return ref


def matched_reference(ref, metrics):
    if not ref:
        return ref, []
    k = metrics.get('encounters_per_participant', 'all')
    if ref.get('encounters_per_participant', 'all') != k:
        return ref, ['baseline/step encounters-per-participant K mismatch']
    if k == 'all':
        return ref, []
    mix = {s: m['participant_turns'] for s, m in metrics.get('by_scenario', {}).items() if m['participant_turns']}
    if not mix or set(mix) - set(ref.get('by_scenario', {})) or set(metrics.get('scenario_mix', {})) - set(mix):
        return ref, ['scenario-matched N=1 reference missing']
    result = dict(ref)
    result['p90_s'] = weighted_percentile([(ref['by_scenario'][s]['samples'], weight) for s, weight in mix.items()], 90)
    for key in ('reply_missing_per_100_turns', 'first_played_missing_per_100_turns'):
        if any(ref['by_scenario'][s][key] is None for s in mix):
            return ref, ['scenario baseline turn denominator missing']
        result[key] = sum(ref['by_scenario'][s][key] * weight for s, weight in mix.items()) / sum(mix.values())
    result['scenario_weights'] = mix
    return result, []


def evaluate(step, ref, *, baseline_valid=True):
    matched, mismatch = matched_reference(ref, step['metrics'])
    if step.get('early_stop') and mismatch == ['scenario-matched N=1 reference missing']:
        matched, mismatch = ref, []  # Missing turns after an intentional stop are censored.
    decision = measure.verdict(step['concurrency'], step['metrics'], matched,
        full_length=step['full_length'], errors=step['errors'] + mismatch,
        server_errors=step.get('server_errors', []), fake_errors=step['fake_errors'],
        baseline_valid=baseline_valid, early_stop=step.get('early_stop'))
    if step.get('baseline_units'):
        states = []
        for unit in step['baseline_units']:
            state = evaluate(unit, ref, baseline_valid=baseline_valid)
            unit.update(state)
            states.append(state)
        if any(s['verdict'] == 'INVALID' for s in states):
            decision.update(verdict='INVALID', reasons=['a per-scenario baseline unit is INVALID'])
        elif any(s['verdict'] == 'FAIL' for s in states):
            decision.update(verdict='FAIL', reasons=['a per-scenario baseline unit FAILed'])
        actual = set(step['metrics'].get('by_scenario', {}))
        if actual != set(step['expected_scenarios']) and not step.get('early_stop'):
            decision.update(verdict='INVALID', reasons=['N=1 first-encounter baseline did not cover every selected scenario'])
    decision['matched_reference'] = matched
    if step.get('tts_proof', {}).get('passed') is False:
        decision['reasons'].extend(step['tts_proof']['reasons'])
        if decision['verdict'] != 'INVALID':
            decision['verdict'] = 'FAIL'
    return decision


def combine_units(units, scenarios, k):
    result = copy.deepcopy(units[0])
    metrics = result['metrics']
    for key in ('latency_samples_s', 'encounters', 'resource_samples', 'concurrency_samples', 'generator_lag_samples', 'post_complete_close'):
        metrics[key] = [v for u in units for v in u['metrics'].get(key, [])]
    for key in ('participant_turns', 'reply_missing', 'first_played_missing', 'lines_said', 'lines_heard', 'lines_answered',
                'encounter_count', 'voice_error', 'websocket_disconnects', 'unexpected_disconnects'):
        metrics[key] = sum(u['metrics'].get(key, 0) for u in units)
    for key in ('reply_missing', 'first_played_missing'):
        metrics[key + '_per_100_turns'] = 100 * metrics[key] / metrics['participant_turns'] if metrics['participant_turns'] else None
    metrics['speech_end_to_first_played_s'] = measure.percentiles(metrics['latency_samples_s'])
    metrics['achieved_concurrency'] = min(u['metrics']['achieved_concurrency'] for u in units)
    for key, stat in [('generator_loop_lag_ms', 'p99'), ('server_loop_lag_ms', 'p99'), ('server_rss_bytes', 'max')]:
        values = [u['metrics'][key][stat] for u in units if u['metrics'][key][stat] is not None]
        metrics[key][stat] = max(values, default=None)
    metrics['voice_error_kinds'] = dict(sum((Counter(u['metrics']['voice_error_kinds']) for u in units), Counter()))
    metrics['bytes_written_per_encounter'] = {sid: size for u in units for sid, size in
                                               u['metrics'].get('bytes_written_per_encounter', {}).items()}
    metrics['upstream_by_unit'] = [u['metrics'].get('upstream', {}) for u in units]
    metrics['upstream'] = {}  # Percentile summaries cannot be pooled without raw attempts.
    metrics['baseline_aggregation'] = 'pooled latency/counts; each unit validates independently; stage distributions remain per unit'
    metrics['by_scenario'] = {}
    for scenario in scenarios:
        rows = [u['metrics']['by_scenario'][scenario] for u in units if scenario in u['metrics'].get('by_scenario', {})]
        if rows:
            pooled = dict(rows[0])
            for key in ('latency_samples_s',): pooled[key] = [x for r in rows for x in r[key]]
            for key in ('participant_turns', 'reply_missing', 'first_played_missing'): pooled[key] = sum(r[key] for r in rows)
            metrics['by_scenario'][scenario] = pooled
    metrics['measurement_windows'] = [u['metrics']['measurement_window'] for u in units]
    metrics['scenario_mix'] = dict(sum((Counter(u['metrics'].get('scenario_mix', {})) for u in units), Counter()))
    metrics['encounter_positions'] = [p for u in units for p in u['metrics'].get('encounter_positions', [])]
    metrics['encounters_per_participant'] = k
    result.update(baseline_units=units, expected_scenarios=list(scenarios),
                  sessions=[s for u in units for s in u.get('sessions', [])],
                  timelines=[t for u in units for t in u.get('timelines', [])],
                  early_stop=next((u.get('early_stop') for u in units if u.get('early_stop')), None),
                  errors=[e for u in units for e in u['errors']],
                  server_errors=[e for u in units for e in u.get('server_errors', [])],
                  fake_errors=[e for u in units for e in u['fake_errors']],
                  live_stop=next((u['live_stop'] for u in units if u['live_stop']), None))
    if 'tts_proof' in units[-1]:
        result['tts_proof'] = dict(units[-1]['tts_proof'],
            passed=all(u['tts_proof']['passed'] for u in units),
            reasons=sorted({r for u in units for r in u['tts_proof']['reasons']}))
    return result


def irreversible(*, server_errors=(), fake_errors=(), events=(), resources=(), lag=(), allowed_kinds=None):
    if server_errors: return 'server error: ' + str(server_errors[0])
    if any(e.get('type') == 'error' for e in events): return 'server error event'
    if fake_errors: return 'fake route/protocol error'
    if any(r.get('rss_bytes', 0) is not None and r.get('rss_bytes', 0) > 900_000_000 for r in resources):
        return 'RSS > 900 MB'
    if any(r['p99_ms'] > 250 for r in lag): return 'completed server loop-lag window p99 > 250 ms'
    if allowed_kinds is not None:
        for event in events:
            if event.get('type') == 'voice_error' and (kind := measure.voice_error_kind(event)) is not None and kind not in allowed_kinds:
                return 'new voice_error kind: ' + kind
    return None
