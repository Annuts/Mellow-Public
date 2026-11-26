"""Weekly profile-memory consolidation, run inside maintenance.prune's transaction.

Merges duplicate multi-slot memories (history is kept as superseded records),
decays stale stable facts, expires timed candidates, promotes well-evidenced
candidates and regenerates a short profile summary per space. The summary is a
context record with ai=False; its source references live in private_items so the
context schema is unchanged and stale facts can never reach a model after a
deletion (the summary is rebuilt from currently eligible memories each run).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from .domain import forgotten_history, memory_source_valid
from .vault import uid, clear_indexes

INTERVAL = timedelta(days=7)
STABLE_DECAY = timedelta(days=180)
SUMMARY_LIMIT = 600
SUMMARY_TITLE = '长期画像摘要'
SUMMARY_TAG = 'profile-summary'
HISTORY_LIMIT = 200
LIVE = {'active': 0, 'needs_review': 1, 'candidate': 2}


def _dt(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _profile():
    from . import profile
    return profile


def _observed(record):
    return _dt(_profile().observed_at(record)) or _dt(record.get('updated_at')) or datetime.min.replace(tzinfo=timezone.utc)


def _hashable(value):
    try:
        hash(value)
        return value
    except TypeError:
        return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def _profile_memories(state):
    """(record, slot_kind) for memories that carry a known profile slot and value."""
    module = _profile()
    for record in sorted(state['records'].values(), key=lambda r: r['id']):
        data = record['data']
        if record['kind'] != 'memory' or not data.get('slot') or data.get('value') in (None, ''):
            continue
        spec = module.slot_spec(data['slot'])
        if spec:
            yield record, spec.get('slot_kind')


def _group_key(record):
    data = record['data']
    return (record['space'], data['slot'], data.get('scenario', 'general'),
            _hashable(_profile().normalize_value(data['value'])), data.get('temporal', 'stable'),
            bool(data.get('inferred')), bool(data.get('hedged')))


def _touch(record, instant, **changes):
    record['data'].update(changes)
    record['revision'] += 1
    record['updated_at'] = instant.isoformat()


def _state(state):
    return state['private_items'].setdefault('profile_consolidation', {})


def _history(state, event):
    entries = _state(state).setdefault('history', [])
    entries.append(event)
    del entries[:-HISTORY_LIMIT]


def merge_duplicates(state, instant):
    """Collapse same-slot/value multi memories; duplicates stay as superseded history."""
    groups = {}
    for record, kind in _profile_memories(state):
        if kind == 'multi' and record['data'].get('status') in LIVE:
            groups.setdefault(_group_key(record), []).append(record)
    forgotten = forgotten_history(state, {key[0] for key in groups})
    merged = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: (LIVE[r['data']['status']], -_observed(r).timestamp(), r['id']))
        canonical, duplicates = members[0], members[1:]
        data = canonical['data']
        sources = [ref for ref in data['sources'] if ref['id'] not in forgotten
                   and memory_source_valid(state, ref, canonical['space'], data.get('inferred', False))]
        known = {ref['id'] for ref in sources}
        for other in duplicates:
            for ref in other['data']['sources']:
                if (ref['id'] not in known and ref['id'] not in forgotten and len(sources) < 20
                        and memory_source_valid(state, ref, canonical['space'], other['data'].get('inferred', False))):
                    sources.append(ref); known.add(ref['id'])
        if not sources:
            continue
        everyone = [m['data'] for m in members]
        changes = dict(
            sources=sources,
            inferred=all(d.get('inferred', False) for d in everyone),
            hedged=all(d.get('hedged', False) for d in everyone),
            protected=any(d.get('protected', False) for d in everyone),
            needs_confirmation=any(d.get('needs_confirmation', False) for d in everyone))
        edited = max((r for r in members if r['data'].get('protected')), key=_observed, default=None)
        if edited:
            changes['text'] = edited['data']['text']
            if edited['data'].get('quote'):
                changes['quote'] = edited['data']['quote']
        observed = [d['observed_at'] for d in everyone if d.get('observed_at')]
        if observed:
            changes['observed_at'] = max(observed, key=lambda v: _dt(v) or datetime.min.replace(tzinfo=timezone.utc))
        confirmed = [d['confirmed_at'] for d in everyone if d.get('confirmed_at')]
        if confirmed:
            changes['confirmed_at'] = max(confirmed, key=lambda v: _dt(v) or datetime.min.replace(tzinfo=timezone.utc))
        confidences = [d['confidence'] for d in everyone if isinstance(d.get('confidence'), (int, float))]
        if confidences:
            changes['confidence'] = max(confidences)
        if all(d.get('valid_until') for d in everyone):
            changes['valid_until'] = max((d['valid_until'] for d in everyone), key=lambda v: _dt(v) or datetime.min.replace(tzinfo=timezone.utc))
        # Never turn a timed state into an unbounded fact while deduplicating.
        _touch(canonical, instant, **changes)
        for other in duplicates:
            _touch(other, instant, status='superseded')
        _history(state, {'at': instant.isoformat(), 'space': canonical['space'], 'slot': data['slot'],
                         'kept': canonical['id'], 'superseded': sorted(o['id'] for o in duplicates)})
        merged += len(duplicates)
    return merged


def decay_stable(state, instant):
    cutoff, changed = instant - STABLE_DECAY, 0
    for record in state['records'].values():
        data = record['data']
        if record['kind'] != 'memory' or data.get('temporal', 'stable') != 'stable' or data.get('status') != 'active':
            continue
        latest = max(_observed(record), _dt(data.get('confirmed_at')) or datetime.min.replace(tzinfo=timezone.utc))
        if latest < cutoff:
            _touch(record, instant, status='needs_review')
            changed += 1
    return changed


def expire_timed_candidates(state, instant):
    changed = 0
    for record in state['records'].values():
        data = record['data']
        if record['kind'] != 'memory' or data.get('status') != 'candidate':
            continue
        until = _dt(data.get('valid_until'))
        if (until and until <= instant) or (data.get('slot') and _profile().stale(record, instant.isoformat())):
            _touch(record, instant, status='expired')
            changed += 1
    return changed


def _days(state, record):
    return _profile().source_days(state, record['data']['sources'])


def promote_candidates(state, instant):
    slots, blocked = {}, set()
    for record, kind in _profile_memories(state):
        data = record['data']
        key = _group_key(record)
        if data.get('status') in {'rejected', 'suppressed'}:
            blocked.add((record['space'], data['slot'], _profile().normalize_value(data['value'])))
        elif data.get('status') in LIVE:
            slots.setdefault((record['space'], data['slot'], data.get('scenario', 'general')), []).append((record, kind, key))
    forgotten = forgotten_history(state, {key[0] for key in slots})
    promoted = 0
    for (space, _, _), entries in slots.items():
        single = entries[0][1] != 'multi'
        values = {key for _, _, key in entries}
        candidates = [e for e in entries if e[0]['data']['status'] == 'candidate']
        if single and (len(values) > 1 or len(candidates) < len(entries)):
            continue  # competing or already-held values need a person, not a majority vote
        by_value = {}
        for record, _, key in candidates:
            data = record['data']
            value_key = (space, data['slot'], _profile().normalize_value(data['value']))
            deleted = any((t.get('space'), t.get('memory_slot'), t.get('memory_value')) == value_key
                          for t in state.get('deleted', {}).values())
            if (value_key in blocked or deleted or data.get('temporal') not in {'stable', 'state'}
                    or data.get('inferred') or data.get('hedged') or data.get('protected')
                    or data.get('needs_confirmation')
                    or _profile().stale(record, instant.isoformat())
                    or not all(memory_source_valid(state, ref, space) for ref in data['sources'])
                    or any(ref['id'] in forgotten for ref in data['sources'])):
                continue
            by_value.setdefault(key, []).append(record)
        for members in by_value.values():
            days = set().union(*(_days(state, m) for m in members))
            if len(days) >= 2:
                for record in members:
                    _touch(record, instant, status='active', confidence=max(.7, record['data'].get('confidence', .4)))
                    promoted += 1
    return promoted


def _render_summary(state, space, instant):
    """Compact, deterministic summary of currently eligible facts; returns (text, records)."""
    module = _profile()
    chosen = {}
    for record in module.eligible_memories(state, space):
        data = record['data']
        slot = module.memory_slot(record)
        spec = module.slot_spec(slot)
        if (not spec or data.get('value', data['text']) in (None, '') or data.get('temporal') == 'episodic'
                or module.stale(record, instant.isoformat())):
            continue
        chosen.setdefault(slot, []).append((record, spec))
    lines, included, size = [], [], 0
    for slot in sorted(chosen):
        entries = sorted(chosen[slot], key=lambda e: (-_observed(e[0]).timestamp(), e[0]['id']))
        if entries[0][1].get('slot_kind') != 'multi':
            entries = entries[:1]
        label = entries[0][1].get('label', slot)
        values, used = [], []
        for record, _ in entries:
            value = record['data'].get('value', record['data']['text'])
            value = value if isinstance(value, str) else record['data']['text']
            value = value.strip()[:40]
            line = label + '：' + '、'.join(values + [value])
            if size + len(line) + (1 if lines else 0) > SUMMARY_LIMIT:
                break
            values.append(value); used.append(record)
        if used:
            line = label + '：' + '、'.join(values)
            size += len(line) + (1 if lines else 0)
            lines.append(line); included.extend(used)
    return '\n'.join(lines), included


def _references(records):
    memory_refs, source_refs, seen = [], [], set()
    for record in records:
        memory_refs.append({'id': record['id'], 'revision': record['revision']})
        for ref in record['data'].get('sources', []):
            if ref['id'] not in seen:
                seen.add(ref['id']); source_refs.append({'id': ref['id'], 'revision': ref['revision']})
    return memory_refs[:100], source_refs[:200]


def refresh_summaries(state, instant):
    from .sharing import shared_space
    summaries = _state(state).setdefault('summaries', {})
    spaces = {r['space'] for r in state['records'].values() if r['kind'] == 'memory' and not shared_space(r['space'])} | set(summaries)
    changed = 0
    for space in sorted(spaces):
        entry = summaries.get(space, {})
        record = state['records'].get(entry.get('id'))
        if entry.get('id') and not record:
            summaries[space] = dict(id=entry['id'], declined=True)  # owner deleted it; keep no refs, never resurrect
            continue
        if record is None:
            restored = [r for r in state['records'].values() if r['kind'] == 'context' and r['space'] == space
                        and SUMMARY_TAG in r['data'].get('tags', [])]
            if restored:
                record = max(restored, key=lambda r: r['updated_at'])
                from .domain import delete_record
                for duplicate in restored:
                    if duplicate['id'] != record['id']:
                        delete_record(state, duplicate['id'])
        text, included = _render_summary(state, space, instant)
        memory_refs, source_refs = _references(included)
        if not text or not memory_refs:
            if record:
                from .domain import delete_record
                delete_record(state, record['id'])
                changed += 1
            summaries.pop(space, None)
            continue
        data = dict(title=SUMMARY_TITLE, body=text, domain='general', layer='profile', sensitivity='private',
                    sync=True, ai=False, tags=[SUMMARY_TAG])
        touched = True
        if record is None:
            identifier = uid()
            state['records'][identifier] = dict(id=identifier, kind='context', space=space, mode='normal',
                                                 revision=1, updated_at=instant.isoformat(), data=data)
            changed += 1
        else:
            identifier = record['id']
            touched = {k: record['data'].get(k) for k in data} != data
            if touched:
                record['data'] = {**record['data'], **data}
                record['revision'] += 1
                record['updated_at'] = instant.isoformat()
                changed += 1
        summaries[space] = dict(id=identifier, memories=memory_refs, sources=source_refs,
                                generated_at=instant.isoformat() if touched else entry.get('generated_at', instant.isoformat()))
    return changed


def consolidate(state, instant):
    counts = dict(merged=merge_duplicates(state, instant), decayed=decay_stable(state, instant),
                  expired=expire_timed_candidates(state, instant), promoted=promote_candidates(state, instant))
    clear_indexes(state)
    counts['summaries'] = refresh_summaries(state, instant)
    clear_indexes(state)
    _state(state)['last_run'] = dict(at=instant.isoformat(), **counts)
    return counts


def consolidate_due(state, instant):
    """Weekly gate. Returns True when synced records changed (caller bumps revision)."""
    if state.get('next_profile_consolidation_at', '') > instant.isoformat():
        from .features import memory_status
        summaries = _state(state).get('summaries', {})
        dirty = any(not state['records'].get(ref['id']) or
                    state['records'][ref['id']]['revision'] != ref['revision'] or
                    memory_status(state, state['records'][ref['id']], instant.isoformat()) != 'active'
                    for entry in summaries.values() for ref in entry.get('memories', []))
        return bool(refresh_summaries(state, instant)) if dirty else False
    counts = consolidate(state, instant)
    state['next_profile_consolidation_at'] = (instant + INTERVAL).isoformat()
    return any(counts.values())
