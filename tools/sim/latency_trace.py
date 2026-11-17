"""Identity joins and causal latency paths for reply_trace schema 1."""
from __future__ import annotations

from collections import Counter, defaultdict
import math
from statistics import median

from tools.sim.analyze import percentile

GROUP_FIELDS = ('scenario', 'mode', 'encounter_reply', 'participant_reply', 'family', 'model', 'origin')
LEGACY_ROUNDING_S = .001
CLOCK_OFFSET_LIMIT_S = .005
# An edge is causal. Sibling branches have no ordering constraint.
REPLY_EDGES = {
    'create_entered': ('commit_dispatched',),
    'create_released': ('create_entered',),
    'request_started': ('create_released',),
    'first_content': ('request_started',),
    'first_sentence_ready': ('first_content',),
    'tts_slot_wait_start': ('create_released',),
    'tts_slot_acquired': ('tts_slot_wait_start',),
    'tts_connected': ('tts_slot_acquired',),
    'first_text_sent': ('first_sentence_ready', 'tts_connected'),
    'tts_first_audio': ('first_text_sent',),
    'first_output_emitted': ('tts_first_audio',),
    'first_output_hold_started': ('first_output_emitted',),
    'first_output_hold_released': ('first_output_hold_started', 'release_transcript_decided'),
}
EDGE_NAMES = {
    'vad_detected': 'estimated_trailing_silence',
    'briefing_entered': 'before_briefing', 'briefing_completed': 'briefing',
    'commit_dispatched': 'commit_dispatch', 'create_entered': 'commit_to_create',
    'create_released': 'create_join', 'request_started': 'request_dispatch',
    'first_content': 'request_to_content', 'first_sentence_ready': 'content_to_sentence',
    'tts_slot_wait_start': 'tts_dispatch', 'tts_slot_acquired': 'tts_slot_wait',
    'tts_connected': 'tts_connect', 'first_text_sent': 'text_dispatch_after_readiness',
    'tts_first_audio': 'text_to_provider_audio', 'first_output_emitted': 'provider_to_output',
    'first_output_hold_started': 'output_to_hold',
    'first_output_hold_released': 'hold_release_after_readiness',
    'release_stt_started': 'stt_dispatch', 'release_stt_done': 'stt',
    'release_transcript_decided': 'transcript_decision',
    'first_audio_from_gateway': 'output_to_gateway',
    'first_audio_to_client': 'available_output_to_client', 'first_audio_played': 'client_to_played_ack',
}
WORK_EDGES = {
    'create_join': ('create_entered', 'create_released'),
    'request_to_content': ('request_started', 'first_content'),
    'content_to_sentence': ('first_content', 'first_sentence_ready'),
    'generation_to_sentence': ('request_started', 'first_sentence_ready'),
    'tts_slot_wait': ('tts_slot_wait_start', 'tts_slot_acquired'),
    'tts_connect': ('tts_slot_acquired', 'tts_connected'),
    'text_to_provider_audio': ('first_text_sent', 'tts_first_audio'),
    'provider_to_output': ('tts_first_audio', 'first_output_emitted'),
    'observed_audio_hold': ('first_output_hold_started', 'first_output_hold_released'),
}
NOTES = [
    'Schema 1 stages are encounter_s seconds. Event t/wall date teardown and are never used as observation stamps.',
    'The provider critical path ends at tts_first_audio. The client path can select STT at a hold-release join. Neither path sums sibling branches.',
    'At a join, the latest observed prerequisite selects the path; exact ties share the label via critical_parents. This is observed timing, not a predicted optimization gain.',
    'Work durations and pairwise overlaps are not additive. Readiness gaps are separate from critical-path allocation.',
    'Path-stage statistics use only complete paths with no issues. n and quantiles are conditional on criticality; path_n is the complete-path denominator. critical_share and mean_contribution_s include zero contributions from other complete paths.',
    'Work n counts observed spans. commit_work n counts referenced commit spans per reply and can exceed the reply count; missing stamps do not become zero-duration samples.',
    'Trace page_turn joins turn_timing.turn within a source session. Commit item IDs join only their own commit_trace; release_commit_item_id can name a different commit.',
    'Unmatched traces, timings, missing commits, duplicates and unsupported schemas remain explicit. Ambiguous identities are not joined by order or by nearest timestamp.',
    'Coverage includes untraced sessions in the same input set. Legacy sessions, rooms and other model families can legitimately lack this 1:1 cascade trace.',
    'Groups inherit the matched legacy row and also split by origin, so participant_commit, silence_probe, replay and other creates do not mix work populations.',
    'Only paired VAD marks estimate and guard clock offset. Each source records their median and spread. Gateway/output is a causal queue hop, never a clock anchor; commit_sent also includes awaited work.',
    'VAD offsets or spread above 0.005 s exclude client/ACK paths. Offsets within the 0.001 s legacy rounding unit are not subtracted. Other verified offsets are applied with an explicit clamp budget of 0.001 s plus their magnitude, at most 0.006 s. Pure trace edges stay strict.',
    'Unheld client paths expose output_to_gateway separately from the exact gateway-to-client wall interval. The queue-hop estimate uses the applied VAD correction; the raw gateway-minus-VAD estimate is also retained. Unresolved sub-ms VAD hop is not subtracted as latency.',
    'Anchor checks detect observed offsets, not a clock step after the final paired mark. Raw stamps, offsets, correction values and clamps remain inspectable.',
    'felt_* paths start at last_voiced_estimate and expose estimated_trailing_silence serially. They are sample-position estimates alongside the VAD-rooted paths, not measured hearing.',
    'tied_joins counts distinct response/join identities on complete paths, once across all targets; ties use deterministic branch allocation and are not evidence of one dominant branch.',
    'The legacy missing_boundaries list applies to the coarse section. Trace rows list their actual missing stamps. Room routing/floor and browser receipt remain outside schema 1.',
]


def number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def stamps(event):
    return {name: value for name, value in event.get('stages', {}).items() if number(value)}


def interval(values, start, end):
    a, b = values.get(start), values.get(end)
    if number(a) and number(b) and b >= a:
        return {'start_field': start, 'end_field': end, 'start_s': a, 'end_s': b,
                'duration_s': round(b - a, 6)}
    return None


def critical_path(values, edges, target, root_field='vad_detected'):
    """Walk backwards through max-prerequisite joins, never sorted timestamps."""
    root, end = values.get(root_field), values.get(target)
    out = {'target': target, 'root_field': root_field, 'total_s': None, 'exposed_s': {}, 'steps': [],
           'unexplained_s': None, 'issues': []}
    if root_field == 'last_voiced_estimate':
        out['estimate'] = 'sample_position'
    if not (number(root) and number(end) and end >= root):
        out['issues'].append('missing_or_invalid_path_endpoints')
        return out
    out['total_s'] = round(end - root, 6)
    node = target
    visited = set()
    while node != root_field:
        parents = edges.get(node, ())
        if node in visited or not parents or not all(number(values.get(p)) for p in parents):
            out['issues'].append('missing_dependency:' + node)
            break
        visited.add(node)
        # A join requires *all* parents. A missing parent is not zero work.
        if any(values[p] > values[node] or values[p] < root for p in parents):
            out['issues'].append('invalid_causal_edge:' + node)
            break
        parent = max(parents, key=lambda p: values[p])
        span = interval(values, parent, node)
        span['prerequisite_fields'] = list(parents)
        span['critical_parents'] = [p for p in parents if values[p] == values[parent]]
        span['stage'] = EDGE_NAMES.get(node, node)
        if node == 'first_audio_to_client' and parent == 'first_audio_from_gateway':
            span['stage'] = 'gateway_to_client'
        out['steps'].append(span)
        out['exposed_s'][span['stage']] = span['duration_s']
        node = parent
    out['steps'].reverse()
    out['unexplained_s'] = round(max(0, out['total_s'] - sum(out['exposed_s'].values())), 6)
    return out


def align_clocks(values, timing):
    """Verify with VAD; preserve the causal emit-to-bridge hop as work."""
    anchors = []
    pairs = [('vad', 'vad_speech_end', 'vad_detected')]
    for name, wall, mono in pairs:
        if timing is not None and number(timing.get(wall)) and number(values.get(mono)):
            anchors.append({'name': name, 'wall_field': 'turn_timing.' + wall, 'trace_field': mono,
                            'offset_s': round(timing[wall] - values[mono], 6)})
    info = {'status': 'not_applicable' if timing is None else 'cross_clock_unverified',
            'anchors': anchors, 'clock_offset_s': None, 'preferred_anchor': None,
            'offset_limit_s': CLOCK_OFFSET_LIMIT_S, 'quantization_tolerance_s': LEGACY_ROUNDING_S,
            'applied_offset_s': 0, 'clamp_tolerance_s': LEGACY_ROUNDING_S,
            'corrected_fields': [], 'clamps': [], 'issues': []}
    if not anchors:
        if timing is not None:
            info['issues'].append('no_paired_clock_anchor')
        return info
    preferred = anchors[0]
    offset = preferred['offset_s']
    info.update(clock_offset_s=offset, preferred_anchor=preferred['name'])
    if any(abs(a['offset_s']) > CLOCK_OFFSET_LIMIT_S for a in anchors):
        info['issues'].append('clock_offset_above_limit')
    if max(a['offset_s'] for a in anchors) - min(a['offset_s'] for a in anchors) > CLOCK_OFFSET_LIMIT_S:
        info['issues'].append('anchor_disagreement')
    if info['issues']:
        return info
    info['status'] = 'verified'
    # The VAD mark has its own rounding and runner hop. Do not subtract an
    # unresolved offset. For larger verified offsets include that uncertainty
    # in the clamp budget, rather than losing a valid held-reply sample.
    applied = 0 if abs(offset) <= LEGACY_ROUNDING_S else offset
    info.update(applied_offset_s=applied, clamp_tolerance_s=LEGACY_ROUNDING_S + abs(applied))
    unheld_gateway = ('first_output_hold_started' not in values
                      and number(values.get('first_audio_from_gateway'))
                      and number(values.get('first_output_emitted')))
    if unheld_gateway:
        gateway_offset = values['first_audio_from_gateway'] - values['first_output_emitted']
        info['gateway_observation'] = {'gateway_minus_output_s': round(gateway_offset, 6),
            'gateway_minus_vad_offset_s': round(gateway_offset - offset, 6),
            'used_as_clock_anchor': False}
    for name in ('first_audio_from_gateway', 'first_audio_to_client', 'first_audio_played'):
        if number(values.get(name)):
            info['corrected_fields'].append({'field': name, 'raw_s': values[name],
                                            'corrected_s': round(values[name] - applied, 6)})
            values[name] = round(values[name] - applied, 6)
    if unheld_gateway:
        delta = values['first_output_emitted'] - values['first_audio_from_gateway']
        if 0 < delta <= info['clamp_tolerance_s'] + 1e-9:
            info['clamps'].append({'field': 'first_audio_from_gateway', 'prerequisite_field': 'first_output_emitted',
                'raw_s': values['first_audio_from_gateway'], 'corrected_s': values['first_output_emitted'],
                'delta_s': round(delta, 6)})
            values['first_audio_from_gateway'] = values['first_output_emitted']
            # Preserve both legacy wall-to-wall intervals exactly when their
            # common gateway origin needs a sub-resolution adjustment.
            for name in ('first_audio_to_client', 'first_audio_played'):
                if number(values.get(name)):
                    before = values[name]
                    values[name] = round(before + delta, 6)
                    info['corrected_fields'].append({'field': name, 'raw_s': before, 'corrected_s': values[name],
                        'reason': 'preserve_wall_tail_after_gateway_clamp'})
    parent = 'first_output_hold_released' if 'first_output_hold_started' in values else 'first_output_emitted'
    if unheld_gateway:
        parent = 'first_audio_from_gateway'
    for name, before in (('first_audio_to_client', parent), ('first_audio_played', 'first_audio_to_client')):
        if number(values.get(name)) and number(values.get(before)):
            delta = values[before] - values[name]
            if 0 < delta <= info['clamp_tolerance_s'] + 1e-9:
                info['clamps'].append({'field': name, 'prerequisite_field': before,
                    'raw_s': values[name], 'corrected_s': values[before], 'delta_s': round(delta, 6)})
                values[name] = values[before]
    return info


def exclude_clock_paths(row):
    for label, path in row['paths'].items():
        if label.endswith(('client_audio', 'played_ack')):
            path.update(total_s=None, exposed_s={}, steps=[], unexplained_s=None,
                        issues=['cross_clock_unverified'])


def split_reply(reply, commits, timing):
    """Keep full work spans, readiness gaps and critical paths separate."""
    values = stamps(reply)
    fields = {name: 'reply_trace.stages.' + name for name in values}
    problems = []
    primary = [c for c in commits if c['commit_item_id'] in reply.get('commit_item_ids', [])
               and c.get('participant_turn_id') is not None
               and c.get('participant_turn_id') == reply.get('participant_turn_id')]
    if len(primary) == 1:
        commit = primary[0]
        for name, value in stamps(commit).items():
            if name == 'commit_dispatched':
                values[name] = value
                fields[name] = 'commit_trace[' + commit['commit_item_id'] + '].stages.' + name
        for name in ('vad_detected', 'last_voiced_estimate', 'briefing_entered', 'briefing_completed'):
            value = commit.get('participant', {}).get(name)
            if number(value):
                values[name] = value
                fields[name] = 'commit_trace[' + commit['commit_item_id'] + '].participant.' + name
    elif reply.get('participant_turn_id') is not None:
        problems.append('missing_or_ambiguous_participant_commit')
    edges = dict(REPLY_EDGES)
    if all(name in values for name in ('briefing_entered', 'briefing_completed')):
        edges.update(briefing_entered=('vad_detected',), briefing_completed=('briefing_entered',),
                     commit_dispatched=('briefing_completed',))
    else:
        edges['commit_dispatched'] = ('vad_detected',)
    work = {name: span for name, (start, end) in WORK_EDGES.items()
            if (span := interval(values, start, end)) is not None}
    estimate = interval(values, 'last_voiced_estimate', 'vad_detected')
    if estimate is not None:
        work['estimated_trailing_silence'] = estimate
    commit_work = []
    for commit in commits:
        cs = stamps(commit)
        for name, start, end in (('stt', 'stt_started', 'stt_done'),
                                 ('transcript_decision', 'stt_done', 'transcript_decided')):
            span = interval(cs, start, end)
            if span is not None:
                commit_work.append({**span, 'stage': name, 'commit_item_id': commit['commit_item_id']})
    overlaps = []
    ranges = [('generation', work.get('generation_to_sentence')), ('tts_connect', work.get('tts_connect'))]
    ranges += [('stt:' + c['commit_item_id'], c) for c in commit_work if c['stage'] == 'stt']
    for i, (a_name, a) in enumerate(ranges):
        for b_name, b in ranges[i + 1:]:
            if a is not None and b is not None:
                overlaps.append({'branches': [a_name, b_name],
                    'duration_s': round(max(0, min(a['end_s'], b['end_s']) - max(a['start_s'], b['start_s'])), 6)})
    gates = {}
    sentence, connected = values.get('first_sentence_ready'), values.get('tts_connected')
    if number(sentence) and number(connected):
        gates['tts_readiness_after_sentence'] = round(max(0, connected - sentence), 6)
        gates['sentence_readiness_after_connection'] = round(max(0, sentence - connected), 6)
    release_id = reply.get('release_commit_item_id')
    release = [c for c in commits if c['commit_item_id'] == release_id]
    if len(release) == 1:
        for name in ('commit_dispatched', 'stt_started', 'stt_done', 'transcript_decided'):
            value = stamps(release[0]).get(name)
            if number(value):
                values['release_' + name] = value
                fields['release_' + name] = 'commit_trace[' + release_id + '].stages.' + name
        # A different commit can unblock this reply. Its entire STT history
        # does not inherit this reply's VAD/commit root.
        if len(primary) == 1 and primary[0]['commit_item_id'] == release_id:
            edges.update(release_stt_started=('commit_dispatched',), release_stt_done=('release_stt_started',),
                         release_transcript_decided=('release_stt_done',))
        hold, decided, released = (values.get(n) for n in
            ('first_output_hold_started', 'release_transcript_decided', 'first_output_hold_released'))
        if (all(number(v) for v in (hold, decided, released)) and max(hold, decided) <= released
                and (release[0].get('decision') or {}).get('outcome') == 'accepted'):
            gates['transcript_readiness_after_audio_hold'] = round(max(0, decided - hold), 6)
    if timing is not None:
        for name in ('first_audio_from_gateway', 'first_audio_to_client', 'first_audio_played'):
            value = timing.get(name)
            if number(value):
                values[name] = value
                fields[name] = 'turn_timing.' + name
    held = 'first_output_hold_started' in values
    edges['first_audio_to_client'] = ('first_output_hold_released',) if held else ('first_output_emitted',)
    if not held and 'first_audio_from_gateway' in values:
        edges['first_audio_from_gateway'] = ('first_output_emitted',)
        edges['first_audio_to_client'] = ('first_audio_from_gateway',)
    edges['first_audio_played'] = ('first_audio_to_client',)
    raw_values = dict(values)
    alignment = align_clocks(values, timing)
    if not held:
        bridge_hop = interval(values, 'first_output_emitted', 'first_audio_from_gateway')
        if bridge_hop is not None:
            work['output_to_gateway'] = {**bridge_hop, 'estimate': 'vad_clock_anchor'}
    for node, parents in edges.items():
        if number(values.get(node)):
            problems.extend('invalid_causal_edge:' + parent + '->' + node for parent in parents
                            if number(values.get(parent)) and values[parent] > values[node])
    paths = {label: critical_path(values, edges, target) for label, target in (
        ('provider_audio', 'tts_first_audio'), ('adapter_output', 'first_output_emitted'),
        ('client_audio', 'first_audio_to_client'), ('played_ack', 'first_audio_played'))}
    felt_edges = {**edges, 'vad_detected': ('last_voiced_estimate',)}
    paths.update({'felt_' + label: critical_path(values, felt_edges, path['target'], 'last_voiced_estimate')
                  for label, path in list(paths.items())})
    for label, path in paths.items():
        if label.endswith(('client_audio', 'played_ack')):
            used_fields = {'first_audio_to_client'} | ({'first_audio_played'} if label.endswith('played_ack') else set())
            if not held:
                used_fields.add('first_audio_from_gateway')
            path['cross_clock_clamped'] = any(c['field'] in used_fields for c in alignment['clamps'])
    out = {'stamps_s': values, 'raw_stamps_s': raw_values, 'stamp_fields': fields,
            'clock_alignment': alignment, 'clock_offset_s': alignment['clock_offset_s'],
            'work': work, 'commit_work': commit_work,
            'overlaps': overlaps, 'readiness_gaps_s': gates, 'paths': paths,
            'missing_reply_stamps': [name for name in REPLY_EDGES if name not in values], 'issues': problems}
    if alignment['status'] == 'cross_clock_unverified':
        exclude_clock_paths(out)
    return out


def session(events, source, meta, legacy_rows):
    trace_events = [e for e in events if e.get('type') in ('reply_trace', 'commit_trace')]
    timing_events = [e for e in events if e.get('type') == 'turn_timing']
    rejected, replies, commits, issues = [], [], [], []
    for event in trace_events:
        reason = None
        if type(event.get('schema_version')) is not int or event['schema_version'] != 1:
            reason = 'unsupported_schema_version'
        elif event.get('clock') != 'encounter_s' or event.get('units') != 'seconds':
            reason = 'unsupported_clock_or_units'
        identity = 'response_id' if event['type'] == 'reply_trace' else 'commit_item_id'
        if reason is None and (not isinstance(event.get(identity), str) or not event[identity]
                               or not isinstance(event.get('stages'), dict)):
            reason = 'invalid_trace_shape'
        if reason is None and event['type'] == 'commit_trace' and (
                not isinstance(event.get('participant', {}), dict)
                or (event.get('decision') is not None and not isinstance(event['decision'], dict))):
            reason = 'invalid_trace_shape'
        if reason is None and event['type'] == 'reply_trace' and (
                (event.get('page_turn') is not None and type(event['page_turn']) is not int)
                or (event.get('origin') is not None and not isinstance(event['origin'], str))
                or (event.get('status') is not None and not isinstance(event['status'], str))
                or (event.get('sentence_trigger') is not None and not isinstance(event['sentence_trigger'], str))
                or (event.get('participant_turn_id') is not None and type(event['participant_turn_id']) is not int)
                or (event.get('release_commit_item_id') is not None and not isinstance(event['release_commit_item_id'], str))):
            reason = 'invalid_trace_shape'
        if reason:
            rejected.append({'source': source, 'type': event['type'], 'schema_version': event.get('schema_version'),
                             identity: event.get(identity), 'reason': reason})
        else:
            (replies if event['type'] == 'reply_trace' else commits).append(event)
    timing_by_page, reply_by_page, commit_by_id = defaultdict(list), defaultdict(list), defaultdict(list)
    for timing in timing_events:
        if type(timing.get('turn')) is int:
            timing_by_page[timing['turn']].append(timing)
    for reply in replies:
        if type(reply.get('page_turn')) is int:
            reply_by_page[reply['page_turn']].append(reply)
    for commit in commits:
        commit_by_id[commit['commit_item_id']].append(commit)
    rid_counts = Counter(r['response_id'] for r in replies)
    legacy_by_page = {r['turn']: r for r in legacy_rows}
    rows, matched_pages, referenced = [], set(), set()
    for reply in replies:
        page, rid = reply.get('page_turn'), reply['response_id']
        matches = timing_by_page.get(page, []) if type(page) is int else []
        ambiguous = rid_counts[rid] != 1 or len(matches) > 1 or len(reply_by_page.get(page, [])) > 1
        timing = matches[0] if len(matches) == 1 and not ambiguous else None
        if timing is not None:
            matched_pages.add(page)
        own_issues = ['ambiguous_reply_or_page_identity'] if ambiguous else []
        linked_ids = reply.get('commit_item_ids') or []
        if not isinstance(linked_ids, list) or not all(isinstance(i, str) for i in linked_ids):
            linked_ids = []
            own_issues.append('invalid_commit_item_ids')
        all_ids = set(linked_ids)
        if isinstance(reply.get('release_commit_item_id'), str):
            all_ids.add(reply['release_commit_item_id'])
        referenced.update(all_ids)
        linked = []
        for iid in sorted(all_ids):
            found = commit_by_id.get(iid, [])
            if len(found) == 1:
                linked.append(found[0])
            else:
                own_issues.append(('missing' if not found else 'ambiguous') + '_commit:' + iid)
        old = legacy_by_page.get(page) if timing is not None else None
        row = {**meta, 'source': source, 'response_id': rid, 'page_turn': page,
               'participant_turn_id': reply.get('participant_turn_id'),
               'commit_item_ids': linked_ids, 'release_commit_item_id': reply.get('release_commit_item_id'),
               'origin': reply.get('origin') or 'unknown', 'status': reply.get('status'),
               'sentence_trigger': reply.get('sentence_trigger'),
               'match': 'matched' if timing is not None else 'trace_without_turn_timing',
               'encounter_reply': old['encounter_reply'] if old else 'unknown',
               'participant_reply': old['participant_reply'] if old else 'unknown',
               'commits': linked, **split_reply({**reply, 'commit_item_ids': linked_ids}, linked, timing)}
        row['issues'].extend(own_issues)
        rows.append(row)
    # All paired marks in the source participate in a session-wide guard.
    offsets = [a['offset_s'] for r in rows for a in r['clock_alignment']['anchors']]
    preferred = [r['clock_offset_s'] for r in rows if r['clock_offset_s'] is not None]
    session_offset = median(preferred) if preferred else None
    spread = max(offsets) - min(offsets) if offsets else None
    session_verified = bool(offsets) and max(map(abs, offsets)) <= CLOCK_OFFSET_LIMIT_S and spread <= CLOCK_OFFSET_LIMIT_S
    for row in rows:
        alignment = row['clock_alignment']
        alignment.update(session_offset_s=session_offset, session_anchor_spread_s=spread)
        if alignment['status'] == 'verified' and not session_verified:
            alignment['status'] = 'cross_clock_unverified'
            alignment['issues'].append('session_anchor_disagreement_or_offset')
            exclude_clock_paths(row)
    unmatched = [{'source': source, 'turn': e.get('turn'), 'reply_index': e.get('reply_index'),
                  'vad_speech_end': e.get('vad_speech_end')} for e in timing_events
                 if type(e.get('turn')) is not int or e['turn'] not in matched_pages]
    return {'present': bool(trace_events), 'rows': rows, 'unsupported': rejected,
            'timings_without_trace': unmatched,
            'commits': [{'source': source, **c} for c in commits],
            'unreferenced_commits': [{'source': source, 'commit_item_id': c['commit_item_id']}
                                     for c in commits if c['commit_item_id'] not in referenced], 'issues': issues,
            'clock_session': {'source': source, 'clock_offset_s': session_offset, 'anchor_spread_s': spread,
                              'verified': session_verified, 'anchor_n': len(offsets), 'offset_limit_s': CLOCK_OFFSET_LIMIT_S}}


def summary_stats(values):
    return {'n': len(values), 'p50_s': percentile(values, 50), 'p90_s': percentile(values, 90)}


def combine(sessions):
    if not any(s['present'] for s in sessions):
        return None
    out = {name: [row for s in sessions for row in s[name]] for name in
           ('rows', 'unsupported', 'timings_without_trace', 'commits', 'unreferenced_commits', 'issues')}
    out['counts'] = {'reply_traces': len(out['rows']), 'matched': sum(r['match'] == 'matched' for r in out['rows']),
        'traces_without_turn_timing': sum(r['match'] != 'matched' for r in out['rows']),
        'turn_timings_without_trace': len(out['timings_without_trace']), 'unsupported': len(out['unsupported']),
        'commit_traces': len(out['commits']), 'unreferenced_commits': len(out['unreferenced_commits'])}
    out['clock_sessions'] = [s['clock_session'] for s in sessions]
    groups = defaultdict(list)
    for row in out['rows']:
        groups[tuple(row[k] for k in GROUP_FIELDS)].append(row)
    out['groups'] = []
    for key, members in sorted(groups.items()):
        samples = defaultdict(list)
        path_counts, path_stages, tied_joins = {}, [], set()
        for target in members[0]['paths']:
            paths = [r['paths'][target] for r in members]
            complete = [p for p in paths if p['total_s'] is not None and not p['issues']]
            path_counts[target] = {'complete': len(complete),
                'incomplete': sum(p['total_s'] is not None and bool(p['issues']) for p in paths),
                'unavailable': sum(p['total_s'] is None for p in paths)}
            if not complete:
                continue
            for stage, field in (('total', 'total_s'), ('unexplained', 'unexplained_s')):
                path_stages.append({'section': target, 'stage': stage, 'path_n': len(complete),
                                    **summary_stats([p[field] for p in complete])})
            for stage in sorted({name for p in paths for name in p['exposed_s']}):
                critical = [p['exposed_s'][stage] for p in complete if stage in p['exposed_s']]
                path_stages.append({'section': target, 'stage': stage, 'path_n': len(complete),
                    **summary_stats(critical), 'critical_share': len(critical) / len(complete),
                    'mean_contribution_s': sum(p['exposed_s'].get(stage, 0) for p in complete) / len(complete)})
            for row in members:
                path = row['paths'][target]
                if path['total_s'] is not None and not path['issues']:
                    tied_joins.update((row['source'], row['response_id'], step['end_field']) for step in path['steps']
                                      if len(step['critical_parents']) > 1)
        for row in members:
            for stage, span in row['work'].items():
                samples[('work', stage)].append(span['duration_s'])
            for span in row['commit_work']:
                samples[('commit_work', span['stage'])].append(span['duration_s'])
            for span in row['overlaps']:
                branches = ['stt' if b.startswith('stt:') else b for b in span['branches']]
                samples[('overlap', '_with_'.join(sorted(branches)))].append(span['duration_s'])
            for stage, duration in row['readiness_gaps_s'].items():
                samples[('readiness_gap', stage)].append(duration)
        out['groups'].append({**dict(zip(GROUP_FIELDS, key)), 'reply_traces': len(members),
            'path_counts': path_counts, 'tied_joins': len(tied_joins),
            'status_counts': dict(Counter(r['status'] if r['status'] is not None else 'unknown' for r in members)),
            'stages': sorted(path_stages + [{'section': section, 'stage': stage, **summary_stats(values)}
                       for (section, stage), values in sorted(samples.items())], key=lambda s: (s['section'], s['stage']))})
    out.update(schema_version=1, clock='encounter_s', units='seconds',
        causal_edges={k: list(v) for k, v in REPLY_EDGES.items()}, notes=NOTES)
    return out


def markdown(trace):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    def fmt(value):
        return 'unavailable' if value is None else f'{value:.3f}'
    lines = ['', 'Reply-path trace', '', 'Counts: ' + '; '.join(f'{k}={v}' for k, v in trace['counts'].items()) + '.', '',
             '| Scenario / mode / encounter reply / participant reply / family / model / origin | Reply traces | Tied joins |',
             '|---|---:|---:|']
    for group in trace['groups']:
        label = ' / '.join(str(group[k]) for k in GROUP_FIELDS)
        lines.append(f'| {cell(label)} | {group["reply_traces"]} | {group["tied_joins"]} |')
    lines += ['', '| Group (including origin) | Section | Stage | n | Complete path n | p50 (s) | p90 (s) | Critical share | Mean per reply (s) |',
              '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for group in trace['groups']:
        label = ' / '.join(str(group[k]) for k in GROUP_FIELDS)
        for stage in group['stages']:
            section = stage['section'] + (' (sample-position estimate)' if stage['section'].startswith('felt_') else '')
            share = stage.get('critical_share')
            lines.append('| ' + ' | '.join(map(cell, [label, section, stage['stage'], stage['n'], stage.get('path_n', '—'),
                fmt(stage['p50_s']), fmt(stage['p90_s']), f'{share:.1%}' if share is not None else '—',
                fmt(stage.get('mean_contribution_s'))])) + ' |')
    lines += ['', '| Source | Response ID / page turn | Status | Join | Trigger | Clock status | Offset (s) | Clamps |',
              '|---|---|---|---|---|---|---:|---:|']
    for row in trace['rows']:
        lines.append('| ' + ' | '.join(map(cell, [row['source'], f"{row['response_id']} / {row['page_turn']}",
            row['status'], row['match'], row['sentence_trigger'], row['clock_alignment']['status'],
            f"{row['clock_offset_s']:.6f}" if row['clock_offset_s'] is not None else 'unavailable',
            len(row['clock_alignment']['clamps'])])) + ' |')
    for row in trace['rows']:
        for issue in row['issues']:
            lines.append(f"\n{cell(row['response_id'])}: {cell(issue)}")
    for event in trace['unsupported']:
        lines.append('\nUnsupported trace: ' + cell(event))
    for timing in trace['timings_without_trace']:
        lines.append('\nTurn timing without trace: ' + cell(timing))
    lines += [''] + ['- ' + note for note in trace['notes']]
    return '\n'.join(lines) + '\n'
