"""Family-space member management for the browser console.

Plain state functions (unit-testable) plus ``routes(vault)``, which returns an APIRouter without a
prefix so it can be mounted under the existing ``/api/preview`` router. Owners (administrator
devices) belong to every family space implicitly; invited phones hold exactly the groups listed in
their ``spaces``. Nothing here exposes tokens or token hashes.
"""
from fastapi import APIRouter, Request

from .admin import audit
from .domain import fields, require, string
from .vault import now


def known_group(state, group_id):
    require(group_id in state.get('family_spaces', {}), 'NOT_FOUND', 404)
    return state['family_spaces'][group_id]


def status_of(device):
    if device['revoked']:
        return 'revoked'
    if device['expires_at'] <= now():
        return 'expired'
    return 'active' if device.get('last_seen') else 'waiting'


def member_view(device_id, device, actor):
    return {'id': device_id, 'name': device['name'], 'platform': device.get('platform'),
            'version': device.get('version'), 'last_seen': device.get('last_seen'),
            'created_at': device.get('created_at'), 'expires_at': device['expires_at'],
            'status': status_of(device), 'current': device_id == actor}


def overview(state, actor, device):
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    instant = now()
    result = []
    for group_id, group in state.get('family_spaces', {}).items():
        members = [member_view(key, value, actor) for key, value in state['devices'].items()
                   if group_id in value['spaces'] and not value['admin']]
        invites = [{'id': key, 'name': value['name'], 'expires_at': value['expires_at']}
                   for key, value in state['pairing'].items()
                   if value['spaces'] == [group_id] and not value['admin'] and value['expires_at'] > instant]
        owners = sum(1 for value in state['devices'].values() if value['admin'] and not value['revoked'])
        result.append({'id': group_id, 'name': group['name'], 'owner_devices': owners,
                       'members': sorted(members, key=lambda m: (m['status'] != 'active', m['name'])),
                       'invites': sorted(invites, key=lambda i: i['expires_at'])})
    return {'groups': result}


def rename_group(state, actor, device, group_id, payload):
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    group = known_group(state, group_id)
    fields(payload, ['name'])
    group['name'] = string(payload['name'].strip() if isinstance(payload['name'], str) else payload['name'], 80)
    audit(state, actor, 'family.rename', group_id)
    return {'id': group_id, 'name': group['name']}


def member_device(state, group_id, device_id):
    known_group(state, group_id)
    device = state['devices'].get(device_id)
    require(device and group_id in device['spaces'], 'NOT_FOUND', 404)
    return device


def rename_member(state, actor, device, group_id, device_id, payload):
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    target = member_device(state, group_id, device_id)
    require(not target['admin'], 'OWNER_DEVICE', 409)
    fields(payload, ['name'])
    target['name'] = string(payload['name'].strip() if isinstance(payload['name'], str) else payload['name'], 80)
    audit(state, actor, 'family.member.rename', device_id)
    return {'id': device_id, 'name': target['name']}


def withdraw(state, actor, group_id, device_id, action):
    """Drop one family space from a device. A device left with nothing is revoked and deleted."""
    target = member_device(state, group_id, device_id)
    require(not target['admin'], 'OWNER_DEVICE', 409)
    target['spaces'] = [space for space in target['spaces'] if space != group_id]
    gone = not target['spaces']
    if gone:
        del state['devices'][device_id]
    for job in state['jobs'].values():
        if (job['device_id'] == device_id and job['state'] in {'queued', 'running'}
                and (gone or job.get('space') == group_id)):
            job.update(state='cancelled', error='PERMISSION_REVOKED', partial='')
    audit(state, actor, action, device_id)
    return {'removed': True, 'device_deleted': device_id not in state['devices']}


def remove_member(state, actor, device, group_id, device_id):
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    return withdraw(state, actor, group_id, device_id, 'family.member.remove')


def leave_group(state, actor, device, group_id):
    require(not device['admin'], 'OWNER_CANNOT_LEAVE', 409)
    return withdraw(state, actor, group_id, actor, 'family.leave')


def cancel_invite(state, actor, device, group_id, pairing_id):
    require(device['admin'], 'ADMIN_REQUIRED', 403)
    known_group(state, group_id)
    entry = state['pairing'].get(pairing_id)
    require(entry and entry['spaces'] == [group_id] and not entry['admin'], 'NOT_FOUND', 404)
    del state['pairing'][pairing_id]
    audit(state, actor, 'family.invite.cancel', pairing_id)
    return {'cancelled': True}


def routes(vault):
    from .api import identity, sync_body
    api = APIRouter()

    @api.get('/family')
    def family(request: Request):
        with vault.view() as state:
            actor, device = identity(state, request, admin=True)
            return overview(state, actor, device)

    @api.post('/groups/{group_id}/rename')
    def group_rename(group_id: str, request: Request):
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            actor, device = identity(state, request, admin=True)
            return rename_group(state, actor, device, group_id, value)

    @api.post('/groups/{group_id}/invitation')
    def group_invitation(group_id: str, request: Request):
        """Same invitation as /invite plus the address and certificate pins a phone needs."""
        from .cli import fingerprint, tls_status
        from .sharing import invite
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            _, device = identity(state, request, admin=True)
            result = invite(state, device, group_id, value)
        return {**result, 'lan': str(request.base_url).rstrip('/'), 'tail': '',
                'fingerprint': fingerprint(vault.path.parent),
                'tls_spki_sha256': tls_status(vault.path.parent)['tls_spki_sha256']}

    @api.delete('/groups/{group_id}/invites/{pairing_id}')
    def invite_cancel(group_id: str, pairing_id: str, request: Request):
        with vault.transaction() as state:
            actor, device = identity(state, request, admin=True)
            return cancel_invite(state, actor, device, group_id, pairing_id)

    @api.post('/groups/{group_id}/members/{device_id}/rename')
    def member_rename(group_id: str, device_id: str, request: Request):
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            actor, device = identity(state, request, admin=True)
            return rename_member(state, actor, device, group_id, device_id, value)

    @api.delete('/groups/{group_id}/members/{device_id}')
    def member_remove(group_id: str, device_id: str, request: Request):
        with vault.transaction() as state:
            actor, device = identity(state, request, admin=True)
            return remove_member(state, actor, device, group_id, device_id)

    @api.post('/groups/{group_id}/leave')
    def group_leave(group_id: str, request: Request):
        with vault.transaction() as state:
            actor, device = identity(state, request)
            return leave_group(state, actor, device, group_id)

    return api


def router(vault):
    """Standalone router for tests and the demo server: mounts ``routes`` under /api/preview."""
    from fastapi import Depends
    from .api import buffer_body
    api = APIRouter(prefix='/api/preview', dependencies=[Depends(buffer_body)])
    api.include_router(routes(vault))
    return api
