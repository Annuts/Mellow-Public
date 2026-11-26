"""Numeric usage receipts; no prompts, answers or credentials enter this ledger."""
import copy
import asyncio
import hashlib
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import Callable
from typing import Any

from .domain import Problem, fields, require, string
from .vault import canonical, now, uid
from .features import DEFAULT_TIMEZONE

usage_observer: ContextVar[Callable[[Any], None] | None] = ContextVar('usage_observer', default=None)
TOKEN_LIMIT = 10**12
REFERENCE_PRICES = {
    'deepseek-flash':dict(currency='CNY', input_per_million='2', cached_input_per_million='0.04', output_per_million='8'),
    'deepseek-v4-pro':dict(currency='CNY', input_per_million='9', cached_input_per_million='0.30', output_per_million='27'),
}


def budget_day(state, instant=None):
    """User-local billing day, shared by HTTP callers and durable workers."""
    instant = instant or datetime.fromisoformat(now())
    return instant.astimezone(ZoneInfo(state.get('preferences', {}).get('timezone', DEFAULT_TIMEZONE))).date().isoformat()


def prices(state):
    return {**REFERENCE_PRICES, **state.get('usage_prices', {})}


def clean_usage(value):
    if not isinstance(value, dict):
        return None
    def count(v):
        return v if type(v) is int and 0 <= v <= TOKEN_LIMIT else None
    prompt = count(value.get('prompt_tokens', value.get('input_tokens')))
    output = count(value.get('completion_tokens', value.get('output_tokens')))
    if prompt is None or output is None:
        return None
    cached = count(value.get('prompt_cache_hit_tokens', value.get('cached_tokens',
        (value.get('prompt_tokens_details') or {}).get('cached_tokens', 0)
        if isinstance(value.get('prompt_tokens_details', {}), dict) else 0)))
    if cached is None or cached > prompt:
        return None
    return {'prompt_tokens':prompt, 'completion_tokens':output,
            'total_tokens':prompt + output, 'cached_tokens':cached}


def validate_prices(value):
    require(isinstance(value, dict) and len(value) <= 100)
    result = {}
    for model, price in value.items():
        string(model, 128)
        fields(price, ['currency','input_per_million','cached_input_per_million','output_per_million'])
        require(price['currency'] in {'CNY','USD'})
        row = {'currency':price['currency']}
        for key in ('input_per_million','cached_input_per_million','output_per_million'):
            try:
                require(type(price[key]) in {str, int, float})
                require(len(str(price[key])) <= 48)
                amount = Decimal(str(price[key]))
                exponent = amount.as_tuple().exponent
                require(amount.is_finite() and 0 <= amount <= 1000000 and isinstance(exponent, int) and exponent >= -12)
            except (InvalidOperation, ValueError):
                raise Problem('INVALID_ARGUMENT') from None
            row[key] = str(amount)
        result[model] = row
    return result


def cost(usage, price):
    if usage is None or not price:
        return None
    amount = ((usage['prompt_tokens'] - usage['cached_tokens']) * Decimal(price['input_per_million'])
        + usage['cached_tokens'] * Decimal(price['cached_input_per_million'])
        + usage['completion_tokens'] * Decimal(price['output_per_million'])) / Decimal(1000000)
    return {'currency':price['currency'], 'amount':format(amount, 'f')}


def observe(value):
    callback = usage_observer.get()
    if callback:
        callback(clean_usage(value))


async def tracked(vault, config, factory, *, kind, space, device_id, identifier=None):
    from .spaces import normalize_space
    space = normalize_space(space)
    identifier = identifier or uid()
    def start():
        with vault.transaction(bump_revision=False) as state:
            price = copy.deepcopy(prices(state).get(config['model']))
            state['usage_events'][identifier] = {'id':identifier, 'source':'center', 'device_id':device_id,
                'space':space, 'provider_host':urlsplit(config.get('base_url','')).hostname or '', 'model':config['model'], 'kind':kind, 'at':now(), 'status':'pending',
                'usage':None, 'price':price, 'cost':None}
            return price
    price = await asyncio.to_thread(start)
    observed = None
    def report(usage):
        nonlocal observed
        if usage is not None:
            observed = usage
    token = usage_observer.set(report)
    status = 'unknown'
    try:
        result = await factory()
        report(clean_usage(result[1]))
        status = 'completed'
        return result
    finally:
        usage_observer.reset(token)
        def finish():
            with vault.transaction(bump_revision=False) as state:
                state['usage_events'][identifier].update(status=status, usage=observed, cost=cost(observed, price))
        await asyncio.to_thread(finish)


def import_events(state, value, *, origin, source, spaces, device_id):
    from .spaces import normalize_space, normalize_spaces
    spaces = normalize_spaces(spaces)
    fields(value, ['events'])
    require(isinstance(value['events'], list) and len(value['events']) <= 100, 'USAGE_LIMIT')
    ledger = state['usage_events']
    for raw in value['events']:
        fields(raw, ['id','model','kind','at','status','usage'], ['space','price'])
        for key, limit in [('id',200),('model',128),('kind',60)]:
            string(raw[key], limit)
        require(raw['status'] in {'completed','unknown','failed','pending'})
        require(isinstance(raw['at'], str) and len(raw['at']) <= 50)
        try:
            stamp = datetime.fromisoformat(raw['at'].replace('Z','+00:00'))
            require(stamp.tzinfo is not None and stamp <= datetime.now(timezone.utc) + timedelta(minutes=5), 'INVALID_TIME')
        except (ValueError, TypeError):
            raise Problem('INVALID_TIME') from None
        space = normalize_space(raw.get('space', next(iter(spaces))))
        require(space in spaces, 'PERMISSION_REVOKED', 403)
        usage = clean_usage(raw['usage'])
        require(raw['usage'] is None or usage is not None, 'INVALID_USAGE')
        identifier = hashlib.sha256(canonical([origin,raw['id']])).hexdigest()
        old = ledger.get(identifier)
        if old:
            old['space'] = normalize_space(old['space'])
            # A source id is immutable, but an interrupted receipt may be completed later.
            require(all(old[k] == v for k,v in {'model':raw['model'],'kind':raw['kind'],'at':stamp.isoformat(),'space':space}.items()), 'OPERATION_REUSED', 409)
            if old['usage'] is not None:
                require(old['usage'] == usage, 'OPERATION_REUSED', 409)
                continue
        price = old.get('price') if old else None
        if not old:
            price = validate_prices({raw['model']:raw['price']})[raw['model']] if raw.get('price') else copy.deepcopy(prices(state).get(raw['model']))
        ledger[identifier] = {'id':identifier, 'external_id':raw['id'], 'source':source,
            'device_id':device_id, 'space':space, 'model':raw['model'], 'kind':raw['kind'],
            'at':stamp.isoformat(), 'status':raw['status'], 'usage':usage, 'price':price, 'cost':cost(usage,price)}
    return {'accepted':len(value['events'])}


def validate_archive(events):
    require(isinstance(events, dict) and len(events) <= 200000, 'INVALID_USAGE')
    allowed = {'id','provider_host','external_id','source','device_id','space','model','kind','at','status','usage','price','cost'}
    for key, e in events.items():
        require(isinstance(e,dict) and set(e) <= allowed and e.get('id') == key, 'INVALID_USAGE')
        for name in ('id','source','space','model','kind','at','status'):
            string(e.get(name),200)
        stamp = datetime.fromisoformat(e['at'].replace('Z','+00:00'))
        require(stamp.tzinfo is not None, 'INVALID_TIME')
        require(e['status'] in {'pending','completed','unknown','failed'}, 'INVALID_USAGE')
        usage = clean_usage(e.get('usage'))
        require(e.get('usage') is None or usage is not None, 'INVALID_USAGE')
        price = validate_prices({e['model']:e['price']})[e['model']] if e.get('price') else None
        e.update(usage=usage,price=price,cost=cost(usage,price))
        if e['status'] == 'pending': e['status']='unknown'


def summary(state, spaces, *, start='', end='', zone=DEFAULT_TIMEZONE):
    from .spaces import normalize_space, normalize_spaces
    spaces = normalize_spaces(spaces)
    try:
        tz = ZoneInfo(zone)
        for value in (start,end):
            if value:
                require(datetime.strptime(value, '%Y-%m-%d').strftime('%Y-%m-%d') == value, 'INVALID_TIME')
        require(not start or not end or start <= end, 'INVALID_TIME')
    except (ZoneInfoNotFoundError, ValueError):
        raise Problem('INVALID_TIME') from None
    events = list(state.get('usage_events',{}).values())
    # Older jobs have no receipts. Include their reported usage without inventing prices.
    for job in state['jobs'].values():
        if 'job:'+job['id'] not in state.get('usage_events',{}) and (job.get('usage') or job.get('started_at')) and not job.get('recall_empty'):
            events.append({'id':'legacy:'+job['id'], 'source':'center', 'space':job['space'],
                'model':'旧版本未记录模型', 'kind':job['kind'], 'at':job.get('finished_at',job.get('created_at',now())),
                'status':job['state'], 'usage':clean_usage(job['usage']), 'cost':None})
    def empty():
        return {'calls':0,'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,'cached_tokens':0,
            'missing_usage':0,'unpriced':0,'costs':{}}
    def add(row, event):
        row['calls'] += 1
        usage = event['usage']
        if usage:
            for k in ('prompt_tokens','completion_tokens','total_tokens','cached_tokens'):
                row[k] += usage[k]
            if not event.get('cost'): row['unpriced'] += 1
        else: row['missing_usage'] += 1
        if event.get('cost'):
            c = event['cost']
            row['costs'][c['currency']] = str(Decimal(row['costs'].get(c['currency'],'0')) + Decimal(c['amount']))
    totals, daily, models, sources, recent = empty(), {}, {}, {}, []
    center_deepseek = empty()
    for event in events:
        if normalize_space(event.get('space')) not in spaces: continue
        day = datetime.fromisoformat(event['at'].replace('Z','+00:00')).astimezone(tz).strftime('%Y-%m-%d')
        if start and day < start or end and day > end: continue
        if event['source'] == 'center' and (event.get('provider_host') == 'api.deepseek.com'
                or 'provider_host' not in event and event['model'].startswith('deepseek-')):
            add(center_deepseek,event)
        if event.get('kind') == 'local_project_summary' and event['source'] not in ('phone','center'):
            event = {**event, 'source':'local_summary'}
        for row in (totals,daily.setdefault(day,empty()),models.setdefault(event['model'],empty()),sources.setdefault(event['source'],empty())):
            add(row,event)
        recent.append(event)
    return {'center_deepseek':center_deepseek,'totals':totals,'daily':daily,'models':models,'sources':sources,
        'recent':sorted(recent,key=lambda e:e['at'],reverse=True)[:100],
        'prices':prices(state),'timezone':zone,'start':start,'end':end,
        'daily_calls_utc':state['budgets'],
        'reported_tokens':{k:totals[k] for k in ('prompt_tokens','completion_tokens','total_tokens')},
        'jobs_without_usage':totals['missing_usage'],'currency_cost':totals['costs'] or None,
        'note':'金额按调用时保存的单价估算，币种分别汇总；未返回用量、历史账单及订阅费无法自动补算。'}
