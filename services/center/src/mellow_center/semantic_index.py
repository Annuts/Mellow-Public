"""Encrypted derived vectors and privacy-filtered, in-memory cosine scoring.

Embedding batches and worker query preparation run outside vault write locks.
Each successful batch is durable; corrupt cache rows and failed documents cannot
poison healthy rows. Neither vectors nor queries are written outside the vault.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from contextlib import AbstractContextManager
import hashlib
import threading
import weakref

from .search_text import record_text
from .vault import Vault, VaultState, canonical

_indexes = weakref.WeakValueDictionary()


class InferenceUnavailable(RuntimeError):
    """Retry a runtime outage instead of quarantining every pending document."""


def index_for(state):
    return _indexes.get(state.get('center_id'))


def semantic_health(state):
    # This endpoint is public for discovery: never disclose cross-space counts.
    index = index_for(state)
    return {'semantic': index.status if index else 'disabled'}


def signature(record):
    return hashlib.sha256(canonical([record.get('revision'), record_text(record)])).hexdigest()


class SemanticIndex:
    def __init__(self, vault: Vault, embedder=None):
        self.vault, self.embedder = vault, embedder
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._rows = {}
        self._queries = OrderedDict()
        self._failed = {}
        self._revision = None
        self.reason = None if embedder else 'model_unavailable'
        with vault.view() as state:
            self.center_id = state['center_id']
        _indexes[self.center_id] = self

    @classmethod
    def local(cls, vault: Vault):
        from .embeddings import LocalEmbedder, model_directory
        try:
            embedder = LocalEmbedder(model_directory(vault.path.parent))
        except Exception:
            embedder = None
        return cls(vault, embedder)

    @property
    def ready(self):
        return self.embedder is not None and self.reason is None

    @property
    def status(self):
        """Coarse public state only: ready, indexing (catching up) or disabled."""
        with self._lock:
            if not self.ready:
                return 'disabled'
            return 'ready' if self._revision is not None else 'indexing'

    def health(self):
        """Detailed diagnostics for the passphrase-protected CLI/evaluation only."""
        with self._lock:
            return dict(semantic='ready' if self.ready else 'disabled',
                        semantic_reason=self.reason, semantic_indexed=len(self._rows),
                        semantic_failed=len(self._failed),
                        semantic_model=self.embedder.model_id if self.embedder else None,
                        semantic_threshold=self.embedder.threshold if self.embedder else None)

    def _decode(self, row):
        import numpy as np
        vector = np.frombuffer(base64.b64decode(row['vector'], validate=True), dtype='<f4')
        return self._normalized(vector[None, :], 1)[0]

    def _write(self) -> AbstractContextManager[VaultState]:
        """Derived bookkeeping must not advance the phone's sync cursor."""
        return self.vault.transaction(bump_revision=False)

    def _prune(self, valid, rows):
        # Delete before replacements: EncryptedMap overwrites authenticate their
        # old body, whereas deletion can remove a damaged derived row directly.
        with self._write() as mutable:
            collection = mutable.setdefault('semantic_vectors', {})
            for key in list(collection):
                record = mutable['records'].get(key)
                if key not in valid or record is None or signature(record) != valid[key]['signature']:
                    del collection[key]
                    rows.pop(key, None)

    def refresh(self, *, rebuild=False, batch_size=16, max_batches=4):
        """Coalesce writes and commit bounded inference batches independently.

        Bad derived rows are deleted without decrypting them again. Even without
        a model, deleted/edited records are purged from the encrypted collection.
        A failed document is quarantined until its signature changes or rebuild.
        """
        if not self._refresh_lock.acquire(blocking=False):
            return 0
        try:
            with self.vault.view() as state:
                if not rebuild and state.store_revision == self._revision and self.reason != 'inference_unavailable':
                    return 0
                revision = state.store_revision
                records = {r['id']: (signature(r), record_text(r))
                           for r in state['records'].values() if record_text(r).strip()}
                valid, rows = {}, {}
                collection = state.get('semantic_vectors', {})
                # Rebuild deliberately never reads old, potentially corrupt bodies.
                for key in ([] if rebuild else collection):
                    try:
                        row = collection[key]
                        if key not in records or row['signature'] != records[key][0]:
                            continue
                        if self.embedder is not None:
                            if row['model'] != self.embedder.model_id:
                                continue
                            rows[key] = (row['signature'], self._decode(row))
                        valid[key] = row
                    except Exception:
                        continue  # Derived cache corruption is repairable, not source loss.
            if rebuild:
                self._failed.clear()
            self._failed = {key: stamp for key, stamp in self._failed.items()
                            if key in records and records[key][0] == stamp}
            self._prune(valid, rows)
            with self._lock:
                self._rows = dict(rows)
            if self.embedder is None:
                self._revision = revision
                return 0
            # Cleanup must run even when a constructed model cannot infer.
            if self.reason == 'inference_unavailable' and self.prime_query('语义检索自检') is None:
                return 0
            pending = [key for key in records if key not in valid and key not in self._failed]
            selected = pending if rebuild else pending[:batch_size * max_batches]
            completed = 0
            for start in range(0, len(selected), batch_size):
                keys = selected[start:start + batch_size]
                vectors = self._embed_batch(keys, records)
                with self._write() as state:
                    for key, vector in vectors.items():
                        record = state['records'].get(key)
                        if record is None or signature(record) != records[key][0]:
                            continue
                        state['semantic_vectors'][key] = dict(signature=records[key][0],
                            model=self.embedder.model_id,
                            vector=base64.b64encode(vector.astype('<f4').tobytes()).decode())
                        rows[key] = (records[key][0], vector)
                        completed += 1
                with self._lock:
                    self._rows = dict(rows)
            with self.vault.view() as state:
                current = {r['id']: signature(r) for r in state['records'].values() if record_text(r).strip()}
                unchanged = state.store_revision == revision and current == {key: item[0] for key, item in records.items()}
            with self._lock:
                self.reason = 'index_unavailable' if self._failed and not rows else None
                self._revision = revision if unchanged and len(selected) == len(pending) else None
            return completed
        except InferenceUnavailable:
            with self._lock:
                self.reason = 'inference_unavailable'
                self._revision = None
                self._queries.clear()
            if rebuild:
                raise
            return 0
        except Exception:
            with self._lock:
                self.reason = 'index_unavailable'
                self._revision = None
            if rebuild:
                raise
            return 0
        finally:
            self._refresh_lock.release()

    def _embed_batch(self, keys, records):
        embedder = self.embedder
        if embedder is None:
            return {}
        try:
            vectors = self._normalized(embedder.embed([records[key][1] for key in keys]), len(keys))
            return dict(zip(keys, vectors))
        except Exception:
            # Isolate a pathological document; healthy documents and earlier
            # batches stay usable and durable rather than globally disabling RRF.
            vectors = {}
            failed = []
            for key in keys:
                try:
                    vectors[key] = self._normalized(embedder.embed([records[key][1]]), 1)[0]
                except Exception:
                    failed.append(key)
            if failed and not vectors:
                try:
                    self._normalized(embedder.embed(['语义检索自检']), 1)
                except Exception:
                    raise InferenceUnavailable('Embedding runtime unavailable') from None
            for key in failed:
                self._failed[key] = records[key][0]
            return vectors

    def _normalized(self, vectors, count):
        import numpy as np
        if self.embedder is None:
            raise ValueError('Embedding model unavailable')
        array = np.asarray(vectors, dtype=np.float32)
        if array.shape != (count, self.embedder.dimensions) or not np.isfinite(array).all():
            raise ValueError('Invalid embedding shape or values')
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if (norms <= 0).any():
            raise ValueError('Zero embedding')
        return array / norms

    def prime_query(self, query, *, cache_only=False):
        """Worker calls this BEFORE BEGIN IMMEDIATE; scores can then be cache-only."""
        embedder = self.embedder
        if embedder is None or not query.strip():
            return None
        key = hashlib.sha256(query.encode()).digest()
        with self._lock:
            if key in self._queries:
                self._queries.move_to_end(key)
                return self._queries[key]
        if cache_only:
            return None
        try:
            vector = self._normalized(embedder.embed([query], query=True), 1)[0]
            with self._lock:
                self._queries[key] = vector
                while len(self._queries) > 128:
                    self._queries.popitem(last=False)
                if self.reason == 'inference_unavailable':
                    self.reason = None
            return vector
        except Exception:
            with self._lock:
                self.reason = 'inference_unavailable'
                self._queries.clear()
            return None

    def scores(self, records, query, *, cache_only=False):
        """Filter before matrix multiplication, never a global top-k then filter."""
        embedder = self.embedder
        if not self.ready or embedder is None or not query.strip():
            return {}
        try:
            import numpy as np
            with self._lock:
                selected = [(r['id'], self._rows[r['id']][1]) for r in records
                            if r['id'] in self._rows and self._rows[r['id']][0] == signature(r)]
            if not selected:
                return {}
            vector = self.prime_query(query, cache_only=cache_only)
            if vector is None:
                return {}
            similarities = np.stack([row[1] for row in selected]) @ vector
            return {row[0]: float(score) for row, score in zip(selected, similarities)
                    if score >= embedder.threshold}
        except Exception:
            with self._lock:
                self.reason = 'inference_unavailable'
            return {}
