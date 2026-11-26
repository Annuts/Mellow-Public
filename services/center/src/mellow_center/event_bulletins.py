"""Dated public announcements. Device interests are not sent to the publisher or the hub."""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .features import DEFAULT_TIMEZONE

TZ = ZoneInfo(DEFAULT_TIMEZONE)
PHASES = (
    ("ticket", r"开票|开放售票|售票开启"), ("registration", r"(?:开放|开启|启动|开始)报名|报名(?:通道)?(?:将于.{1,16}|今日|明日|今天|明天|现已|正式|即日|今起|现)?(?:正式)?(?:开放|启动|开启|开始)"),
    ("results", r"公布结果|成绩公布|公布成绩|正式放榜"), ("screening", r"上映|开播|首播"),
    ("sale", r"开售|开始销售|预售开启"), ("release", r"发售|发行|正式上线|正式上市|推出正式版"),
    ("competition", r"开赛|揭幕战开打"),
    ("presentation", r"发布会.{0,20}(?:举办|举行|召开)|举办.{0,12}发布会"),
)
UNCERTAIN = re.compile(r"传闻|网传|疑似|据传|爆料|或将|有望|预计|或于|延期|跳票|推迟|取消|暂停|尚未|未能|辟谣|假消息|[？?]")
MONTH_DAY = re.compile(r"(?:(20\d{2})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def parse_bulletin(row, observed):
    title = row["title"]
    if UNCERTAIN.search(title):
        return None
    phase = next((key for key, marker in PHASES if re.search(marker, title)), None)
    if not phase:
        return None
    # Use the article date to interpret relative dates. Only firm, dated statements become reminders.
    stamp = row.get("published")
    try:
        if isinstance(stamp, (int, float)):
            published = datetime.fromtimestamp(stamp / (1000 if stamp > 10**11 else 1), TZ).date()
        else:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            published = (parsed.astimezone(TZ) if parsed.tzinfo else parsed).date()
    except (ValueError, TypeError, OverflowError, OSError):
        published = None
    # Bind the date to its own clause; pre-orders and release often have different dates.
    marker = dict(PHASES)[phase]
    clause = next((s for s in re.split(r"[，,；;。！!]", title) if re.search(marker,s)), title)
    names = re.findall(r"《([^》]{2,60})》",title)
    if len(set(names))>1 or len({k for k,m in PHASES if re.search(m,clause)})>1:
        return None
    dates = list(MONTH_DAY.finditer(clause))
    if len(dates) == 1:
        year, month, day = dates[0].groups()
        if year:
            resolved_year = int(year)
        elif published is not None:
            resolved_year = published.year
        else:
            return None
        try: due = date(resolved_year,int(month),int(day))
        except ValueError: return None
        if not year and published is not None and due < published - timedelta(days=180):
            try: due = due.replace(year=due.year+1)
            except ValueError: return None
    elif published and re.search(r"今天|今日|今起|即日|今晚|明晚|明日|明天",clause):
        due = published + timedelta(days=int(bool(re.search(r"明日|明天|明晚",clause))))
    elif published and re.search(r"现已|正式开售|正式发售|正式上映|正式开票|报名正式开启|正式上线",clause) and not re.search(r"将|计划|定于|预告|即将",clause):
        due = published
    else:
        return None
    if not observed - timedelta(days=1) <= due <= observed + timedelta(days=370):
        return None
    return dict(title=title, aliases=re.findall(r"《([^》]{2,60})》",title), phase=phase,
                date=due.isoformat(), evidence='report', source=row['source'], url=row['url'])


def catalog_path(vault):
    return Path(vault.path).parent / "public-event-bulletins.json"


def read_catalog(vault, day=None):
    day = day or datetime.now(TZ).date()
    try: rows = json.loads(catalog_path(vault).read_text())
    except (FileNotFoundError, ValueError): rows = []
    return [r for r in rows if r.get('evidence')=='report' and day.isoformat() <= r['date']]


def update_catalog(vault, rows):
    day=datetime.now(TZ).date()
    # Retain dated announcements when they leave a board, replace corrected items from the same source URL.
    kept={r['url']:r for r in read_catalog(vault,day)}
    for row in rows:
        if row['url'] in kept and UNCERTAIN.search(row['title']):
            kept.pop(row['url'])
        item=parse_bulletin(row,day)
        if item and item['date']>=day.isoformat():
            kept[item['url']]=item
    rows=sorted(kept.values(), key=lambda r:r['date'])[:400]
    p=catalog_path(vault)
    tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows,ensure_ascii=False))
    tmp.replace(p)
