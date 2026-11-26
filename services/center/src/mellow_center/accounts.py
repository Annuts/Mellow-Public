"""External life accounts: owner configuration, scoped reads and phone-only confirmations."""
from __future__ import annotations

import asyncio
import copy
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, Request

from . import ha
from .console import COOKIE, browser_identity
from .domain import Problem, authenticate, fields, require
from .mail import MAIL_DEFAULTS
from .request_body import buffer_body, sync_body

DEFAULTS = {
    'ha': {'enabled': False, 'base_url': ha.DEFAULT_URL, 'token': '', 'entities': []},
    'mail': copy.deepcopy(MAIL_DEFAULTS),
}


def settings(state):
    stored = state.get('credentials', {}).get('external_accounts', {})
    return {key: {**copy.deepcopy(value), **copy.deepcopy(stored.get(key, {}))} for key, value in DEFAULTS.items()}


def public_settings(state):
    result = settings(state)
    for kind, secret in [('ha', 'token'), ('mail', 'password')]:
        result[kind]['has_credentials'] = bool(result[kind].pop(secret, ''))
    status = state['private_items'].get('external_mail_status', {})
    result['mail']['status'] = {key: status[key] for key in ('state', 'message', 'last_attempt_at', 'last_success_at', 'imported', 'drafts') if key in status}
    result['revision'] = state['private_items'].get('external_accounts_revision', 0)
    return result


def validate_mail(value):
    fields(value, DEFAULTS['mail'])
    require(type(value['enabled']) is bool, '请明确选择是否启用邮箱连接。')
    require(isinstance(value['host'], str) and len(value['host']) <= 253 and
            (not value['host'] or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]*', value['host'])), '请填写有效的 IMAP 服务器域名。')
    require(type(value['port']) is int and 1 <= value['port'] <= 65535, 'IMAP SSL 端口须为 1 到 65535。')
    require(type(value['days']) is int and 1 <= value['days'] <= 30, '邮箱读取范围须为最近 1 到 30 天。')
    for key, limit in [('username', 320), ('password', 4000)]:
        require(isinstance(value[key], str) and len(value[key]) <= limit and not any(c in value[key] for c in '\r\n\x00'),
                '邮箱账号或应用密码格式不正确。')
    for key in ('senders', 'keywords'):
        require(isinstance(value[key], list) and len(value[key]) <= 50, '每类邮箱白名单最多 50 条。')
        require(all(isinstance(v, str) and 0 < len(v.strip()) <= 200 and not any(c in v for c in '\r\n\x00') for v in value[key]),
                '邮箱白名单不能包含空规则或换行。')
        value[key] = list(dict.fromkeys(v.strip() for v in value[key]))
    require(all(re.fullmatch(r'[^\s@<>*?]+@[^\s@<>*?]+\.[^\s@<>*?]+', v) for v in value['senders']),
            '发件人白名单请填写完整邮箱地址，不接受通配符。')
    require(not value['enabled'] or all(value[k] for k in ('host', 'username', 'password')), '启用邮箱前请先填写服务器、账号和应用密码。')
    return value


def save_settings(state, value):
    fields(value, ['revision'], ['ha', 'mail'])
    require(type(value['revision']) is int and value['revision'] == state['private_items'].get('external_accounts_revision', 0),
            '账户设置已在其他窗口修改，请刷新后重试。', 409)
    require('ha' in value or 'mail' in value, '请选择要保存的连接器。')
    result = settings(state)
    for kind in ('ha', 'mail'):
        if kind not in value:
            continue
        patch = value[kind]
        fields(patch, [], DEFAULTS[kind])
        secret = 'token' if kind == 'ha' else 'password'
        location = ('base_url',) if kind == 'ha' else ('host', 'port', 'username')
        if result[kind].get(secret) and any(key in patch and patch[key] != result[kind][key] for key in location):
            require(secret in patch, '连接地址或账号已变化，请重新填写凭据，或关闭连接器并清除原凭据。')
        result[kind].update(patch)
        result[kind] = ha.validate_config(result[kind]) if kind == 'ha' else validate_mail(result[kind])
    state.setdefault('credentials', {})['external_accounts'] = result
    state['private_items']['external_accounts_revision'] = value['revision'] + 1
    return public_settings(state)


def device_identity(state, request, *, phone=False):
    auth = request.headers.get('authorization', '')
    require(auth.startswith('Bearer ') and len(auth) <= 160, 'AUTH_FAILED', 401)
    actor, device = authenticate(state, auth[7:])
    require('personal' in device['spaces'], '这台设备未获准访问你的资料。', 403)
    if phone:
        require(COOKIE not in request.cookies and device.get('platform') in {'android', 'ios'},
                '家居控制必须在已配对的手机上明确确认。', 403)
    return actor, device


def routes(vault):
    api = APIRouter(dependencies=[Depends(buffer_body)])

    @api.get('/console/accounts')
    def console_accounts(request: Request):
        with vault.view() as state:
            _, device = browser_identity(state, request)
            require('personal' in device['spaces'], '需要本人授权。', 403)
            return public_settings(state)

    @api.put('/console/accounts')
    def console_save(request: Request):
        value = sync_body(request)
        with vault.transaction(bump_revision=False) as state:
            actor, device = browser_identity(state, request)
            require('personal' in device['spaces'], '需要本人授权。', 403)
            result = save_settings(state, value)
            from .admin import audit
            audit(state, actor, 'accounts.save', '外部生活账户设置已更新')
            return result

    @api.get('/api/preview/accounts')
    def phone_accounts(request: Request):
        with vault.view() as state:
            device_identity(state, request)
            public = public_settings(state)
            return {'ha': public['ha'], 'mail': {'enabled': public['mail']['enabled']}}

    @api.get('/api/preview/accounts/ha/states')
    def states(request: Request):
        with vault.view() as state:
            device_identity(state, request)
            value = settings(state)['ha']
        result = ha.read_states(value)
        with vault.view() as state:
            device_identity(state, request)
            require(ha.signature(value) == ha.signature(settings(state)['ha']), '家居授权已变化，请刷新。', 409)
        return {'states': result}

    @api.post('/api/preview/accounts/ha/drafts')
    def draft(request: Request):
        value = sync_body(request)
        with vault.transaction(bump_revision=False) as state:
            actor, _ = device_identity(state, request)
            return ha.draft(state, actor, value)

    @api.post('/api/preview/accounts/ha/confirm')
    def confirm(request: Request):
        value = sync_body(request)
        fields(value, ['id', 'confirmed'])
        require(value['confirmed'] is True, '请先在手机上确认家居操作。', 403)
        require(isinstance(value['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{20,80}', value['id']), '家居操作编号无效。')
        with vault.view() as state:
            actor, _ = device_identity(state, request, phone=True)
        return ha.confirm(vault, actor, value['id'])

    @api.post('/api/preview/accounts/bills/import')
    def import_bills(request: Request):
        value = sync_body(request)
        fields(value, ['entries'])
        require(isinstance(value['entries'], list) and len(value['entries']) <= 100, '每次最多同步 100 条账单。')
        rows = []
        for entry in value['entries']:
            fields(entry, ['id', 'amount_fen', 'merchant', 'occurred_at', 'source'])
            require(isinstance(entry['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', entry['id']), '账单编号无效。')
            require(type(entry['amount_fen']) is int and 0 < entry['amount_fen'] <= 10000000000, '账单金额无效。')
            for key, limit in [('merchant', 80), ('source', 80), ('occurred_at', 64)]:
                require(isinstance(entry[key], str) and 0 < len(entry[key]) <= limit and not any(ord(c) < 32 for c in entry[key]), '账单字段格式无效。')
            try:
                instant = datetime.fromisoformat(entry['occurred_at'].replace('Z', '+00:00'))
            except ValueError:
                raise Problem('账单时间格式无效。') from None
            require(instant.tzinfo is not None, '账单时间须包含时区。')
            rows.append(copy.deepcopy(entry))
        with vault.transaction(bump_revision=False) as state:
            actor, _ = device_identity(state, request)
            items = state['private_items']
            prefix = 'bill:' + actor + ':'
            additions = {prefix + entry['id'] for entry in rows if prefix + entry['id'] not in items}
            require(sum(k.startswith(prefix) for k in items) + len(additions) <= 10000, '中枢账单数量已达上限，请先清理。', 409)
            for entry in rows:
                key = prefix + entry['id']
                old = items.get(key)
                require(old is None or old == entry, '账单编号已被其他内容使用。', 409)
                items[key] = entry
        return {'imported': len(rows)}

    @api.get('/api/preview/accounts/bills')
    def bills(request: Request):
        with vault.view() as state:
            actor, _ = device_identity(state, request)
            return {'entries': [v for k, v in state['private_items'].items() if k.startswith('bill:' + actor + ':')]}

    @api.get('/api/preview/accounts/bills/email')
    def email_bills(request: Request):
        from .bills import email_bills as parsed_email_bills
        with vault.view() as state:
            device_identity(state, request)
            return {'entries': parsed_email_bills(state)}

    @api.delete('/api/preview/accounts/bills')
    def clear_bills(request: Request):
        with vault.transaction(bump_revision=False) as state:
            actor, _ = device_identity(state, request)
            items = state['private_items']
            for key in list(items):
                if key.startswith('bill:' + actor + ':'):
                    del items[key]
        return {'cleared': True}

    return api


def lifespan(vault, original=None):
    """Compose rather than replace the CLI lifecycle; network work never holds the vault lock."""
    @asynccontextmanager
    async def empty(_app):
        yield

    @asynccontextmanager
    async def combined(app):
        async with (original or empty)(app) as state:
            from .mail import mail_loop
            task = asyncio.create_task(mail_loop(vault), name='external-mail')
            try:
                yield state
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    return combined
