"""Explicit, revocable, device-bound read-only legacy grants. Disabled by default."""
from datetime import datetime, timedelta, timezone
import copy
import hashlib

from .archive import entries, instant
from .domain import fields, require, string
from .vault import now, uid


def audit(state, actor, action, identifier):
    state.setdefault('legacy_audit', []).append(dict(at=now(), actor=actor, action=action, grant_id=identifier))


def _digest(*parts):
    return hashlib.sha256('|'.join(parts).encode()).hexdigest()[:16]


def _put_once(state, record):
    """Create a notification once. An existing record or deletion tombstone is the user's decision."""
    if record['id'] in state['records'] or record['id'] in state.get('deleted', {}):
        return False
    state['records'][record['id']] = record
    return True


def _recipient_space(state, grant):
    """A family space the recipient shares with an owner device; None when no such space exists."""
    recipient = state['devices'].get(grant['data']['recipient_id'])
    owners = [d for d in state['devices'].values() if not d['revoked'] and (d.get('admin') or d.get('owner_enrolled'))]
    for space in sorted(recipient['spaces'] if recipient else []):
        if space.startswith('family:') and any(space in d['spaces'] for d in owners):
            return space
    return None


def _confirmation_origin(grant_id):
    return 'legacy_grant:' + grant_id


def notify_recipient_pending(state, grant, base, at):
    """Generic, non-sensitive request in the shared space asking the trusted person to confirm."""
    space = _recipient_space(state, grant)
    if not space:
        return False
    identifier = 'legacy-confirm:' + grant['id'] + ':' + _digest(base.isoformat())
    created = _put_once(state, dict(id=identifier, kind='event', space=space, mode='normal', revision=1,
        updated_at=at.isoformat(), occurred_at=at.isoformat(), data=dict(
            text='托付确认：有一项托付需要你打开小满人生档案确认。', start=at.isoformat(),
            end=(at + timedelta(hours=1)).isoformat(), all_day=False, location='', status='confirmed',
            origin=_confirmation_origin(grant['id']))))
    if created:
        audit(state, 'scheduler', 'recipient.confirmation_reminder', grant['id'])
    return created


def close_recipient_notices(state, grant_id, at=None):
    """Cancel still-open confirmation requests once they no longer apply (confirmed, cancelled or revoked)."""
    stamp = at or now()
    for r in state['records'].values():
        if r['kind'] == 'event' and r['data'].get('origin') == _confirmation_origin(grant_id) and r['data']['status'] != 'cancelled':
            r['data']['status'] = 'cancelled'
            r.update(revision=r['revision'] + 1, updated_at=stamp)


def notify_owner_wait_started(state, grant, at):
    d = grant['data']
    identifier = 'legacy-wait:' + grant['id'] + ':' + _digest(d['confirmed_at'])
    created = _put_once(state, dict(id=identifier, kind='reminder', space=grant['space'], mode='normal', revision=1,
        updated_at=at, data=dict(text=f"托付确认：亲友已确认，{d['condition']['waiting_days']} 天等待期后将交付。如非本意，请打开小满主动签到以取消。",
        due_at=at, status='pending')))
    if created:
        audit(state, 'scheduler', 'owner.wait_started_reminder', grant['id'])
    return created


def owner_activity(state, actor, clock=None):
    device = state['devices'].get(actor, {})
    # Family member polls/confirmations and automatic sync must not impersonate owner activity.
    if not (device.get('admin') or device.get('owner_enrolled')): return
    at = clock or datetime.now(timezone.utc)
    state['legacy_last_activity'] = at.isoformat()
    if not state.get('legacy_configured'):
        return
    for r in state['records'].values():
        if r['kind'] != 'legacy_grant' or r['data']['condition']['type'] != 'inactivity': continue
        d = r['data']
        if d['status'] in {'pending', 'released'}:
            d.update(status='armed', confirmed_at=None, released_at=None)
            r.update(revision=r['revision'] + 1, updated_at=at.isoformat())
            close_recipient_notices(state, r['id'], at.isoformat())
            audit(state, actor, 'owner_activity.cancelled_release', r['id'])


def contacts(state, device):
    return [dict(id=k, name=d['name'], spaces=d['spaces']) for k, d in state['devices'].items()
            if not d['revoked'] and not d.get('admin') and not d.get('owner_enrolled')
            and any(s.startswith('family:') and s in device['spaces'] for s in d['spaces'])]


def create(state, actor, value):
    from .spaces import normalize_payload
    from .spaces import normalize_spaces
    value = normalize_payload(copy.deepcopy(value))
    device = state['devices'][actor]
    require(device.get('admin'), 'ADMIN_REQUIRED', 403)
    fields(value, ['recipient_id', 'scope', 'condition', 'confirm', 'confirm_again'])
    require(value['confirm'] is True and value['confirm_again'] is True, 'MULTIPLE_CONFIRMATIONS_REQUIRED')
    recipient = string(value['recipient_id'], 128)
    # Bind a person to a separately authenticated family-space device, never a name/group.
    require(recipient in {c['id'] for c in contacts(state, device)}, 'TRUSTED_DEVICE_REQUIRED')
    scope = value['scope']; fields(scope, ['spaces', 'tags', 'entry_ids'])
    require(isinstance(scope['spaces'], list) and bool(scope['spaces']) and
            set(scope['spaces']) <= set(normalize_spaces(device['spaces'])) & {'personal'})
    for key in ('tags', 'entry_ids'):
        require(isinstance(scope[key], list) and len(scope[key]) <= 1000)
        for item in scope[key]: string(item, 128)
    condition = value['condition']
    require(isinstance(condition, dict))
    if condition.get('type') == 'date':
        fields(condition, ['type', 'at'])
        require(instant(condition['at']) > datetime.now(timezone.utc), 'FUTURE_DATE_REQUIRED')
        condition = dict(type='date', at=instant(condition['at']).isoformat())
    else:
        fields(condition, ['type'], ['days', 'waiting_days'])
        require(condition['type'] == 'inactivity')
        days, waiting = condition.get('days', 180), condition.get('waiting_days', 14)
        require(type(days) is int and 31 <= days <= 36500 and type(waiting) is int and 14 <= waiting <= 365)
        condition = dict(type='inactivity', days=days, waiting_days=waiting)
    identifier = uid(); timestamp = now()
    record = dict(id=identifier, kind='legacy_grant', space=scope['spaces'][0], mode='source_only', revision=1,
                  updated_at=timestamp, data=dict(recipient_id=recipient, scope=copy.deepcopy(scope), condition=condition,
                  status='armed', created_at=timestamp, confirmed_at=None, released_at=None, reminders=[]))
    state['records'][identifier] = record
    state['legacy_configured'] = True
    state.setdefault('legacy_last_activity', timestamp)
    audit(state, actor, 'grant.created_twice_confirmed', identifier)
    return record


def granted_entries(state, grant, actor):
    d = grant['data']; target = state['devices'].get(d['recipient_id'])
    require(d['recipient_id'] == actor and target and not target['revoked'] and d['status'] == 'released', 'GRANT_NOT_RELEASED', 403)
    scope = d['scope']
    # Intersect live entry visibility, live scope and current originals on EVERY read.
    # Old revisions are owner history and are never part of a grant, so they are not even loaded.
    selected = [e for e in entries(state, scope['spaces'], include_revisions=False) if e['metadata']['visibility'] == 'designated'
            and (not scope['entry_ids'] or e['id'] in scope['entry_ids'])
            and (not scope['tags'] or set(scope['tags']) & set(e['tags']))]
    allowed = {e['id'] for e in selected}
    # A memory's source quote is independently private unless explicitly granted too.
    return [{k: v for k, v in e.items() if k != 'revisions'}
            for e in selected if all(q['id'] in allowed for q in e['quotes'])]


def tick(state, clock=None):
    at = clock or datetime.now(timezone.utc); changed = False
    last = instant(state.get('legacy_last_activity', at.isoformat()))
    for r in list(state['records'].values()):
        if r['kind'] != 'legacy_grant': continue
        d = r['data']; c = d['condition']; record_changed = False
        if d['status'] in {'revoked', 'released'}: continue
        target = state['devices'].get(d['recipient_id'])
        if not target or target['revoked']: continue
        if c['type'] == 'date':
            if at < instant(c['at']): continue
            d.update(status='released', released_at=at.isoformat())
            audit(state, 'scheduler', 'grant.released_date', r['id']); record_changed = True
        else:
            base = max(last, instant(d['created_at']))
            due = base + timedelta(days=c['days'])
            for before in (30, 14, 7):
                key = base.isoformat() + ':' + str(before)
                if at >= due - timedelta(days=before) and at < due and key not in d['reminders']:
                    identifier = uid()
                    state['records'][identifier] = dict(id=identifier, kind='reminder', space=r['space'], mode='normal',
                        revision=1, updated_at=at.isoformat(), data=dict(text=f'托付签到：距未活跃检查还有 {before} 天。打开小满主动签到可取消交付。', due_at=at.isoformat(), status='pending'))
                    d['reminders'].append(key); record_changed = True
                    audit(state, 'scheduler', 'owner.check_in_reminder', r['id'])
            if at >= due and d['status'] == 'armed':
                d.update(status='pending', confirmed_at=None); record_changed = True
                audit(state, 'scheduler', 'grant.awaiting_trusted_confirmation', r['id'])
                notify_recipient_pending(state, r, base, at)
            if (d['status'] == 'pending' and d.get('confirmed_at') and
                    at >= instant(d['confirmed_at']) + timedelta(days=c['waiting_days'])):
                d.update(status='released', released_at=at.isoformat()); record_changed = True
                audit(state, 'scheduler', 'grant.released_after_wait', r['id'])
        if record_changed:
            r.update(revision=r['revision'] + 1, updated_at=at.isoformat())
            changed = True
    return changed


def router(vault):
    from fastapi import APIRouter, Request, Depends
    from .api import identity
    from .request_body import buffer_body, sync_body
    api = APIRouter(prefix='/api/preview/archive/legacy', dependencies=[Depends(buffer_body)])

    @api.get('')
    def listing(request: Request):
        with vault.view() as state:
            actor, device = identity(state, request)
            grants = [r for r in state['records'].values() if r['kind'] == 'legacy_grant' and
                      (device.get('admin') or r['data']['recipient_id'] == actor)]
            return {'grants': grants, 'contacts': contacts(state, device) if device.get('admin') else [],
                    'audit': state.get('legacy_audit', []) if device.get('admin') else []}

    @api.post('')
    def creating(request: Request):
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            return create(state, actor, sync_body(request))

    @api.post('/check-in')
    def check_in(request: Request):
        with vault.transaction() as state:
            actor, device = identity(state, request)
            require(device.get('admin') or device.get('owner_enrolled'), 'OWNER_REQUIRED', 403)
            owner_activity(state, actor)
            return {'checked_in_at': state['legacy_last_activity']}

    @api.get('/received')
    def received(request: Request):
        # Audit-only write: it must not look like a sync change to the recipient or owner.
        with vault.transaction(bump_revision=False) as state:
            actor, _ = identity(state, request)
            result = []
            for r in state['records'].values():
                if r['kind'] == 'legacy_grant' and r['data']['recipient_id'] == actor and r['data']['status'] == 'released':
                    result.append(dict(id=r['id'], entries=granted_entries(state, r, actor)))
                    audit(state, actor, 'grant.read', r['id'])
            return {'grants': result}

    @api.post('/{identifier}/revoke')
    def revoke(identifier: str, request: Request):
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            r = state['records'].get(identifier)
            require(r and r['kind'] == 'legacy_grant', 'NOT_FOUND', 404)
            r['data'].update(status='revoked', released_at=None, confirmed_at=None)
            r.update(revision=r['revision'] + 1, updated_at=now())
            close_recipient_notices(state, identifier)
            audit(state, actor, 'grant.revoked', identifier)
            return {'revoked': True}

    @api.post('/{identifier}/confirm')
    def confirm(identifier: str, request: Request):
        value = sync_body(request); fields(value, ['confirm']); require(value['confirm'] is True)
        with vault.transaction() as state:
            actor, _ = identity(state, request)
            r = state['records'].get(identifier)
            require(r and r['kind'] == 'legacy_grant' and r['data']['recipient_id'] == actor, 'NOT_FOUND', 404)
            require(r['data']['status'] == 'pending' and not r['data'].get('confirmed_at'), 'CONFIRMATION_NOT_DUE', 409)
            at = now(); r['data']['confirmed_at'] = at; r.update(revision=r['revision'] + 1, updated_at=at)
            close_recipient_notices(state, identifier, at)
            notify_owner_wait_started(state, r, at)
            audit(state, actor, 'trusted.confirmed_wait_started', identifier)
            return r

    return api
