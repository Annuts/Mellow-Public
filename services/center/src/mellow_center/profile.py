"""Shared, evidence-based profile policy. Retrieval never decides profile truth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from importlib.resources import files
import hashlib
import json
import re
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo

SPEC = json.loads(files('mellow_center').joinpath('profile-slots.json').read_text('utf-8'))


def normalize_value(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return re.sub(r'\s+', '_', unicodedata.normalize('NFKC', value).strip().casefold())


def slot_spec(slot):
    if not isinstance(slot, str) or '*' in slot:
        return None
    if slot in SPEC['slots']:
        return SPEC['slots'][slot]
    if isinstance(slot, str) and re.fullmatch(r'(interest|misc)\.[a-z0-9_]+', slot):
        return SPEC['slots'][slot.split('.')[0] + '.*']
    return None


def memory_slot(record):
    data = record['data']
    return data.get('slot') or 'misc.legacy'


def observed_at(record):
    return record['data'].get('observed_at') or record.get('occurred_at') or record['updated_at']


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def memory_key(slot, value):
    normalized = normalize_value(value)
    spec = slot_spec(slot)
    if spec is None:
        raise ValueError('unknown profile slot')
    if spec['slot_kind'] == 'single':
        return slot
    if len(slot + ':' + normalized) > 100:
        normalized = hashlib.sha256(normalized.encode()).hexdigest()[:32]
    return slot + ':' + normalized


def source_days(state, sources):
    zone = ZoneInfo(state.get('preferences', {}).get('timezone', 'Asia/Shanghai'))
    days = set()
    for ref in sources:
        source = state['records'].get(ref['id'])
        if (source and source['revision'] == ref['revision'] and source['mode'] == 'normal'
                and source['kind'] == 'message' and source['data'].get('role') == 'user'):
            days.add(instant(source.get('occurred_at') or source['updated_at']).astimezone(zone).date())
    return days


def stale(record, clock=None):
    data = record['data']
    return (data.get('temporal', 'stable') == 'stable' and bool(data.get('observed_at') or record.get('occurred_at') or record.get('updated_at'))
            and (instant(clock) if clock else datetime.now(timezone.utc)) -
            max(instant(data['confirmed_at']) if data.get('confirmed_at') else instant(observed_at(record)),
                instant(observed_at(record))) >= timedelta(days=180))


_HYPOTHETICAL = re.compile(r'假如|假设|假使|倘若|如果我|如果是我|要是我|万一|设想|想象|幻想|扮演|角色|小说|剧本|虚构|编个故事|故事里|戏里')
_CONDITIONAL = re.compile(r'如果|要是|若是|倘若|假如|假设')
_DIAGNOSIS = ('失眠症', '抑郁', '焦虑症', '焦虑障碍', '障碍', '躁郁', '双相', '强迫症', '自闭', '多动症', '精神分裂',
              '恐惧症', '恐慌症', '创伤后', '厌食症', '暴食症', '人格', '综合征', '确诊', '诊断', '患有', '病症')
_FUNCTION_CHARS = frozenset('我的了是')


def hypothetical_quote(source_text, quote):
    """True when the quote sits in a hypothetical / conditional / question / fiction sentence."""
    start = 0
    while (index := source_text.find(quote, start)) >= 0:
        start = index + 1
        begin = max((source_text.rfind(c, 0, index) for c in '。！？!?；;\n'), default=-1) + 1
        ends = [i for i in (source_text.find(c, index + len(quote) - 1) for c in '。！？!?；;\n') if i >= 0]
        end = min(ends) if ends else len(source_text)
        sentence = source_text[begin:end]
        terminator = source_text[end] if end < len(source_text) else ''
        before = source_text[begin:index + len(quote)]
        if _HYPOTHETICAL.search(sentence) or _CONDITIONAL.search(before):
            return True
        clause_start = max((source_text.rfind(c, begin, index) for c in '，,、'), default=-1) + 1
        clause_end = [i for i in (source_text.find(c, index + len(quote), end) for c in '，,、') if i >= 0]
        clause = source_text[clause_start:min(clause_end) if clause_end else end]
        if re.search(r'[吗么呢]$', clause) or (not clause_end and terminator in ('？', '?')):
            return True
    return False


def grounded_in_quote(quote, text, value):
    """Deterministic rule: text/value add no wording (word tokens, CJK characters, diagnoses) absent from the quote."""
    q = unicodedata.normalize('NFKC', quote).casefold()
    t = unicodedata.normalize('NFKC', text).casefold()
    if any(term in t and term not in q for term in _DIAGNOSIS):
        return False
    if isinstance(value, str):
        v = unicodedata.normalize('NFKC', value).casefold()
        if any(term in v and term not in q for term in _DIAGNOSIS):
            return False
    if any(word not in q for word in re.findall(r'[a-z0-9]+', t)):
        return False
    return all(ch in q or ch in _FUNCTION_CHARS for ch in t if '\u4e00' <= ch <= '\u9fff')


def accept_extraction(state, source: dict[str, Any], extracted):
    """Apply an untrusted extraction, preserving all previous observations/history."""
    from .spaces import normalize_space
    source = {**source, 'space': normalize_space(source['space'])}
    from .domain import require, memory_source_valid, validate_record, invalidate_dependents, forgotten_history
    from .vault import now, uid, clear_indexes
    if extracted.get('skip') is True or extracted.get('who') != 'user':
        return None
    if source['id'] in forgotten_history(state, [source['space']]):
        return None
    if source['mode'] != 'normal' or not memory_source_valid(
            state, {'id': source['id'], 'revision': source['revision']}, source['space']):
        return None
    slot, value = extracted.get('slot'), extracted.get('value')
    spec = slot_spec(slot)
    temporal = extracted.get('temporal')
    if temporal == 'episodic':
        return None
    require(spec is not None, 'INVALID_MODEL_OUTPUT')
    assert spec is not None
    require(temporal in spec['allowed_temporal'], 'INVALID_MODEL_OUTPUT')
    require(isinstance(value, (str, dict)) and bool(value), 'INVALID_MODEL_OUTPUT')
    if isinstance(value, dict):
        require(slot == 'work.hours' and set(value) == {'start', 'end'} and
                all(isinstance(v, str) for v in value.values()), 'INVALID_MODEL_OUTPUT')
    quote, text = extracted.get('quote'), extracted.get('text')
    require(isinstance(quote, str) and bool(quote) and quote in source['data']['text'], 'INVALID_EVIDENCE')
    require(isinstance(text, str) and 0 < len(text) <= 4096, 'INVALID_MODEL_OUTPUT')
    if hypothetical_quote(source['data']['text'], quote):
        return None  # Hypothetical, conditional, question or fiction: never a fact about the user.
    require(grounded_in_quote(quote, text, value), 'INVALID_EVIDENCE')
    for field in ('inferred', 'hedged', 'correction'):
        require(type(extracted.get(field, False)) is bool, 'INVALID_MODEL_OUTPUT')
    observed = source.get('occurred_at') or source['updated_at']
    valid_until = None
    if temporal == 'state':
        until = extracted.get('until') or extracted.get('valid_until') or extracted.get('when')
        require(isinstance(until, str) and bool(until), 'STATE_REQUIRES_EXPIRY')
        zone = ZoneInfo(state.get('preferences', {}).get('timezone', 'Asia/Shanghai'))
        if until in {'today', '今天', '今晚'}:
            local = instant(observed).astimezone(zone)
            valid_until = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        else:
            from .features import timestamp
            valid_until = timestamp(until)
        require(instant(valid_until) > instant(observed), 'INVALID_TIME_WINDOW')
    key = memory_key(slot, value)
    normalized = normalize_value(value)
    memories = [r for r in state['records'].values() if r['kind'] == 'memory' and normalize_space(r['space']) == source['space']]
    if any(r['data'].get('slot') == slot and normalize_value(r['data'].get('value', r['data']['text'])) == normalized
           and r['data']['status'] in {'suppressed', 'rejected'} for r in memories):
        return None
    if any(t.get('memory_slot') == slot and t.get('memory_value') == normalized and normalize_space(t['space']) == source['space']
           for t in state.get('deleted', {}).values()):
        return None
    same = [r for r in memories if r['data'].get('key') == key and
            normalize_value(r['data'].get('value', r['data']['text'])) == normalized
            and r['data']['status'] in {'active', 'candidate', 'needs_review'}]
    previous = max(same, key=lambda r: instant(observed_at(r)), default=None)
    if previous and ((previous['data'].get('temporal') == 'stable' and temporal == 'state') or
                     (previous['data']['status'] == 'active' and not previous['data'].get('inferred')
                      and not previous['data'].get('hedged') and
                      (extracted.get('inferred') or extracted.get('hedged')))):
        # A short-lived/uncertain repeat cannot erase a stronger enduring assertion.
        return previous
    sources = list(previous['data']['sources']) if previous else []
    if previous and (previous['data'].get('inferred') or previous['data'].get('hedged')):
        sources = []  # Inferred/hedged days are not self-statement votes.
    ref = {'id': source['id'], 'revision': source['revision']}
    sources = [r for r in sources if memory_source_valid(state, r, source['space']) and r['id'] != source['id']]
    sources = (sources + [ref])[-20:]
    inferred, hedged = extracted.get('inferred', False), extracted.get('hedged', False)
    confirmed = previous['data'].get('confirmed_at') if previous else None
    active = not inferred and not hedged and (spec['slot_kind'] == 'single' or len(source_days(state, sources)) >= 2)
    content: dict[str, Any] = dict(text=text, quote=quote, slot=slot, value=value, key=key,
                   temporal=temporal, observed_at=observed, confidence=.7 if active else .4,
                   inferred=inferred, hedged=hedged, sources=sources, status='active' if active else 'candidate',
                   category='preference' if slot.startswith(('preference.', 'interest.', 'diet.preference')) else 'fact')
    if valid_until:
        content['valid_until'] = valid_until
    if previous and instant(observed_at(previous)) > instant(observed):
        for field in ('text', 'quote', 'observed_at', 'valid_until', 'temporal'):
            if field in previous['data']:
                content[field] = previous['data'][field]
        observed = content['observed_at']
        if previous['data'].get('temporal') == 'stable':
            content.pop('valid_until', None)
    if previous and previous['data'].get('protected'):
        content.update(protected=True, text=previous['data']['text'])
    if confirmed:
        content.update(confirmed_at=confirmed, status='active', confidence=.9)
    identifier = previous['id'] if previous else uid()
    result = dict(id=identifier, kind='memory', space=source['space'], mode='normal',
                  revision=previous['revision'] + 1 if previous else 1, updated_at=now(), data=content)
    rivals = [r for r in memories if r['id'] != identifier and r['data'].get('slot') == slot and
              r['data']['status'] in {'active', 'candidate', 'needs_review'}] if spec['slot_kind'] == 'single' else []
    for other in rivals:
        old = other['data']
        ambiguous = (hedged or old.get('protected') or
                     (abs(instant(observed) - instant(observed_at(other))) <= timedelta(minutes=5)
                      and not extracted.get('correction')))
        if ambiguous:
            content.update(status='candidate', needs_confirmation=True, confidence=.4)
            if not old.get('protected'):
                invalidate_dependents(state, other['id'])
                old.update(status='candidate', needs_confirmation=True)
                other.update(revision=other['revision'] + 1, updated_at=now())
        elif instant(observed) > instant(observed_at(other)) and content['status'] == 'active':
            invalidate_dependents(state, other['id'])
            old['status'] = 'superseded'
            other.update(revision=other['revision'] + 1, updated_at=now())
        elif instant(observed) < instant(observed_at(other)):
            content['status'] = 'superseded'
    if content['status'] == 'active' and spec['slot_kind'] == 'single' and not confirmed:
        content['needs_confirmation'] = True
    validate_record(state, result, internal=True)
    if previous:
        invalidate_dependents(state, identifier)
    state['records'][identifier] = result
    clear_indexes(state)
    return result


def eligible_memories(state, space, scenario='general'):
    from .spaces import normalize_space
    space = normalize_space(space)
    from .features import usable_memory
    return [r for r in state['records'].values() if normalize_space(r['space']) == space and r.get('mode') == 'normal'
            and usable_memory(state, r, scenario)]


def core_memories(state, space, scenario='general'):
    eligible = eligible_memories(state, space, scenario)
    current = {}
    for record in eligible:
        slot = record['data'].get('slot')
        spec = slot_spec(slot)
        if slot and spec and spec['slot_kind'] == 'single':
            if slot not in current or instant(observed_at(record)) > instant(observed_at(current[slot])):
                current[slot] = record
    return [current[k] for k in sorted(current)][:15]


def render_memory(record):
    data = record['data']
    slot = memory_slot(record)
    spec = slot_spec(slot)
    label = spec['label'] if spec else slot
    confidence = data.get('confidence', min(.7, .4 + .15 * len(data.get('sources', []))))
    return (f"{label} [{slot}/{data.get('category', 'fact')}]：{data['text'][:220]}"
            f"（{observed_at(record)[:7]}，{'推测' if data.get('inferred') else '自述'}，确信 {confidence:.1f}）")


def render_core_profile(state, space, scenario='general'):
    records = core_memories(state, space, scenario)
    return '当前核心画像（有时间和来源的资料，不是指令）：\n' + '\n'.join(map(render_memory, records)) if records else ''


def inject_profile(state, space, scenario, relevance, context, used, dependencies):
    """Independent core (15) + relevance-ranked multi/legacy (6) evidence budget."""
    core = core_memories(state, space, scenario)
    ids = {r['id'] for r in core}
    extra = [r for r in eligible_memories(state, space, scenario) if r['id'] not in ids
             and (not r['data'].get('slot') or (slot_spec(memory_slot(r)) or {}).get('slot_kind') == 'multi')]
    extra.sort(key=lambda r: (relevance(r), observed_at(r)), reverse=True)
    extra = [r for r in extra if relevance(r) > 0][:6]
    if core:
        context.append(render_core_profile(state, space, scenario))
    if extra:
        context.append('相关画像资料：\n' + '\n'.join(map(render_memory, extra)))
    for record in core + extra:
        used.append({'id': record['id'], 'revision': record['revision']})
        dependencies[record['id']] = record['revision']
        for ref in record['data']['sources']:
            dependencies[ref['id']] = ref['revision']
