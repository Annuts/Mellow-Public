"""Opt-in login service. Only explicit CLI commands touch launchd or Keychain.

Keys go through a private stdin pipe, never argv, environment, plist or logs.
"""
from __future__ import annotations
import base64
import hashlib
import os
from pathlib import Path
import plistlib
import secrets
import subprocess
import sys

SERVICE = 'com.mekhicode.mellow.center.vault'


def account(directory):
    return hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()


def label(directory):
    return 'com.mekhicode.mellow.center.' + account(directory)[:16]


def load_key(directory, runner=subprocess.run):
    result = runner(['/usr/bin/security', 'find-generic-password', '-s', SERVICE,
                     '-a', account(directory), '-w'], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Mellow vault key is unavailable in the login Keychain.')
    try:
        key = base64.b64decode(result.stdout.strip(), validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except ValueError:
        raise RuntimeError('Invalid Mellow Keychain item.') from None


def store_key(directory, key, runner=subprocess.run):
    # All values have a fixed safe alphabet; security's interactive command parser
    # receives no user text. Never use -A (unrestricted item access).
    encoded = base64.b64encode(key).decode()
    command = f'add-generic-password -U -s {SERVICE} -a {account(directory)} -w {encoded}\nquit\n'
    result = runner(['/usr/bin/security', '-i'], input=command,
                    capture_output=True, text=True, timeout=30)
    if result.returncode or not secrets.compare_digest(load_key(directory, runner), key):
        raise RuntimeError('Could not verify the Mellow Keychain item; service was not installed.')


def install(vault, host='127.0.0.1', port=8765, *, home=None, runner=subprocess.run):
    directory = vault.path.parent.resolve()
    home = Path.home() if home is None else Path(home)
    store_key(directory, vault.key, runner)
    agents = home / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    target = agents / (label(directory) + '.plist')
    log = directory / 'service.log'
    log.touch(mode=0o600, exist_ok=True)
    data = {
        'Label': label(directory),
        'ProgramArguments': [sys.executable, '-m', 'mellow_center', '--data-dir', str(directory),
                             'serve', '--keychain', '--host', host, '--port', str(port)],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 15,
        'WorkingDirectory': str(directory),
        'StandardOutPath': str(log), 'StandardErrorPath': str(log),
        'ProcessType': 'Background', 'Umask': 0o077,
    }
    temporary = target.with_suffix('.plist.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(plistlib.dumps(data))
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(target)
    domain = 'gui/' + str(os.getuid())
    runner(['/bin/launchctl', 'bootout', domain + '/' + label(directory)], capture_output=True, timeout=20)
    result = runner(['/bin/launchctl', 'bootstrap', domain, str(target)], capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('LaunchAgent file created, but launchctl bootstrap failed. Check the local login session.')
    return target


def uninstall(directory, *, home=None, runner=subprocess.run):
    home = Path.home() if home is None else Path(home)
    runner(['/bin/launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + label(directory)],
           capture_output=True, timeout=20)
    target = home / 'Library/LaunchAgents' / (label(directory) + '.plist')
    target.unlink(missing_ok=True)
    runner(['/usr/bin/security', 'delete-generic-password', '-s', SERVICE, '-a', account(directory)],
           capture_output=True, timeout=30)
    # Database, backups, TLS identity and the user's independent passphrase survive.
