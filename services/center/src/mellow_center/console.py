"""Same-origin browser console. Device credentials stay in an HttpOnly cookie."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter
import re
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from .request_body import buffer_body, sync_body
from .domain import Problem, authenticate, fields, new_pairing, pair, require, string

COOKIE = '__Host-mellow-console'
BROWSER_COOKIE = '__Host-mellow-browser'
WEB = Path(__file__).parent / 'web'
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def check_browser(request):
    require(request.headers.get('x-mellow-console') == '1', 'BROWSER_REQUEST_REQUIRED', 403)
    origin = request.headers.get('origin')
    # Unsafe methods always need an exact Origin. GET requires the custom header,
    # which cross-origin pages cannot send without a preflight (CORS is not enabled).
    if request.method not in {'GET', 'HEAD'} or origin is not None:
        require(origin == str(request.base_url).rstrip('/'), 'ORIGIN_DENIED', 403)


def browser_identity(state, request):
    check_browser(request)
    device_id, device = authenticate(state, request.cookies.get(COOKIE, ''))
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    return device_id, device


def brief_facts(body):
    """Pick distinct, source-reported facts. Generic complaints are not project outcomes."""
    candidates = {'description': [], 'purpose': [], 'progress': [], 'next': []}
    labels = {'项目简介': 'description', '讨论与需求': 'purpose', '会话报告的进展': 'progress', '待确认': 'next'}
    objective = re.compile(r'目标是|目的是|为了|用来|旨在|想做|希望|想要|打造|搭建|构建|建立|用于|为.{1,12}提供')
    outcome = re.compile(r'改成了|清理了|减少|已完成|已经|已注册|验收[标准规则]|规则验收|修复了|降低到|降到|保留了')
    result = re.compile(r'已|完成|通过|验证|上线|支持|实现|修复|更新|接入|部署|选好|交付|保留|测试')
    for line in body.splitlines():
        label, sep, value = line.partition(' · ')
        key = labels.get(label)
        text = value.split('〔', 1)[0].replace('**', '').replace('`', '').strip()[:240]
        if not sep or not key or not text:
            continue
        if re.search(r'没有我想要|没啥用|不符合预期|会话进行中|https?://|\]\(|'
                     r'(?:现在|请|可以先|接下来你|建议你)(?:打开|点击?|进入|查看|看一下|看)|'
                     r'(?:点开|点击).{0,20}(?:按钮|菜单|管理台)|(?:如果你|你愿意|要不要我)', text):
            continue
        if key == 'progress' and (not result.search(text) or re.search(r'希望|计划|需要|预计|尚未|还没|规则验收|验收[标准规则]|如果|应当|应该', text)):
            continue
        score = 1
        if key == 'purpose':
            if not objective.search(text) or outcome.search(text) or re.match(r'(?:会话报告|目前)?已', text):
                continue
            score += 2 * bool(re.search(r'目标是|目的是|旨在|打造|希望', text))
        candidates[key].append((score, text))
    facts = {}
    for key, rows in candidates.items():
        for _, text in sorted(reversed(rows), key=lambda entry: entry[0], reverse=True):
            if text not in facts.values():
                facts[key] = text
                break
    return facts


def router(vault):
    api = APIRouter(dependencies=[Depends(buffer_body)])

    @api.get('/', include_in_schema=False)
    async def root():
        return RedirectResponse('/console')

    @api.get('/console', include_in_schema=False)
    async def index():
        return FileResponse(WEB / 'index.html', headers={'Content-Security-Policy': CSP})

    @api.get('/console/assets/{name}', include_in_schema=False)
    async def asset(name: str):
        require(name in {'app.js', 'planning.js', 'style.css', 'admin.js', 'usage.js', 'living.js', 'accounts.js', 'archive.js', 'mellow-icon.png'}, 'NOT_FOUND', 404)
        return FileResponse(WEB / name)

    @api.get('/console/project-previews')
    def project_previews(request: Request):
        from .connector_gateway import project_key
        with vault.view() as state:
            _, device = browser_identity(state, request)
            instant = datetime.now(timezone.utc).isoformat()
            pending = sorted((b for b in state.get('connector_batches', {}).values()
                              if b['status'] == 'pending' and b['expires_at'] > instant and b['space'] in device['spaces']),
                             key=lambda b: b['created_at'], reverse=True)
            previews = {}
            for batch in pending:
                for item in batch['items']:
                    key = (batch['space'], batch['connector_id'], project_key(item))
                    if not key[2]:
                        continue
                    data = item['data']
                    title = re.fullmatch(r'(.+) · (?:项目交接(?: \d+)?|项目简介)', data['title'])
                    if not title:
                        continue
                    facts = brief_facts(data['body'])
                    preview = previews.setdefault(key, {'project': title[1], 'space': batch['space'], 'at': batch['created_at']})
                    if data['title'].endswith(' · 项目简介') and not preview.get('profile_batch'):
                        preview.update(facts, profile_batch=batch['id'])
                    else:
                        for field, text in facts.items():
                            preview.setdefault(field, text)
            return {'pending': dict(Counter(b['space'] for b in pending)), 'projects': list(previews.values())}

    @api.post('/console/session')
    def login(request: Request):
        check_browser(request)
        value = sync_body(request, 8192)
        owner_login = set(value) == {"token"}
        browser_key = request.cookies.get(BROWSER_COOKIE) or secrets.token_urlsafe(32)
        require(len(browser_key) <= 128)
        if owner_login:
            from .owner_access import enroll
            string(value["token"], 128)
            with vault.transaction() as state:
                result = enroll(vault, state, value["token"], "小满浏览器", admin=True)
                if result:
                    from .owner_access import reuse_browser
                    result = reuse_browser(state, result, browser_key)
                else:
                    state.bump_revision = False
        else:
            fields(value, ['pairing_id', 'token', 'center_id'])
            string(value['pairing_id'], 128); string(value['token'], 128)
            failure = None
            with vault.transaction() as state:
                invitation = state['pairing'].get(value['pairing_id'])
                if value['center_id'] != state['center_id']:
                    failure = ('CENTER_ID_MISMATCH', 409)
                elif invitation and not invitation['admin']:
                    failure = ('ADMIN_INVITATION_REQUIRED', 403)
                result = pair(state, '' if failure else value['pairing_id'], value['token'])
                if result is not None:
                    state['devices'][result['device_id']]['expires_at'] = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat()
                    from .owner_access import reuse_browser
                    result = reuse_browser(state, result, browser_key)
                else:
                    state.bump_revision = False
            if failure:
                raise Problem(*failure)
        if result is None:
            raise Problem('PAIRING_FAILED', 401)
        with vault.view() as state:
            device = state['devices'][result['device_id']]
            permissions = {'is_admin': bool(device.get('admin')), 'owner_enrolled': bool(device.get('owner_enrolled'))}
        response = JSONResponse({'device_id':result['device_id'], 'spaces':result['spaces'], 'center_id':result['center_id'], **permissions})
        response.set_cookie(COOKIE, str(result['access_token']), max_age=365*86400 if owner_login else 8*3600,
                             secure=True, httponly=True, samesite='strict', path='/')
        response.set_cookie(BROWSER_COOKIE, browser_key, max_age=365*86400,
                            secure=True, httponly=True, samesite='strict', path='/')
        return response

    @api.get('/console/session')
    def session(request: Request):
        with vault.view() as state:
            device_id, device = browser_identity(state, request)
            return {'device_id':device_id, 'spaces':device['spaces'], 'center_id':state['center_id'],
                    'is_admin': bool(device.get('admin')), 'owner_enrolled': bool(device.get('owner_enrolled'))}

    @api.delete('/console/session')
    def logout(request: Request):
        with vault.transaction() as state:
            device_id, device = browser_identity(state, request)
            device['revoked'] = True
            from .thought_candidates import revoke_device
            revoke_device(state, device_id)
        response = JSONResponse({'signed_out':True})
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response

    @api.post('/console/invitations')
    def invite(request: Request):
        from .certificates import fingerprint, tls_status
        check_browser(request)
        value = sync_body(request, 4096)
        fields(value, ['name', 'spaces'])
        string(value['name'], 80)
        require(isinstance(value['spaces'], list) and value['spaces'])
        with vault.transaction() as state:
            _, device = browser_identity(state, request)
            require(all(isinstance(s, str) and s in device['spaces'] and s in {'personal','life','work'}
                        for s in value['spaces']), 'PERMISSION_REVOKED', 403)
            # Browser invitations are for scoped, non-admin clients only.
            result = new_pairing(state, value['name'], value['spaces'], admin=False)
        return {**result, 'lan':str(request.base_url).rstrip('/'), 'tail':'',
                'fingerprint':fingerprint(vault.path.parent),
                'tls_spki_sha256':tls_status(vault.path.parent)['tls_spki_sha256']}

    return api
