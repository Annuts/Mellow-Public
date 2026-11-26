"""Read-only, manifest-backed APK downloads for the owner's hub."""
from html import escape
from ipaddress import IPv6Address, ip_address, ip_network
import json
import os
from pathlib import Path
import re
from typing import TypedDict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .console import COOKIE
from .domain import Problem, authenticate, require

LAN_NETWORKS = tuple(ip_network(value) for value in (
    '100.64.0.0/10', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8',
))
APK_NAME = re.compile(r'Mellow-[0-9]+\.apk')
APK_TYPE = 'application/vnd.android.package-archive'


class Package(TypedDict):
    file: str
    version: str
    build_number: int
    bytes: int
    sha256: str
    built_at: str
    notes: str


def lan_client(request: Request) -> bool:
    # Never inspect X-Forwarded-For. Disable proxy trust at the ASGI server too.
    if request.client is None:
        return False
    try:
        address = ip_address(request.client.host)
    except ValueError:
        return False
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in LAN_NETWORKS)


def packages(root: Path) -> list[Package]:
    manifest = root / 'manifest.json'
    if not manifest.exists():
        return []
    try:
        if manifest.is_symlink():
            raise ValueError('Manifest must be a regular file')
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if value['schema'] != 1 or not isinstance(value['android'], list):
            raise ValueError('Unsupported manifest')
        rows: list[Package] = []
        for item in value['android']:
            name = item['file']
            if (not isinstance(name, str) or not APK_NAME.fullmatch(name)
                    or type(item['build_number']) is not int or item['build_number'] < 1
                    or name != f"Mellow-{item['build_number']}.apk"
                    or type(item['bytes']) is not int or item['bytes'] < 1
                     or not isinstance(item['version'], str) or not isinstance(item['built_at'], str)
                    or not isinstance(item.get('notes', ''), str) or len(item.get('notes', '')) > 16000
                    or not isinstance(item['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', item['sha256'])):
                raise ValueError('Invalid package metadata')
            path = root / name
            if (not path.is_symlink() and path.resolve().parent == root.resolve()
                    and path.is_file() and path.stat().st_size == item['bytes']):
                rows.append(item)
        return sorted(rows, key=lambda row: row['build_number'], reverse=True)[:3]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Problem('DOWNLOADS_UNAVAILABLE', 503) from error


def router(vault):
    root = Path(os.environ.get('MELLOW_DOWNLOADS_DIR', '/var/lib/mellow-downloads'))
    public_lan = os.environ.get('MELLOW_DOWNLOADS_PUBLIC_LAN', '1') == '1'

    def authorize(request: Request):
        if public_lan and lan_client(request):
            return
        auth = request.headers.get('authorization', '')
        candidates = []
        if auth.startswith('Bearer ') and len(auth) <= 160:
            candidates.append((auth[7:], False))
        cookie = request.cookies.get(COOKIE, '')
        if cookie and len(cookie) <= 128:
            candidates.append((cookie, True))
        if candidates:
            with vault.view() as state:
                for token, console in candidates:
                    try:
                        _, device = authenticate(state, token)
                    except Problem:
                        continue
                    if not console or device['admin']:
                        return
        raise Problem('AUTH_FAILED', 401)

    api = APIRouter(prefix='/downloads', dependencies=[Depends(authorize)])

    @api.get('/manifest.json', include_in_schema=False)
    def manifest():
        rows = [dict(version=item['version'], build_number=item['build_number'],
                     sha256=item['sha256'], size=item['bytes'],
                     url=f'/downloads/android/{item["file"]}', notes=item.get('notes', ''),
                     date=item['built_at']) for item in packages(root)]
        return JSONResponse({'latest': rows[0] if rows else None, 'android': rows},
                            headers={'Cache-Control': 'no-store'})

    @api.get('/', response_class=HTMLResponse, include_in_schema=False)
    def index():
        status = 200
        empty = '暂时没有可下载的安装包。'
        try:
            rows = packages(root)
        except Problem:
            rows = []
            status = 503
            empty = '下载清单暂不可用，请稍后再试。'
        cards = []
        for index, item in enumerate(rows):
            name = item['file']
            label = '最新版' if index == 0 else '历史版本'
            latest = ('<p><a href="/downloads/android/latest.apk" download>下载最新版（固定链接）</a></p>'
                      if index == 0 else '')
            watch = ''
            if item.get('watch_sha256'):
                watch = f'<p><a href="/downloads/watch/{item["build_number"]}.apk" download>下载配套手表端</a></p>'
            cards.append(
                f'<article class="login-card"><h2>Android · {label}</h2>'
                f'<p>版本：{escape(item["version"])} · 构建号：{item["build_number"]}</p>'
                f'<p>大小：{item["bytes"] / 1024 / 1024:.1f} MB（{item["bytes"]} 字节）</p>'
                f'<p>构建日期：{escape(item["built_at"])}</p>'
                f'<pre>{escape(item.get("notes", ""))}</pre>'
                f'<p>SHA-256：{item["sha256"]}</p>'
                f'{latest}{watch}'
                f'<p><a href="/downloads/android/{name}" download>下载 {name}</a></p></article>'
            )
        content = ''.join(cards) or f'<p>{empty}</p>'
        return HTMLResponse(
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>小满 · 下载</title><link rel="stylesheet" href="/console/assets/style.css">'
            '</head><body><main class="login-shell"><header class="login-intro">'
            '<h1>小满 · 下载</h1><p>从自己的主机下载 Android 安装包，保留最近三个构建。</p>'
            '<p>更新前请备份；直接覆盖安装，不要卸载旧版。</p>'
            '<p><a href="/console">返回中枢后台</a></p></header><section class="admin-list" aria-label="Android 安装包">'
            + content + '</section></main></body></html>', status_code=status,
        )

    @api.api_route('/watch/{number}.apk', methods=['GET', 'HEAD'], include_in_schema=False)
    def watch_package(number: int):
        item = next((row for row in packages(root) if row['build_number'] == number), {})
        require(bool(re.fullmatch(r'[0-9a-f]{64}', str(item.get('watch_sha256', '')))), 'NOT_FOUND', 404)
        path = root / f'Mellow-Watch-{number}.apk'
        require(not path.is_symlink() and path.is_file(), 'NOT_FOUND', 404)
        return FileResponse(path, media_type=APK_TYPE, filename=path.name)

    @api.api_route('/android/{name:path}', methods=['GET', 'HEAD'], include_in_schema=False)
    def android(name: str):
        require(name == 'latest.apk' or APK_NAME.fullmatch(name), 'NOT_FOUND', 404)
        rows = packages(root)
        item = next((row for row in rows if name == 'latest.apk' or row['file'] == name), None)
        if item is None:
            raise Problem('NOT_FOUND', 404)
        return FileResponse(root / item['file'], media_type=APK_TYPE, filename=item['file'])

    return api
