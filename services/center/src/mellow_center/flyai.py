"""Read-only Fliggy travel search using the pinned official client."""
import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from .domain import Problem


def _run(command: str, args: list[str], timeout: int) -> dict:
    env = {"PATH": os.defpath + ":/usr/local/bin", "LANG": "C.UTF-8"}
    # The container can run a numeric UID without an entry in /etc/passwd.
    for name in ("HOME", "FLYAI_API_KEY"):
        if name in os.environ:
            env[name] = os.environ[name]
    result = subprocess.run(
        ["flyai", command, *args],
        capture_output=True, timeout=timeout, check=True, env=env,
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or payload.get("status") != 0:
        raise ValueError("supplier error")
    return payload


def _products(data: object) -> str:
    items = data.get("itemList") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("invalid product list")
    rows = []
    for item in items[:8]:
        info = item.get("info") if isinstance(item, dict) else None
        if not isinstance(info, dict):
            continue
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        link = str(info.get("jumpUrl") or "")
        if urlsplit(link).netloc == "router.feizhu.com" and urlsplit(link).scheme == "https":
            title = f"[{title}]({link})"
        price = info.get("price")
        rows.append(f"{title} · ¥{price}" if price else title)
    if not rows:
        return "飞猪关键词搜索暂时没有找到相关商品。"
    return "飞猪关键词搜索返回以下相关商品，具体日期、价格和余位请在商品页核对。\n\n" + "\n\n".join(rows)


def search(query: str) -> dict:
    try:
        payload = _run("ai-search", ["--query", query], 35)
        text = payload.get("data")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty search response")
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            payload = _run("keyword-search", ["--query", query], 35)
            text = _products(payload.get("data"))
        except subprocess.TimeoutExpired:
            raise Problem("FLYAI_TIMEOUT", 504) from None
        except (OSError, ValueError, subprocess.SubprocessError):
            raise Problem("FLYAI_UNAVAILABLE", 502) from None
    return {"text": text[:9000], "notice": str(payload.get("systemMessage") or "")[:500],
            "checked_at": datetime.now(timezone.utc).isoformat()}


def _cny_range(raw):
    """Inclusive integer-fen bounds for the supplier's displayed nightly rate."""
    match = re.fullmatch(r'[¥￥]?\s*([0-9]{1,7})(?:\.(\d{1,2})|([xX]{1,3}))?', str(raw).strip())
    if not match:
        return None
    digits, fraction, masked = match.groups()
    if masked:
        low = int(digits) * 10**len(masked) * 100
        return low, low + 10**len(masked) * 100 - 1
    price = int(digits) * 100 + int((fraction or '').ljust(2, '0'))
    return (price, price) if price > 0 else None


def hotel_quotes(city, day, keyword):
    if not (isinstance(city, str) and 0 < len(city.strip()) <= 64
            and isinstance(keyword, str) and len(keyword) <= 160
            and isinstance(day, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)):
        raise Problem('INVALID_REQUEST', 400)
    try:
        checkout = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    except (ValueError, OverflowError):
        raise Problem('INVALID_REQUEST', 400) from None
    args = ['--dest-name', city.strip(), '--check-in-date', day, '--check-out-date', checkout]
    if keyword.strip():
        args.extend(['--key-words', keyword.strip()])
    try:
        payload = _run('search-hotel', args, 25)
        items = payload['data']['itemList']
        if not isinstance(items, list):
            raise ValueError('invalid hotel list')
    except subprocess.TimeoutExpired:
        raise Problem('FLYAI_TIMEOUT', 504) from None
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        raise Problem('FLYAI_UNAVAILABLE', 502) from None
    quotes = {}
    ambiguous = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        hotel = str(row.get('shId') or '')
        name = row.get('name')
        bounds = _cny_range(row.get('price'))
        if not re.fullmatch(r'[0-9]{1,24}', hotel) or not isinstance(name, str) or not name.strip() or not bounds:
            continue
        link = str(row.get('detailUrl') or '')
        offer = {'id': hotel, 'name': name.strip()[:160], 'low': bounds[0], 'high': bounds[1],
                 'display': str(row['price']).strip().lstrip('¥￥').strip()}
        if link.startswith('https://router.feizhu.com/'):
            offer['url'] = link
        if hotel in quotes and (quotes[hotel]['low'], quotes[hotel]['high']) != bounds:
            ambiguous.add(hotel)
        quotes[hotel] = offer
    offers = [q for k, q in quotes.items() if k not in ambiguous][:20]
    lines = []
    for i, q in enumerate(offers, 1):
        name = f"[{q['name']}]({q['url']})" if 'url' in q else q['name']
        lines.append(f"{i}. {name} · ¥{q['display']}")
    return {'offers': offers, 'day': day, 'checkout': checkout,
            'text': f'{day} 入住，{checkout} 离店 · 飞猪酒店展示价\n' + ('\n'.join(lines) or '暂无可比较的酒店报价。'),
            'checked_at': datetime.now(timezone.utc).isoformat(), 'notice': str(payload.get('systemMessage') or '')[:500]}
