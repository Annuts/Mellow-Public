"""Opt-in discovery, quiet hours, evidence invalidation and bounded model planning."""
from datetime import datetime, timedelta, timezone
import hashlib
from zoneinfo import ZoneInfo
from .domain import Problem, dependent_ids, queue_job, require
from .features import memory_status
from .vault import DEFAULT_SUGGESTION_LIMIT, canonical, now
from .retrieval import chronology
from .features import DEFAULT_TIMEZONE

URGENT_KINDS = {'imminent_departure', 'cancellation', 'due_now', 'due_now_reminder'}
UPCOMING_AHEAD = timedelta(hours=36)
CATCHUP_BEHIND = timedelta(hours=12)
DEPARTURE_LEAD = timedelta(minutes=30)
# Lock-screen safe: nothing the user wrote ever travels in an urgent suggestion or the upcoming list.
GENERIC_REMINDER_TEXT = '打开小满查看提醒。'
GENERIC_REMINDER_REASON = '到点提醒'
GENERIC_DEPARTURE_TEXT = '打开小满查看出发提醒。'


def urgent(item):
    return bool(item and item.get('kind', item.get('type')) in URGENT_KINDS)


def learned_action(state, item, instant):
    from .spaces import normalize_space
    prefs = state['preferences']
    hour = datetime.fromisoformat(instant).astimezone(ZoneInfo(prefs.get('timezone', DEFAULT_TIMEZONE))).hour
    source = state.get('records', {}).get(item.get('source_id'), {})
    kind = item.get('item_type', item.get('kind', item.get('type',
        source.get('data', {}).get('category', source.get('kind')))))
    for rule in reversed(prefs.get('dreaming', {}).get('rules', [])):
        if rule['item_type'] != kind or normalize_space(rule.get('space')) != normalize_space(item.get('space')):
            continue
        window = rule.get('window')
        # Old implicit, unbounded silent rules must not keep an entire type off.
        expires = rule.get('expires_at')
        if expires and datetime.fromisoformat(expires) <= datetime.fromisoformat(instant):
            continue
        if rule['action'] == 'silent' and not expires:
            continue
        if window and not (window[0] <= hour < window[1] if window[0] < window[1]
                           else hour >= window[0] or hour < window[1]):
            continue
        return rule['action']
    return 'notify'


def parse_instant(value):
    """Aware UTC datetime, or None for anything that is not an offset-qualified ISO time."""
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def quiet_until(state, space, instant):
    """Earliest instant at which the conversation in a space counts as quiet again, else None."""
    from .spaces import normalize_space
    space = normalize_space(space)
    current = parse_instant(instant)
    if current is None:
        raise ValueError('invalid instant')
    quiet = timedelta(seconds=state['preferences'].get('conversation_quiet_seconds', 180))
    stamps = [parse_instant(chronology(r)) for r in state['records'].values()
              if r['kind'] == 'message' and normalize_space(r['space']) == space]
    latest = max((stamp for stamp in stamps if stamp), default=None)
    until = latest + quiet if latest else None
    return until if until and until > current else None


def conversation_quiet(state, space, instant):
    return quiet_until(state, space, instant) is None


def conversation_quiet_until(state, space, instant):
    """Absolute UTC ISO time when a busy conversation becomes quiet, or None when already quiet."""
    until = quiet_until(state, space, instant)
    return until.isoformat() if until else None


def reminder_due(record):
    """Due instant of a normal, pending reminder; anything else is not an exact reminder."""
    if record.get('kind') != 'reminder' or record.get('mode') != 'normal':
        return None
    data = record.get('data', {})
    return parse_instant(data.get('due_at')) if data.get('status') == 'pending' else None


def explicit_departure(record):
    """Use only the departure time explicitly saved on a timed event."""
    if record.get('kind') != 'event' or record.get('mode') != 'normal':
        return None
    data = record.get('data', {})
    return None if data.get('status') == 'cancelled' else parse_instant(data.get('departure_at'))


def record_actionable(record):
    """Source still worth interrupting for: pending reminders, uncancelled events."""
    if record['kind'] == 'reminder':
        return reminder_due(record) is not None
    if record['kind'] == 'event':
        return record.get('data', {}).get('status') != 'cancelled'
    return True


def upcoming_reminders(state, device, instant):
    """Pending normal reminders due in [now-12h, now+36h] for the device's spaces, densest day included."""
    from .spaces import normalize_space, normalize_spaces
    current = parse_instant(instant)
    if current is None:
        raise ValueError('invalid instant')
    start, end = current - CATCHUP_BEHIND, current + UPCOMING_AHEAD
    rows = []
    for record in state['records'].values():
        due = reminder_due(record)
        if due and normalize_space(record['space']) in normalize_spaces(device['spaces']) and start <= due <= end:
            rows.append((due, record['id'], record))
    rows.sort(key=lambda row: row[:2])
    return [dict(id=r['id'], revision=r['revision'], kind='reminder', space=normalize_space(r['space']), mode='normal',
                 updated_at=r['updated_at'],
                 data=dict(text=GENERIC_REMINDER_TEXT, due_at=r['data']['due_at'], status='pending'))
            for _, _, r in rows]


def suggestion_invalid(state, suggestion):
    source = state['records'].get(suggestion['source_id'])
    invalid = source is None or not record_actionable(source) or (source['kind'] == 'memory' and
        (memory_status(state, source) != 'active' or source['data'].get('goal_state', 'open') != 'open'))
    return invalid or any(state['records'].get(ref['id'], {}).get('revision') != ref['revision']
        or not record_actionable(state['records'][ref['id']])
        or (state['records'][ref['id']]['kind'] == 'memory' and memory_status(state, state['records'][ref['id']]) != 'active')
        for ref in suggestion.get('sources', []))


def urgent_plan(state, instant):
    """Pure planner: (new urgent suggestions by key, urgent suggestion keys that went stale).

    Urgent items are suggestions, so they stay behind the proactive switch; they ignore quiet hours,
    snooze and the optional daily quota by design. Keys carry the source revision and due time, so an
    edit or reschedule produces a fresh item and the old one is retired.
    """
    create, expire = {}, []
    if not state['preferences'].get('proactive'):
        return create, expire
    current = parse_instant(instant)
    if current is None:
        raise ValueError('invalid instant')
    for key, item in state['suggestions'].items():
        if urgent(item) and item['state'] in {'new', 'snoozed'} and (
                suggestion_invalid(state, item) or (parse_instant(item.get('expires_at')) or current) <= current):
            expire.append(key)

    def add(kind, record, at, expires, text, reason, label):
        key = f"{label}:{record['id']}:{record['revision']}:{at.isoformat()}"
        if key not in state['suggestions']:
            create[key] = dict(id=key, kind=kind, source_id=record['id'], space=record['space'], text=text,
                reason=reason, action='none', state='new', created_at=instant, expires_at=expires.isoformat(),
                sources=[{'id': record['id'], 'revision': record['revision']}],
                due_record=dict(id=record['id'], revision=record['revision'], at=at.isoformat()))

    for record in state['records'].values():
        due = reminder_due(record)
        # Flexible/elected check-ins are owned by the client's follow-up alarm
        # protocol and delivery windows, not by the hub's urgent fallback.
        if due and not record['id'].endswith('-check-in') and current - CATCHUP_BEHIND < due <= current:
            add('due_now_reminder', record, due, due + CATCHUP_BEHIND, GENERIC_REMINDER_TEXT,
                GENERIC_REMINDER_REASON, 'due')
        departure = explicit_departure(record)
        if departure and departure - DEPARTURE_LEAD <= current < departure:
            add('imminent_departure', record, departure, departure, GENERIC_DEPARTURE_TEXT,
                GENERIC_REMINDER_REASON, 'departure')
    return create, expire


def produce_urgent(state, instant=None):
    create, expire = urgent_plan(state, instant or now())
    for key in expire:
        state['suggestions'][key]['state'] = 'expired'
    state['suggestions'].update(create)
    state['seq'] += len(create)
    return bool(create or expire)


def pending_report(state, actor, space, instant):
    from .spaces import normalize_space
    space = normalize_space(space)
    """Deliverable suggestion ids (urgent first), creation times, and when a busy chat lets the rest go."""
    urgent_ids, normal_ids, waits = [], [], []
    until = quiet_until(state, space, instant)
    from .news import article_keys, discussed_news_keys
    discussed = discussed_news_keys(state, space)
    for item in state['suggestions'].values():
        claim = item.get('delivery_claim')
        if (item['state'] != 'new' or normalize_space(item['space']) != space or item.get('expires_at', '') <= instant
                or claim and not (claim.get('device') == actor and claim.get('retry_at', '~') <= instant)
                or not delivery_window_open(state, instant, item)):
            continue
        if item.get('news') and not discussed.isdisjoint(article_keys(item['news'])):
            continue
        if urgent(item):
            urgent_ids.append(item['id'])
        elif until:
            expires = parse_instant(item['expires_at'])
            if learned_action(state, item, instant) == 'notify' and expires and until < expires:
                waits.append(until)
        else:
            normal_ids.append(item['id'])
    ids = (urgent_ids + normal_ids)[:8]
    return dict(ids=ids, created_at={key: state['suggestions'][key].get('created_at') for key in ids},
                retry_after=min(waits).isoformat() if waits else None)


def day_key(value, preferences):
    from .usage import budget_day
    return budget_day({'preferences': preferences}, datetime.fromisoformat(value))


def expires_at():
    return (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()


def delivery_window_open(state, instant, item=None):
    instant = instant or now()
    prefs = state['preferences']
    if not prefs['proactive']:
        return False
    if urgent(item):
        return True
    if prefs.get('snooze_until', '') > instant:
        return False
    hour = datetime.fromisoformat(instant).astimezone(ZoneInfo(prefs.get('timezone', DEFAULT_TIMEZONE))).hour
    start, end = prefs.get('quiet_start', 22), prefs.get('quiet_end', 8)
    if start is not None and end is not None and start != end:
        if (start < end and start <= hour < end) or (start > end and (hour >= start or hour < end)):
            return False
    return True


def may_suggest(state, instant=None, item=None):
    instant = instant or now()
    if not delivery_window_open(state, instant, item):
        return False
    if urgent(item):
        return True
    prefs = state["preferences"]
    day = day_key(instant, prefs)
    return sum(not urgent(s) and day_key(s['created_at'], prefs) == day
               for s in state['suggestions'].values()) < prefs.get('daily_limit', DEFAULT_SUGGESTION_LIMIT)


def check_automatic_job(state, job, source):
    """Queued work must recheck the current policy before spending a model call."""
    instant, prefs = now(), state['preferences']
    require(not state.get('private_items', {}).get('thought_control', {}).get('owner'), 'THOUGHT_RUNTIME')
    require(prefs['proactive'] and prefs.get('model_proactive', False), 'PROACTIVE_DISABLED')
    if not job.get('dreaming'):
        require(may_suggest(state, instant), 'PROACTIVE_PAUSED')
        cutoff = datetime.fromisoformat(instant) - timedelta(days=1)
        # Both the server's freshness and the offline event time must be recent.
        # A recently synced old event is not a fresh reason to interrupt someone.
        require(min(datetime.fromisoformat(source['updated_at']),
                    datetime.fromisoformat(chronology(source))) > cutoff,
                'SOURCE_EXPIRED')
    attempts = sum(j['kind'] == 'proactive' and j['id'] != job['id'] and bool(j.get('started_at'))
                   and day_key(j['started_at'], prefs) == day_key(instant, prefs)
                   for j in state['jobs'].values())
    require(attempts < prefs.get('model_daily_limit', 3), 'PROACTIVE_BUDGET_EXCEEDED')
    blocked = {ref['id'] for record in state['records'].values() if record['kind'] == 'memory'
               and (record['data']['status'] in {'suppressed', 'rejected'} or
                    (record['data']['status'] == 'active' and
                     (memory_status(state, record) != 'active' or record['data'].get('goal_state', 'open') != 'open')))
               for ref in record['data']['sources']}
    require(source['id'] not in dependent_ids(state, blocked), 'SOURCE_INVALIDATED')


def discover(vault):
    with vault.transaction() as state:
        instant = now()
        # Capability opt-in has one generation owner. Old phones without grants
        # retain the legacy path; paired Thought clients must not get both paths.
        thought_owner = state.get('private_items', {}).get('thought_control', {}).get('owner')
        from .dreaming import schedule
        if not thought_owner:
            schedule(state, instant)
        if state.get('restore_preview', {}).get('expires_at', '~') <= instant:
            state.pop('restore_preview', None)
        for suggestion in state['suggestions'].values():
            if suggestion['state'] not in {'new', 'snoozed'}:
                continue
            if suggestion_invalid(state, suggestion) or suggestion.get('expires_at', '~') <= instant:
                suggestion['state'] = 'expired'
            elif suggestion['state'] == 'snoozed' and suggestion.get('snooze_until', '~') <= instant:
                suggestion['state'] = 'new'
        # Before the quota early-return: urgent due items must not wait behind optional-suggestion limits.
        produce_urgent(state, instant)
        if thought_owner:
            return
        if not may_suggest(state, instant):
            return
        for record in list(state['records'].values()):
            if not may_suggest(state, instant):
                break
            data = record['data']
            if record['kind'] != 'memory' or data.get('category') != 'goal' or data.get('goal_state', 'open') != 'open':
                continue
            if memory_status(state, record) != 'active':
                continue
            key = record['id'] + ':' + str(record['revision'])
            if learned_action(state, {'item_type':'goal', 'space':record['space']}, instant) != 'notify':
                continue
            if key not in state['suggestions']:
                state['suggestions'][key] = dict(id=key, source_id=record['id'], space=record['space'],
                    text='要不要为这个目标确定一个下一步？', reason=data['text'], action='checklist',
                    state='new', created_at=instant, expires_at=expires_at(),
                    sources=[{'id': record['id'], 'revision': record['revision']}])
                state['seq'] += 1
        config, prefs = state['model'], state['preferences']
        if not may_suggest(state, instant) or not prefs.get('model_proactive', False) or not config or 'proactive' not in config['roles']:
            return
        attempts = sum(j['kind'] == 'proactive' and day_key(j['created_at'], prefs) == day_key(instant, prefs)
                       for j in state['jobs'].values())
        if attempts >= prefs.get('model_daily_limit', 3):
            return
        blocked = {ref['id'] for r in state['records'].values() if r['kind'] == 'memory'
                   and (r['data']['status'] in {'suppressed', 'rejected'} or
                        (r['data']['status'] == 'active' and memory_status(state, r) != 'active')) for ref in r['data']['sources']}
        if blocked:
            blocked = dependent_ids(state, blocked)
        cutoff = (datetime.fromisoformat(instant) - timedelta(days=1)).isoformat()
        candidates = [r for r in state['records'].values() if r['kind'] == 'message' and r['data']['role'] == 'user'
                      and r['mode'] == 'normal' and r['space'] in config['spaces'] and r['updated_at'] > cutoff and r['id'] not in blocked]
        for source in sorted(candidates, key=lambda r: r['updated_at'], reverse=True)[:20]:
            if learned_action(state, {'item_type':'message', 'space':source['space']}, instant) != 'notify':
                continue
            operation = 'proactive:' + source['id'] + ':' + str(source['revision']) + ':' + day_key(instant, prefs)
            if 'job:' + operation in state['receipts']:
                continue
            device = next((key for key, value in state['devices'].items() if not value['revoked']
                           and value['expires_at'] > instant and source['space'] in value['spaces']), None)
            if device:
                try:
                    queue_job(state, device, dict(operation_id=operation, kind='proactive',
                              source_id=source['id'], source_revision=source['revision']))
                except Problem as error:
                    from .maintenance import log_event
                    log_event('PROACTIVE_SKIPPED', 'discover', error)
                break  # At most one pending candidate per scan; never a bulk surprise bill.

def claim_suggestion(state, key, actor):
    """Commit the sole delivery attempt before the client talks to its OS."""
    return claim_decision(state, key, actor)[0]


def claim_decision(state, key, actor):
    from .spaces import normalize_space, normalize_spaces
    """(claimed, retry_after): retry_after is an absolute UTC ISO time, set only for a transient chat pause."""
    item = state['suggestions'].get(key)
    instant = now()
    device = state['devices'][actor]
    if (not item or normalize_space(item['space']) not in normalize_spaces(device['spaces']) or item['state'] != 'new'
            or item.get('delivery_claim') or item.get('expires_at', '') <= instant
            or not delivery_window_open(state, instant, item)):
        return False, None
    wait = None
    if not urgent(item):
        if learned_action(state, item, instant) != 'notify':
            return False, None
        wait = quiet_until(state, item['space'], instant)
        prefs = state['preferences']; day = day_key(instant, prefs)
        delivered = [s for s in state['suggestions'].values() if not urgent(s) and s.get('delivery_claim')
                     and day_key(s['delivery_claim']['at'], prefs) == day]
        if len(delivered) >= prefs.get('daily_limit', DEFAULT_SUGGESTION_LIMIT):
            return False, None
        source = state['records'].get(item.get('source_id'))
        signature = hashlib.sha256(canonical(source.get('data', {}) if source else {})).hexdigest()
        if any(s.get('source_id') == item.get('source_id')
               and s['delivery_claim'].get('source_state') == signature for s in delivered):
            return False, None
    if item.get('news'):
        from .news import article_keys, discussed_news_keys, sale_headline
        if sale_headline(item['news']) or not discussed_news_keys(state, item['space']).isdisjoint(article_keys(item['news'])):
            return False, None
    refs = item.get('sources', [])
    if not refs or not any(ref['id'] == item['source_id'] for ref in refs):
        return False, None
    for ref in refs:
        record = state['records'].get(ref['id'])
        if (not record or normalize_space(record['space']) not in normalize_spaces(device['spaces']) or record['revision'] != ref['revision']
                or not record_actionable(record)
                or record['kind'] == 'memory' and (memory_status(state, record) != 'active'
                    or record['data'].get('goal_state', 'open') != 'open')):
            return False, None
    if wait:
        expires = parse_instant(item['expires_at'])
        return False, wait.isoformat() if expires and wait < expires else None
    source = state['records'][item['source_id']]
    item['delivery_claim'] = {'device': actor, 'at': instant,
        'source_state': hashlib.sha256(canonical(source.get('data', {}))).hexdigest()}
    return True, None
