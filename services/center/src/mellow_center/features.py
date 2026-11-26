"""Preview feature policies, independent of transport and model provider."""
from datetime import datetime, timezone
import unicodedata
from importlib.metadata import PackageNotFoundError, version
from .domain import Problem, fields, memory_source_valid, require, string
from .vault import now, record_index
from .spaces import normalize_space

SCENARIOS = {'general', 'work', 'family', 'social'}
DEFAULT_TIMEZONE = 'Asia/Shanghai'


def _package_version():
    try:
        return version('mellow-center')
    except PackageNotFoundError:
        return '0+unknown'


VERSION = _package_version()


def timestamp(value) -> str:
    try:
        parsed = datetime.fromisoformat(string(value, 64).replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, 'INVALID_TIME')
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        raise Problem('INVALID_TIME') from None


def in_window(data, instant=None):
    current = timestamp(instant or now())
    return (not data.get('valid_from') or timestamp(data['valid_from']) <= current) and (
        not data.get('valid_until') or current < timestamp(data['valid_until']))


def memory_status(state, record, instant=None):
    data = record['data']
    status = data['status']
    if status in {'active', 'candidate'} and data.get('valid_until') and timestamp(data['valid_until']) <= timestamp(instant or now()):
        return 'expired'
    if status != 'active':
        return status
    if not in_window(data, instant):
        return 'expired' if data.get('valid_until') and data['valid_until'] <= (instant or now()) else 'scheduled'
    if not data['sources'] or not all(memory_source_valid(state, ref, record['space'], data.get('inferred', False)) for ref in data['sources']):
        return 'invalidated'
    from .domain import forgotten_history
    forgotten = forgotten_history(state, [record['space']])
    if any(ref['id'] in forgotten for ref in data['sources']):
        return 'invalidated'
    from .profile import stale, instant as parse_instant, observed_at
    legacy_inference = not data.get('slot') and str(data.get('key', '')).startswith(('interest:', 'learned-place:'))
    if (data.get('inferred') or legacy_inference) and not data.get('confirmed_at'):
        return 'candidate'
    if stale(record, instant):
        return 'needs_review'
    key = data.get('key')
    if key:
        for identifier in record_index(state)['keys'].get((normalize_space(record['space']), key), ()):
            other = state['records'][identifier]
            content = other['data']
            if (other['id'] != record['id'] and other['kind'] == 'memory' and normalize_space(other['space']) == normalize_space(record['space'])
                and content.get('key') == key and content.get('scenario', 'general') == data.get('scenario', 'general')
                and content.get('status') == 'active' and in_window(content, instant)
                and not (content.get('inferred') and not content.get('confirmed_at'))
                and not stale(other, instant)
                and not any(ref['id'] in forgotten for ref in content['sources'])
                and content['text'] != data['text']
                and all(memory_source_valid(state, ref, other['space'], content.get('inferred', False)) for ref in content['sources'])):
                if (parse_instant(observed_at(other)), other['id']) > (parse_instant(observed_at(record)), record['id']):
                    return 'superseded'
    return 'active'


def usable_memory(state, record, scenario='general'):
    return (record['kind'] == 'memory' and memory_status(state, record) == 'active'
            and (record['data'].get('category') != 'goal' or record['data'].get('goal_state', 'open') == 'open')
            and record['data'].get('scenario', 'general') in {'general', scenario})


def validate_extended(state, record):
    data = record['data']
    require(record['mode'] == 'normal')
    if record['kind'] == 'list':
        fields(data, ['text', 'purpose', 'items'], ['sources'])
        string(data['text'], 200)
        require(data['purpose'] in {'todo', 'shopping', 'packing', 'meal'})
        require(isinstance(data['items'], list) and len(data['items']) <= 100)
        ids = set()
        for item in data['items']:
            fields(item, ['id', 'text', 'done'])
            string(item['id'], 128); string(item['text'], 1000)
            require(item['id'] not in ids and type(item['done']) is bool)
            ids.add(item['id'])
    elif record['kind'] == 'draft':
        fields(data, ['text', 'scenario', 'status', 'sources'], ['used_memories'])
        string(data['text'], 16000)
        require(data['scenario'] in SCENARIOS and data['status'] in {'draft', 'adopted', 'discarded'})
    require(isinstance(data.get('sources', []), list) and isinstance(data.get('used_memories', []), list))
    references = data.get('sources', []) + data.get('used_memories', [])
    require(len(references) <= 1024)
    previous = state['records'].get(record['id'])
    unchanged_evidence = (previous is not None and previous['kind'] == record['kind'] and
                          previous['data'].get('sources', []) == data.get('sources', []) and
                          previous['data'].get('used_memories', []) == data.get('used_memories', []))
    for ref in references:
        fields(ref, ['id', 'revision'])
        string(ref['id'], 128)
        require(type(ref['revision']) is int and ref['revision'] > 0)
        source = state['records'].get(ref['id'])
        require(source and source['space'] == record['space'] and source['mode'] == 'normal'
                and (source['revision'] == ref['revision'] or unchanged_evidence), 'SOURCE_INVALIDATED', 409)


def capability_catalog():
    return {'protocol': 'preview-1', 'capabilities': [
        {'id': 'recall.quote', 'model': True, 'effect': 'cited_originals_or_abstention'},
        {'id': 'life.records', 'model': False, 'effect': 'local_records'},
        {'id': 'kitchen.cook', 'model': False, 'effect': 'confirmed_atomic_stock_change'},
        {'id': 'calendar.import', 'model': False, 'effect': 'preview_then_local_records'},
        {'id': 'weather.lookup', 'model': False, 'effect': 'explicit_coordinates_to_open_meteo'},
        {'id': 'family.share', 'model': False, 'effect': 'scoped_lists_and_events'},
        {'id': 'memory.search', 'model': False, 'effect': 'read'},
        {'id': 'memory.evidence', 'model': False, 'effect': 'read'},
        {'id': 'writing.prepare', 'model': True, 'effect': 'local_draft'},
        {'id': 'checklist.prepare', 'model': True, 'effect': 'local_list'},
        {'id': 'reminder.propose', 'model': False, 'effect': 'requires_user_confirmation'},
    ]}


def normalize(text):
    return unicodedata.normalize('NFKC', text).casefold()
