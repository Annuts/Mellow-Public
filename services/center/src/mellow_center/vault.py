"""Transactional encrypted records with keyed indexes and a lazy unit of work.

Bodies and logical identifiers are encrypted; SQL stores only collection names and
HMAC lookup keys. Legacy whole-document vaults migrate atomically on the next write.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
from collections.abc import MutableMapping
import json
import os
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3
import threading
from typing import Any
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DEFAULT_SUGGESTION_LIMIT = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid4())


def audit_failure(state, action):
    """Anonymous failures cannot evict the owner's administrative audit trail."""
    entries = state.setdefault('security_events', [])
    entries.append(dict(id=uid(), at=now(), actor='anonymous', action=action))
    del entries[:-1000]


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)


def seal_raw(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def seal(key: bytes, data, aad: bytes) -> bytes:
    return seal_raw(key, canonical(data), aad)


def unseal(key: bytes, data: bytes, aad: bytes):
    return json.loads(AESGCM(key).decrypt(data[:12], data[12:], aad))


def empty_state() -> dict[str, Any]:
    return dict(version=1, owner_id=uid(), center_id=uid(), seq=0, owner_personal_space_v1=True,
                records={}, deleted={}, receipts={}, devices={}, pairing={},
                jobs={}, usage_events={}, model=None, credentials={}, budgets={},
                preferences={"proactive": True, "daily_limit": DEFAULT_SUGGESTION_LIMIT,
                             "quiet_start": 22, "quiet_end": 8,
                             "field_versions": {key: {"version": 0, "origin": "default"}
                                                for key in ("proactive", "daily_limit", "quiet_start", "quiet_end")}},
                suggestions={}, private_items={}, semantic_vectors={})


COLLECTIONS = ("records", "deleted", "receipts", "jobs", "suggestions", "usage_events", "private_items", "semantic_vectors")
ITEM_DDL = "CREATE TABLE IF NOT EXISTS vault_items (collection TEXT NOT NULL, key_hash BLOB NOT NULL, body BLOB NOT NULL, PRIMARY KEY(collection,key_hash))"


class VaultState(dict[str, Any]):
    store_revision = 0
    bump_revision = True

    def record_index(self):
        if not hasattr(self, '_record_index'):
            self._record_index = build_record_index(self['records'], self.get('jobs', {}), self.get('private_items', {}).get('job_dependencies', {}))
        return self._record_index


def build_record_index(records, jobs=None, dependencies=None):
    from collections import defaultdict
    keys, parents, sources = defaultdict(set), defaultdict(set), defaultdict(set)
    job_sources, job_results = defaultdict(set), defaultdict(set)
    for r in records.values():
        d, identifier = r['data'], r['id']
        if r.get('kind') == 'memory' and d.get('key'):
            from .spaces import normalize_space
            keys[normalize_space(r['space']), d['key']].add(identifier)
        if d.get('parent_id'):
            parents[d['parent_id']].add(identifier)
        references = d.get('sources', []) + d.get('used_memories', [])
        for ref in references:
            sources[ref['id']].add(identifier)
        origin = d.get('origin', {})
        if isinstance(origin, dict) and origin.get('format') == 'phone-model-v1':
            sources[origin['source_id']].add(identifier)
            for ref in origin.get('context_refs', []):
                sources[ref['id']].add(identifier)
    for key, job in (jobs or {}).items():
        for parent in job.get('source_ids', []):
            job_sources[parent].add(key)
            if job.get('result_id') in records:
                sources[parent].add(job['result_id'])
        if job.get('result_id'):
            job_results[job['result_id']].add(key)
    for result, source_ids in (dependencies or {}).items():
        if result in records:
            for parent in source_ids:
                sources[parent].add(result)
    return dict(keys=keys, parents=parents, sources=sources, job_sources=job_sources, job_results=job_results)


def record_index(state):
    return state.record_index() if isinstance(state, VaultState) else build_record_index(state['records'], state.get('jobs', {}), state.get('private_items', {}).get('job_dependencies', {}))


def clear_indexes(state):
    if isinstance(state, VaultState):
        state.__dict__.pop('_record_index', None)


class EncryptedMap(MutableMapping[str, Any]):
    def __init__(self, db, key, collection, identifiers, shared=None, lock=None):
        self.db, self.key, self.collection = db, key, collection
        self.index = dict.fromkeys(identifiers)
        self.initial = set(identifiers)
        self.cache, self.original = {}, {}
        self.shared = shared if shared is not None else {}
        self.lock = lock or threading.RLock()

    def key_hash(self, name):
        return hmac.new(self.key, (self.collection + "\0" + name).encode(), hashlib.sha256).digest()

    def aad(self, name):
        return b"mellow-record-1\0" + self.collection.encode() + self.key_hash(name)

    def read_raw(self, name):
        row = self.db.execute("SELECT body FROM vault_items WHERE collection=? AND key_hash=?",
                              (self.collection, self.key_hash(name))).fetchone()
        if row is None:
            raise ValueError("Encrypted record missing")
        cipher = row[0]
        return AESGCM(self.key).decrypt(cipher[:12], cipher[12:], self.aad(name))

    def __getitem__(self, name):
        if name not in self.index:
            raise KeyError(name)
        if name not in self.cache:
            with self.lock:
                if name not in self.shared:
                    raw = self.read_raw(name)
                    self.shared[name] = (raw, json.loads(raw))
                raw, value = self.shared[name]
            self.original[name] = raw
            self.cache[name] = copy.deepcopy(value)
        return self.cache[name]

    def __setitem__(self, name, value):
        if not isinstance(name, str):
            raise TypeError("Record keys must be strings")
        self.index[name] = None
        self.cache[name] = value

    def __delitem__(self, name):
        if name not in self.index:
            raise KeyError(name)
        del self.index[name]
        self.cache.pop(name, None)

    def __iter__(self):
        return iter(self.index)

    def __len__(self):
        return len(self.index)

    def __contains__(self, name):
        return name in self.index

    def __deepcopy__(self, memo):
        return {name: copy.deepcopy(self[name], memo) for name in self.index}

    def flush(self):
        changed = False
        self.changed_keys = set()
        for name in self.initial - set(self.index):
            self.db.execute("DELETE FROM vault_items WHERE collection=? AND key_hash=?",
                            (self.collection, self.key_hash(name)))
            changed = True
            self.changed_keys.add(name)
        for name, value in self.cache.items():
            if name not in self.index:
                continue
            raw = canonical(value)
            before = self.original.get(name)
            if before is None and name in self.initial:
                before = self.read_raw(name)
            if raw != before:
                self.db.execute("INSERT INTO vault_items VALUES(?,?,?) ON CONFLICT(collection,key_hash) DO UPDATE SET body=excluded.body",
                                (self.collection, self.key_hash(name), seal_raw(self.key, raw, self.aad(name))))
                changed = True
                self.changed_keys.add(name)
        return changed


class Vault:
    AAD = b"mellow-vault-preview-1"
    _owner_space_migrated: bool = False

    def __init__(self, path: Path, key: bytes):
        self.path, self.key = Path(path), key
        self._cache_lock = threading.RLock()
        self._header_body, self._header_raw = None, None
        self._header_value: dict[str, Any] = {}
        self._items = {}

    @classmethod
    def create(cls, path: Path, password: str) -> "Vault":
        if len(password) < 12:
            raise ValueError("Use at least 12 characters for the vault passphrase.")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # O_EXCL refuses accidental overwrite, including symlinks.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        salt = secrets.token_bytes(16)
        vault = cls(path, derive(password, salt))
        with closing(vault.connect()) as db, db:
            db.execute("CREATE TABLE vault (id INTEGER PRIMARY KEY CHECK(id=1), salt BLOB NOT NULL, body BLOB NOT NULL)")
            db.execute(ITEM_DDL)
            header = empty_state()
            for name in COLLECTIONS:
                del header[name]
            header.update(_collections={name: [] for name in COLLECTIONS}, _revision=0)
            db.execute("INSERT INTO vault VALUES(1,?,?)", (salt, seal(vault.key, header, cls.AAD)))
        vault._owner_space_migrated = True
        return vault

    @classmethod
    def unlock(cls, path: Path, password: str) -> "Vault":
        path = Path(path)
        if not path.is_file():
            raise ValueError("Vault not initialized; run init first.")
        with closing(sqlite3.connect(f"file:{path}?mode=rw", uri=True)) as db:
            salt, body = db.execute("SELECT salt,body FROM vault WHERE id=1").fetchone()
        key = derive(password, salt)
        state = unseal(key, body, cls.AAD)
        if state.get("version") != 1:
            raise ValueError("Unsupported vault version")
        vault = cls(path, key)
        from .space_migration import migrate_owner_spaces
        migrate_owner_spaces(vault)
        return vault

    def connect(self):
        db = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        return db

    def _load(self, db):
        body = db.execute("SELECT body FROM vault WHERE id=1").fetchone()[0]
        with self._cache_lock:
            if body != self._header_body:
                raw = AESGCM(self.key).decrypt(body[:12], body[12:], self.AAD)
                self._header_body, self._header_raw = body, raw
                self._header_value = json.loads(raw)
                self._items = {}
            raw, shared = self._header_raw, self._items
            header = copy.deepcopy(self._header_value)
        indexes = header.pop("_collections", None)
        revision = header.pop("_revision", 0)
        state = VaultState(header)
        state.store_revision = revision
        maps = {}
        if indexes is not None:
            for name in COLLECTIONS:
                maps[name] = EncryptedMap(db, self.key, name, indexes.get(name, []),
                                         shared.setdefault(name, {}), self._cache_lock)
                state[name] = maps[name]
            # Collections added after preview-1 may still live in a legacy header.
            for name in COLLECTIONS:
                if name in header:
                    state[name] = header[name]
        for name in COLLECTIONS:
            state.setdefault(name, {})
        return state, maps, raw

    def _commit(self, db, state, maps, original, bump_revision=True):
        db.execute(ITEM_DDL)
        preview = state.get('restore_preview')
        if preview and 'archive' in preview:
            state['private_items']['restore_archive'] = preview.pop('archive')
        if not preview:
            state['private_items'].pop('restore_archive', None)
        changed = not maps  # A legacy document is migrated within this transaction.
        metadata = {k: v for k, v in state.items() if k not in COLLECTIONS}
        indices = {}
        record_changes = set()
        changes = {}
        for name in COLLECTIONS:
            collection = maps[name] if name in maps else EncryptedMap(db, self.key, name, [])
            values = state.get(name, {})
            if values is not collection:
                for identifier in list(collection):
                    if identifier not in values:
                        del collection[identifier]
                for identifier, value in values.items():
                    collection[identifier] = value
            changed = collection.flush() or changed
            if name == 'records': record_changes = collection.changed_keys
            changes[name] = collection.changed_keys
            indices[name] = list(collection)
        metadata.update(_collections=indices, _revision=state.store_revision)
        encoded = canonical(metadata)
        if changed or encoded != original:
            metadata["_revision"] += int(bump_revision)
            if bump_revision:
                journal = list(metadata.get('record_changes', []))
                floor = metadata.get('change_floor', state.store_revision)
                journal.append({'revision': metadata['_revision'], 'ids': sorted(record_changes),
                                'jobs': sorted(changes['jobs']), 'deleted': sorted(changes['deleted'])})
                size = sum(sum(len(entry.get(k, [])) for k in ('ids', 'jobs', 'deleted')) for entry in journal)
                while len(journal) > 256 or size > 20000:
                    old = journal.pop(0); floor = old['revision']
                    size -= sum(len(old.get(k, [])) for k in ('ids', 'jobs', 'deleted'))
                metadata.update(record_changes=journal, change_floor=floor)
            encoded = canonical(metadata)
            body = seal_raw(self.key, encoded, self.AAD)
            db.execute("UPDATE vault SET body=? WHERE id=1", (body,))
            return body, encoded, metadata, changes
        return None

    @contextmanager
    def transaction(self, *, bump_revision=True):
        """Use bump_revision=False only for bookkeeping/streaming, not sync changes.

        Every write replaces the encrypted header. Locally known writes invalidate
        only changed items; external writes conservatively invalidate all items.
        Streaming callers must publish their final job with a normal transaction.
        """
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            state, maps, original = self._load(db)
            yield state
            publication = self._commit(db, state, maps, original, bump_revision and state.bump_revision)
            cached_header = json.loads(publication[1]) if publication is not None else {}
            with self._cache_lock:
                db.commit()
                if publication is not None:
                    body, raw, metadata, changes = publication
                    # Never mutate a cache generation held by an older WAL reader.
                    items = self._items if self._header_raw == original else {}
                    self._items = {name: {key: value for key, value in items.get(name, {}).items()
                                          if key not in changes.get(name, ())}
                                   for name in COLLECTIONS}
                    self._header_body, self._header_raw, self._header_value = body, raw, cached_header
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def view(self):
        db = self.connect()
        try:
            db.execute("BEGIN")
            state, _, _ = self._load(db)
            yield state
        finally:
            db.rollback()
            db.close()

    def read(self) -> dict[str, Any]:
        with self.view() as state:
            return copy.deepcopy(dict(state))

    def backup(self, password: str) -> dict[str, Any]:
        if len(password) < 12:
            raise ValueError("Backup passphrase must have at least 12 characters")
        # Never export credentials, session tokens, job prompts or pairing tokens.
        with self.view() as state:
            data = {k: copy.deepcopy(state[k]) for k in ("version", "owner_id", "records", "deleted", "preferences", "usage_events")}
            if state.get('family_spaces'): data['family_spaces'] = copy.deepcopy(state['family_spaces'])
        salt = secrets.token_bytes(16)
        body = seal(derive(password, salt), data, b"mellow-backup-preview-1")
        return {"format": "mellow-backup-preview-1", "salt": base64.b64encode(salt).decode(),
                "body": base64.b64encode(body).decode()}


def open_backup(bundle: dict[str, Any], password: str) -> dict[str, Any]:
    if set(bundle) != {"format", "salt", "body"} or bundle["format"] != "mellow-backup-preview-1":
        raise ValueError("Unsupported backup")
    salt = base64.b64decode(bundle["salt"], validate=True)
    if len(salt) != 16:
        raise ValueError("Invalid salt")
    body = base64.b64decode(bundle["body"], validate=True)
    data = unseal(derive(password, salt), body, b"mellow-backup-preview-1")
    required = {"version", "owner_id", "records", "deleted", "preferences"}
    if not required <= set(data) <= required | {'family_spaces','usage_events'} or data["version"] != 1:
        raise ValueError("Invalid backup contents")
    from .usage import validate_archive
    validate_archive(data.get('usage_events', {}))
    from .space_migration import migrate_state
    migrate_state(data)
    return data
