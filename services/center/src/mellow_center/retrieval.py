"""Privacy-filtered lexical/semantic RRF retrieval, with offline lexical fallback."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import math
import re
from zoneinfo import ZoneInfo
from .domain import dependent_ids, forgotten_history
from .features import DEFAULT_TIMEZONE, usable_memory, normalize
from .search_text import record_text as record_text

RULES = json.loads(files('mellow_center').joinpath('chat-rules.json').read_text())
STOP = set(RULES['stop'])
STOP.update(term[i:i+2] for term in RULES['stop'] if re.fullmatch(r'[\u3400-\u9fff]+', term)
            for i in range(len(term)-1))
CHAT_FILLER = set(RULES['chat_filler'])
FOLLOW_UP = re.compile(RULES['follow_up'], re.IGNORECASE)
PAST_PERIODS = (('前天', '前日'), ('昨天', '昨日'), ('上周', '上星期', '上礼拜'),
                ('上月', '上个月'), ('去年', '上年'), ('前年',))


def tokenize(text):
    value = normalize(text)
    result = re.findall(r'[a-z0-9]+', value)
    for run in re.findall(r'[\u3400-\u9fff]+', value):
        result.extend(run[i:i+2] for i in range(len(run)-1))
    return [term for term in result if term not in STOP]


def tokens(text):
    return set(tokenize(text))


def chat_tokens(text):
    return tokens(text) - CHAT_FILLER


def follows_previous_turn(text):
    return bool(FOLLOW_UP.match(text.strip()))


def standalone_chat(text):
    return bool(re.fullmatch(RULES['standalone'], text.strip(), re.IGNORECASE)
                and not re.search(RULES['personal_reference'], text) and not follows_previous_turn(text))


def chronology(record):
    return record.get('occurred_at') or record['updated_at']


def temporal_window(query, preferences=None, instant=None):
    local = (instant or datetime.now(timezone.utc)).astimezone(
        ZoneInfo((preferences or {}).get('timezone', DEFAULT_TIMEZONE)))
    today = local.replace(hour=0, minute=0, second=0, microsecond=0)
    for index, spellings in enumerate(PAST_PERIODS):
        if not any(word in query for word in spellings):
            continue
        if index < 2:
            start = today - timedelta(days=2-index); end = start + timedelta(days=1)
        elif index == 2:
            end = today - timedelta(days=today.weekday()); start = end - timedelta(days=7)
        elif index == 3:
            end = today.replace(day=1); start = (end - timedelta(days=1)).replace(day=1)
        else:
            start = today.replace(year=today.year-(1 if index == 4 else 2), month=1, day=1)
            end = start.replace(year=start.year+1)
        return start, end, spellings
    return None


def rank_records(records, query, *, preferences=None, instant=None, temporal=True, chat=False, broad=False,
                 semantic=None, semantic_cached_only=False):
    records = list(records)
    instant = instant or datetime.now(timezone.utc)
    value = normalize(query).strip()
    window = temporal_window(value, preferences, instant) if temporal else None
    lexical_query = value
    if window:
        for word in window[2]:
            lexical_query = lexical_query.replace(word, ' ')
        # A period overview has no topic: Chinese bigrams such as “说了”
        # must not become mandatory keywords for all records in that period.
        if re.fullmatch(r'\s*(?:我(?:们)?)?(?:(?:做|说|发生)了?(?:什么|啥)|有(?:什么|哪些)(?:事|事情)?|的?(?:记录|事情|回顾))?[？?。!！\s]*', lexical_query):
            lexical_query = ''
    requested = chat_tokens(lexical_query) if chat else tokens(lexical_query)
    core = re.sub(r'我|你|现在|哪里|哪儿|什么|怎么|多少|[？?。!！\s]', '', lexical_query)
    texts = [normalize(record_text(r)) for r in records]
    counts = [Counter(t for t in tokenize(text) if not chat or t not in CHAT_FILLER) for text in texts]
    frequencies = Counter(t for group in counts for t in group)
    average = sum(sum(group.values()) for group in counts) / max(1, len(records)) or 1
    candidates = []
    # Apply time constraints before even scoring a vector. Other privacy filters
    # are enforced by callers on the candidate records, identically for branches.
    allowed = []
    for record, text in zip(records, texts):
        if not window or matches_window(record, text, value, window, preferences):
            allowed.append(record)
    semantic_scores = semantic.scores(allowed, query, cache_only=semantic_cached_only) if semantic else {}
    for record, text, group in zip(records, texts, counts):
        shared = requested & group.keys()
        exact = bool(lexical_query.strip() and lexical_query.strip() in text)
        single = (len(value) == 1 and value in text) or (len(core) == 1 and core in text)
        in_window = False
        if window:
            stamp = datetime.fromisoformat(chronology(record))
            in_window = window[0] <= stamp < window[1]
            if not matches_window(record, text, value, window, preferences):
                continue
        coverage = len(shared) / max(1, len(requested))
        similarity = semantic_scores.get(record['id'], 0)
        if window and requested and not single and len(shared) < 2 and coverage < 0.25 and not similarity:
            continue
        rare = any(frequencies[t] <= max(1, len(records)*0.2) for t in shared)
        if not (single or exact or shared and (broad or len(shared) >= 2 or rare or coverage >= 0.5)):
            if not similarity and not (window and in_window and not requested):
                continue
        bm25 = 0.0
        for term in shared:
            idf = math.log(1 + (len(records)-frequencies[term]+0.5)/(frequencies[term]+0.5))
            tf = group[term]
            bm25 += idf * tf * 2.2 / (tf + 1.2*(0.25+0.75*sum(group.values())/average))
        title = normalize(' '.join(str(record['data'].get(k, '')) for k in ('title', 'key', 'name')))
        title_score = len(requested & tokens(title)) + (2 if value and value in title else 0)
        bucket = 3 if exact or single or coverage >= 0.8 else 2 if coverage >= 0.4 or similarity else 1
        age = max(0, (instant-datetime.fromisoformat(chronology(record))).total_seconds()/86400)
        candidates.append(dict(record=record, lexical=bm25+(2 if exact or single else 0),
                               title=title_score, temporal=int(in_window), semantic=similarity, bucket=bucket,
                               recency=0.03*max(0, 1-age/60)))
    fused = {row['record']['id']: 0.0 for row in candidates}
    for branch in ('lexical', 'title', 'temporal', 'semantic'):
        ranked = sorted((r for r in candidates if r[branch] > 0),
                        key=lambda r: (r[branch], chronology(r['record']), r['record']['id']), reverse=True)
        for position, row in enumerate(ranked, 1):
            fused[row['record']['id']] += 1/(60+position)
    candidates.sort(key=lambda r: (r['bucket'], fused[r['record']['id']]*(1+r['recency']),
                                  chronology(r['record']), r['record']['id']), reverse=True)
    return [r['record'] for r in candidates]


def matches_window(record, text, value, window, preferences):
    stamp = datetime.fromisoformat(chronology(record))
    if window[0] <= stamp < window[1]:
        return True
    retrospective = (not record.get('occurred_at') or record['occurred_at'] == record['updated_at']) and any(word in text for word in window[2])
    stated = temporal_window(value, preferences, stamp) if retrospective else None
    return stated is not None and stated[:2] == window[:2]


def eligible_records(state, space, purpose='recall', *, scenario=None, chat=False):
    from .spaces import normalize_space
    space = normalize_space(space)
    scenario = scenario or ('work' if space == 'work' else 'general')
    blocked = set()
    if purpose != 'recall':
        blocked = forgotten_history(state, [space]) | {ref['id'] for r in state['records'].values() if r['kind'] == 'memory'
                   and (r['data']['status'] in {'suppressed', 'rejected'} or
                        (r['data']['status'] == 'active' and not usable_memory(state, r, scenario)))
                   for ref in r['data']['sources']}
        if blocked:
            blocked = dependent_ids(state, blocked)
    records = [r for r in state['records'].values() if normalize_space(r['space']) == space and r['id'] not in blocked]
    if purpose != 'recall':
        records = [r for r in records if r['mode'] == 'normal' and (
            (r['kind'] == 'message' and r['data']['role'] == 'user') or
            (r['kind'] == 'memory' and usable_memory(state, r, scenario)) or
            (chat and r['kind'] not in {'message', 'memory'}))]
    return records


def search(state, q, space, purpose='recall', limit=20, *, instant=None):
    from .semantic_index import index_for
    records = eligible_records(state, space, purpose)
    # Library recall intentionally finds partial matches across periods; only
    # personalization treats a period as a claim requiring dated evidence.
    return rank_records(records, q, preferences=state.get('preferences'), instant=instant,
                        temporal=purpose != 'recall', broad=purpose == 'recall', semantic=index_for(state),
                        semantic_cached_only=getattr(state, '_semantic_cached_only', False))[:limit]
