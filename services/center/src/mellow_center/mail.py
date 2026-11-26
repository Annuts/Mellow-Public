"""Opt-in, read-only IMAP header capture for the owner's mailbox.

Off by default. Only subject, sender, date and Message-ID headers are fetched with
BODY.PEEK; message bodies are never requested, flagged, moved or deleted. Matching
mail becomes a source_only inbox document; billing-like subjects additionally yield
an unconfirmed draft that the owner must review on the phone. Network I/O never
holds the vault lock, and the account configuration is re-checked before commit.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import imaplib
import re
import socket
import ssl
import unicodedata
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.parser import Parser
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, TypedDict

from .domain import Problem, validate_record
from .maintenance import log_event
from .vault import canonical, clear_indexes

Json = dict[str, Any]


class FetchResult(TypedDict):
    seen: int
    matches: list[Json]

SPACE = 'personal'
MAIL_DEFAULTS = {'enabled': False, 'host': '', 'port': 993, 'username': '', 'password': '',
                 'days': 7, 'senders': [], 'keywords': []}
STATUS_KEY = 'external_mail_status'
PROVENANCE_KEY = 'external_mail_provenance'
MIN_INTERVAL = timedelta(minutes=15)
LOOP_SECONDS = 60
TIMEOUT_SECONDS = 15
FETCH_LIMIT = 200
HEADER_LIMIT = 65536
SUBJECT_MAX, SENDER_MAX, DATE_MAX, SNIPPET_MAX = 500, 320, 64, 200
FETCH_SPEC = '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])'
BILL_WORDS = ('账单', '还款', '扣款', '发货', '快递', '订单')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')

AMOUNT = re.compile(r'[¥￥]\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?|(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?\s*元')
FULL_DATE = re.compile(r'(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\d)')
SHIPPING_STATUSES = ('已发货', '已出库', '已揽收', '已收件', '运输中', '派送中', '配送中', '待取件', '请取件',
                     '已到站', '已入柜', '已签收', '已送达', '已投递', '代收')
PACKAGE_CONTEXT = ('快递', '包裹', '物流', '订单', '运单', '驿站', '发货', '取件', '派送', '配送', '快件')
TRACKING = re.compile(r'(?<![订A-Za-z0-9])(?:(?:快递|物流|运)?单号|tracking\s*(?:number|no\.?)?)\s*[:：#]?\s*([A-Za-z0-9]{8,30})(?![A-Za-z0-9])|'
                      r'(?<![A-Za-z0-9])((?:SF|YT|JT|ZT|YD|JD[A-Z]{0,2})\d{10,16})(?![A-Za-z0-9])', re.I)
PICKUP = re.compile(r'(?:取件码|取货码|提货码|取件密码|pickup\s*code)\s*[:：为是]?\s*([A-Za-z0-9][A-Za-z0-9\-]{2,11})(?![A-Za-z0-9\-])', re.I)
MONTH_DAY = re.compile(r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日')
ADDRESS = re.compile(r'[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+')


class MailError(Exception):
    """Carries only a caller-owned, already sanitized Chinese message."""


# ---------------------------------------------------------------- pure helpers

def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def clean(value, limit: int) -> str:
    """Single-line, control-free, valid UTF-8 text no longer than ``limit``."""
    out = []
    for char in str(value):
        category = unicodedata.category(char)
        if category == 'Cf':
            continue
        out.append(' ' if category in {'Cc', 'Zl', 'Zp'} or char.isspace() else char)
    text = ' '.join(''.join(out).split())
    return text.encode('utf-8', 'replace').decode('utf-8')[:limit]


def decode_value(value, limit: int) -> str:
    """MIME-decode a header; unknown charsets or broken words degrade instead of failing."""
    if value is None:
        return ''
    try:
        text = str(make_header(decode_header(str(value))))
    except Exception:
        text = str(value)
    return clean(text, limit)


def fold(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def signature(config) -> str:
    return hashlib.sha256(canonical(config)).hexdigest()


def active_config(state) -> Json:
    stored = state.get('credentials', {}).get('external_accounts', {}).get('mail', {})
    value = {**copy.deepcopy(MAIL_DEFAULTS), **copy.deepcopy(stored)}
    for key in ('senders', 'keywords'):
        raw = value.get(key)
        value[key] = [v.strip() for v in raw if isinstance(v, str) and v.strip()] if isinstance(raw, list) else []
    days = value.get('days')
    value['days'] = min(30, max(1, days)) if type(days) is int else 7
    return value


def idle_reason(config) -> str | None:
    if config.get('enabled') is not True:
        return '邮箱连接未启用，未读取邮箱。'
    if not all(isinstance(config.get(k), str) and config[k] for k in ('host', 'username', 'password')) \
            or type(config.get('port')) is not int:
        return '邮箱服务器、账号或应用密码未填写完整，未读取邮箱。'
    if not config['senders'] and not config['keywords']:
        return '未设置发件人或关键词白名单，未读取邮箱。'
    return None


def account_identity(config) -> str:
    return f"{config['host'].strip().lower()}:{config['port']}:{config['username'].strip().lower()}"


def stable_id(config, message_id: str, uidvalidity: str, uid: str) -> str:
    """Stable per account: Message-ID when present, otherwise UIDVALIDITY plus UID."""
    token = message_id.strip().strip('<>').strip()
    material = ('message-id\0' + token) if token else ('uid\0' + uidvalidity + '\0' + uid)
    return 'mail-' + hashlib.sha256((account_identity(config) + '\0' + material).encode()).hexdigest()[:40]


def match_rule(config, address: str, subject: str) -> str | None:
    """Exact sender address or subject keyword; never wildcards, never a body search."""
    if address and address.casefold() in {s.casefold() for s in config['senders']}:
        return 'sender'
    folded = fold(subject)
    if any(fold(word) in folded for word in config['keywords']):
        return 'keyword'
    return None


def sender_address(raw) -> str:
    try:
        address = parseaddr(str(raw or ''))[1].strip()
    except Exception:
        return ''
    return address if ADDRESS.fullmatch(address) else ''


def parse_headers(raw: bytes) -> Json:
    # Servers may send raw UTF-8 headers (RFC 6532); decode defensively before MIME words.
    message = Parser().parsestr(raw[:HEADER_LIMIT].decode('utf-8', 'replace'), headersonly=True)
    subject = decode_value(message.get('Subject'), SUBJECT_MAX)
    sender = decode_value(message.get('From'), SENDER_MAX)
    stamp = ''
    try:
        stamp = _utc(parsedate_to_datetime(str(message.get('Date')))).isoformat()[:DATE_MAX]
    except Exception:
        pass
    return {'subject': subject, 'sender': sender, 'address': sender_address(message.get('From')),
            'date': stamp, 'message_id': clean(message.get('Message-ID') or '', 998)}


def package_hints(text: str) -> Json | None:
    """Subject-only parcel clues: shipping status, tracking number or pickup code."""
    status = next((word for word in SHIPPING_STATUSES if word in text), None)
    if status is not None and not any(word in text for word in PACKAGE_CONTEXT):
        status = None
    tracking = TRACKING.search(text)
    pickup = PICKUP.search(text)
    tracking_no = (tracking.group(1) or tracking.group(2)).upper() if tracking else None
    pickup_code = pickup.group(1).upper() if pickup else None
    if status is None and tracking_no is None and pickup_code is None:
        return None
    return {'shipping_status': status, 'tracking_no': tracking_no, 'pickup_code': pickup_code}


def billing_hints(subject: str, today: date) -> Json | None:
    """Subject-only candidates; amounts, dates and parcel clues are unverified, not facts.

    Bill-only hints keep the original three keys. Parcel notices add shipping_status,
    tracking_no and pickup_code (each possibly None) so older readers still work.
    """
    text = unicodedata.normalize('NFKC', subject)
    package = package_hints(text)
    keyword = next((word for word in BILL_WORDS if word in text), None)
    if keyword is None and package is not None:
        keyword = package['shipping_status'] or ('取件码' if package['pickup_code'] else '快递')
    if keyword is None:
        return None
    amount_fen = None
    found = AMOUNT.search(text)
    if found:
        whole, cents = (found.group(1), found.group(2)) if found.group(1) else (found.group(3), found.group(4))
        value = int(whole.replace(',', '')) * 100 + int((cents or '0').ljust(2, '0'))
        amount_fen = value if 0 < value <= 10_000_000_000 else None
    due = None
    try:
        full = FULL_DATE.search(text)
        if full:
            due = date(int(full[1]), int(full[2]), int(full[3]))
        elif partial := MONTH_DAY.search(text):
            month, day = (int(x) for x in partial.groups())
            due = date(today.year, month, day)
            if due < today - timedelta(days=60):
                due = date(today.year + 1, month, day)
    except ValueError:
        due = None
    if amount_fen is None and due is None and package is None:
        return None
    result = {'keyword': keyword, 'amount_fen': amount_fen, 'due_date': due.isoformat() if due else None}
    if package is not None:
        result.update(package)
    return result


def sanitize_error(error) -> str:
    """Never echo exception text: it may contain the account, host or password."""
    if isinstance(error, MailError):
        return str(error)
    if isinstance(error, imaplib.IMAP4.error):
        return '邮箱登录或读取被服务器拒绝，请检查账号、应用密码并确认已开启 IMAP。'
    if isinstance(error, ssl.SSLError):
        return '邮箱服务器的加密连接或证书验证失败。'
    if isinstance(error, (socket.timeout, TimeoutError)):
        return '连接邮箱服务器超时，稍后会自动重试。'
    if isinstance(error, (OSError, EOFError)):
        return '无法连接邮箱服务器，请检查服务器地址和端口。'
    return '读取邮箱失败，稍后会自动重试。'


# -------------------------------------------------------------------- network

def _uidvalidity(connection) -> str:
    try:
        _, data = connection.response('UIDVALIDITY')
        value = data[0] if data else b''
        value = value.decode('ascii', 'ignore') if isinstance(value, (bytes, bytearray)) else str(value)
        return value if value.isdigit() else ''
    except Exception:
        return ''


def _header_bytes(data) -> bytes:
    for part in data or ():
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], (bytes, bytearray)):
            return bytes(part[1])[:HEADER_LIMIT]
    return b''


def fetch_matches(config, instant: datetime, imap_factory=None) -> FetchResult:
    """One bounded read-only session. No vault access; always logs out."""
    factory = imap_factory or imaplib.IMAP4_SSL
    since = instant - timedelta(days=config['days'])
    since_text = f'{since.day:02d}-{MONTHS[since.month - 1]}-{since.year}'
    connection = None
    try:
        connection = factory(config['host'], config['port'], ssl_context=ssl.create_default_context(),
                             timeout=TIMEOUT_SECONDS)
        status, _ = connection.login(config['username'], config['password'])
        if status != 'OK':
            raise MailError('邮箱登录失败，请检查应用密码和 IMAP 授权。')
        # Some 163/QQ deployments require the RFC 2971 client ID before EXAMINE.
        capabilities = {v.decode('ascii', 'ignore').upper() if isinstance(v, bytes) else str(v).upper()
                        for v in getattr(connection, 'capabilities', ())}
        if 'ID' in capabilities:
            status, _ = connection.xatom('ID', '("name" "Mellow" "version" "preview" "vendor" "Mellow")')
            if status != 'OK':
                raise MailError('邮箱拒绝客户端标识，请检查服务商的 IMAP 设置。')
        status, _ = connection.select('INBOX', readonly=True)
        if status != 'OK':
            raise MailError('无法以只读方式打开收件箱。')
        uidvalidity = _uidvalidity(connection)
        status, data = connection.uid('SEARCH', 'SINCE', since_text)
        if status != 'OK':
            raise MailError('邮箱搜索失败，稍后会自动重试。')
        uids = sorted({int(x) for chunk in (data or ()) if isinstance(chunk, (bytes, bytearray))
                       for x in chunk.split() if x.isdigit()})[-FETCH_LIMIT:]
        matches, seen = [], 0
        for number in uids:
            status, parts = connection.uid('FETCH', str(number), FETCH_SPEC)
            if status != 'OK':
                continue
            raw = _header_bytes(parts)
            if not raw:
                continue
            seen += 1
            try:
                headers = parse_headers(raw)
            except Exception:
                continue
            rule = match_rule(config, headers['address'], headers['subject'])
            if rule is None:
                continue
            identifier = stable_id(config, headers['message_id'], uidvalidity, str(number))
            hints = billing_hints(headers['subject'], instant.date())
            matches.append({**headers, 'id': identifier, 'rule': rule, 'hints': hints})
        return {'seen': seen, 'matches': matches}
    finally:
        if connection is not None:
            try:
                connection.logout()
            except Exception:
                pass


# --------------------------------------------------------------------- commit

def message_record(item, instant: datetime) -> Json:
    stamp = instant.isoformat()
    occurred = item['date'] or stamp
    if occurred > (instant + timedelta(minutes=5)).isoformat():
        occurred = stamp
    snippet = clean(item['subject'], SNIPPET_MAX)
    text = '\n'.join([f"邮件标题：{item['subject'] or '（无标题）'}", f"发件人：{item['sender'] or '（未知）'}",
                      f"日期：{item['date'] or '（未知）'}", f"摘要：{snippet or '（无标题）'}",
                      f"邮件编号：{item['message_id'] or '（未提供）'}"])
    return {'id': item['id'], 'kind': 'document', 'space': SPACE, 'mode': 'source_only', 'revision': 1,
            'updated_at': stamp, 'occurred_at': occurred,
            'data': {'title': clean(item['subject'], 200) or '无标题邮件', 'channel': 'email',
                     'mime': 'text/plain', 'pages': [{'number': 1, 'text': text, 'state': 'done', 'engine': 'imap-header-v1'}],
                     'summary': snippet, 'tags': ['邮件'], 'organization': 'local', 'state': 'done', 'manual': False, 'ai': False}}


def draft_record(item, instant: datetime) -> Json:
    hints = item['hints']
    lines = ['邮件待核对提醒草稿（仅依据邮件标题推测，尚未核实）', f"标题：{item['subject']}",
             f"发件人：{item['sender'] or '（未知）'}", f"关键词：{hints['keyword']}"]
    if hints.get('amount_fen') is not None:
        lines.append(f"金额线索：¥{hints['amount_fen'] // 100}.{hints['amount_fen'] % 100:02d}")
    if hints.get('shipping_status'):
        lines.append(f"物流状态线索：{hints['shipping_status']}")
    if hints.get('tracking_no'):
        lines.append(f"运单号线索：{hints['tracking_no']}")
    if hints.get('pickup_code'):
        lines.append(f"取件码线索：{hints['pickup_code']}")
    if hints.get('due_date'):
        lines.append(f"日期线索：{hints['due_date']}")
    lines.append('来源邮件记录：' + item['id'])
    stamp = instant.isoformat()
    return {'id': 'mail-draft-' + item['id'][5:], 'kind': 'draft', 'space': SPACE, 'mode': 'normal', 'revision': 1,
            'updated_at': stamp, 'occurred_at': stamp,
            'data': {'text': '\n'.join(lines), 'scenario': 'general', 'status': 'draft', 'sources': []}}


def _add(state, record) -> bool:
    """Create only: never overwrite an edit, never revive a tombstoned item."""
    if record['id'] in state['records'] or record['id'] in state['deleted']:
        return False
    try:
        validate_record(state, record)
    except Problem:
        return False
    state['records'][record['id']] = record
    state['seq'] += 1
    clear_indexes(state)
    return True


def _status(instant, sig, state, **values) -> Json:
    return {'state': state, 'signature': sig, 'last_attempt_at': instant.isoformat(),
            'next_at': (instant + MIN_INTERVAL).isoformat(), **values}


def _due(status, sig, instant) -> bool:
    if not status or status.get('signature') != sig:
        return True
    try:
        return _utc(status['next_at']) <= instant
    except (KeyError, ValueError, TypeError):
        return True


def _record_off(vault, config, message):
    sig = signature(config)
    with vault.transaction(bump_revision=False) as state:
        current = state['private_items'].get(STATUS_KEY) or {}
        if current.get('state') != 'off' or current.get('message') != message:
            state['private_items'][STATUS_KEY] = {'state': 'off', 'signature': sig, 'message': message,
                                                  'updated_at': _utc().isoformat()}


def poll_mail(vault, *, imap_factory=None, now=None) -> Json:
    instant = _utc(now)
    with vault.view() as state:
        config = active_config(state)
        previous = copy.deepcopy(state['private_items'].get(STATUS_KEY) or {})
    reason = idle_reason(config)
    if reason:
        # An enabled connector must say why it is idle; a never-configured one stays silent.
        if (previous or config.get('enabled') is True) and \
                (previous.get('state') != 'off' or previous.get('message') != reason):
            _record_off(vault, config, reason)
        return {'status': 'off', 'message': reason, 'imported': 0, 'drafts': 0, 'seen': 0, 'matched': 0}
    sig = signature(config)
    if not _due(previous, sig, instant):
        return {'status': 'skipped', 'message': '距离上次读取不足 15 分钟，暂不读取。',
                'next_at': previous.get('next_at'), 'imported': 0, 'drafts': 0, 'seen': 0, 'matched': 0}
    found: FetchResult
    try:
        found, failure = fetch_matches(config, instant, imap_factory), None
    except Exception as error:
        found = {'seen': 0, 'matches': []}
        failure = sanitize_error(error)
        log_event('MAIL_POLL_FAILED', 'external_mail', error)
    imported = drafts = 0
    with vault.transaction() as state:
        if signature(active_config(state)) != sig:
            state.bump_revision = False
            return {'status': 'stale', 'message': '邮箱设置在读取期间已更改，本次结果已丢弃。',
                    'imported': 0, 'drafts': 0, 'seen': 0, 'matched': 0}
        if failure is None:
            provenance = state['private_items'].setdefault(PROVENANCE_KEY, {})
            for stale in [key for key in provenance if key not in state['records']]:
                del provenance[stale]
            for item in found['matches']:
                if _add(state, message_record(item, instant)):
                    imported += 1
                    # Server-side only (private_items never reach phones): which rule imported it, and from whom.
                    provenance[item['id']] = {'rule': item['rule'], 'address': item['address'].casefold()}
                    if item['hints'] and _add(state, draft_record(item, instant)):
                        drafts += 1
            outcome = _status(instant, sig, 'ok', last_success_at=instant.isoformat(), seen=found['seen'],
                              matched=len(found['matches']), imported=imported, drafts=drafts)
        else:
            outcome = _status(instant, sig, 'error', message=failure,
                              last_success_at=previous.get('last_success_at'))
        state['private_items'][STATUS_KEY] = outcome
        state.bump_revision = imported + drafts > 0
    if failure is not None:
        return {'status': 'error', 'message': failure, 'imported': 0, 'drafts': 0, 'seen': 0, 'matched': 0}
    return {'status': 'ok', 'message': f'已读取邮件头 {found["seen"]} 封，新增 {imported} 封。',
            'imported': imported, 'drafts': drafts, 'seen': found['seen'], 'matched': len(found['matches'])}


async def mail_loop(vault):
    """Wake every minute; poll_mail itself enforces the 15 minute cadence."""
    while True:
        try:
            await asyncio.to_thread(poll_mail, vault)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_event('MAIL_LOOP_FAILED', 'external_mail', error)
        await asyncio.sleep(LOOP_SECONDS)
