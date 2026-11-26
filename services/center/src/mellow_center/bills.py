"""Conservative subject-only expense extraction. A due bill is never a paid expense."""
import re
import unicodedata

from .mail import PROVENANCE_KEY, active_config

EXCLUDE = re.compile(r'验证码|校验码|动态密码|一次性密码|verification code|one.time password|\bOTP\b|退款|退还|撤销|失败|未成功|收款|到账|收入|转入|待还|应还|待支付|待付款|未支付|未付款|未扣款|待扣|应付|还款提醒|还款日|到期|账单提醒|即将|将于|将扣|预计|预授权|授权冻结|支付请求|USD|美元|港币|HKD|EUR|欧元|日元|JPY|GBP|英镑|澳元|加元|韩元|[$€£]', re.I)
PAID = re.compile(r'消费|支出|扣款|支付成功|付款成功|还款成功|成功还款|已还款|已支付|成功支付|交易成功')
AMOUNT = re.compile(r'(?:[¥￥]\s*|(?:人民币|RMB|CNY)\s*)(\d[\d,]*(?:\.\d{1,2})?)(?![\d.])|(\d[\d,]*(?:\.\d{1,2})?)\s*元', re.I)


def parse_email_bill(identifier, subject, occurred_at):
    text = unicodedata.normalize('NFKC', subject)
    if EXCLUDE.search(text) or not PAID.search(text) or re.search(r'\d+\.\d{3,}', text):
        return None
    values = []
    for match in AMOUNT.finditer(text):
        value = match[1] or match[2]
        if not re.fullmatch(r'(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?', value):
            return None
        parts = value.replace(',', '').split('.')
        values.append(int(parts[0]) * 100 + int((parts[1] if len(parts) > 1 else '0').ljust(2, '0')))
    amounts = set(values)
    if len(amounts) != 1 or not 0 < next(iter(amounts)) <= 10_000_000_000:
        return None
    merchant = '邮件账单'
    for pattern in (r'商户\s*[:：]\s*([^\n，,。；;]{1,30})', r'在\s*([^\n，,。；;]{1,30}?)\s*(?:消费|支付|支出)', r'向\s*([^\n，,。；;]{1,30}?)\s*(?:支付|付款)'):
        match = re.search(pattern, subject)
        if match:
            candidate = match[1].strip()
            if candidate and not re.search(r'尾号|账户|余额|银行卡|通知|验证码|\d{7,}', candidate):
                merchant = candidate
            break
    return {'id': 'email-' + identifier, 'amount_fen': amounts.pop(), 'merchant': merchant,
            'occurred_at': occurred_at, 'source': 'email'}


def _trusted_senders(state):
    """Imported-by-sender provenance whose address is still exactly on the current allowlist."""
    allowed = {sender.casefold() for sender in active_config(state)['senders']}
    provenance = state.get('private_items', {}).get(PROVENANCE_KEY, {})
    return {identifier for identifier, proof in provenance.items()
            if isinstance(proof, dict) and proof.get('rule') == 'sender' and proof.get('address') in allowed}


def email_bills(state):
    from .spaces import normalize_space
    result = []
    trusted = _trusted_senders(state)
    for identifier, record in state['records'].items():
        data = record.get('data', {})
        if record.get('kind') != 'document' or normalize_space(record.get('space')) != 'personal' or data.get('channel') != 'email' or not identifier.startswith('mail-') or identifier not in trusted:
            continue
        pages = data.get('pages', [])
        if not pages or pages[0].get('engine') != 'imap-header-v1':
            continue
        subject = pages[0].get('text', '').split('\n', 1)[0].removeprefix('邮件标题：')
        bill = parse_email_bill(identifier, subject, record.get('occurred_at') or record['updated_at'])
        if bill:
            result.append(bill)
    return sorted(result, key=lambda row: row['occurred_at'], reverse=True)[:1000]
