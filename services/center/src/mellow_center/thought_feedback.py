"""Pair-scoped, aggregate-only learning. Windows replace; batches never add twice."""
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from .domain import require

COUNTS = {'shown', 'useful', 'ignored', 'not_now', 'snoozed'}
CHANNELS = {'bubble', 'chat', 'inbox', 'notification'}


def _store(state, actor):
    return state.setdefault('private_items', {}).setdefault('thought_feedback:' + actor,
        {'version': 0, 'enabled': True, 'windows': {}, 'batches': {}})


def import_batch(state, actor, batch_id, value):
    require(isinstance(batch_id, str) and bool(re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', batch_id)),
            'INVALID_FEEDBACK_BATCH')
    require(isinstance(value, dict) and type(value.get('schema_version')) is int and
            value['schema_version'] == 1, 'INVALID_FEEDBACK_BATCH')
    store = _store(state, actor)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = store['batches'].get(batch_id)
    if previous:
        require(previous['digest'] == digest, 'FEEDBACK_BATCH_CONFLICT', 409)
        return copy.deepcopy(previous['result'])
    if 'enabled' in value:
        require(set(value) == {'schema_version', 'enabled', 'revision'}, 'INVALID_FEEDBACK_BATCH')
        require(type(value['enabled']) is bool and type(value['revision']) is int, 'INVALID_FEEDBACK_BATCH')
        require(value['revision'] > store.get('setting_revision', 0), 'STALE_FEEDBACK_REVISION', 409)
        store.update(enabled=value['enabled'], windows={}, setting_revision=value['revision'])
    else:
        require(store['enabled'], 'THOUGHT_LEARNING_DISABLED', 403)
        require(set(value) == {'schema_version', 'window', 'revision', 'buckets'}, 'INVALID_FEEDBACK_BATCH')
        window, revision, rows = value['window'], value['revision'], value['buckets']
        require(isinstance(window, dict) and set(window) == {'start', 'end'}, 'INVALID_FEEDBACK_WINDOW')
        try:
            start, end = (datetime.fromisoformat(window[k]) for k in ('start', 'end'))
            instant = datetime.now(timezone.utc)
            valid = (start.tzinfo is not None and end.tzinfo is not None and
                     timedelta(0) < end-start <= timedelta(days=30) and
                     start >= instant - timedelta(days=30) and end <= instant + timedelta(minutes=5))
        except (ValueError, TypeError):
            valid = False
        require(valid, 'INVALID_FEEDBACK_WINDOW')
        require(type(revision) is int and revision > 0 and isinstance(rows, list) and len(rows) <= 100,
                'INVALID_FEEDBACK_BATCH')
        # Normalize offset spellings so one window cannot be counted twice.
        window = {k: datetime.fromisoformat(window[k]).astimezone(timezone.utc).isoformat() for k in ('start', 'end')}
        key = window['start'] + '/' + window['end']
        old = store['windows'].get(key)
        require(old is None or revision > old['revision'], 'STALE_FEEDBACK_REVISION', 409)
        require(all(key == other or window['end'] <= entry['window']['start'] or
                    window['start'] >= entry['window']['end'] for other, entry in store['windows'].items()),
                'OVERLAPPING_FEEDBACK_WINDOW', 409)
        merged = {}
        from .thought_candidates import DIRECTIONS
        for row in rows:
            require(isinstance(row, dict) and set(row) <= COUNTS | {'direction', 'topic_token', 'channel'} and
                    COUNTS <= set(row) and isinstance(row.get('direction'), str) and
                    row['direction'] in DIRECTIONS and isinstance(row.get('channel'), str) and row['channel'] in CHANNELS and
                    all(type(row[k]) is int and 0 <= row[k] <= 100000 for k in COUNTS), 'INVALID_FEEDBACK_BUCKET')
            require(all(row[k] <= row['shown'] for k in COUNTS - {'shown'}), 'INVALID_FEEDBACK_BUCKET')
            token = row.get('topic_token')
            require(token is None or isinstance(token, str) and
                    bool(re.fullmatch(r'[A-Za-z0-9_-]{16,128}', token)), 'INVALID_TOPIC_TOKEN')
            # Sparse observations retain direction only, not an identifying topic/channel.
            sparse = row['shown'] < 5
            group = (row['direction'], None if sparse else token, None if sparse else row['channel'])
            bucket = merged.setdefault(group, {'direction': row['direction'], **({} if sparse else
                {'channel': row['channel'], **({'topic_token': token} if token else {})}), **dict.fromkeys(COUNTS, 0)})
            for count in COUNTS:
                bucket[count] += row[count]
        store['windows'][key] = {'window': window, 'revision': revision, 'buckets': list(merged.values())}
        store['windows'] = dict(sorted(store['windows'].items())[-30:])
    store['version'] += 1
    result = {'schema_version': 1, 'batch_id': batch_id, 'version': store['version'], 'accepted': True}
    store['batches'][batch_id] = {'digest': digest, 'result': result}
    # Receipts are bounded; a repeated old window still fails the revision check.
    store['batches'] = dict(list(store['batches'].items())[-512:])
    return copy.deepcopy(result)


def learning_view(state, actor) -> dict[str, Any]:
    store = _store(state, actor)
    return {'schema_version': 1, 'version': store['version'], 'enabled': store['enabled'],
            'setting_revision': store.get('setting_revision', 0),
            'windows': copy.deepcopy(list(store['windows'].values())),
            'buckets': [copy.deepcopy(row) for entry in store['windows'].values() for row in entry['buckets']]}


def routes(api, vault, identity):
    from .request_body import sync_body

    def feedback(request: Request, batch_id: str):
        value = sync_body(request, 65536)
        with vault.transaction() as state:
            actor, _ = identity(state, request)
            result = import_batch(state, actor, batch_id, value)
            from .dreaming import thought_weight_suggestions
            return {**result, 'learning': thought_weight_suggestions(learning_view(state, actor))}

    api.add_api_route('/thoughts/feedback-batches/{batch_id}', feedback, methods=['PUT'])
