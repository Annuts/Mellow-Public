"""Owner-managed shared lists/events. Personal memory and model use are never shared."""
import re
from .domain import fields, new_pairing, require, string
from .vault import uid
from .spaces import normalize_space


def shared_space(space):
    return isinstance(space, str) and re.fullmatch(r'family:[0-9a-f-]{36}',space) is not None


def valid_space(space):
    return isinstance(space, str) and (normalize_space(space) == 'personal' or shared_space(space))


def groups(state,device):
    return [{'id':key,'name':value['name']} for key,value in state.get('family_spaces',{}).items()
            if device['admin'] or key in device['spaces']]


def create_group(state,device_id,payload):
    device=state['devices'][device_id];require(device['admin'],'ADMIN_REQUIRED',403)
    fields(payload,['name']);name=string(payload['name'],80)
    require(len(state.get('family_spaces',{})) < 20,'GROUP_LIMIT',409)
    identifier='family:'+uid()
    state.setdefault('family_spaces',{})[identifier]={'name':name}
    for owner in state['devices'].values():
        if owner['admin'] and not owner['revoked']: owner['spaces'].append(identifier)
    return {'id':identifier,'name':name}


def invite(state,device,group_id,payload):
    require(device['admin'],'ADMIN_REQUIRED',403)
    require(group_id in state.get('family_spaces',{}),'NOT_FOUND',404)
    fields(payload,['name'])
    # Invitations can only grant this one shared group, never administrator rights.
    return new_pairing(state,string(payload['name'],80),[group_id],False)
