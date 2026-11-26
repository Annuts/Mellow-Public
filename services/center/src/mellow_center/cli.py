"""Interactive administration: passphrases never appear in command-line arguments."""
from __future__ import annotations
import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
import fcntl
import getpass
import ipaddress
import json
import logging
import os
from pathlib import Path
import socket
import stat
import tempfile
import time
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from .domain import new_pairing
from .vault import Vault, open_backup
from .features import DEFAULT_TIMEZONE
from .certificates import fingerprint as fingerprint, tls_status as tls_status
from .background import worker_tick as worker_tick


def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    # Third-party HTTP logs can include URLs/credentials; access logging is disabled too.
    for name in ('httpx', 'httpcore', 'urllib3'):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def scheduled_work(vault, discover):
    from .vault import now
    with vault.view() as state:
        queued = any(j['state'] == 'queued' for j in state['jobs'].values())
        marker = (state.store_revision, now()[:16])
        due = marker != getattr(vault, '_discover_check', None)
        enabled = state.get('preferences', {}).get('proactive', False)
    if due and enabled:
        discover(vault)
        with vault.view() as state:
            marker = (state.store_revision, now()[:16])
    vault._discover_check = marker
    return queued


def write_private(path, text):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        out.write(text)


def tls(directory, hostnames, *, renew=False):
    if renew:
        key = serialization.load_pem_private_key((directory / 'tls.key').read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError('Unsupported TLS key')
        old = x509.load_pem_x509_certificate((directory / 'tls.crt').read_bytes())
        if old.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise ValueError('TLS key mismatch')
        hostnames = [*hostnames, *[str(name.value) for name in old.extensions.get_extension_for_class(x509.SubjectAlternativeName).value]]
    else:
        key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Mellow personal center')])
    names: list[x509.GeneralName] = []
    for host in sorted(set(['localhost', '127.0.0.1', '::1', socket.gethostname(), *hostnames])):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    start = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(start - timedelta(minutes=5))
        .not_valid_after(start + timedelta(days=365)).add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256()))
    if not renew:
        write_private(directory / 'tls.key', key.private_bytes(serialization.Encoding.PEM,
                      serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())
        write_private(directory / 'tls.crt', cert.public_bytes(serialization.Encoding.PEM).decode())
    else:
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as staged:
            staged.write(cert.public_bytes(serialization.Encoding.PEM))
            staged.flush(); os.fsync(staged.fileno())
        os.replace(staged.name, directory / 'tls.crt')


def renew_if_due(directory):
    info = tls_status(directory)
    if info.get('tls_expiring'):
        from .maintenance import log_event
        log_event('TLS_EXPIRING', 'tls')
    if info and datetime.fromisoformat(info['tls_expires_at']) < datetime.now(timezone.utc) + timedelta(days=60):
        try:
            tls(directory, [], renew=True)
        except Exception as error:
            from .maintenance import log_event
            log_event('TLS_RENEW_FAILED', 'tls', error)
            if datetime.fromisoformat(info['tls_expires_at']) <= datetime.now(timezone.utc):
                raise


def read_vault_key(path):
    with path.open('rb') as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
            raise ValueError('Vault key file must be private and regular.')
        key = source.read(33)
    if len(key) != 32:
        raise ValueError('Invalid vault key file.')
    return key


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description='Mellow single Mac mini center')
    parser.add_argument('--data-dir', type=Path, default=Path.home() / 'Library/Application Support/MellowCenter')
    sub = parser.add_subparsers(dest='command', required=True)
    certificate = sub.add_parser('tls', help='Renew TLS certificate without changing its private key')
    certificate.add_subparsers(dest='tls_command', required=True).add_parser('renew')
    initialize = sub.add_parser('init')
    initialize.add_argument('--hostname', action='append', default=[])
    pairing = sub.add_parser('pair')
    pairing.add_argument('--name', required=True)
    pairing.add_argument('--space', choices=['personal', 'life', 'work'], action='append', required=True)
    pairing.add_argument('--admin', action='store_true')
    pairing.add_argument('--lan', default='https://localhost:8765')
    pairing.add_argument('--tail', default='')
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--no-discovery', action='store_true', help='Disable LAN service announcement')
    serve.add_argument('--port', type=int, default=8765)
    key_source = serve.add_mutually_exclusive_group()
    key_source.add_argument('--keychain', action='store_true', help='Read an explicitly installed login Keychain key')
    key_source.add_argument('--vault-key-file', type=Path, help='Read an existing raw vault key from a private file')
    service = sub.add_parser('service-install', help='Opt in to a login service with a Keychain-held vault key')
    service.add_argument('--host', default='127.0.0.1')
    service.add_argument('--port', type=int, default=8765)
    sub.add_parser('service-uninstall', help='Stop only Mellow and remove its autostart/keychain entry; retain data')
    backup = sub.add_parser('backup')
    backup.add_argument('--output', type=Path, required=True)
    restore = sub.add_parser('recover-empty')
    restore.add_argument('--input', type=Path, required=True)
    policy = sub.add_parser('backup-policy', help='Configure opt-in daily encrypted backups')
    policy.add_argument('--directory', type=Path)
    policy.add_argument('--keep', type=int, default=7)
    policy.add_argument('--hour', type=int, default=3)
    policy.add_argument('--timezone', default=DEFAULT_TIMEZONE)
    policy.add_argument('--disable', action='store_true')
    voice = sub.add_parser('voice-setup', help='Explicitly download a local Chinese speech model')
    voice.add_argument('--model', choices=['base', 'small'], default='small')
    sub.add_parser('rotate-access-token', help='Provision or rotate the host-owned enrollment token; existing devices stay signed in')
    sub.add_parser('status')
    models = sub.add_parser('models', help='Manage the verified local embedding model (no vault unlock needed)')
    models.add_subparsers(dest='models_command', required=True).add_parser('fetch')
    index = sub.add_parser('index', help='Rebuild the encrypted semantic index')
    index.add_subparsers(dest='index_command', required=True).add_parser('rebuild')
    archive = sub.add_parser('archive', help='Export model-independent life archive')
    archive_export = archive.add_subparsers(dest='archive_command', required=True).add_parser('export')
    archive_export.add_argument('--output', type=Path, required=True)
    archive_export.add_argument('--space', choices=['personal', 'life', 'work'], action='append')
    archive_export.add_argument('--period', default='')
    archive_export.add_argument('--visibility', choices=['private', 'future_self', 'designated'])
    archive_export.add_argument('--encrypt', action='store_true', help='Prompt for a separate archive passphrase')
    args = parser.parse_args()
    directory = args.data_dir.expanduser().resolve()
    path = directory / 'vault.sqlite3'
    if args.command == 'models':
        from .embeddings import fetch_model
        try:
            print(fetch_model(directory / 'models' / 'bge-small-zh-v1.5'))
        except (ValueError, OSError):
            parser.error('无法下载已校验的本地模型；请检查 HTTPS 根证书、网络或 MELLOW_MODEL_MIRROR。词法检索仍可使用。')
        print('Model verified. Restart a running center to enable semantic retrieval.')
        return
    if args.command == 'tls':
        tls(directory, [], renew=True)
        print(json.dumps(tls_status(directory)))
        return
    if args.command == 'rotate-access-token':
        if not path.is_file():
            parser.error('Select the existing center data directory')
        from .owner_access import rotate
        print(rotate(directory))
        return
    if args.command == 'voice-setup':
        from .speech import setup
        try:
            setup(directory, args.model)
        except ImportError:
            parser.error('Install the voice extra first: pip install -e \"services/center[voice]\"')
        return
    if args.command == 'init':
        password = getpass.getpass('New vault passphrase (12+ characters): ')
        if password != getpass.getpass('Repeat passphrase: '):
            parser.error('Passphrases do not match')
        Vault.create(path, password)
        tls(directory, args.hostname)
        print('Initialized. TLS SHA-256:', fingerprint(directory))
        print('TLS SPKI SHA-256:', tls_status(directory)['tls_spki_sha256'])
        return
    if args.command == 'service-uninstall':
        from .macos_service import uninstall
        uninstall(directory)
        print('Mellow login service and its Keychain item removed. Data and backups were retained.')
        return
    try:
        if args.command == 'serve' and args.keychain:
            from .macos_service import load_key
            vault = Vault(path, load_key(directory))
            vault.read()  # Authenticate ciphertext before accepting connections.
        elif args.command == 'serve' and args.vault_key_file:
            if not path.is_file():
                raise ValueError('Vault does not exist.')
            vault = Vault(path, read_vault_key(args.vault_key_file))
            vault.read()
        else:
            vault = Vault.unlock(path, getpass.getpass('Vault passphrase: '))
    except Exception as error:
        from .maintenance import log_event
        log_event('VAULT_UNLOCK_FAILED', 'unlock', error)
        parser.error('Unable to unlock vault. Check path and passphrase; existing data was not reset.')
    if args.command == 'archive':
        from .archive_export import export_archive
        password = None
        if args.encrypt:
            password = getpass.getpass('Archive passphrase (12+ characters): ')
            if password != getpass.getpass('Repeat archive passphrase: '):
                parser.error('Passphrases do not match')
        with vault.view() as state:
            content = export_archive(state, args.space or ['personal'], period=args.period, visibility=args.visibility, passphrase=password)
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(content); target.flush(); os.fsync(target.fileno())
        print('Archive exported. Keep this independent copy private; it cannot be remotely revoked.')
    elif args.command == 'index':
        from .semantic_index import SemanticIndex
        semantic = SemanticIndex.local(vault)
        if not semantic.ready:
            parser.error('Local embedding model unavailable; run models fetch first.')
        try:
            count = semantic.refresh(rebuild=True)
        except Exception:
            parser.error('语义索引重建失败，原始记录未改变；请检查本地模型和运行环境。词法检索仍可使用。')
        print(json.dumps({**semantic.health(), 'embedded': count}))
    elif args.command == 'service-install':
        from .macos_service import install
        location = install(vault, args.host, args.port)
        print('Installed login service:', location)
        print('Requires an unlocked login Keychain. Quit a manually running center to release its worker lock.')
        print('The vault passphrase remains valid; uninstall never deletes your records.')
    elif args.command == 'pair':
        from urllib.parse import urlsplit
        for endpoint in [args.lan, args.tail]:
            if endpoint:
                url = urlsplit(endpoint)
                if url.scheme != 'https' or not url.hostname or url.username or url.query or url.fragment or url.path not in {'', '/'}:
                    parser.error('Endpoints must be HTTPS origins, e.g. https://192.168.1.10:8765')
        with vault.transaction() as state:
            invite = new_pairing(state, args.name, list(dict.fromkeys(args.space)), args.admin)
        invite.update(fingerprint=fingerprint(directory), tls_spki_sha256=tls_status(directory)['tls_spki_sha256'], lan=args.lan.rstrip('/'), tail=args.tail.rstrip('/'))
        print(json.dumps(invite, ensure_ascii=False, indent=2))
        print('Paste JSON into the pairing screen. Expires in 10 minutes; keep it private.')
    elif args.command == 'backup-policy':
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        if args.disable:
            with vault.transaction() as state:
                state.pop('backup_policy', None)
                state['credentials'].pop('backup', None)
            print('Automatic backups disabled; existing backup files retained.')
            return
        if args.directory is None or not 1 <= args.keep <= 90 or not 0 <= args.hour <= 23:
            parser.error('Provide --directory, --keep 1..90 and --hour 0..23.')
        try: ZoneInfo(args.timezone)
        except ZoneInfoNotFoundError: parser.error('Unknown IANA timezone.')
        password = getpass.getpass('Automatic backup recovery passphrase (12+ characters): ')
        if len(password) < 12 or password != getpass.getpass('Repeat recovery passphrase: '):
            parser.error('Passphrases must match and have at least 12 characters.')
        with vault.transaction() as state:
            state['credentials']['backup'] = password
            state['backup_policy'] = {'enabled': True, 'directory': str(args.directory.expanduser().resolve()),
                                      'keep': args.keep, 'hour': args.hour, 'timezone': args.timezone}
            state.pop('last_backup', None)
        print('Daily backup configured. Keep the recovery passphrase outside this Mac. Only verified same-owner backups are pruned.')
    elif args.command == 'status':
        from .semantic_index import SemanticIndex
        semantic = SemanticIndex.local(vault)
        semantic.refresh(max_batches=0)  # Load/prune the derived cache; status never embeds records.
        state = vault.read()
        print(json.dumps({'center_id': state['center_id'], 'records': len(state['records']),
                          'jobs': len(state['jobs']), 'last_backup': state.get('last_backup'),
                          **semantic.health(), **tls_status(directory)}, indent=2))
    elif args.command == 'backup':
        password = getpass.getpass('Backup passphrase (12+ characters): ')
        if password != getpass.getpass('Repeat backup passphrase: '):
            parser.error('Passphrases do not match')
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_private(args.output, json.dumps(vault.backup(password)))
        print('Encrypted backup created. Keep the passphrase separately.')
    elif args.command == 'recover-empty':
        from .api import restore_plan
        try:
            archive = open_backup(json.loads(args.input.read_text()), getpass.getpass('Backup passphrase: '))
            with vault.transaction() as state:
                if state['records'] or state['deleted'] or state['devices']:
                    parser.error('Recovery requires a newly initialized EMPTY center, before pairing.')
                state['owner_id'] = archive['owner_id']
                additions, _ = restore_plan(state, archive)
                state['records'], state['deleted'] = additions, archive['deleted']
                state['family_spaces'] = archive.get('family_spaces',{})
                state['usage_events'] = archive.get('usage_events',{})
                state['seq'] += 1
        except Exception as error:
            from .maintenance import log_event
            log_event('RECOVERY_FAILED', 'recover', error)
            parser.error('Recovery failed; destination transaction was rolled back.')
        print('Recovered. Re-pair devices and re-enter model credentials. Suggestions remain off.')
    elif args.command == 'serve':
        renew_if_due(directory)
        from .app import create_app
        from .worker import discover, recover_interrupted, run_once
        from .maintenance import automatic_backup, log_event, prune
        import uvicorn
        lock = open(directory / 'worker.lock', 'a+')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('A Mellow worker is already running for this vault.')

        @asynccontextmanager
        async def lifespan(_app):
            from .semantic_index import SemanticIndex
            semantic = await asyncio.to_thread(SemanticIndex.local, vault)
            _app.state.semantic_index = semantic  # Keep the process-local registry entry alive.
            announcement = None
            if not args.no_discovery:
                from .discovery import Announcement
                try:
                    announcement = await asyncio.to_thread(Announcement, args.host, args.port, vault.read()['center_id'])
                except Exception as error:
                    log_event('DISCOVERY_UNAVAILABLE', 'announcement', error)
            _app.state.worker_error = None
            _app.state.worker_status = {}
            recovered = False
            async def run_jobs():
                nonlocal recovered
                if not recovered:
                    await asyncio.to_thread(recover_interrupted, vault)
                    recovered = True
                def has_queued():
                    with vault.view() as state:
                        return any(j['state'] == 'queued' for j in state['jobs'].values())
                queued = await asyncio.to_thread(has_queued)
                if queued:
                    try:
                        await run_once(vault)
                    except Exception:
                        recovered = False
                        raise  # worker_tick logs only the redacted reason code.
            async def discover_step():
                await asyncio.to_thread(scheduled_work, vault, discover)
            from .automations import run_rules
            steps = {'semantic_index': lambda: asyncio.to_thread(semantic.refresh),
                     'run_once': run_jobs, 'discover': discover_step,
                     'run_rules': lambda: asyncio.to_thread(run_rules, vault),
                     'automatic_backup': lambda: asyncio.to_thread(automatic_backup, vault),
                     'prune': lambda: asyncio.to_thread(prune, vault)}
            async def loop():
                while True:
                    await worker_tick(vault, _app.state.worker_status, steps)
                    _app.state.worker_error = 'WORKER_RETRYING' if _app.state.worker_status.get('run_once', {}).get('consecutive_failures') else None
                    await asyncio.sleep(2)
            from .news import news_loop
            news_task = asyncio.create_task(news_loop(vault, _app.state.worker_status))
            task = asyncio.create_task(loop())
            yield
            news_task.cancel()
            with suppress(asyncio.CancelledError):
                await news_task
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if announcement:
                await asyncio.to_thread(announcement.close)
            lock.close()

        uvicorn.run(create_app(vault, lifespan), host=args.host, port=args.port, workers=1,
                    ssl_certfile=str(directory / 'tls.crt'), ssl_keyfile=str(directory / 'tls.key'),
                    access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
