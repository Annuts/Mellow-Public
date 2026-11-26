"""Life archive is a projection of live originals, never a second undeletable vault."""
import copy
from datetime import datetime, timezone
from typing import Any

from .domain import Problem, fields, require, string, forgotten_history
from .vault import now, uid
from .spaces import normalize_space, normalize_spaces

KINDS = {'archive_note', 'letter', 'legacy_grant'}
VISIBILITIES = {'private', 'future_self', 'designated'}
ORIGINAL_KINDS = {'message', 'journal', 'memory', 'document', 'event', 'project',
                  'goal', 'vision', 'review', 'context'}


def validate_photo_preview(photo):
    import base64, binascii
    require(isinstance(photo, str) and 0 < len(photo) <= 2796204)
    try:
        image = base64.b64decode(photo, validate=True)
    except (ValueError, binascii.Error):
        require(False)
    require(image.startswith(b'\x89PNG\r\n\x1a\n') and len(image) <= 2 * 1024 * 1024)
    require(len(image) >= 33 and image[12:16] == b'IHDR')
    require(all(0 < int.from_bytes(image[i:i + 4], 'big') <= 640 for i in (16, 20)))


def instant(value) -> datetime:
    require(isinstance(value, str) and len(value) <= 64, 'INVALID_TIME')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(stamp.tzinfo is not None, 'INVALID_TIME')
        return stamp.astimezone(timezone.utc)
    except ValueError:
        raise Problem('INVALID_TIME') from None


def validate(state, record, internal=False):
    record['space'] = normalize_space(record['space'])
    d = record['data']
    require(record['space'] in {'personal', 'life', 'work'}, 'PRIVATE_SPACE_REQUIRED', 403)
    if record['kind'] == 'archive_note':
        fields(d, ['entry_id', 'featured', 'visibility', 'reflection', 'people', 'places', 'tags'], ['sources'])
        parent = state['records'].get(d['entry_id'])
        require(parent and parent['space'] == record['space'] and parent['kind'] in ORIGINAL_KINDS, 'NOT_FOUND', 404)
        require(record['id'] == 'archive-note:' + d['entry_id'], 'INVALID_ARCHIVE_NOTE')
        require(type(d['featured']) is bool and d['visibility'] in VISIBILITIES)
        require(isinstance(d['reflection'], str) and len(d['reflection']) <= 16000)
        for key in ('people', 'places', 'tags'):
            require(isinstance(d[key], list) and len(d[key]) <= 30)
            for item in d[key]:
                string(item, 128)
        # Owner-created annotation follows the source through edits and deletion.
        d['sources'] = [{'id': parent['id'], 'revision': parent['revision']}]
    elif record['kind'] == 'letter':
        fields(d, ['title', 'text', 'open_at', 'recipient', 'condition'], ['condition_confirmed', 'photo_png'])
        string(d['title'], 200); string(d['text'], 64000)
        if 'photo_png' in d:
            validate_photo_preview(d['photo_png'])
        d['open_at'] = instant(d['open_at']).isoformat()
        string(d['recipient'], 128)
        require(isinstance(d['condition'], str) and len(d['condition']) <= 2000)
        require(type(d.get('condition_confirmed', False)) is bool)
        require(d['recipient'] == 'self' or d['recipient'] in state.get('devices', {}), 'UNKNOWN_RECIPIENT')
    else:
        require(internal, 'READ_ONLY_RECORD', 403)
        fields(d, ['recipient_id', 'scope', 'condition', 'status', 'created_at', 'confirmed_at', 'released_at', 'reminders'])
        string(d['recipient_id'], 128)
        require(d['status'] in {'armed', 'pending', 'released', 'revoked'})
        fields(d['scope'], ['spaces', 'tags', 'entry_ids'])
        d['scope']['spaces'] = normalize_spaces(d['scope']['spaces'])
        require(isinstance(d['scope']['spaces'], list) and bool(d['scope']['spaces']) and
                set(d['scope']['spaces']) <= {'personal', 'life', 'work'})
        for key in ('tags', 'entry_ids'):
            require(isinstance(d['scope'][key], list) and len(d['scope'][key]) <= 1000)
            for item in d['scope'][key]: string(item, 128)
        c = d['condition']
        if c.get('type') == 'date':
            fields(c, ['type', 'at']); instant(c['at'])
        else:
            fields(c, ['type', 'days', 'waiting_days'])
            require(c['type'] == 'inactivity' and type(c['days']) is int and 31 <= c['days'] <= 36500
                    and type(c['waiting_days']) is int and 14 <= c['waiting_days'] <= 365)
        for key in ('created_at', 'confirmed_at', 'released_at'):
            if d[key] is not None: instant(d[key])
        require(isinstance(d['reminders'], list) and all(isinstance(v, str) for v in d['reminders']))


def text(record):
    d = record['data']
    if record['kind'] == 'document':
        return '\n\n'.join(p.get('text', '') for p in d.get('pages', []))
    return d.get('text') or d.get('body') or d.get('title', '')


def stamp(record):
    if record['kind'] == 'journal' and record['data'].get('occurred_at'):
        return record['data']['occurred_at']
    if record['kind'] == 'journal' and record['data'].get('date'):
        return record['data']['date'] + 'T00:00:00+00:00'
    if record['kind'] == 'event' and record['data'].get('start'):
        return record['data']['start']
    return record.get('occurred_at') or record['data'].get('origin', {}).get('occurred_at') or record['updated_at']


def eligible(state, record):
    if record['kind'] not in ORIGINAL_KINDS or record['space'] not in {'personal', 'life', 'work'}:
        return False
    if record['id'] in forgotten_history(state, [record['space']]):
        return False
    if any(record['id'] == ref['id'] for m in state['records'].values() if m['kind'] == 'memory'
           and m['data'].get('status') in {'suppressed', 'rejected'} for ref in m['data'].get('sources', [])):
        return False
    d = record['data']
    if record['kind'] == 'message':
        return record['mode'] == 'normal' and d.get('role') == 'user' and not d.get('origin')
    if record['kind'] == 'context':
        return d.get('layer') == 'archive' and d.get('sensitivity') != 'secret'
    if record['kind'] == 'memory':
        return d.get('status') not in {'suppressed', 'rejected', 'expired'} and bool(d.get('sources'))
    return True


def metadata(state, identifier) -> dict[str, Any]:
    note = state['records'].get('archive-note:' + identifier)
    return copy.deepcopy(note['data']) if note else dict(featured=False, visibility='private', reflection='', people=[], places=[], tags=[])


def sort_instant(value) -> datetime:
    """Real chronological order; unparsable legacy values sort as oldest rather than failing the archive."""
    try:
        return instant(value)
    except Problem:
        return datetime.min.replace(tzinfo=timezone.utc)


def entries(state, spaces, *, period='', start='', end='', person='', place='', tag='', featured=False,
            include_revisions=True):
    spaces = normalize_spaces(spaces)
    result = []
    for r in state['records'].values():
        if r['space'] not in spaces or not eligible(state, r):
            continue
        at = stamp(r); m = metadata(state, r['id']); d = r['data']
        tags = list(dict.fromkeys([*d.get('tags', []), *m['tags']]))
        if period and not at.startswith(period): continue
        if start and instant(at) < instant(start): continue
        if end and instant(at) >= instant(end): continue
        if person and person not in m['people']: continue
        if place and place not in m['places'] and place != d.get('location'): continue
        if tag and tag not in tags: continue
        if featured and not m['featured']: continue
        quotes = []
        if r['kind'] == 'memory':
            for ref in d['sources']:
                source = state['records'].get(ref['id'])
                if source and source['space'] in spaces and eligible(state, source) and source['revision'] == ref['revision']:
                    quotes.append(dict(id=source['id'], revision=source['revision'], at=stamp(source), text=text(source)))
            if not quotes: continue
        else:
            quotes.append(dict(id=r['id'], revision=r['revision'], at=at, text=text(r)))
        revisions = state.get('private_items', {}).get('archive_history', {}).get(r['id'], []) if include_revisions else []
        result.append(dict(id=r['id'], revision=r['revision'], kind=r['kind'], space=r['space'], at=at,
            title=d.get('title') or text(r)[:80], quotes=quotes, metadata=m, tags=tags,
            **({'mood': d['mood']} if r['kind'] == 'journal' and d.get('mood', 'unset') != 'unset' else {}),
            **({'about': copy.deepcopy(d['about'])} if r['kind'] == 'journal' and d.get('about') else {}),
            revisions=copy.deepcopy(revisions), sources=copy.deepcopy(d.get('sources', []))))
    return sorted(result, key=lambda e: (sort_instant(e['at']), e['id']), reverse=True)


def remember_revision(state, old):
    if not eligible(state, old): return
    history = state['private_items'].setdefault('archive_history', {}).setdefault(old['id'], [])
    if not any(r['revision'] == old['revision'] for r in history):
        history.append(dict(revision=old['revision'], at=stamp(old), revised_at=now(), text=text(old),
                            sources=copy.deepcopy(old['data'].get('sources', []))))


def forget_revision(state, identifiers):
    history = state.get('private_items', {}).get('archive_history', {})
    for identifier in identifiers:
        history.pop(identifier, None)
    for style in state.get('private_items', {}).get('archive_style', {}).values():
        style['examples'] = [e for e in style.get('examples', []) if e['id'] not in identifiers]


def put_note(state, actor, identifier, value):
    parent = state['records'].get(identifier)
    require(parent and parent['space'] in state['devices'][actor]['spaces'] and eligible(state, parent), 'NOT_FOUND', 404)
    key = 'archive-note:' + identifier; old = state['records'].get(key)
    record = dict(id=key, kind='archive_note', space=parent['space'], mode='source_only',
                  revision=old['revision'] + 1 if old else 1, updated_at=now(), data={**value, 'entry_id': identifier})
    validate(state, record)
    state['records'][key] = record
    return record


def letters(state, device, clock=None, actor=None):
    at = clock or datetime.now(timezone.utc)
    result = []
    for r in state['records'].values():
        if r['kind'] != 'letter': continue
        owns = r['space'] in device['spaces']
        addressed = actor is not None and r['data']['recipient'] == actor
        if not owns and not addressed: continue
        item = copy.deepcopy(r)
        sealed = at < instant(r['data']['open_at']) or bool(r['data']['condition'] and not r['data'].get('condition_confirmed'))
        item['sealed'] = sealed
        if sealed:
            item['data'].pop('text', None)
            item['data'].pop('photo_png', None)
        if not owns and sealed: continue
        result.append(item)
    return result


def due_letters(state, clock):
    changed = False
    delivered = state['private_items'].setdefault('archive_letter_deliveries', {})
    for r in list(state['records'].values()):
        if r['kind'] != 'letter' or r['id'] in delivered or instant(r['data']['open_at']) > clock: continue
        identifier = 'letter-reminder:' + r['id']
        # A phone-edited (done/dismissed) reminder or a deletion tombstone is the user's decision: never recreate it.
        if identifier not in state['records'] and identifier not in state.get('deleted', {}):
            state['records'][identifier] = dict(id=identifier, kind='reminder', space=r['space'], mode='normal',
                revision=1, updated_at=clock.isoformat(), data=dict(text='写给未来：有一封信到了约定日期，请到人生档案查看。',
                due_at=instant(r['data']['open_at']).isoformat(), status='pending'))
        delivered[r['id']] = clock.isoformat(); changed = True
    return changed


def router(vault):
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import Response
    from .api import identity
    from .request_body import buffer_body, sync_body
    api = APIRouter(prefix='/api/preview/archive', dependencies=[Depends(buffer_body)])

    @api.get('')
    def listing(request: Request, period: str = '', start: str = '', end: str = '', person: str = '', place: str = '', tag: str = '', featured: bool = False):
        with vault.view() as state:
            _, device = identity(state, request)
            return {'entries': entries(state, device['spaces'], period=period, start=start, end=end, person=person, place=place, tag=tag, featured=featured)}

    @api.put('/entries/{identifier}/note')
    def annotate(identifier: str, request: Request):
        with vault.transaction() as state:
            actor, _ = identity(state, request)
            return put_note(state, actor, identifier, sync_body(request))

    @api.get('/letters')
    def letter_list(request: Request):
        with vault.view() as state:
            actor, device = identity(state, request)
            return {'letters': letters(state, device, actor=actor)}

    @api.post('/export')
    def export(request: Request):
        from .archive_export import export_archive
        value = sync_body(request); fields(value, [], ['passphrase', 'period', 'visibility'])
        with vault.view() as state:
            _, device = identity(state, request)
            content = export_archive(state, device['spaces'], **value)
        encrypted = bool(value.get('passphrase'))
        return Response(content, media_type='application/octet-stream' if encrypted else 'application/zip',
                        headers={'Content-Disposition': 'attachment; filename="mellow-archive.' + ('encrypted.json' if encrypted else 'zip') + '"'})

    return api
