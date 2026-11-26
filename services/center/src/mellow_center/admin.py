"""Browser administration with optimistic concurrency and retry-safe commands."""
import copy
import hashlib
import secrets
import json
import re
from typing import Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from .request_body import buffer_body, sync_body
from .console import browser_identity
from .connector_gateway import public, stage, token_hash, project_key
from .domain import fields, model_config, mutate, public_preferences, require, string, Problem
from .vault import canonical, now, uid
from .spaces import normalize_space, normalize_spaces


def revision(state):
    values = {k:state.get(k) for k in ('connectors','automation_rules','model','preferences','backup_policy','usage_prices')}
    values['credentials'] = state['credentials']
    # Heartbeats are operational state, not configuration edits.
    values['connectors'] = {k:{a:b for a,b in c.items() if a!='last_seen'} for k,c in state.get('connectors',{}).items()}
    return hashlib.sha256(canonical(values)).hexdigest()


def zone(value):
    string(value,80)
    try: ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError): raise Problem('INVALID_TIMEZONE') from None
    return value


def number(value, low, high):
    require(type(value) is int and low <= value <= high)
    return value


def boolean(value):
    require(type(value) is bool)
    return value


def scoped(device, space):
    space = normalize_space(space)
    require(space == 'personal' and space in normalize_spaces(device['spaces']), 'PERMISSION_REVOKED',403)
    return space


def audit(state, actor, action, target=''):
    entries = state.setdefault('admin_audit',[])
    entries.append({'id':uid(),'at':now(),'actor':actor,'action':action,'target':target})
    del entries[:-500]


def preferences(value):
    require(isinstance(value, dict))
    value = dict(value)
    value.pop('dreaming', None)
    fields(value,['proactive','daily_limit','model_proactive','model_daily_limit','timezone','quiet_start','quiet_end'],
           ['conversation_quiet_seconds', 'dreaming_hour'])
    result = {'proactive':boolean(value['proactive']), 'daily_limit':number(value['daily_limit'],0,10),
        'model_proactive':boolean(value['model_proactive']), 'model_daily_limit':number(value['model_daily_limit'],0,20),
        'timezone':zone(value['timezone']), 'quiet_start':number(value['quiet_start'],0,23), 'quiet_end':number(value['quiet_end'],0,23)}
    for key, high in [('conversation_quiet_seconds', 86400), ('dreaming_hour', 7)]:
        if key in value:
            result[key] = number(value[key], 0, high)
    return result


def review_import(state, actor, device, target, approve, allow_ai):
    batch=state.get('connector_batches',{}).get(target)
    require(batch and batch['status']=='pending' and batch['expires_at']>now(),'PREVIEW_REQUIRED',409)
    scoped(device,batch['space'])
    if approve:
        c=state['connectors'][batch['connector_id']]
        require(c['enabled'] and c['import'],'CONNECTOR_SCOPE_DENIED',403)
        require(not any(i['conflict'] for i in batch['items']),'IMPORT_CONFLICT',409)
        sources=state.setdefault('connector_sources',{})
        for item in batch['items']:
            # Recheck all versions under the same transaction. No partial batch writes.
            old=state['records'].get(item['id'])
            require((old['revision'] if old else 0)==item['base_revision'],'REVISION_CONFLICT',409)
            d=copy.deepcopy(item['data'])
            d['ai'] = allow_ai  # Only the reviewing owner can grant this.
            mutate(state,actor,{'operation_id':uid(),'action':'put','id':item['id'],
                'base_revision':item['base_revision'],'record':{'kind':'context','space':batch['space'],'mode':'normal','data':d}})
            sources[item['source']]={'id':item['id'],'revision':state['records'][item['id']]['revision']}
        batch['status']='approved'
    else: batch['status']='rejected'
    batch['count']=len(batch['items'])
    batch['items']=[]  # Only the review outcome remains; source text lives in records.
    return {'status':batch['status'],'count':batch['count']}


def apply(state, actor, device, value, directory):
    from .spaces import normalize_payload
    value = normalize_payload(copy.deepcopy(value))
    device = {**device, 'spaces': normalize_spaces(device['spaces'])}
    action, data = value['action'], value['data']
    result: dict[str, Any] = {'saved':True}
    target: Any = ''
    if action == 'model.save':
        fields(data,['config'],['key'])
        config = model_config(data['config'])
        require(set(config['spaces']) <= set(device['spaces']), 'PERMISSION_REVOKED',403)
        if 'key' in data:
            require(isinstance(data['key'],str) and len(data['key'])<=4096)
            state['credentials']['model'] = data['key']
        elif state['model'] and state['model']['base_url'] != config['base_url']:
            state['credentials'].pop('model',None)
        state['model'] = config
        for job in state['jobs'].values():
            if job['state'] in {'queued','running'}: job.update(state='cancelled',error='MODEL_CONFIG_CHANGED')
    elif action == 'usage.prices':
        from .usage import validate_prices
        fields(data,['prices'])
        state['usage_prices'] = validate_prices(data['prices'])
    elif action == 'model.clear':
        fields(data,[])
        state['model'] = None
        state['credentials'].pop('model',None)
        for job in state['jobs'].values():
            if job['state'] in {'queued','running'}: job.update(state='cancelled',error='MODEL_CONFIG_CHANGED')
    elif action == 'preferences.save':
        state['preferences'].update(preferences(data))
        from .thought_candidates import refresh_policy
        refresh_policy(state)
        if not data['proactive']:
            for s in state['suggestions'].values():
                if s['state'] in {'new','snoozed'}: s['state']='dismissed'
        if not data['proactive'] or not data['model_proactive']:
            for job in state['jobs'].values():
                if job['kind']=='proactive' and job['state'] in {'queued','running'}:
                    job.update(state='cancelled',error='PROACTIVE_DISABLED',partial='')
    elif action == 'rule.save':
        fields(data,['id','name','kind','space','enabled','timezone','hour','weekday','days'])
        string(data['id'],128); string(data['name'],80)
        require(data['kind'] in {'task_due','project_stalled','weekly_review'})
        scoped(device,data['space']); boolean(data['enabled']); zone(data['timezone'])
        number(data['hour'],0,23); number(data['weekday'],0,6); number(data['days'],1,90)
        rules=state.setdefault('automation_rules',{})
        old=rules.get(data['id'])
        if old: scoped(device,old['space'])
        require(old or len(rules)<100,'RULE_LIMIT')
        rules[data['id']]=copy.deepcopy(data)
        target=data['id']
        # Editing/disabling a rule withdraws its old suggestions.
        for s in state['suggestions'].values():
            if s.get('rule_id')==target and s['state'] in {'new','snoozed'}: s['state']='expired'
    elif action == 'rule.delete':
        fields(data,['id']); target=data['id']
        rule=state.get('automation_rules',{}).get(target)
        require(rule,'NOT_FOUND',404); scoped(device,rule['space'])
        del state['automation_rules'][target]
        for s in state['suggestions'].values():
            if s.get('rule_id')==target: s['state']='expired'
    elif action == 'connector.save':
        fields(data,['id','name','adapter','space','enabled','import','export','layers'])
        string(data['id'],128); string(data['name'],80); scoped(device,data['space'])
        require(data['adapter'] in {'generic','codex','opencode','dsh'})
        for key in ('enabled','import','export'): boolean(data[key])
        require(isinstance(data['layers'],list) and set(data['layers']) <= {'profile','active','event','archive'})
        connectors=state.setdefault('connectors',{})
        old=connectors.get(data['id'])
        if old:
            scoped(device,old['space'])
            require(old['space']==data['space'],'IMMUTABLE_FIELD')
        require(old or len(connectors)<100,'CONNECTOR_LIMIT')
        c={**(old or {}), **copy.deepcopy(data)}
        if not old:
            token=secrets.token_urlsafe(32)
            c.update(token_hash=token_hash(token), expires_at=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),last_seen=None)
            result['token']=token
        connectors[data['id']]=c; target=data['id']
    elif action == 'connector.rotate':
        fields(data,['id']); target=data['id']
        c=state.get('connectors',{}).get(target)
        require(c,'NOT_FOUND',404); scoped(device,c['space'])
        token=secrets.token_urlsafe(32)
        c.update(token_hash=token_hash(token),expires_at=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat())
        result['token']=token
    elif action == 'import.stage':
        fields(data,['connector_id','items'])
        c=state.get('connectors',{}).get(data['connector_id'])
        require(c and c['enabled'] and c['import'],'CONNECTOR_SCOPE_DENIED',403); scoped(device,c['space'])
        batch=stage(state,c,{'operation_id':value['operation_id'],'items':data['items']})
        result={'batch_id':batch['id'],'count':len(batch['items'])}; target=c['id']
    elif action == 'import.mute':
        fields(data,['id'])
        batch = state.get('connector_batches',{}).get(data['id'])
        require(batch and batch['status']=='pending' and len(batch['items'])==1,'PREVIEW_REQUIRED',409)
        scoped(device,batch['space'])
        item = batch['items'][0]
        target = project_key(item)
        require(target, 'INVALID_ARGUMENT')
        connector = state['connectors'][batch['connector_id']]
        connector.setdefault('ignored_projects',{})[target] = item['data']['title']
        for other in state['connector_batches'].values():
            if other['connector_id'] != connector['id'] or other['status'] != 'pending':
                continue
            remaining = [i for i in other['items'] if project_key(i) != target]
            if not remaining:
                other.update(status='ignored',count=0)
            other['items'] = remaining
    elif action == 'import.unmute':
        fields(data,['connector_id','project'])
        connector = state.get('connectors',{}).get(data['connector_id'])
        require(connector,'NOT_FOUND',404)
        scoped(device,connector['space'])
        target = string(data['project'],64)
        connector.get('ignored_projects',{}).pop(target,None)
    elif action in {'import.approve','import.reject'}:
        approve = action == 'import.approve'
        fields(data, [], ['id','ids','allow_ai'] if approve else ['id','ids'])
        require(('id' in data) != ('ids' in data))
        allow_ai = data.get('allow_ai', False)
        boolean(allow_ai)
        if 'id' in data:
            target = string(data['id'],128)
            result = review_import(state,actor,device,target,approve,allow_ai)
        else:
            ids = data['ids']
            require(isinstance(ids,list) and 1 <= len(ids) <= 100)
            for batch_id in ids:
                string(batch_id,128)
            require(len(set(ids)) == len(ids))
            count = 0
            for batch_id in ids:
                count += review_import(state,actor,device,batch_id,approve,allow_ai)['count']
                audit(state,actor,action,batch_id)
            return {'batches':len(ids),'count':count}
    elif action == 'backup.policy':
        fields(data,['enabled','keep','hour','timezone'],['password'])
        boolean(data['enabled']); number(data['keep'],1,90); number(data['hour'],0,23); zone(data['timezone'])
        if 'password' in data:
            string(data['password'],1024); require(len(data['password'])>=12,'WEAK_PASSPHRASE')
            state['credentials']['backup']=data['password']
        if data['enabled']: require(state['credentials'].get('backup'),'BACKUP_PASSWORD_REQUIRED')
        # A fixed private location avoids a browser turning into an arbitrary file writer.
        state['backup_policy']={'enabled':data['enabled'],'keep':data['keep'],'hour':data['hour'],
            'timezone':data['timezone'],'directory':str(directory/'backups')}
        if not data['enabled']: state['credentials'].pop('backup',None)
    else: raise Problem('UNKNOWN_ADMIN_ACTION')
    audit(state,actor,action,target)
    return result


def backup_files(directory):
    folder=directory/'backups'
    if not folder.is_dir(): return []
    return [{'name':p.name,'size':p.stat().st_size} for p in sorted(folder.glob('mellow-auto-????-??-??.json'),reverse=True)
            if p.is_file() and not p.is_symlink()][:100]


def router(vault):
    api=APIRouter(prefix='/console', dependencies=[Depends(buffer_body)])

    @api.get('/admin')
    def admin(request: Request):
        from .usage import budget_day
        with vault.view() as state:
            _,device=browser_identity(state,request)
            return {'revision':revision(state), 'connectors':[public(c) for c in state.get('connectors',{}).values() if c['space'] in device['spaces']],
                'rules':[r for r in state.get('automation_rules',{}).values() if r['space'] in device['spaces']],
                'batches':[b for b in state.get('connector_batches',{}).values() if b['space'] in device['spaces'] and b['status']=='pending' and b['expires_at']>now()],
                'backup_files':backup_files(vault.path.parent),
                'model':state['model'],'has_model_key':bool(state['credentials'].get('model')),
                'preferences':public_preferences(state, device),'backup_policy':state.get('backup_policy'),
                'has_backup_password':bool(state['credentials'].get('backup')),'last_backup':state.get('last_backup'),
                'audit':list(reversed(state.get('admin_audit',[])))[:100],
                'suggestions':[s for s in state['suggestions'].values() if s['space'] in device['spaces']],
                'usage':{'date_utc':now()[:10], 'date_local':budget_day(state),
                         'calls':state['budgets'].get(budget_day(state),0)}}

    @api.get('/backup-files/{name}')
    def backup_file(name: str, request: Request):
        with vault.view() as state:
            browser_identity(state,request)
        require(re.fullmatch(r'mellow-auto-\d{4}-\d{2}-\d{2}\.json',name),'NOT_FOUND',404)
        path=vault.path.parent/'backups'/name
        require(path.is_file() and not path.is_symlink(),'NOT_FOUND',404)
        require(path.stat().st_size <= 32*1024*1024,'PAYLOAD_TOO_LARGE',413)
        value=json.loads(path.read_text())
        require(isinstance(value,dict) and set(value)=={'format','salt','body'} and value['format']=='mellow-backup-preview-1','INVALID_BACKUP_OR_PASSPHRASE')
        with vault.transaction() as state:
            actor,_=browser_identity(state,request)
            audit(state,actor,'backup.download',name)
        return value

    @api.post('/actions')
    def actions(request: Request):
        value=sync_body(request,512*1024)
        fields(value,['operation_id','base_revision','action','data'])
        string(value['operation_id'],128); string(value['base_revision'],128); string(value['action'],80)
        require(isinstance(value['data'],dict))
        digest=hashlib.sha256(canonical(value)).hexdigest()
        with vault.transaction() as state:
            actor,device=browser_identity(state,request)
            receipts=state.setdefault('admin_receipts',{})
            key=actor+':'+value['operation_id']
            if key in receipts:
                wire_digest = hashlib.sha256(canonical(json.loads(request._body))).hexdigest()
                require(receipts[key]['hash'] in {digest, wire_digest},'OPERATION_REUSED',409)
                return receipts[key]['result']
            require(value['base_revision']==revision(state),'SETTINGS_CONFLICT',409)
            result=apply(state,actor,device,value,vault.path.parent)
            receipts[key]={'hash':digest,'result':result, 'created_at':now()}
            return result

    @api.post('/rules/preview')
    def rule_preview(request: Request):
        from .automations import candidates
        value=sync_body(request,4096); fields(value,['id'])
        with vault.view() as state:
            _,device=browser_identity(state,request)
            rule=state.get('automation_rules',{}).get(value['id'])
            require(rule,'NOT_FOUND',404); scoped(device,rule['space'])
            from .proactive import may_suggest
            return {'candidates':candidates(state,rule,now()), 'can_deliver':rule['enabled'] and may_suggest(state),
                'explanation':'预览不会发送提醒；实际投递受启用状态、时区、触发时间、安静时段和每日上限限制。'}

    return api
