"""Whatshot public boards cached on the hub. Interest matching stays local."""
import asyncio
import hashlib
import json
import re
import math
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import urlsplit, unquote_plus
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

from .features import memory_status
from .vault import now

ORIGIN = "https://api.whatshot.top"
FEEDS = {
    "weibo/hot": "微博 · 热搜", "zhihu/hot": "知乎 · 热榜",
    "baidu/realtime": "百度 · 实时热搜", "douyin/hot": "抖音 · 热榜",
    "bilibili/0": "哔哩哔哩 · 全站热门", "toutiao/hot": "今日头条 · 热榜",
    "kuaishou/hot": "快手 · 热榜", "thepaper/hot": "澎湃新闻 · 热榜",
    "qq-news/hot": "腾讯新闻 · 热榜", "36kr/hot": "36氪 · 热榜",
    "ithome/hot": "IT之家 · 热榜", "cls/telegraph": "财联社 · 电报",
    "wallstreetcn/global": "华尔街见闻 · 全球快讯", "hupu/1": "虎扑 · 热帖",
    "juejin/hot": "稀土掘金 · 热榜", "github/daily": "GitHub · 今日趋势",
    "v2ex/hot": "V2EX · 热议",
}
DEFAULT_BOARDS = tuple(FEEDS)[:9]
TOPICS = ("网球", "篮球", "足球", "体育", "比赛", "游戏", "电竞", "电影", "音乐", "人工智能", "大模型",
          "机器人", "编程", "手机", "苹果", "科技", "航天", "摄影", "旅行", "经济", "财经")


def cache_path(vault):
    return Path(vault.path).parent / "whatshot-public.json"


def read_cache(vault):
    try:
        result = json.loads(cache_path(vault).read_text())
        return result if isinstance(result, dict) else {}
    except (FileNotFoundError, ValueError) as error:
        if not isinstance(error, FileNotFoundError):
            from .maintenance import log_event
            log_event('NEWS_CACHE_INVALID', 'news_cache', error)
        return {}


class NewsConfiguration(TypedDict):
    boards: list[str]
    poll_minutes: int


def configuration(vault) -> NewsConfiguration:
    preferences = vault.read().get('preferences', {}) if vault is not None else {}
    value = preferences.get('news', {})
    if not isinstance(value, dict):
        value = {}
    boards = value.get('boards', list(DEFAULT_BOARDS))
    if not isinstance(boards, list):
        boards = list(DEFAULT_BOARDS)
    minutes = value.get('poll_minutes', 15)
    if type(minutes) is not int or not 1 <= minutes <= 1440:
        minutes = 15
    return {'boards': list(dict.fromkeys(board for board in boards if isinstance(board, str) and board in FEEDS)),
            'poll_minutes': minutes}


def settings(vault):
    return {**configuration(vault), 'sources': FEEDS,
            'has_key': bool(vault.read().get('credentials', {}).get('whatshot'))}


def save_settings(vault, value, request, authorize):
    from .domain import fields, require
    fields(value, ['boards', 'poll_minutes'], ['key'])
    boards, minutes = value['boards'], value['poll_minutes']
    require(isinstance(boards, list) and len(boards) <= len(FEEDS))
    require(all(isinstance(board, str) and board in FEEDS for board in boards))
    require(len(set(boards)) == len(boards))
    require(type(minutes) is int and 1 <= minutes <= 1440)
    if 'key' in value:
        require(isinstance(value['key'], str) and len(value['key']) <= 4096
                and all(32 <= ord(char) < 127 for char in value['key']))
    with vault.transaction() as state:
        actor, _ = authorize(state, request, admin=True)
        state['preferences']['news'] = dict(boards=boards, poll_minutes=minutes)
        if 'key' in value:
            if value['key']:
                state['credentials']['whatshot'] = value['key']
            else:
                state['credentials'].pop('whatshot', None)
        from .admin import audit
        audit(state, actor, 'news.settings')
        state['seq'] += 1
    return settings(vault)


def routes(api, vault, authorize):
    """Keep news configuration routes out of the shared API module."""
    from fastapi import Request
    from .request_body import sync_body

    @api.get('/headlines/settings')
    def get_settings(request: Request):
        authorize(vault.read(), request, admin=True)
        return settings(vault)

    @api.put('/headlines/settings')
    def put_settings(request: Request):
        authorize(vault.read(), request, admin=True)
        return save_settings(vault, sync_body(request, 8192), request, authorize)


def valid_url(value):
    if not isinstance(value, str) or len(value) >= 2048:
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme in ('http', 'https') and bool(parts.hostname) and parts.username is None
    except ValueError:
        return False


def normalize_items(board, items, instant):
    rows = []
    for index, item in enumerate(items[:50]):
        if not isinstance(item, dict):
            continue
        title, url = item.get('title'), item.get('url')
        if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not valid_url(url):
            continue
        rank = item.get('sourceRank')
        if not isinstance(rank, int) or isinstance(rank, bool) or rank <= 0:
            rank = index + 1
        hot = item.get('hot')
        if isinstance(hot, bool) or not isinstance(hot, (int, float)) or not math.isfinite(hot) or hot < 0:
            hot = None
        identifier = item.get('id')
        if not isinstance(identifier, (str, int)):
            identifier = hashlib.sha256(url.encode()).hexdigest()[:24]
        rows.append(dict(source=FEEDS[board], board=board, rank=rank, title=title.strip()[:240],
                         url=url, mobile_url=item.get('mobileUrl') if valid_url(item.get('mobileUrl')) else url,
                         hot=hot, hot_label=f'平台热度 {hot:g}' if hot else '',
                         observed_at=instant, item_id=str(identifier)[:256]))
    return rows


def publications(items):
    """Article dates are separate metadata, never inferred from fetch time."""
    stamps = {}
    for item in items[:50]:
        if not isinstance(item, dict) or not valid_url(item.get('url')):
            continue
        stamp = item.get('timestamp')
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and math.isfinite(stamp):
            stamps[item['url']] = stamp
        elif isinstance(item.get('pubDate'), str):
            try:
                stamps[item['url']] = parsedate_to_datetime(item['pubDate']).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
    return stamps


class HostBackoff(Exception):
    pass


class NewsClient:
    """Sequential requests share a persisted host cooldown, including RSS and v1."""

    def __init__(self, client, host, *, clock=time.time, sleep=time.sleep):
        self.client, self.host, self.clock, self.sleep = client, host, clock, sleep

    def request(self, method, path, *, before_attempt=None, **kwargs) -> httpx.Response:
        if self.clock() < self.host.get('retry_at', 0):
            raise HostBackoff
        for attempt in range(3):
            try:
                if before_attempt is not None and not before_attempt():
                    raise ValueError('NEWS_V1_DAILY_LIMIT')
                response = self.client.request(method, ORIGIN + path, timeout=15, **kwargs)
                if response.status_code in (403, 429):
                    self.host.update(retry_at=self.clock() + 600, rss_pending=True,
                                     error_code=f'HTTP_{response.status_code}')
                    raise HostBackoff
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                    raise
                if attempt == 2:
                    raise
                self.sleep(2 ** attempt)
        raise RuntimeError('NEWS_RETRIES_EXHAUSTED')

    def fetch(self, board, previous, *, rss_only=False):
        site, kind = board.split('/')
        try:
            if not rss_only:
                try:
                    response = self.request('GET', '/' + board, params={'limit': 50})
                    if len(response.content) > 2 * 1024 * 1024:
                        raise ValueError('INVALID_NEWS_RESPONSE')
                    result = response.json()
                    if not isinstance(result, dict) or result.get('code') not in (0, 200) or not isinstance(result.get('data'), list):
                        raise ValueError('INVALID_NEWS_RESPONSE')
                    stamp = result.get('updateTime')
                    if not isinstance(stamp, str) or not stamp:
                        raise ValueError('INVALID_NEWS_UPDATE_TIME')
                    datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                    if stamp == previous.get('update_time') and 'items' in previous:
                        return {**previous, 'at': now(), 'status': 'unchanged', 'error_code': None}
                    return dict(at=now(), items=normalize_items(board, result['data'], now()),
                                published=publications(result['data']),
                                update_time=stamp, status='ok', via='json', error_code=None)
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
            response = self.request('GET', '/api/v1/rss/' + site, params={'boardKey': kind})
            if len(response.content) > 2 * 1024 * 1024 or b'<!DOCTYPE' in response.content.upper():
                raise ValueError('INVALID_NEWS_RSS')
            root = ElementTree.fromstring(response.content)
            if root.tag != 'rss' or root.find('channel') is None:
                raise ValueError('INVALID_NEWS_RSS')
            items = [dict(title=item.findtext('title'), url=item.findtext('link'), id=item.findtext('guid'),
                          pubDate=item.findtext('pubDate'))
                     for item in root.findall('./channel/item')[:50]]
            return dict(at=now(), items=normalize_items(board, items, now()), published=publications(items),
                        status='ok', via='rss', error_code=None)
        except HostBackoff:
            return {**previous, 'status': 'backoff', 'error_code': self.host.get('error_code', 'HOST_BACKOFF')}
        except (httpx.HTTPError, ValueError, TypeError, ElementTree.ParseError):
            return {**previous, 'status': 'error', 'error_code': 'NEWS_FETCH_FAILED'}


def batch_fallback(vault, source, feeds):
    """Spend optional v1 quota only on boards whose public JSON and RSS failed."""
    key = vault.read().get('credentials', {}).get('whatshot')
    boards = [board for board, feed in feeds.items() if feed['status'] == 'error']
    if not key or not boards or source.clock() < source.host.get('retry_at', 0):
        return

    def reserve():
        # Official daily quota resets at midnight Asia/Shanghai. Account usage
        # elsewhere is still enforced by the provider; never leak the key to cache.
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with vault.transaction() as state:
            if state['credentials'].get('whatshot') != key:
                return False
            usage: dict[str, Any] = state['credentials'].get('whatshot_usage', {})
            if usage.get('day') != day:
                usage = {'day': day, 'calls': 0}
            if usage['calls'] >= 50:
                return False
            usage['calls'] += 1
            state['credentials']['whatshot_usage'] = usage
        return True

    for offset in range(0, len(boards), 12):
        requested = boards[offset:offset + 12]
        try:
            response = source.request('POST', '/api/v1/current/batch', before_attempt=reserve,
                                      headers={'Authorization': 'Bearer ' + key},
                                      json={'targets': [dict(zip(('site', 'boardKey'), board.split('/')))
                                                        for board in requested], 'limitPerBoard': 50})
            payload = response.json().get('data', {})
            results = payload.get('results', []) if isinstance(payload, dict) else payload
            if not isinstance(results, list):
                continue
            for result in results:
                if not isinstance(result, dict) or result.get('error'):
                    continue
                target = result.get('target', result)
                board = f"{target.get('site')}/{target.get('boardKey')}"
                body = result.get('data', result)
                if board not in requested or not isinstance(body, dict) or not isinstance(body.get('items'), list):
                    continue
                stamp = body.get('updateTime')
                if isinstance(stamp, str) and stamp and stamp == feeds[board].get('update_time') and 'items' in feeds[board]:
                    feeds[board].update(at=now(), status='unchanged', via='v1', error_code=None)
                    continue
                feeds[board] = dict(at=now(), items=normalize_items(board, body['items'], now()),
                                    published=publications(body['items']), status='ok', via='v1', error_code=None)
                if isinstance(stamp, str) and stamp:
                    feeds[board]['update_time'] = stamp
        except HostBackoff:
            break
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            # Optional credentials never prevent the public feeds from working.
            break


def refresh(vault, *, transport=None, clock=time.time, sleep=time.sleep):
    previous = read_cache(vault)
    config = configuration(vault)
    host = previous.get('host', {})
    rss_only = host.get('rss_pending', False)
    feeds = {}
    with httpx.Client(transport=transport, headers={'User-Agent': 'Mellow/1.0', 'Accept': 'application/json, application/rss+xml'}) as client:
        source = NewsClient(client, host, clock=clock, sleep=sleep)
        for index, board in enumerate(config['boards']):
            if index and clock() >= host.get('retry_at', 0):
                sleep(0.2)
            feeds[board] = source.fetch(board, previous.get('feeds', {}).get(board, {}), rss_only=rss_only)
        batch_fallback(vault, source, feeds)
    if clock() >= host.get('retry_at', 0):
        host.clear()
    instant = now()
    staged = cache_path(vault).with_suffix(".tmp")
    succeeded = not feeds or any(feed['status'] in ('ok', 'unchanged') for feed in feeds.values())
    staged.write_text(json.dumps({'at': instant if succeeded else previous.get('at'), 'feeds': feeds, 'host': host}, ensure_ascii=False))
    staged.replace(cache_path(vault))
    if not succeeded:
        from .domain import Problem
        raise Problem('NEWS_UNAVAILABLE', 503)
    from .event_bulletins import update_catalog
    rows = list(headlines({"feeds": feeds}))
    published = {url: stamp for feed in feeds.values() for url, stamp in feed.get('published', {}).items()}
    update_catalog(vault, [{**row, 'published': published.get(row['url'])} for row in rows])
    with vault.transaction() as state:
        from .thought_candidates import notify
        notify(state, 'headline', fingerprint=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
               data=rows[:30])
        suggest_news(state, rows)


def headlines(cache, *, publication_times=False):
    seen = set()
    feeds = list(cache.get("feeds", {}).values())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    for rank in range(50):
        for feed in feeds:
            if feed.get('at', '') < cutoff or rank >= len(feed.get('items', [])):
                continue
            row = feed["items"][rank]
            key = re.sub(r"[\W_]+", "", row["title"]).casefold()
            if key and key not in seen and row["url"] not in seen:
                seen.update((key, row["url"]))
                if publication_times:
                    stamp = feed.get('published', {}).get(row['url'])
                    published_at = None
                    try:
                        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                            published_at = datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc).isoformat()
                        elif isinstance(stamp, str):
                            try:
                                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                            except ValueError:
                                parsed = parsedate_to_datetime(stamp)
                            if parsed.tzinfo is not None:
                                published_at = parsed.astimezone(timezone.utc).isoformat()
                    except (ValueError, TypeError, OverflowError, OSError):
                        pass
                    # Missing publication metadata is unknown, not observed_at.
                    yield {**row, 'published_at': published_at}
                else:
                    yield row


def interest_signals(state, space):
    from .spaces import normalize_space
    space = normalize_space(space)
    if space.startswith('family:'):
        return
    today = datetime.now(timezone.utc)
    for r in state["records"].values():
        if normalize_space(r['space']) != space or r['mode'] != 'normal':
            continue
        d = r['data']
        text = d.get('text', '').casefold()
        if r['id'] in {'news-interests-personal', 'news-interests-life', 'news-interests-work'} and r['kind'] == 'message' and text.startswith('热点关注：'):
            words = {w for w in re.split(r'[、，,；;\s]+', text[5:]) if 2 <= len(w) <= 40}
            yield r, words, 3, '你关注'
            continue
        if any(word in text for word in ('不喜欢', '不关注', '不想', '讨厌', '没兴趣')):
            continue
        words = {t for t in TOPICS if t != '比赛' and t in text}
        if r['kind'] == 'event' and d.get('status') != 'cancelled':
            try:
                start = datetime.fromisoformat(d['start'].replace('Z', '+00:00'))
                if today <= start <= today + timedelta(days=7):
                    yield r, words, 1, '与你近期的安排有关：'
            except (ValueError, KeyError, TypeError) as error:
                from .maintenance import log_event
                log_event('NEWS_EVENT_TIME_INVALID', 'news_signals', error)
        elif r['kind'] == 'memory' and d.get('category') in ('goal', 'preference') and memory_status(state, r) == 'active':
            if d.get('goal_state', 'open') != 'open':
                continue
            words.update(re.findall(r'(?:喜欢|爱看|关注|学习)\s*([a-z0-9][a-z0-9+.-]{1,24}|[\u4e00-\u9fff]{2,8})(?:[。！ ，,]|$)', text))
            if d['category'] == 'preference':
                words.update(re.findall(r'(?<![a-z0-9])[a-z]+[0-9]+(?![a-z0-9])', text))
            yield r, words, 2, '与你的目标有关：' if d['category'] == 'goal' else '你喜欢'


def article_keys(item):
    title = item.get('title', '')
    if title:
        yield ('title', re.sub(r'[\W_]+', '', title.casefold()) or title.strip())
    try:
        url = urlsplit(item.get('url') or '')
        if url.scheme not in ('https', 'http') or not url.hostname:
            return
        query = sorted(p for p in url.query.split('&') if p and not re.fullmatch(
            r'utm_.*|fbclid|gclid|spm|share_source|share_medium',
            unquote_plus(p.split('=', 1)[0]), re.I))
        fragment = url.fragment if url.fragment.startswith(('/', '!/')) else ''
        if url.path.strip('/') or query or fragment:
            yield ('url', url.netloc.lower(), url.path, tuple(query), fragment)
    except ValueError:
        return


def discussed_news_keys(state, space):
    from .spaces import normalize_space
    keys = set()
    for row in state['records'].values():
        data = row['data']
        if (row['kind'] != 'message' or normalize_space(row['space']) != normalize_space(space)
                or data.get('role') != 'user' or data.get('origin')):
            continue
        topic = data.get('discussion') or {}
        if topic.get('kind') == 'news':
            keys.update(article_keys(topic))
        elif row['mode'] == 'normal':
            old = re.match(r'^聊聊这条新闻[：:]\s*(.+)（[^\n（）]+）\s+(https?://[^\s]+)', data.get('text', ''))
            if old:
                keys.update(article_keys(dict(title=old[1], url=old[2])))
    return keys


def matching_news(state, rows, space):
    signals = list(interest_signals(state, space))
    matches = []
    discussed = discussed_news_keys(state, space)
    for rank, item in enumerate(rows):
        if not discussed.isdisjoint(article_keys(item)):
            continue
        title = item['title'].casefold()
        best = None
        for record, words, priority, label in signals:
            hits = sorted((w for w in words if w in title), key=lambda w: (-len(w), w))
            if hits:
                score = (priority, len(hits), len(hits[0]))
                if best is None or score > best[0]:
                    best = score, record, label + hits[0]
        if best:
            matches.append((best[0], rank, item, best[1], best[2]))
    matches.sort(key=lambda m: (tuple(-n for n in m[0]), m[1]))
    for _, _, item, record, reason in matches:
        yield item, record, reason


def news_key(space, item):
    from .spaces import normalize_space
    return _legacy_news_key(normalize_space(space), item)


def _legacy_news_key(space, item):
    title = re.sub(r"[\W_]+", "", item["title"]).casefold()
    return "news-" + hashlib.sha256((space + title).encode()).hexdigest()[:24]


def _news_keys(space, item):
    from .spaces import normalize_space
    spaces = ('personal', 'life', 'work') if normalize_space(space) == 'personal' else (space,)
    return [_legacy_news_key(alias, item) for alias in spaces]


def selected_news(state, rows, space):
    picks = []
    for item, _, reason in matching_news(state, rows, space):
        if any(state['suggestions'].get(key, {}).get('state') in ('dismissed', 'snoozed') for key in _news_keys(space, item)):
            continue
        picks.append({**item, 'reason': reason})
        if len(picks) == 3:
            break
    return picks


def sale_headline(item):
    return bool(re.search(r'开售|预售|开票|售票|开始销售|开启销售|发售抢购|限时抢购', item.get('title', '')))


def suggest_news(state, rows):
    if state.get('private_items', {}).get('thought_control', {}).get('owner'):
        return  # Thought clients pull candidates; no parallel optional suggestion.
    from .proactive import expires_at, may_suggest
    from .spaces import normalize_space
    instant = now()
    if not may_suggest(state, instant):
        return
    rows = list(rows)
    for space in sorted({normalize_space(r['space']) for r in state['records'].values()}):
        for item, r, reason in matching_news(state, rows, space):
            key = news_key(space, item)
            if sale_headline(item) or any(key in state['suggestions'] for key in _news_keys(space, item)):
                continue
            state['suggestions'][key] = dict(id=key, space=space, source_id=r["id"],
                sources=[{"id": r["id"], "revision": r["revision"]}], text=item["title"],
                reason=reason, news=item, action="none", state="new", created_at=instant, expires_at=expires_at())
            state["seq"] += 1
            return


async def news_loop(vault, status=None):
    from .background import worker_tick
    status = status if status is not None else {}
    while True:
        await worker_tick(vault, status, {'news': lambda: asyncio.to_thread(refresh, vault)})
        cache = read_cache(vault) if vault is not None else {}
        status['news']['boards'] = {
            board: {key: feed.get(key) for key in ('status', 'via', 'at', 'update_time', 'error_code')}
            for board, feed in cache.get('feeds', {}).items()
        }
        host_retry = cache.get('host', {}).get('retry_at', 0)
        status['news']['host_retry_at'] = host_retry
        delay = configuration(vault)['poll_minutes'] * 60
        remaining = host_retry - time.time()
        await asyncio.sleep(remaining if remaining > 0 else delay)
