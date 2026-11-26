"""Scoped pull/push gateway. Imports stay pending until a browser admin approves."""
import re
import copy
import hashlib
from typing import Any
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from .request_body import buffer_body, sync_body
from .domain import fields, require, string
from .life_system import context_consented, validate
from .vault import canonical, now
from .spaces import normalize_space


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def connector_identity(state, request, permission):
    auth = request.headers.get('authorization', '')
    require(auth.startswith('Bearer ') and 20 < len(auth) < 200, 'AUTH_FAILED', 401)
    digest = token_hash(auth[7:])
    connector = next((c for c in state.get('connectors', {}).values() if c.get('token_hash') == digest), None)
    require(connector and connector['enabled'] and connector['expires_at'] > now(), 'AUTH_FAILED', 401)
    assert connector is not None
    connector['space'] = normalize_space(connector['space'])
    require(connector[permission], 'CONNECTOR_SCOPE_DENIED', 403)
    return connector


def public(connector):
    return {k:copy.deepcopy(v) for k,v in connector.items() if k != 'token_hash'}


def normalize(value):
    fields(value, ['items'])
    require(isinstance(value['items'], list) and 1 <= len(value['items']) <= 100, 'IMPORT_LIMIT')
    seen, items = set(), []
    for raw in value['items']:
        fields(raw, ['title','body'], ['external_id','id','layer','domain','tags'])
        require(('id' in raw) != ('external_id' in raw))
        external_id = raw.get('external_id',raw.get('id'))
        string(external_id,200)
        require(external_id not in seen, 'DUPLICATE_SOURCE')
        seen.add(external_id)
        data = {'title':raw['title'], 'body':raw['body'], 'layer':raw.get('layer','archive'),
            'domain':raw.get('domain','general'), 'tags':raw.get('tags',[]),
            'sensitivity':'private', 'sync':True, 'ai':False}
        validate({}, {'kind':'context','mode':'normal','data':data})
        items.append({'external_id':external_id, 'data':data})
    return items


def expire_batches(state, instant):
    for batch in state.get('connector_batches',{}).values():
        if batch['status']=='pending' and batch['expires_at'] <= instant:
            batch['status']='expired'
            batch['count']=len(batch['items'])
            batch['items']=[]


def project_key(item):
    match = re.fullmatch(r'project-brief-([a-f0-9]{64})-(?:\d+|profile)', item['external_id'])
    return match.group(1) if match else None


def stage(state, connector, value) -> dict[str, Any]:
    connector['space'] = normalize_space(connector['space'])
    fields(value, ['operation_id','items'])
    string(value['operation_id'],128)
    items = normalize({'items':value['items']})
    digest = token_hash(canonical(items).decode())
    key = hashlib.sha256(canonical([connector['id'], value['operation_id']])).hexdigest()
    batches = state.setdefault('connector_batches', {})
    if key in batches:
        require(batches[key]['digest'] == digest, 'OPERATION_REUSED', 409)
        return batches[key]
    expire_batches(state, now())
    ignored = connector.get('ignored_projects', {})
    items = [i for i in items if project_key(i) not in ignored]
    if not items:
        return {'id':key,'status':'ignored','items':[],'count':0}
    require(sum(b['status']=='pending' for b in batches.values()) < 100, 'IMPORT_QUEUE_FULL', 429)
    changes = []
    for item in items:
        source = hashlib.sha256(canonical([connector['id'], item['external_id']])).hexdigest()
        previous = state.get('connector_sources', {}).get(source)
        record = state['records'].get(previous['id']) if previous else None
        changes.append({**item, 'source':source, 'id':previous['id'] if previous else 'context-' + source,
            'base_revision':record['revision'] if record else 0,
            'conflict':bool(previous and (not record or record['revision'] != previous['revision']))})
    batches[key] = {'id':key,'connector_id':connector['id'],'space':connector['space'], 'status':'pending',
        'created_at':now(),'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
        'digest':digest,'items':changes}
    connector['last_seen'] = now()
    return batches[key]


def router(vault):
    api = APIRouter(prefix='/api/connectors', dependencies=[Depends(buffer_body)])

    @api.get('/context')
    def context(request: Request, project: str = ""):
        require(len(project) <= 160, 'INVALID_ARGUMENT')
        needle = project.strip().casefold()
        with vault.transaction(bump_revision=False) as state:
            c = connector_identity(state, request, 'export')
            records = [r for r in state['records'].values() if context_consented(r,c['space']) and r['data']['layer'] in c['layers']]
            if project.strip():
                needle = project.strip().casefold()
                def matches_project(record):
                    parent = state['records'].get(record['data'].get('parent_id'))
                    if parent and parent['kind'] == 'project' and parent['space'] == c['space']:
                        return needle == parent['data']['title'].strip().casefold() or needle in record['data']['title'].casefold()
                    return needle in record['data']['title'].casefold()
                records = [r for r in records if matches_project(r)]
                records.sort(key=lambda r: (r['updated_at'],r['id']),reverse=True)
            result: dict[str, Any] = {'format':'mellow-context-1', 'items':[{'id':r['id'], **{k:r['data'][k] for k in ('title','body','domain','layer')}} for r in records]}
            if project.strip() and 'active' in c['layers']:
                for record in state['records'].values():
                    d = record['data']
                    if (record['kind'] == 'project' and record['space'] == c['space'] and record['mode'] == 'normal'
                        and d.get('share_summary') is True and d['status'] != 'archived'
                        and d['title'].strip().casefold() == needle):
                        status = {'active':'进行中', 'paused':'暂停', 'done':'已完成'}[d['status']]
                        summary = '\n'.join(f'{label}：{value}' for label, value in [
                            ('项目目的', d['body']), ('状态', status), ('目前进展', d.get('progress_note')), ('下一步', d.get('next_step'))] if value)
                        result['items'].insert(0, {'id':record['id'], 'kind':'project', 'title':d['title'], 'body':summary,
                                                   'domain':d['domain'], 'layer':'active'})
            require(len(result['items'])<=100 and len(canonical(result))<=480*1024,'EXPORT_LIMIT',413)
            c['last_seen'] = now()
            return result

    @api.post('/imports')
    def imports(request: Request):
        value = sync_body(request, 512*1024)
        with vault.transaction(bump_revision=False) as state:
            c = connector_identity(state, request, 'import')
            batch = stage(state,c,value)
            return {'batch_id':batch['id'],'status':batch['status'],'count':batch.get('count',len(batch['items']))}

    @api.post('/activity')
    def activity(request: Request):
        value = sync_body(request, 8192)
        with vault.transaction() as state:
            c = connector_identity(state, request, 'import')
            return record_activity(state,c,value)

    @api.post('/usage')
    def usage(request: Request):
        from .usage import import_events
        value = sync_body(request, 256000)
        with vault.transaction(bump_revision=False) as state:
            c = connector_identity(state, request, 'import')
            result = import_events(state, value, origin='connector:'+c['id'], source=c['adapter'],
                spaces=[c['space']], device_id=c['id'])
            c['last_seen'] = now()
            return result

    return api


def record_activity(state, connector, value):
    connector['space'] = normalize_space(connector['space'])
    fields(value, ['device','tool','session','project','task','status','observed_at'])
    for key,limit in [('device',160),('tool',30),('session',200),('project',160),('task',500),('observed_at',50)]:
        string(value[key],limit)
    require(value['tool'] in {'codex','opencode','dsh','claude-code'})
    require(value['status'] in {'running','waiting','completed','stopped','failed','unknown'})
    from .features import timestamp
    observed=timestamp(value['observed_at'])
    require(datetime.fromisoformat(observed) <= datetime.now(timezone.utc)+timedelta(minutes=5), 'INVALID_TIME_WINDOW')
    identifier='activity-'+hashlib.sha256(canonical([connector['id'],value['device'],value['tool'],value['session']])).hexdigest()
    require(identifier not in state['deleted'],'DELETED_OBJECT',409)
    old=state['records'].get(identifier)
    import json
    if old:
        previous=json.loads(old['data']['body'])
        if previous['observed_at'] >= observed:return {'id':identifier,'status':'unchanged'}
    payload={**value,'observed_at':observed,'received_at':now()}
    state['records'][identifier]={'id':identifier,'kind':'context','space':connector['space'],'mode':'normal',
        'revision':old['revision']+1 if old else 1,'updated_at':now(),
        'data':{'title':(value['project']+' · '+value['tool'])[:200],'body':json.dumps(payload,ensure_ascii=False),
                'layer':'active','domain':'work','tags':['agent-activity'],'sensitivity':'private','sync':True,'ai':False}}
    connector['last_seen']=now()
    return {'id':identifier,'status':'recorded'}
