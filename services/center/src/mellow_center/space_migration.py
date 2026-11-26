"""Atomic owner-space upgrade, including metadata in old logical backups.

Record ids are globally unique and MUST NOT be renamed (including the old
news-interests-life/work ids). Memory keys are not unique ids: retained competing
memories are resolved by memory_status/profile recency and confirmation rules.
Only current revision references are advanced; stale evidence stays stale.
"""
from __future__ import annotations

from collections.abc import MutableMapping
import copy
import hashlib
import json
from typing import Any

from .spaces import normalize_space, normalize_spaces

MIGRATION = 'owner_personal_space_v1'
SPACE_MAPS = {'last_days', 'retry_at', 'archive_style', 'summaries'}


def _merge(left, right):
    """Merge per-space bookkeeping deterministically without dropping list entries."""
    if isinstance(left, dict) and isinstance(right, dict):
        # Generated cache entries are indivisible; keep the most recent one.
        stamps = ('generated_at', 'updated_at', 'at', 'next_at')
        if any(key in left or key in right for key in stamps):
            stamp = lambda item: max((str(item.get(key, '')) for key in stamps), default='')
            return copy.deepcopy(right if stamp(right) > stamp(left) else left)
        result = copy.deepcopy(left)
        for key, value in right.items():
            result[key] = _merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    if isinstance(left, list) and isinstance(right, list):
        return left + [item for item in right if item not in left]
    if isinstance(left, str) and isinstance(right, str):
        return max(left, right)  # ISO days and retry timestamps.
    return left


def normalize_metadata(value: Any, *, field: str | None = None) -> Any:
    """Rewrite schema fields, never arbitrary strings, record ids or user text."""
    if isinstance(value, MutableMapping):
        for key in list(value):
            item = value[key]
            if key == 'space':
                value[key] = normalize_space(item)
            elif key in {'spaces', 'allowed_spaces'}:
                value[key] = normalize_spaces(item)
            else:
                normalize_metadata(item, field=key)
        if field in SPACE_MAPS:
            for alias in ('life', 'work'):
                if alias in value:
                    item = value.pop(alias)
                    value['personal'] = _merge(value['personal'], item) if 'personal' in value else item
    elif isinstance(value, list):
        for item in value:
            normalize_metadata(item)
    return value


def _advance_refs(value, revisions):
    if isinstance(value, MutableMapping):
        identifier = value.get('id')
        if isinstance(identifier, str) and identifier in revisions and value.get('revision') == revisions[identifier]:
            value['revision'] += 1
        if isinstance(identifier, str) and identifier in revisions and value.get('base_revision') == revisions[identifier]:
            value['base_revision'] += 1
        if isinstance(identifier, str) and identifier in revisions and value.get('after_revision') == revisions[identifier]:
            value['after_revision'] += 1
        sources = value.get('source_ids')
        if isinstance(sources, list) and sources and isinstance(sources[0], str):
            source = sources[0]
            if source in revisions and value.get('source_revision') == revisions[source]:
                value['source_revision'] += 1
        for key, source in list(value.items()):
            if not key.endswith('_id') or not isinstance(source, str) or source not in revisions:
                continue
            revision_key = key[:-3] + '_revision'
            if value.get(revision_key) == revisions[source]:
                value[revision_key] += 1
        for item in value.values():
            _advance_refs(item, revisions)
    elif isinstance(value, list):
        for item in value:
            _advance_refs(item, revisions)


def migrate_state(state):
    """Pure unit-of-work migration; caller commits data and marker atomically."""
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    records = state.get('records', {})
    old_model_hash = hashlib.sha256(canonical(state.get('model'))).hexdigest()
    revisions = {}
    # Include records with changed embedded grant scopes, not just owner records.
    for identifier, record in records.items():
        before = canonical(record)
        normalize_metadata(record)
        if canonical(record) != before:
            revisions[identifier] = record['revision']
    # Reference changes are synced as record revisions as well. Compute closure
    # before applying increments so no valid dependency chain is invalidated.
    while True:
        added = False
        for identifier, record in records.items():
            if identifier in revisions:
                continue
            probe = copy.deepcopy(record)
            _advance_refs(probe.get('data', {}), revisions)
            if probe != record:
                revisions[identifier] = record['revision']
                added = True
        if not added:
            break
    for identifier, record in records.items():
        _advance_refs(record.get('data', {}), revisions)
        if identifier in revisions:
            record['revision'] = revisions[identifier] + 1
    if revisions:
        state.setdefault('owner_space_revisions', {}).update(revisions)
    # Collections are lazy mappings too; changes are detected by Vault._commit.
    # Receipts retain request hashes so old clients can retry committed requests.
    for key in list(state):
        if key in {'records', 'semantic_vectors'}:
            continue
        normalize_metadata(state[key], field=key)
        if key not in {'receipts', 'record_changes'}:
            _advance_refs(state[key], revisions)
    if hasattr(state, '__dict__'):
        state.__dict__.pop('_record_index', None)
    new_model_hash = hashlib.sha256(canonical(state.get('model'))).hexdigest()
    for job in state.get('jobs', {}).values():
        if job.get('model_hash') == old_model_hash:
            job['model_hash'] = new_model_hash
    return len(revisions)


def migrate_owner_spaces(vault):
    if getattr(vault, '_owner_space_migrated', False):
        return 0
    with vault.view() as state:
        if state.get(MIGRATION):
            vault._owner_space_migrated = True
            return 0
    with vault.transaction() as state:
        if state.get(MIGRATION):
            return 0
        count = migrate_state(state)
        state[MIGRATION] = True
        # transaction publishes record_changes and store_revision together with
        # the marker; interruption rolls all of them back through SQLite WAL.
        state.bump_revision = True
    vault._owner_space_migrated = True
    return count
