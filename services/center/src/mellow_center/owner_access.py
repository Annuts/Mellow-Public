"""Locally provisioned owner enrollment. Rotation preserves issued devices."""
from datetime import datetime, timedelta, timezone
import hmac
from pathlib import Path
import secrets
import tempfile

from .domain import Problem, register_device
from .vault import audit_failure, digest, now

PAIRING_ID = "owner-access-v1"


def rotate(directory):
    token = "xm_" + secrets.token_urlsafe(32)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as out:
        out.write(digest(token))
    Path(out.name).replace(directory / "owner-access.sha256")
    return token


def enroll(vault, state, token, name, *, admin):
    try:
        saved = (vault.path.parent / "owner-access.sha256").read_text()
    except FileNotFoundError:
        saved = ""
    throttle = state.setdefault('owner_enrollment', {})
    if throttle.get('generation') != saved:
        throttle.clear()
        throttle.update(generation=saved, failures=0)
    valid = bool(saved) and hmac.compare_digest(saved, digest(token))
    if not valid and throttle.get('retry_at', '') > now():
        return None
    if not valid:
        failures = min(16, throttle.get('failures', 0) + 1)
        throttle.update(failures=failures, retry_at=(datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** (failures - 1)))).isoformat())
        audit_failure(state, 'owner.enrollment_failed')
        return None  # Commit the throttle/audit before the HTTP caller raises.
    throttle.update(failures=0, retry_at='')
    spaces = ["personal"]
    if admin:
        spaces.extend(state.get("family_spaces", {}))
    result = register_device(state, dict(name=name, spaces=spaces, admin=admin),
                             datetime.now(timezone.utc) + timedelta(days=365))
    state['devices'][result['device_id']]['owner_enrolled'] = True
    return result


def reuse_browser(state, result, browser_key):
    """Only called after successful enrollment/invitation; rotate, never expose tokens."""
    hashed = digest(browser_key)
    new_id = result['device_id']
    new = state['devices'][new_id]
    old_id = next((key for key, device in state['devices'].items()
                   if key != new_id and device.get('browser_key') == hashed), None)
    if old_id:
        created = state['devices'][old_id]['created_at']
        state['devices'][old_id] = {**new, 'created_at': created}
        del state['devices'][new_id]
        result['device_id'] = old_id
    state['devices'][result['device_id']]['browser_key'] = hashed
    return result


def renew_access(vault, token):
    """Read-only unless an owner device needs its daily rolling extension."""
    hashed = digest(token)
    with vault.view() as state:
        device = next((d for d in state['devices'].values() if d['token_hash'] == hashed), None)
        if not device or device['revoked'] or not device.get('owner_enrolled') or device['expires_at'] <= now():
            return None
        due = datetime.fromisoformat(device['expires_at']) < datetime.now(timezone.utc) + timedelta(days=364)
    if not due:
        return None
    from .domain import authenticate
    try:
        with vault.transaction(bump_revision=False) as state:
            _, device = authenticate(state, token)
            device['expires_at'] = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
            return device['expires_at']
    except Problem:
        return None  # Revocation between the read check and writer lock wins.
