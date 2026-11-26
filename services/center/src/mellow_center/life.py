"""Personal life records and atomic kitchen actions. No external purchasing or health claims."""
from __future__ import annotations
import base64
import copy
import hashlib
import math
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .domain import fields, require, string, visible
from .features import DEFAULT_TIMEZONE, timestamp, normalize
from .vault import canonical, now, uid

KINDS = {'activity', 'pantry', 'recipe', 'meal', 'consumption', 'food_profile', 'journal', 'habit', 'habit_log',
         'event', 'wardrobe', 'outfit', 'attachment'}
# 很好 / 不错 / 一般 / 有点累 / 不太好; `unset` keeps plain diary entries mood-free.
FEELING_MOODS = {'unset', 'great', 'good', 'okay', 'tired', 'low'}
FEELING_ABOUT = {'task', 'reminder', 'followup', 'habit', 'event', 'activity'}
FEELING_LABELS = {'great': '很好', 'good': '不错', 'okay': '一般', 'tired': '有点累', 'low': '不太好'}
UNITS = {'g': ('mass', 1), 'kg': ('mass', 1000), 'ml': ('volume', 1), 'l': ('volume', 1000),
         '个': ('count', 1), '份': ('serving', 1), '包': ('pack', 1)}


def number(value, low: float = 0, high: float = 1000000):
    require(type(value) in {int, float} and math.isfinite(value) and low <= value <= high, 'INVALID_QUANTITY')
    return Decimal(str(value))


def day(value):
    string(value, 10)
    try: require(date.fromisoformat(value).isoformat() == value, 'INVALID_DATE')
    except ValueError: require(False, 'INVALID_DATE')
    return value


def strings(value, maximum=50):
    require(isinstance(value, list) and len(value) <= maximum)
    for text in value: string(text, 200)


def references(value):
    require(isinstance(value, list) and len(value) <= 1024)
    for ref in value:
        fields(ref, ['id', 'revision']); string(ref['id'], 128)
        require(type(ref['revision']) is int and ref['revision'] > 0)


def inventory_batches(value, unit):
    require(isinstance(value, list) and len(value) <= 50)
    identifiers = set()
    total = Decimal(0)
    for batch in value:
        fields(batch, ['id', 'quantity', 'unit'], ['purchased_on', 'expires_on', 'source', 'price', 'note', 'photo_ref'])
        string(batch['id'], 128)
        require(batch['id'] not in identifiers)
        identifiers.add(batch['id'])
        quantity = number(batch['quantity'])
        require(isinstance(batch['unit'], str) and batch['unit'] in UNITS)
        require(UNITS[batch['unit']][0] == UNITS[unit][0], 'INCOMPATIBLE_UNIT')
        total += quantity * Decimal(UNITS[batch['unit']][1]) / Decimal(UNITS[unit][1])
        for key in ('purchased_on', 'expires_on'):
            if key in batch: day(batch[key])
        for key, limit in [('source', 200), ('note', 2000), ('photo_ref', 256)]:
            if key in batch: string(batch[key], limit)
        if 'price' in batch: number(batch['price'])
    number(float(total))
    return float(total)


def owner_timezone(state=None):
    """Owner preferences govern life dates; backup schedules have their own zone."""
    preferences = (state or {}).get('preferences')
    zone = preferences.get('timezone') if isinstance(preferences, dict) else None
    try:
        return ZoneInfo(zone) if isinstance(zone, str) and zone else ZoneInfo(DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def derive_inventory(data, today=None, *, state=None):
    if 'batches' not in data: return
    today = today or datetime.now(owner_timezone(state)).date().isoformat()
    dates = [b['expires_on'] for b in data['batches'] if (b['quantity'] > 0 or data['certainty'] == 'unknown') and b.get('expires_on', '') >= today]
    if dates: data['expires_on'] = min(dates)
    else: data.pop('expires_on', None)


def validate_life(state, record, internal=False):
    kind, data = record['kind'], record['data']
    require(record['mode'] == ('source_only' if kind == 'attachment' else 'normal'))
    if kind == 'pantry':
        fields(data, ['text', 'quantity', 'unit', 'location', 'certainty'], ['check_on', 'confirmed_at', 'expires_on', 'batches'])
        string(data['text'], 200); number(data['quantity']); require(isinstance(data['unit'], str) and data['unit'] in UNITS)
        string(data['location'], 100); require(data['certainty'] in {'confirmed', 'approximate', 'unknown'})
        if data.get('check_on'): day(data['check_on'])
        if 'expires_on' in data: day(data['expires_on'])
        if 'batches' in data:
            total = inventory_batches(data['batches'], data['unit'])
            require(abs(total - data['quantity']) < 0.000001, 'BATCH_QUANTITY_MISMATCH')
            data['quantity'] = total
            derive_inventory(data, state=state)
        if data.get('confirmed_at'): data['confirmed_at'] = timestamp(data['confirmed_at'])
        require(data['certainty'] != 'confirmed' or data.get('confirmed_at'), 'CONFIRMATION_REQUIRED')
    elif kind == 'recipe':
        fields(data, ['text', 'servings', 'minutes', 'ingredients', 'steps', 'equipment', 'verified'], ['source_url', 'feedback'])
        string(data['text'], 200); number(data['servings'], 0.1, 100); number(data['minutes'], 1, 1440)
        require(type(data['verified']) is bool)
        require(isinstance(data['ingredients'], list) and 1 <= len(data['ingredients']) <= 100)
        for item in data['ingredients']:
            fields(item, ['name', 'quantity', 'unit']); string(item['name'], 200)
            number(item['quantity'], 0.001); require(item['unit'] in UNITS)
        strings(data['steps'], 100); strings(data['equipment'])
        require(len(data['steps']) > 0)
        if data.get('source_url'):
            from urllib.parse import urlsplit
            url = urlsplit(string(data['source_url'], 2048))
            require(url.scheme == 'https' and url.hostname and not url.username and not url.password)
        require(data.get('feedback', 'none') in {'none', 'liked', 'too_slow', 'avoid'})
    elif kind == 'food_profile':
        fields(data, ['text', 'excluded', 'equipment', 'constraints_confirmed'])
        string(data['text'], 200); strings(data['excluded']); strings(data['equipment'])
        require(type(data['constraints_confirmed']) is bool)
    elif kind == 'meal':
        fields(data, ['text', 'recipe_id', 'recipe_revision', 'servings', 'date', 'status'], ['sources', 'recipe_signature'])
        string(data['text'], 200); string(data['recipe_id'], 128); day(data['date']); number(data['servings'], 0.1, 100)
        require(type(data['recipe_revision']) is int and data['recipe_revision'] > 0)
        require(data['status'] in {'planned', 'cooked', 'cancelled'})
        if 'recipe_signature' in data:
            require(isinstance(data['recipe_signature'], str) and len(data['recipe_signature']) == 64)
        if not internal:
            require(data['status'] in {'planned', 'cancelled'}, 'USE_KITCHEN_ACTION')
            recipe = state['records'].get(data['recipe_id'])
            require(recipe and recipe['kind'] == 'recipe' and recipe['space'] == record['space'] and
                    (data['status'] == 'cancelled' or recipe['revision'] == data['recipe_revision']), 'SOURCE_INVALIDATED', 409)
            data['sources'] = [{'id': recipe['id'], 'revision': data['recipe_revision']}]
            if data['status'] == 'planned': data['recipe_signature'] = recipe_signature(recipe)
        if 'sources' in data: references(data['sources'])
    elif kind == 'consumption':
        require(internal, 'READ_ONLY_RECORD', 403)
        fields(data, ['text', 'meal_id', 'changes', 'status', 'sources'])
        string(data['text'], 200); string(data['meal_id'], 128); references(data['sources'])
        require(data['status'] in {'applied', 'undone', 'archived'})
        require(isinstance(data['changes'], list) and len(data['changes']) <= 1000)
        for change in data['changes']:
            fields(change, ['id', 'before', 'after', 'after_revision'], ['before_batches', 'unit'])
            string(change['id'], 128); number(change['before']); number(change['after'])
            require(type(change['after_revision']) is int and change['after_revision'] > 0)
            if 'before_batches' in change:
                require(change.get('unit') in UNITS)
                inventory_batches(change['before_batches'], change['unit'])
    elif kind == 'journal':
        fields(data, ['text', 'date', 'mood', 'tags'], ['about', 'occurred_at'])
        string(data['text'], 16000); day(data['date']); strings(data['tags'], 20)
        require(data['mood'] in FEELING_MOODS)
        if 'about' in data:  # a feeling about one thing the person did or attended
            about = data['about']; fields(about, ['kind', 'id', 'title'])
            require(about['kind'] in FEELING_ABOUT); string(about['id'], 128); string(about['title'], 200)
        if 'occurred_at' in data: data['occurred_at'] = timestamp(data['occurred_at'])
    elif kind == 'activity':
        fields(data, ['text', 'date', 'category', 'amount'])
        string(data['text'], 200); day(data['date'])
        require(data['category'] in {'exercise', 'water'})
        require(type(data['amount']) is int and 1 <= data['amount'] <=
                (1440 if data['category'] == 'exercise' else 10000), 'INVALID_QUANTITY')
    elif kind == 'habit':
        fields(data, ['text', 'weekdays', 'status'])
        string(data['text'], 200); require(data['status'] in {'active', 'paused', 'done'})
        require(isinstance(data['weekdays'], list) and 1 <= len(data['weekdays']) <= 7 and
                all(type(x) is int and 1 <= x <= 7 for x in data['weekdays']) and len(set(data['weekdays'])) == len(data['weekdays']))
    elif kind == 'habit_log':
        fields(data, ['text', 'habit_id', 'date', 'done'], ['sources'])
        string(data['text'], 200); string(data['habit_id'], 128); day(data['date']); require(type(data['done']) is bool)
        require(record['id'] == 'habit-log-' + hashlib.sha256((data['habit_id'] + ':' + data['date']).encode()).hexdigest(), 'INVALID_LOG_ID')
        parent = state['records'].get(data['habit_id'])
        require(parent and parent['kind'] == 'habit' and parent['space'] == record['space'], 'SOURCE_INVALIDATED', 409)
        if not internal: data['sources'] = [{'id': parent['id'], 'revision': parent['revision']}]
        if 'sources' in data: references(data['sources'])
    elif kind == 'event':
        fields(data, ['text', 'start', 'end', 'all_day', 'location', 'status'], ['origin', 'departure_at', 'start_only'])
        if 'start_only' in data:
            require(type(data['start_only']) is bool)
        string(data['text'], 500); require(isinstance(data['location'], str) and len(data['location']) <= 500)
        data['start'], data['end'] = timestamp(data['start']), timestamp(data['end'])
        require(data['start'] < data['end'], 'INVALID_TIME_WINDOW')
        if 'departure_at' in data:
            data['departure_at'] = timestamp(data['departure_at'])
            require(data['departure_at'] <= data['start'] and not data['all_day'], 'INVALID_TIME_WINDOW')
        require(type(data['all_day']) is bool and data['status'] in {'confirmed', 'tentative', 'cancelled'})
        if 'origin' in data: string(data['origin'], 256)
    elif kind == 'wardrobe':
        fields(data, ['text', 'category', 'color', 'seasons', 'status'])
        string(data['text'], 200); string(data['color'], 100)
        require(data['category'] in {'top', 'bottom', 'outer', 'shoes', 'accessory'})
        require(data['status'] in {'ready', 'washing', 'stored'})
        require(isinstance(data['seasons'], list) and set(data['seasons']) <= {'spring', 'summer', 'autumn', 'winter'})
    elif kind == 'outfit':
        fields(data, ['text', 'date', 'items', 'sources'])
        string(data['text'], 500); day(data['date']); strings(data['items']); references(data['sources'])
        for item_id in data['items']:
            item = state['records'].get(item_id)
            require(item and item['kind'] == 'wardrobe' and item['space'] == record['space'], 'SOURCE_INVALIDATED', 409)
        if not internal:
            data['sources'] = [{'id': item_id, 'revision': state['records'][item_id]['revision']} for item_id in data['items']]
    elif kind == 'attachment':
        fields(data, ['text', 'mime', 'content', 'sha256'])
        string(data['text'], 200); require(data['mime'] in {'text/plain', 'application/pdf', 'image/png', 'image/jpeg', 'audio/mpeg', 'audio/mp4'})
        require(isinstance(data['content'], str) and len(data['content']) <= 1400000)
        raw = base64.b64decode(data['content'], validate=True)
        require(0 < len(raw) <= 1024 * 1024, 'ATTACHMENT_TOO_LARGE')
        require(hashlib.sha256(raw).hexdigest() == data['sha256'], 'CONTENT_HASH_MISMATCH')


def get(state, device, identifier, kind=None):
    record = state['records'].get(identifier)
    require(record and visible(record, device) and (kind is None or record['kind'] == kind), 'NOT_FOUND', 404)
    return record


def profile(state, space):
    profiles = [r['data'] for r in state['records'].values() if r['kind'] == 'food_profile' and r['space'] == space]
    # Multiple devices may have independent constraints. Combine, never silently relax.
    return {'excluded': {normalize(x) for p in profiles for x in p['excluded']},
            'equipment': set.intersection(*(set(p['equipment']) for p in profiles)) if profiles else set(),
            'confirmed': bool(profiles) and all(p['constraints_confirmed'] for p in profiles)}


def recipe_allowed(recipe, prefs):
    data = recipe['data']
    return (prefs['confirmed'] and data['verified'] and data.get('feedback') != 'avoid'
            and set(data['equipment']) <= prefs['equipment']
            and not any(word in normalize(item['name']) for word in prefs['excluded'] for item in data['ingredients']))


def recipe_signature(recipe):
    # Preference feedback does not alter a planned meal's execution conditions.
    return hashlib.sha256(canonical({key: value for key, value in recipe['data'].items() if key != 'feedback'})).hexdigest()


def stock_confirmed(data, day_value, *, state=None):
    return (data['certainty'] == 'confirmed' and bool(data.get('confirmed_at')) and
            datetime.fromisoformat(data['confirmed_at']).astimezone(owner_timezone(state)).date().isoformat() == day_value and
            not (data.get('check_on') and data['check_on'] <= day_value))


def kitchen_preview(state, device, meal_id) -> dict[str, Any]:
    meal = get(state, device, meal_id, 'meal'); data = meal['data']
    require(data['status'] == 'planned', 'MEAL_NOT_PLANNED', 409)
    recipe = get(state, device, data['recipe_id'], 'recipe')
    require(recipe['space'] == meal['space'] and (data['recipe_signature'] == recipe_signature(recipe) if 'recipe_signature' in data
            else recipe['revision'] == data['recipe_revision']), 'SOURCE_INVALIDATED', 409)
    prefs = profile(state, meal['space'])
    require(recipe_allowed(recipe, prefs), 'RECIPE_CONSTRAINTS_UNCONFIRMED', 409)
    today = datetime.now(owner_timezone(state)).date().isoformat()
    batches = [r for r in state['records'].values() if r['kind'] == 'pantry' and r['space'] == meal['space']]
    lots = [(r, b) for r in batches for b in r['data'].get('batches', [r['data']])]
    lots.sort(key=lambda pair: (pair[1].get('expires_on') or '9999', pair[1].get('purchased_on') or '9999',
                               pair[0]['id'], pair[1].get('id', '')))
    available = {(r['id'], b.get('id')): number(b['quantity']) for r, b in lots}
    allocations, missing = {}, []
    factor = number(data['servings']) / number(recipe['data']['servings'])
    for ingredient in recipe['data']['ingredients']:
        required = number(ingredient['quantity']) * factor
        dimension, unit_factor = UNITS[ingredient['unit']]
        for batch, lot in lots:
            b = batch['data']; batch_dimension, batch_factor = UNITS[lot['unit']]
            if (normalize(b['text']) != normalize(ingredient['name']) or batch_dimension != dimension or
                 not stock_confirmed(b, today, state=state) or (lot.get('expires_on') and lot['expires_on'] < today)):
                continue
            key = (batch['id'], lot.get('id'))
            take = min(available[key], required * Decimal(unit_factor) / Decimal(batch_factor))
            if take <= 0: continue
            available[key] -= take
            required -= take * Decimal(batch_factor) / Decimal(unit_factor)
            entry = allocations.setdefault(batch['id'], {'id': batch['id'], 'revision': batch['revision'],
                'text': b['text'], 'unit': b['unit'], 'quantity': Decimal(0)})
            entry['quantity'] += take * Decimal(batch_factor) / Decimal(UNITS[b['unit']][1])
            if 'batches' in b:
                takes = entry.setdefault('batches', {})
                takes[lot['id']] = takes.get(lot['id'], 0) + float(take)
            if required <= 0: break
        if required > 0:
            missing.append({'name': ingredient['name'], 'quantity': float(required), 'unit': ingredient['unit']})
    plan = {'meal_id': meal_id, 'meal_revision': meal['revision'], 'recipe_revision': recipe['revision'],
            'profile_revisions': sorted((r['id'], r['revision']) for r in state['records'].values() if r['kind'] == 'food_profile' and r['space'] == meal['space']),
            'allocations': [{**a, 'quantity': float(a['quantity'])} for a in allocations.values()], 'missing': missing, 'date': today}
    return {**plan, 'confirmation_hash': hashlib.sha256(canonical(plan)).hexdigest()}


def put_internal(state, record):
    from .domain import validate_record, invalidate_dependents
    old = state['records'].get(record['id'])
    if old: invalidate_dependents(state, record['id'])
    validate_record(state, record, internal=True)
    state['records'][record['id']] = record
    state['seq'] += 1


def kitchen_action(state, device, payload):
    fields(payload, ['operation_id', 'action'], ['meal_id', 'confirmation_hash', 'consumption_id'])
    operation = string(payload['operation_id'], 128); require(payload['action'] in {'cook', 'shopping', 'undo'})
    key = 'kitchen:' + operation; hashed = hashlib.sha256(canonical(payload)).hexdigest()
    previous = state['receipts'].get(key)
    if previous:
        require(previous['space'] in device['spaces'], 'PERMISSION_REVOKED', 403)
        require(previous['hash'] == hashed, 'IDEMPOTENCY_MISMATCH', 409)
        return previous['result']
    if payload['action'] == 'undo':
        log = get(state, device, payload.get('consumption_id'), 'consumption')
        require(log['data']['status'] != 'archived', 'ARCHIVED_RECORD', 409)
        require(log['data']['status'] == 'applied', 'ALREADY_UNDONE', 409)
        meal = get(state, device, log['data']['meal_id'], 'meal')
        require(meal['data']['status'] == 'cooked', 'UNDO_CONFLICT', 409)
        for ref in log['data']['sources']:
            require(state['records'].get(ref['id'], {}).get('revision') == ref['revision'], 'UNDO_CONFLICT', 409)
        for change in log['data']['changes']:
            batch = copy.deepcopy(get(state, device, change['id'], 'pantry'))
            batch['data']['quantity'] = change['before']; batch['revision'] += 1; batch['updated_at'] = now()
            if 'before_batches' in change: batch['data']['batches'] = copy.deepcopy(change['before_batches'])
            put_internal(state, batch)
        meal = copy.deepcopy(meal); meal['revision'] += 1; meal['updated_at'] = now(); meal['data']['status'] = 'planned'
        put_internal(state, meal)
        log = copy.deepcopy(log); log['revision'] += 1; log['updated_at'] = now(); log['data']['status'] = 'undone'
        put_internal(state, log)
        result, space = {'undone': log['id']}, log['space']
    else:
        plan = kitchen_preview(state, device, payload.get('meal_id'))
        require(payload.get('confirmation_hash') == plan['confirmation_hash'], 'PREVIEW_STALE', 409)
        meal = copy.deepcopy(get(state, device, plan['meal_id'], 'meal')); space = meal['space']
        if payload['action'] == 'shopping':
            require(plan['missing'], 'NOTHING_TO_BUY', 409)
            record_id = uid()
            record = dict(id=record_id, kind='list', space=space, mode='normal', revision=1, updated_at=now(), data={
                'text': '采购 · ' + meal['data']['text'], 'purpose': 'shopping',
                'items': [{'id': uid(), 'text': f"{x['name']} {x['quantity']:g} {x['unit']}（库存不足或待核对）", 'done': False} for x in plan['missing']],
                'sources': [{'id': meal['id'], 'revision': meal['revision']}]})
            put_internal(state, record); result = {'list_id': record_id}
        else:
            require(not plan['missing'], 'INSUFFICIENT_CONFIRMED_STOCK', 409)
            changes, sources = [], []
            for allocation in plan['allocations']:
                batch = copy.deepcopy(get(state, device, allocation['id'], 'pantry'))
                before = batch['data']['quantity']; after = max(0, float(number(before) - number(allocation['quantity'])))
                batch_change = {}
                if 'batches' in batch['data']:
                    batch_change = {'before_batches': copy.deepcopy(batch['data']['batches']), 'unit': batch['data']['unit']}
                    for lot in batch['data']['batches']:
                        lot['quantity'] = max(0, float(number(lot['quantity']) - number(allocation['batches'].get(lot['id'], 0))))
                batch['data']['quantity'] = after; batch['revision'] += 1; batch['updated_at'] = now()
                put_internal(state, batch)
                changes.append({'id': batch['id'], 'before': before, 'after': after, 'after_revision': batch['revision'], **batch_change})
                sources.append({'id': batch['id'], 'revision': batch['revision']})
            meal['data']['status'] = 'cooked'; meal['revision'] += 1; meal['updated_at'] = now(); put_internal(state, meal)
            sources.append({'id': meal['id'], 'revision': meal['revision']})
            record_id = uid()
            put_internal(state, dict(id=record_id, kind='consumption', space=space, mode='normal', revision=1,
                updated_at=now(), data={'text': '已确认做过 · ' + meal['data']['text'], 'meal_id': meal['id'],
                'changes': changes, 'status': 'applied', 'sources': sources}))
            result = {'consumption_id': record_id, 'meal_id': meal['id']}
    state['receipts'][key] = {'hash': hashed, 'space': space, 'result': result}
    return result


def life_dashboard(state, device, space, day_value):
    from .spaces import normalize_space, normalize_spaces
    space = normalize_space(space)
    device = {**device, 'spaces': normalize_spaces(device['spaces'])}
    require(space in device['spaces'], 'PERMISSION_REVOKED', 403); day(day_value)
    records = [copy.deepcopy(r) for r in state['records'].values() if r['space'] == space]
    prefs = profile(state, space)
    recipes = [r for r in records if r['kind'] == 'recipe' and recipe_allowed(r, prefs)]
    recipes.sort(key=lambda r: (r['data'].get('feedback') != 'liked', r['data'].get('feedback') == 'too_slow', r['data']['minutes']))
    logs = [r for r in records if r['kind'] == 'habit_log' and r['data']['date'] == day_value and r['data']['done']]
    day_start = datetime.fromisoformat(day_value).replace(tzinfo=owner_timezone(state))
    day_end = day_start + timedelta(days=1)
    return {'date': day_value, 'features': ['pantry_expires_on', 'inventory_batches'],
            'recipes': recipes[:10], 'constraints_confirmed': prefs['confirmed'],
            'stock_to_check': [r for r in records if r['kind'] == 'pantry' and
                              not stock_confirmed(r['data'], day_value, state=state)],
            'habit_done_ids': [r['data']['habit_id'] for r in logs],
            'events': [r for r in records if r['kind'] == 'event' and r['data']['status'] != 'cancelled' and
                       datetime.fromisoformat(r['data']['start']) < day_end and datetime.fromisoformat(r['data']['end']) > day_start],
            'counts': {kind: sum(r['kind'] == kind for r in records) for kind in KINDS}}


def feelings_context(state, space, *, days=14, limit=5, instant=None):
    """Recent feelings the person recorded about their own day, as short reference lines for chat.

    Plain data, not instructions: moods are the person's own words, never a diagnosis.
    """
    from .domain import forgotten_history
    from .spaces import normalize_space
    cutoff = (instant or datetime.now(timezone.utc)) - timedelta(days=days)
    forgotten = forgotten_history(state, [space])
    rows = []
    for r in state['records'].values():
        d = r['data']
        if (r['kind'] != 'journal' or r['mode'] != 'normal' or normalize_space(r['space']) != normalize_space(space)
                or r['id'] in forgotten or d.get('mood') not in FEELING_LABELS):
            continue
        try: at = datetime.fromisoformat((d.get('occurred_at') or d['date'] + 'T00:00:00+00:00').replace('Z', '+00:00'))
        except ValueError: continue
        if at >= cutoff: rows.append((at, r))
    lines = []
    for at, r in sorted(rows, key=lambda item: item[0], reverse=True)[:limit]:
        d = r['data']; about = d.get('about')
        subject = f"关于「{about['title'][:40]}」" if about else '当天'
        note = '' if d['text'].endswith('感觉' + FEELING_LABELS[d['mood']] + '。') else d['text'][:120]
        lines.append(f"用户自己记下的感受 {at.astimezone(owner_timezone(state)).date().isoformat()}：{subject}感觉{FEELING_LABELS[d['mood']]}" + (f"，原话：{note}" if note else ''))
    return lines
