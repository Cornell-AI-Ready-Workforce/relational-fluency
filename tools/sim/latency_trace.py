"""Identity joins and causal latency paths for reply_trace schema 1."""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from tools.sim.analyze import percentile

GROUP_FIELDS = ('scenario', 'mode', 'encounter_reply', 'participant_reply', 'family', 'model')
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
    'Stage n counts observed spans. commit_work n counts referenced commit spans per reply and can exceed the reply count; missing stamps do not become zero-duration samples.',
    'Trace page_turn joins turn_timing.turn within a source session. Commit item IDs join only their own commit_trace; release_commit_item_id can name a different commit.',
    'Unmatched traces, timings, missing commits, duplicates and unsupported schemas remain explicit. Ambiguous identities are not joined by order or by nearest timestamp.',
    'Coverage includes untraced sessions in the same input set. Legacy sessions, rooms and other model families can legitimately lack this 1:1 cascade trace.',
    'Groups inherit the matched legacy row. Unmatched replies retain unknown first/handoff/later metadata where it cannot be established by identity.',
    'Legacy playback endpoints use wall elapsed; trace uses monotonic elapsed after attachment. Cross-clock client/ACK tails are conditional on aligned encounter clocks; reversed edges are rejected.',
    'last_voiced_estimate is a sample-position estimate, not a microphone wall-clock boundary. Playback ACK does not establish hearing.',
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


def critical_path(values, edges, target):
    """Walk backwards through max-prerequisite joins, never sorted timestamps."""
    root, end = values.get('vad_detected'), values.get(target)
    out = {'target': target, 'total_s': None, 'exposed_s': {}, 'steps': [],
           'unexplained_s': None, 'issues': []}
    if not (number(root) and number(end) and end >= root):
        out['issues'].append('missing_or_invalid_path_endpoints')
        return out
    out['total_s'] = round(end - root, 6)
    node = target
    visited = set()
    while node != 'vad_detected':
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
        out['steps'].append(span)
        out['exposed_s'][span['stage']] = span['duration_s']
        node = parent
    out['steps'].reverse()
    out['unexplained_s'] = round(max(0, out['total_s'] - sum(out['exposed_s'].values())), 6)
    return out


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
        for name in ('first_audio_to_client', 'first_audio_played'):
            value = timing.get(name)
            if number(value):
                values[name] = value
                fields[name] = 'turn_timing.' + name
    held = 'first_output_hold_started' in values
    edges['first_audio_to_client'] = ('first_output_hold_released',) if held else ('first_output_emitted',)
    edges['first_audio_played'] = ('first_audio_to_client',)
    for node, parents in edges.items():
        if number(values.get(node)):
            problems.extend('invalid_causal_edge:' + parent + '->' + node for parent in parents
                            if number(values.get(parent)) and values[parent] > values[node])
    paths = {label: critical_path(values, edges, target) for label, target in (
        ('provider_audio', 'tts_first_audio'), ('adapter_output', 'first_output_emitted'),
        ('client_audio', 'first_audio_to_client'), ('played_ack', 'first_audio_played'))}
    return {'stamps_s': values, 'stamp_fields': fields, 'work': work, 'commit_work': commit_work,
            'overlaps': overlaps, 'readiness_gaps_s': gates, 'paths': paths,
            'missing_reply_stamps': [name for name in REPLY_EDGES if name not in values], 'issues': problems}


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
               'origin': reply.get('origin'), 'status': reply.get('status'),
               'sentence_trigger': reply.get('sentence_trigger'),
               'match': 'matched' if timing is not None else 'trace_without_turn_timing',
               'encounter_reply': old['encounter_reply'] if old else 'unknown',
               'participant_reply': old['participant_reply'] if old else 'unknown',
               'commits': linked, **split_reply({**reply, 'commit_item_ids': linked_ids}, linked, timing)}
        row['issues'].extend(own_issues)
        rows.append(row)
    unmatched = [{'source': source, 'turn': e.get('turn'), 'reply_index': e.get('reply_index'),
                  'vad_speech_end': e.get('vad_speech_end')} for e in timing_events
                 if type(e.get('turn')) is not int or e['turn'] not in matched_pages]
    return {'present': bool(trace_events), 'rows': rows, 'unsupported': rejected,
            'timings_without_trace': unmatched,
            'commits': [{'source': source, **c} for c in commits],
            'unreferenced_commits': [{'source': source, 'commit_item_id': c['commit_item_id']}
                                     for c in commits if c['commit_item_id'] not in referenced], 'issues': issues}


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
    groups = defaultdict(list)
    for row in out['rows']:
        groups[tuple(row[k] for k in GROUP_FIELDS)].append(row)
    out['groups'] = []
    for key, members in sorted(groups.items()):
        samples = defaultdict(list)
        for row in members:
            for target, path in row['paths'].items():
                if path['total_s'] is not None:
                    samples[(target, 'total')].append(path['total_s'])
                    samples[(target, 'unexplained')].append(path['unexplained_s'])
                for stage, duration in path['exposed_s'].items():
                    samples[(target, stage)].append(duration)
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
            'status_counts': dict(Counter(r['status'] for r in members)),
            'stages': [{'section': section, 'stage': stage, **summary_stats(values)}
                       for (section, stage), values in sorted(samples.items())]})
    out.update(schema_version=1, clock='encounter_s', units='seconds',
        causal_edges={k: list(v) for k, v in REPLY_EDGES.items()}, notes=NOTES)
    return out


def markdown(trace):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['', 'Reply-path trace', '', 'Counts: ' + '; '.join(f'{k}={v}' for k, v in trace['counts'].items()) + '.', '',
             '| Scenario / mode / encounter reply / participant reply / family / model | Section | Stage | n | p50 (s) | p90 (s) |',
             '|---|---|---|---:|---:|---:|']
    for group in trace['groups']:
        label = ' / '.join(str(group[k]) for k in GROUP_FIELDS)
        for stage in group['stages']:
            lines.append('| ' + ' | '.join(map(cell, [label, stage['section'], stage['stage'], stage['n'],
                f"{stage['p50_s']:.3f}", f"{stage['p90_s']:.3f}"])) + ' |')
    lines += ['', '| Source | Response ID / page turn | Status | Join | Trigger |', '|---|---|---|---|---|']
    for row in trace['rows']:
        lines.append('| ' + ' | '.join(map(cell, [row['source'], f"{row['response_id']} / {row['page_turn']}",
            row['status'], row['match'], row['sentence_trigger']])) + ' |')
    for row in trace['rows']:
        for issue in row['issues']:
            lines.append(f"\n{cell(row['response_id'])}: {cell(issue)}")
    for event in trace['unsupported']:
        lines.append('\nUnsupported trace: ' + cell(event))
    for timing in trace['timings_without_trace']:
        lines.append('\nTurn timing without trace: ' + cell(timing))
    lines += [''] + ['- ' + note for note in trace['notes']]
    return '\n'.join(lines) + '\n'
