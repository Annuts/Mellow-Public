"""Personal planning and explicitly consented context, independent of model providers."""
from datetime import date

KINDS = {'context', 'vision', 'goal', 'project', 'task', 'review'}
PARENTS = {'goal': 'vision', 'project': 'goal', 'task': 'project', 'context': 'project'}
DOMAINS = {'general', 'clothing', 'food', 'home', 'mobility', 'health', 'finance',
           'work', 'social', 'schedule', 'entertainment', 'learning', 'family', 'shopping', 'travel'}


def validate(state, record):
    from .domain import fields, require, string, Problem
    data, kind = record['data'], record['kind']
    common = ['title', 'body', 'domain']
    if kind == 'context':
        fields(data, common + ['layer', 'sensitivity', 'sync', 'ai', 'tags'], ['parent_id'])
        require(data['layer'] in {'profile', 'active', 'event', 'archive'})
        require(data['sensitivity'] in {'ordinary', 'private', 'secret'})
        require(type(data['sync']) is bool and type(data['ai']) is bool)
        # Local-only content must never enter the center, even through another client.
        require(data['sync'] and data['sensitivity'] != 'secret', 'LOCAL_ONLY_RECORD', 403)
        require(not data['ai'] or record['mode'] == 'normal', 'CONTEXT_CONSENT_REQUIRED')
        require(isinstance(data['tags'], list) and len(data['tags']) <= 12)
        for tag in data['tags']:
            string(tag, 40)
    else:
        optional = ['completed_at']
        if kind == 'task':
            optional.append('place')
            if 'place' in data:
                place = data['place']
                fields(place, ['text', 'name', 'city', 'status'], ['source_ref'])
                string(place['text'], 160)
                require(isinstance(place['name'], str) and len(place['name']) <= 120)
                require(isinstance(place['city'], str) and len(place['city']) <= 80)
                require(place['status'] in ('candidate', 'confirmed'))
                require(place['status'] != 'confirmed' or bool(place['city'].strip()))
                if 'source_ref' in place:
                    ref = place['source_ref']
                    fields(ref, ['id', 'revision'])
                    require(type(ref['revision']) is int and ref['revision'] > 0)
                    source = state['records'].get(ref['id']) if isinstance(ref['id'], str) else None
                    require(source and source['space'] == record['space'] and source['revision'] == ref['revision'], 'SOURCE_INVALIDATED', 409)
        if kind == 'project':
            optional += ['progress_note', 'next_step', 'share_summary', 'area_tags']
            for key, limit in [('progress_note', 2000), ('next_step', 500)]:
                require(isinstance(data.get(key, ''), str) and len(data.get(key, '')) <= limit)
            require(type(data.get('share_summary', False)) is bool)
            tags = data.get('area_tags', [])
            require(isinstance(tags, list) and len(tags) <= 3 and all(t in ('工作','生活','个人项目') for t in tags))
            require(len(tags) == len(set(tags)))
        fields(data, common + ['status', 'parent_id', 'due_on'], optional)
        require(data['status'] in {'active', 'paused', 'done', 'archived'})
        for key in ('due_on', 'completed_at'):
            value = data.get(key)
            if value is not None:
                require(isinstance(value, str) and len(value) == 10, 'INVALID_TIME')
                try:
                    date.fromisoformat(value)
                except ValueError:
                    raise Problem('INVALID_TIME') from None
        require(not data.get('completed_at') or data['status'] in {'done','archived'}, 'INVALID_COMPLETION')
    parent_id = data.get('parent_id')
    require(parent_id is None or isinstance(parent_id, str))
    if parent_id is not None:
        parent = state['records'].get(parent_id)
        require(kind in PARENTS and parent and parent['kind'] == PARENTS[kind]
                and parent['space'] == record['space'], 'INVALID_PARENT', 409)
    string(data['title'], 200)
    require(isinstance(data['body'], str) and len(data['body']) <= 16000)
    require(data['domain'] in DOMAINS)


def context_consented(record, space):
    from .spaces import normalize_space
    return (record['kind'] == 'context' and normalize_space(record['space']) == normalize_space(space)
            and record['mode'] == 'normal' and record['data'].get('ai') is True
            and record['data'].get('sync') is True and record['data'].get('sensitivity') != 'secret')


def allowed_context(records, space, relevance=None):
    """Bounded, same-space input; explicit AI consent on each context item."""
    eligible = [r for r in records if context_consented(r, space)]
    scores = {r['id']: relevance(r) for r in eligible} if relevance is not None else {}
    if relevance is not None:
        eligible = [r for r in eligible if scores[r['id']] > 0]
    selected, size = [], 0
    for record in sorted(eligible, key=lambda r: (scores.get(r['id'], 0), r['updated_at'], r['id']), reverse=True):
        count = len(record['data']['title']) + len(record['data']['body'])
        if size + count <= 8000:
            selected.append(record)
            size += count
        if len(selected) == 8:
            break
    return selected
