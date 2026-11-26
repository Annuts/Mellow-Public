"""Owner space compatibility. Family identifiers are opaque and never rewritten."""
from __future__ import annotations
from typing import Any


def normalize_space(space):
    # Leave malformed input intact so the caller's ordinary validation rejects it.
    return 'personal' if isinstance(space, str) and space in {'life', 'work'} else space


def normalize_spaces(spaces):
    if not isinstance(spaces, (list, tuple, set)):
        return spaces
    result = []
    for space in spaces:
        space = normalize_space(space)
        if space not in result:
            result.append(space)
    return result


def normalize_payload(value: Any) -> Any:
    """Normalize known request envelopes, never arbitrary user/service data."""
    if not isinstance(value, dict):
        return value
    if 'space' in value:
        value['space'] = normalize_space(value['space'])
    for key in ('spaces', 'allowed_spaces'):
        if key in value:
            value[key] = normalize_spaces(value[key])
    for key in ('record', 'config', 'scope'):
        if isinstance(value.get(key), dict):
            normalize_payload(value[key])
    if value.get('action') in {'model.save', 'connector.save', 'rule.save'} and isinstance(value.get('data'), dict):
        normalize_payload(value['data'])
    if isinstance(value.get('events'), list):
        for event in value['events']:
            if isinstance(event, dict) and 'space' in event:
                event['space'] = normalize_space(event['space'])
    return value
