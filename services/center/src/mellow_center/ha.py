"""Narrow Home Assistant adapter; reading is free, every write is a phone draft."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from .domain import Problem, fields, require
from .vault import canonical, now

DEFAULT_URL = 'http://host.docker.internal:8123'
DOMAINS = {'light', 'switch', 'climate', 'sensor', 'cover'}
SERVICES = {'light.turn_on', 'light.turn_off', 'switch.turn_on', 'switch.turn_off',
            'climate.set_temperature', 'cover.open_cover', 'cover.close_cover'}
# User-facing open/close aliases are translated to HA's actual REST services.
ALIASES = {'cover.open': 'cover.open_cover', 'cover.close': 'cover.close_cover'}


def config(state):
    return copy.deepcopy(state.get('credentials', {}).get('external_accounts', {}).get('ha', {}))


def signature(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_config(value):
    fields(value, ['enabled', 'base_url', 'token', 'entities'])
    require(type(value['enabled']) is bool, '请明确选择是否启用家居连接。')
    base = value['base_url']
    require(isinstance(base, str) and len(base) <= 2000, '请填写有效的家居地址。')
    try:
        url = urlsplit(base)
        valid = (url.scheme in {'http', 'https'} and bool(url.hostname) and
                 url.username is None and url.password is None and not url.query and not url.fragment and
                 url.path in {'', '/'} and (url.port is None or 1 <= url.port <= 65535))
    except ValueError:
        valid = False
    require(valid and not any(c.isspace() for c in base), '家居地址须为 HTTP 或 HTTPS 根地址，不能包含密码或查询参数。')
    token = value['token']
    require(isinstance(token, str) and len(token) <= 4000 and not any(c.isspace() for c in token), '家居令牌格式不正确。')
    require(not value['enabled'] or bool(token), '请先保存 Home Assistant 长期访问令牌。')
    entities = value['entities']
    require(isinstance(entities, list) and len(entities) <= 50, '最多选择 50 个家居实体。')
    require(all(isinstance(e, str) and re.fullmatch(r'(light|switch|climate|sensor|cover)\.[a-z0-9_]{1,120}', e) for e in entities),
            '只支持灯、开关、空调、传感器和窗帘的有效实体 ID。')
    value['entities'] = list(dict.fromkeys(entities))
    value['base_url'] = base.rstrip('/')
    return value


def active(value):
    require(value.get('enabled') and value.get('token'), '家居连接尚未启用。', 409)


def request(value, method, path, payload=None, *, transport=None, timeout: float = 8):
    active(value)
    deadline = time.monotonic() + timeout
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(4, timeout)), follow_redirects=False,
                          trust_env=False, transport=transport) as client:
            with client.stream(method, value['base_url'] + '/api/' + path,
                               headers={'Authorization': 'Bearer ' + value['token']}, json=payload) as response:
                require(response.status_code not in {401, 403}, '家居授权失败，请检查或更新长期令牌。', 502)
                require(response.status_code != 404, '家居实体或服务不存在，请核对配置。', 502)
                require(200 <= response.status_code < 300, '家居服务暂时不可用，请稍后刷新核对状态。', 502)
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    require(time.monotonic() <= deadline, '读取家居返回内容超时，请刷新核对状态。', 504)
                    size += len(chunk)
                    require(size <= 262144, '家居返回内容过大，已停止读取。', 502)
                    chunks.append(chunk)
                return json.loads(b''.join(chunks))
    except httpx.TimeoutException:
        raise Problem('连接家居超时；控制结果可能未知，请先刷新设备状态。', 504) from None
    except (httpx.HTTPError, ValueError):
        raise Problem('无法连接家居或返回格式有误，请检查地址与网络。', 502) from None


def read_states(value, *, transport=None):
    active(value)
    result = []
    deadline = time.monotonic() + 8
    for entity in value.get('entities', []):
        try:
            remaining = deadline - time.monotonic()
            require(remaining > 0, '读取家居状态超时，请稍后刷新。', 504)
            raw = request(value, 'GET', 'states/' + entity, transport=transport, timeout=remaining)
            require(isinstance(raw, dict) and raw.get('entity_id') == entity and isinstance(raw.get('state'), str),
                    '家居状态格式有误。', 502)
            attrs = raw.get('attributes', {})
            attrs = attrs if isinstance(attrs, dict) else {}
            allowed = {k: v for k, v in attrs.items() if k in {'friendly_name', 'unit_of_measurement', 'temperature',
                       'current_temperature', 'min_temp', 'max_temp'} and isinstance(v, (str, int, float, bool))}
            allowed = {k: v[:120] if isinstance(v, str) else v for k, v in allowed.items()
                       if not isinstance(v, float) or math.isfinite(v)}
            result.append({'entity_id': entity, 'state': raw['state'][:100], 'attributes': allowed})
        except Problem as error:
            result.append({'entity_id': entity, 'state': 'unavailable', 'error': error.code, 'attributes': {}})
    return result


def action(value, body):
    active(value)
    fields(body, ['entity_id', 'service'], ['temperature'])
    entity, service = body['entity_id'], body['service']
    require(isinstance(entity, str) and entity in value.get('entities', []), '该设备没有获得授权。', 403)
    require(isinstance(service, str), '家居操作格式不正确。')
    service = ALIASES.get(service, service)
    require(service in SERVICES and service.split('.')[0] == entity.split('.')[0], '此家居操作不在允许列表中。', 403)
    payload = {'entity_id': entity}
    if service == 'climate.set_temperature':
        temperature = body.get('temperature')
        require(type(temperature) in {int, float} and math.isfinite(temperature) and 5 <= temperature <= 35,
                '空调温度须在 5 到 35 摄氏度之间。')
        payload['temperature'] = temperature
    else:
        require('temperature' not in body, '此操作不接受温度参数。')
    return {'entity_id': entity, 'service': service, 'data': payload}


def draft(state, actor, body):
    value = config(state)
    command = action(value, body)
    instant = datetime.now(timezone.utc)
    items = state['private_items']
    # Bound storage; receipts remain long enough to prevent accidental retries.
    for key, entry in list(items.items()):
        if key.startswith('ha-action:') and (entry.get('retire_at', '') <= instant.isoformat() or
                entry.get('status') == 'draft' and entry.get('expires_at', '') <= instant.isoformat()):
            del items[key]
    pending = [entry for key, entry in items.items() if key.startswith('ha-action:') and entry.get('status') == 'draft']
    require(len(pending) < 500 and sum(entry['device_id'] == actor for entry in pending) < 50,
            '待确认家居操作过多，请等待两分钟后再试。', 429)
    receipts = sorted(((key, entry) for key, entry in items.items() if key.startswith('ha-action:') and entry.get('status') != 'draft'),
                      key=lambda row: row[1].get('retire_at', ''))
    for key, _ in receipts[:-2000]:
        del items[key]
    identifier = secrets.token_urlsafe(24)
    entry = {**command, 'id': identifier, 'device_id': actor, 'config_signature': signature(value),
             'status': 'draft', 'expires_at': (instant + timedelta(minutes=2)).isoformat(),
             'retire_at': (instant + timedelta(days=7)).isoformat()}
    items['ha-action:' + identifier] = entry
    return {k: entry[k] for k in ('id', 'entity_id', 'service', 'data', 'status', 'expires_at')}


def confirm(vault, actor, identifier, *, transport=None):
    from .spaces import normalize_spaces
    key = 'ha-action:' + identifier
    with vault.transaction(bump_revision=False) as state:
        device = state['devices'].get(actor)
        require(device and not device.get('revoked') and device.get('expires_at', '') > now() and
                'personal' in normalize_spaces(device.get('spaces', [])) and device.get('platform') in {'android', 'ios'},
                '手机授权已失效，请重新配对后确认。', 403)
        entry = state['private_items'].get(key)
        require(entry and entry['device_id'] == actor, '未找到这台手机的家居操作草稿。', 404)
        value = config(state)
        require(entry['config_signature'] == signature(value), '家居设置已变化，请重新生成操作草稿。', 409)
        if entry['status'] != 'draft':
            return {'id': identifier, 'status': entry['status'], 'message': entry.get('message', '操作已经提交，请刷新设备状态。')}
        require(entry['expires_at'] > now(), '家居操作草稿已过期，请重新确认。', 409)
        action(value, {k: entry['data'][k] for k in entry['data']} | {'service': entry['service']})
        entry['status'] = 'submitted'
        state['private_items'][key] = entry
    # Consume before I/O: a timeout or crash can never silently replay a service.
    status, message = 'done', '家居操作已提交，请以最新设备状态为准。'
    try:
        request(value, 'POST', 'services/' + entry['service'].replace('.', '/'), entry['data'], transport=transport)
    except Problem as error:
        status, message = 'unknown', error.code + ' 不会自动重试。'
    with vault.transaction(bump_revision=False) as state:
        saved = state['private_items'].get(key)
        if saved:
            saved.update(status=status, message=message)
            state['private_items'][key] = saved
    return {'id': identifier, 'status': status, 'message': message}
