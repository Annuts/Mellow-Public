"""Model-independent Markdown/JSON/attachments ZIP, optionally scrypt + AES-GCM."""
import base64
import hashlib
import io
import json
import secrets
import zipfile
from typing import Any

from .archive import entries, VISIBILITIES
from .domain import require
from .vault import canonical, derive, seal_raw, now


def export_archive(state, spaces, *, period='', visibility=None, passphrase=None):
    require(visibility is None or visibility in VISIBILITIES)
    require(passphrase is None or isinstance(passphrase, str) and 12 <= len(passphrase) <= 1024, 'PASSPHRASE_TOO_SHORT')
    selected = [e for e in entries(state, spaces, period=period) if visibility is None or e['metadata']['visibility'] == visibility]
    files: dict[str, bytes] = {}
    manifest: dict[str, Any] = dict(format='mellow-life-archive-1', created_at=now(), entries=[], files={})
    periods, people = {}, {}
    for e in selected:
        r = state['records'][e['id']]
        path = 'originals/' + hashlib.sha256(e['id'].encode()).hexdigest() + '.json'
        files[path] = canonical(r)
        item = {k: e[k] for k in ('id', 'revision', 'kind', 'at', 'sources', 'metadata', 'revisions')}
        item['original'] = path
        title = ' '.join(e['title'].splitlines())
        section = f"## {e['at']} · {title}\n\nID: `{e['id']}` · 修订 {e['revision']} · {e['metadata']['visibility']}\n\n"
        for q in e['quotes']:
            section += f"原话 · {q['at']} · `{q['id']}` · 修订 {q['revision']}\n\n" + '\n'.join('> ' + line for line in q['text'].splitlines()) + '\n\n'
        for rev in e['revisions']:
            section += f"### 先前修订 {rev['revision']}（{rev['revised_at']} 修订）\n\n" + '\n'.join('> ' + line for line in rev['text'].splitlines()) + '\n\n'
        if e['metadata']['reflection']:
            section += '### 后来的反思（不是当时原话）\n\n' + e['metadata']['reflection'] + '\n\n'
        original = r['data'].get('original')
        if original:
            raw = base64.b64decode(original['content'], validate=True)
            require(hashlib.sha256(raw).hexdigest() == original['sha256'], 'ATTACHMENT_CHECKSUM_MISMATCH')
            suffix = {'application/pdf': 'pdf', 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(r['data'].get('mime'), 'bin')
            attachment = 'attachments/' + original['sha256'] + '.' + suffix
            files[attachment] = raw; item['attachment'] = attachment
            section += f'原附件：[{attachment}](../{attachment})\n\n'
        manifest['entries'].append(item)
        periods.setdefault(e['at'][:7], []).append(section)
        for person in e['metadata']['people']:
            people.setdefault(person, []).append(section)
    for period_key, sections in periods.items():
        files['periods/' + period_key + '.md'] = ('# 当时我是怎么想的 · ' + period_key + '\n\n' + '\n'.join(sections)).encode()
    for person, sections in people.items():
        key = hashlib.sha256(person.encode()).hexdigest()[:24]
        files['people/' + key + '.md'] = ('# 人物 · ' + person + '\n\n' + '\n'.join(sections)).encode()
    files['README.md'] = ('# 人生档案\n\n不依赖小满或模型。按 periods/ 年月、people/ 人物阅读；originals/ 保存原记录，manifest.json 保存稳定 ID、修订、来源和 SHA-256。\n'
        '原话、后来反思、旧修订分别标注。已删除或遗忘的原件不导出。信件不在本包中。\n'
        '此文件是导出时的副本，离线副本无法远程撤回，请自行妥善保管。\n').encode()
    manifest['files'] = {p: {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)} for p, raw in files.items()}
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path, raw in files.items(): archive.writestr(path, raw)
    raw = output.getvalue()
    if passphrase is None: return raw
    salt = secrets.token_bytes(16)
    envelope = dict(format='mellow-life-archive-encrypted-1', kdf='scrypt', n=32768, r=8, p=1,
                    cipher='AES-256-GCM', salt=base64.b64encode(salt).decode(),
                    aad='mellow-life-archive-1', payload=base64.b64encode(seal_raw(derive(passphrase, salt), raw, b'mellow-life-archive-1')).decode())
    return canonical(envelope)
