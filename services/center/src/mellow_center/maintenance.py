"""Opt-in encrypted logical backups. No credentials in exported archives."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
from zoneinfo import ZoneInfo

from .vault import canonical, now, open_backup


def log_event(code, step, error=None, **counts):
    """Only caller-owned codes, exception types and numeric metrics; no messages."""
    fields = {key: value for key, value in counts.items() if type(value) in (int, float)}
    fields.update(code=code, step=step)
    if error is not None:
        fields['error_class'] = type(error).__name__
    logging.getLogger('mellow_center').warning(json.dumps(fields, sort_keys=True))


def prune(vault, clock=None):
    """Hourly bounded retention; never remove active jobs or live suggestions."""
    from .space_migration import migrate_owner_spaces
    migrate_owner_spaces(vault)
    instant = clock or datetime.now(timezone.utc)
    with vault.view() as state:
        if state.get('next_prune_at', '') > instant.isoformat():
            return 0
    removed = 0
    with vault.transaction() as state:
        if state.get('next_prune_at', '') > instant.isoformat():
            return 0
        sync_before = canonical([dict(state[name]) for name in ('jobs', 'suggestions', 'deleted')])
        policies = {'receipts': (30, 20000), 'jobs': (90, 5000),
                    'suggestions': (30, 5000), 'usage_events': (365, 100000),
                    'followup_deliveries': (30, 10000), 'connector_batches': (30, 2000),
                    'restore_receipts': (30, 1000), 'admin_receipts': (7, 2000),
                    'pairing': (1, 2000)}
        for name, (days, cap) in policies.items():
            items = state.get(name, {})
            eligible = []
            cutoff = (instant - timedelta(days=days)).isoformat()
            for key, item in items.items():
                stamp = item.get('finished_at') or item.get('at') or item.get('created_at') or item.get('expires_at') or item.get('expires')
                if not stamp:
                    # Give legacy undated receipts a complete retention window.
                    item['created_at'] = instant.isoformat()
                    stamp = item['created_at']
                if name == 'jobs' and item.get('state') in {'queued', 'running'}:
                    continue
                if name == 'suggestions' and item.get('state') not in {'expired','dismissed','used'} and item.get('expires_at', '~') > instant.isoformat():
                    continue
                if name == 'connector_batches' and item.get('status') == 'pending' and item.get('expires_at', '~') > instant.isoformat():
                    continue
                if name == 'followup_deliveries' and item.get('expires', '~') > instant.isoformat():
                    continue
                eligible.append((stamp, key))
            eligible.sort(reverse=True)
            for index, (stamp, key) in enumerate(eligible):
                if stamp < cutoff or index >= cap or (name == 'followup_deliveries' and items[key].get('expires', '~') <= instant.isoformat()):
                    if name == 'jobs' and items[key].get('result_id') in state['records']:
                        dependencies = state['private_items'].setdefault('job_dependencies', {})
                        result = items[key]['result_id']
                        dependencies[result] = sorted(set(dependencies.get(result, [])) | set(items[key].get('source_ids', [])))
                    del items[key]
                    removed += 1
        cutoff = (instant - timedelta(days=90)).isoformat()
        audit = state.get('admin_audit', [])
        state['admin_audit'] = [entry for entry in audit if entry.get('at', instant.isoformat()) >= cutoff][-5000:]
        state['security_events'] = [entry for entry in state.get('security_events', []) if entry.get('at', '') >= cutoff][-1000:]
        devices = [d for d in state['devices'].values() if not d['revoked'] and d['expires_at'] > instant.isoformat()]
        compacted = False
        for key, tombstone in list(state['deleted'].items()):
            if tombstone.get('memory_source_ids'):
                # This is durable consent/forgetting policy, not merely a sync
                # receipt. Acknowledging deletion must never re-enable its sources.
                continue
            revision = tombstone.get('store_revision')
            acknowledged = revision is not None and devices and all(d.get('ack_revision', -1) >= revision for d in devices)
            if acknowledged or tombstone['deleted_at'] < cutoff:
                del state['deleted'][key]
                removed += 1
                compacted = True
        if compacted:
            # Offline clients beyond this floor must rebuild rather than miss deletes.
            state['change_floor'] = state.store_revision + 1
            state['record_changes'] = []
        preview = state.get('restore_preview')
        if preview and preview['expires_at'] <= instant.isoformat():
            state.pop('restore_preview')
        dependencies = state['private_items'].get('job_dependencies', {})
        for identifier in list(dependencies):
            if identifier not in state['records']:
                del dependencies[identifier]
        profile_changed = False
        try:
            from .profile_consolidation import consolidate_due
            profile_changed = consolidate_due(state, instant)
        except Exception as error:
            # Retried at the next hourly prune; never blocks retention.
            log_event('PROFILE_CONSOLIDATION_FAILED', 'prune', error)
            profile_changed = True  # Also publish any bounded changes made before failure.
        from .archive import due_letters
        from .legacy import tick as legacy_tick
        from .digital_self import style_due
        archive_changed = due_letters(state, instant)
        archive_changed = legacy_tick(state, instant) or archive_changed
        style_due(state, instant)
        state['next_prune_at'] = (instant + timedelta(hours=1)).isoformat()
        state.bump_revision = archive_changed or profile_changed or sync_before != canonical([dict(state[name]) for name in ('jobs', 'suggestions', 'deleted')])
    if removed:
        log_event('RETENTION_PRUNED', 'prune', count=removed)
    return removed


def automatic_backup(vault, clock=None):
    with vault.view() as current:
        state = {key: current.get(key) for key in ('backup_policy', 'last_backup', 'credentials', 'owner_id')}
    state['last_backup'] = state['last_backup'] or {}
    policy = state.get('backup_policy')
    if not policy or not policy.get('enabled'):
        return
    instant = clock or datetime.now(timezone.utc)
    retry = state.get('last_backup', {}).get('retry_after')
    if retry and instant < datetime.fromisoformat(retry):
        return
    local = instant.astimezone(ZoneInfo(policy['timezone']))
    day = local.date().isoformat()
    if local.hour < policy['hour'] or state.get('last_backup', {}).get('date') == day:
        return
    temporary = None
    try:
        destination = Path(policy['directory'])
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = destination / ('mellow-auto-' + day + '.json')
        password = state['credentials']['backup']
        if target.exists():
            # A crash after rename but before the receipt must not destroy a backup.
            archived = open_backup(json.loads(target.read_text()), password)
            if archived['owner_id'] != state['owner_id']:
                raise ValueError('backup belongs to another owner')
        else:
            bundle = vault.backup(password)
            fd, temporary = tempfile.mkstemp(prefix='.mellow-', suffix='.tmp', dir=destination)
            with os.fdopen(fd, 'w') as stream:
                json.dump(bundle, stream)
                stream.flush(); os.fsync(stream.fileno())
            # Hard-link publication refuses overwriting a file created concurrently.
            os.link(temporary, target)
            os.unlink(temporary); temporary = None
            descriptor = os.open(destination, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        # Validate the new archive before pruning older files.
        open_backup(json.loads(target.read_text()), password)
        candidates = sorted(destination.glob('mellow-auto-????-??-??.json'), reverse=True)
        owned = []
        for old in candidates:
            # Only delete same-owner archives that authenticate with this policy's key.
            try:
                data = open_backup(json.loads(old.read_text()), password)
                if data['owner_id'] == state['owner_id']:
                    owned.append(old)
            except Exception as error:
                log_event('BACKUP_ARCHIVE_SKIPPED', 'backup_prune', error)
                continue
        for old in owned[policy['keep']:]:
            old.unlink()
        with vault.transaction(bump_revision=False) as current:
            if current.get('backup_policy') == policy:
                current['last_backup'] = {'date': day, 'finished_at': now(), 'state': 'succeeded'}
    except Exception as error:
        log_event('BACKUP_FAILED_CHECK_DESTINATION', 'automatic_backup', error)
        with vault.transaction(bump_revision=False) as current:
            current['last_backup'] = {'date': None, 'finished_at': now(), 'state': 'failed',
                                      'error': 'BACKUP_FAILED_CHECK_DESTINATION',
                                      'retry_after': (instant + timedelta(minutes=10)).isoformat()}
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
