"""Preview domain, shared by HTTP and the durable worker; no framework imports."""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timedelta, timezone
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from .vault import audit_failure, canonical, clear_indexes, digest, now, record_index, uid
from .spaces import normalize_space, normalize_spaces


class Problem(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code="INVALID_ARGUMENT", status=400):
    if not condition:
        raise Problem(code, status)


def fields(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional))


def string(value, limit=65536):
    require(isinstance(value, str) and 0 < len(value) <= limit)
    return value


def new_pairing(state, name: str, spaces: list[str], admin=False):
    from .sharing import valid_space
    spaces = normalize_spaces(spaces)
    require(spaces and all(valid_space(space) for space in spaces))
    require(all(space in {'personal','life','work'} or space in state.get('family_spaces',{}) for space in spaces))
    if admin:
        spaces = list(dict.fromkeys([*spaces, *state.get('family_spaces',{})]))
    token, pairing_id = secrets.token_urlsafe(32), uid()
    state["pairing"][pairing_id] = dict(hash=digest(token), name=string(name, 80), spaces=spaces,
        admin=admin, failures=0, expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
    return dict(pairing_id=pairing_id, token=token, expires_at=state["pairing"][pairing_id]["expires_at"],
                center_id=state["center_id"])


def pair(state, pairing_id, token):
    entry = state["pairing"].get(pairing_id)
    if not entry:
        guard = state.setdefault('pairing_guard', {'failures': 0})
        if guard.get('retry_at', '') <= now():
            guard['failures'] = min(16, guard['failures'] + 1)
            guard['retry_at'] = (datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** (guard['failures'] - 1)))).isoformat()
            audit_failure(state, 'pairing.failed')
        return None
    if entry["expires_at"] <= now() or entry["failures"] >= 5:
        return None
    if entry.get('retry_at', '') > now():
        return None
    if not secrets.compare_digest(entry["hash"], digest(token)):
        entry["failures"] += 1
        entry['retry_at'] = (datetime.now(timezone.utc) + timedelta(seconds=min(60, 2 ** (entry['failures'] - 1)))).isoformat()
        audit_failure(state, 'pairing.failed')
        return None  # Caller commits failed attempt before raising.
    del state["pairing"][pairing_id]
    return register_device(state, entry, datetime.now(timezone.utc) + timedelta(days=30))


def register_device(state, entry, expiry):
    entry = {**entry, 'spaces': normalize_spaces(entry['spaces'])}
    token, device_id = secrets.token_urlsafe(48), uid()
    state["devices"][device_id] = dict(name=entry["name"], spaces=entry["spaces"], admin=entry["admin"],
        token_hash=digest(token), revoked=False, created_at=now(),
        expires_at=expiry.isoformat())
    return dict(device_id=device_id, access_token=token, capabilities=["life-system-1", "heartbeat-1"], owner_id=state["owner_id"],
                center_id=state["center_id"], spaces=entry["spaces"], admin=entry["admin"],
                expires_at=state["devices"][device_id]["expires_at"])


def authenticate(state, token):
    hashed = digest(token)
    for device_id, device in state["devices"].items():
        if secrets.compare_digest(device["token_hash"], hashed) and not device["revoked"] and device["expires_at"] > now():
            device['spaces'] = normalize_spaces(device['spaces'])
            return device_id, device
    raise Problem("AUTH_FAILED", 401)


def visible(record, device):
    return normalize_space(record["space"]) in normalize_spaces(device["spaces"])


def refs(record):
    return record["data"].get("sources", [])


def source_valid(state, ref, purpose=None, space=None):
    space = normalize_space(space)
    source = state["records"].get(ref.get("id"))
    return bool(source and source["revision"] == ref.get("revision") and source["kind"] == "message"
                and source["data"]["role"] == "user" and (space is None or normalize_space(source["space"]) == space)
                and (purpose is None or source["mode"] == "normal"))


def memory_source_valid(state, ref, space, inferred=False):
    space = normalize_space(space)
    if source_valid(state, ref, "personalization", space):
        return True
    source = state["records"].get(ref.get("id"))
    if (inferred and source and source['revision'] == ref.get('revision') and normalize_space(source['space']) == space
            and source['mode'] == 'normal' and (source['kind'] == 'event' or
            source['kind'] == 'journal' and '自动位置观察' in source['data'].get('tags', []))):
        return True
    return bool(source and source["revision"] == ref.get("revision") and normalize_space(source["space"]) == space
                and source["mode"] == "normal" and source["kind"] == "journal"
                and "小满问答" in source["data"].get("tags", []))


def forgotten_history(state, spaces):
    spaces = normalize_spaces(spaces)
    return {source for entry in state.get("deleted", {}).values() if normalize_space(entry["space"]) in spaces
            for source in entry.get("memory_source_ids", [])}


def migrated_revision(state, identifier, revision):
    """Rebase only an unchanged migration bump, never a later real edit."""
    original = state.get('owner_space_revisions', {}).get(identifier)
    current = state['records'].get(identifier)
    if original is not None and revision == original and current and current['revision'] == original + 1:
        return original + 1
    return revision


def validate_record(state, record, internal=False):
    from .life import KINDS, validate_life
    from . import life_system
    from .sharing import valid_space, shared_space
    fields(record, ["id", "kind", "space", "mode", "revision", "updated_at", "data"], ['occurred_at'])
    record['space'] = normalize_space(record['space'])
    # Old phones may hold evidence revisions from immediately before the move.
    # This is safe only while the referenced record has not really changed.
    def rebase_refs(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str) and type(value.get('revision')) is int:
                value['revision'] = migrated_revision(state, value['id'], value['revision'])
            for key, identifier in list(value.items()):
                if key.endswith('_id') and isinstance(identifier, str):
                    revision_key = key[:-3] + '_revision'
                    if type(value.get(revision_key)) is int:
                        value[revision_key] = migrated_revision(state, identifier, value[revision_key])
            for item in value.values(): rebase_refs(item)
        elif isinstance(value, list):
            for item in value: rebase_refs(item)
    rebase_refs(record['data'])
    if 'occurred_at' in record:
        from .features import timestamp
        record['occurred_at'] = timestamp(record['occurred_at'])
    string(record["id"], 128)
    require(valid_space(record['space']) and record["mode"] in {"normal", "source_only"})
    require(not shared_space(record['space']) or record['kind'] in {'list','event'}, 'SHARED_KIND_FORBIDDEN',403)
    require(type(record["revision"]) is int and record["revision"] > 0)
    try:
        timestamp = datetime.fromisoformat(string(record["updated_at"], 64).replace("Z", "+00:00"))
        require(timestamp.tzinfo is not None, "INVALID_TIME")
    except (ValueError, TypeError):
        raise Problem("INVALID_TIME") from None
    data, kind = record["data"], record["kind"]
    if kind == "message":
        fields(data, ["text", "role", "conversation_id"], ["used_memories", "origin", "discussion", "topic_links"])
        string(data["text"]); string(data["conversation_id"], 128)
        require(data["role"] in ({"user", "assistant"} if internal else {"user"}), "ROLE_FORBIDDEN", 403)
        require("used_memories" not in data or internal, "ROLE_FORBIDDEN", 403)
        if "topic_links" in data:
            links = data["topic_links"]
            require(isinstance(links, dict) and len(links) <= 4)
            for topic_id, title in links.items():
                string(topic_id, 128)
                string(title, 80)
        if "discussion" in data:
            topic = data["discussion"]
            require(data["role"] == "user" and "origin" not in data)
            fields(topic, ["kind", "title"], ["source", "url"])
            require(topic["kind"] in ("news", "evening"))
            string(topic["title"], 512)
            if "source" in topic:
                string(topic["source"], 120)
            if "url" in topic:
                from urllib.parse import urlsplit
                url = string(topic["url"], 2048)
                try:
                    link = urlsplit(url)
                    require(link.scheme in ("http", "https") and link.hostname and link.username is None)
                except ValueError:
                    raise Problem("INVALID_ARGUMENT") from None
        if "origin" in data:
            origin = data["origin"]
            fields(origin, ["format", "source_id", "original_role", "occurred_at"], ["source_revision", "context_refs"])
            string(origin["format"], 80); string(origin["source_id"], 256)
            require(origin["original_role"] in {"user", "assistant", "system", "unknown"})
            if origin['format'] == 'phone-model-v1':
                source = state['records'].get(origin['source_id'])
                require(source and source['kind'] == 'message' and source['space'] == record['space']
                        and origin['original_role'] == 'assistant' and record['mode'] == 'source_only', 'SOURCE_INVALIDATED', 409)
                require(type(origin.get('source_revision')) is int and origin['source_revision'] == source['revision'], 'SOURCE_INVALIDATED', 409)
                context_refs = origin.get('context_refs', [{'id':origin['source_id'], 'revision':origin['source_revision']}])
                require(isinstance(context_refs, list) and 1 <= len(context_refs) <= 40)
                for ref in context_refs:
                    fields(ref, ['id','revision']); string(ref['id'], 128)
                    parent = state['records'].get(ref['id'])
                    from .features import usable_memory
                    scenario = 'work' if record['space'] == 'work' else 'general'
                    require(ref['id'] != record['id'] and type(ref['revision']) is int and parent and (
                        parent['kind'] == 'message' or (parent['kind'] in {'event', 'reminder'} and parent['mode'] == 'normal')
                        or life_system.context_consented(parent, record['space'])
                        or usable_memory(state, parent, scenario))
                            and parent['space'] == record['space'] and parent['revision'] == ref['revision'], 'SOURCE_INVALIDATED', 409)

            if origin["occurred_at"] is not None:
                from .features import timestamp
                origin["occurred_at"] = timestamp(origin["occurred_at"])
            require(record["mode"] == "source_only", "IMPORT_REQUIRES_SOURCE_ONLY")
    elif kind == "memory":
        fields(data, ["text", "status", "sources"], ["category", "quote", "scenario", "key", "valid_from", "valid_until", "goal_state",
            "slot", "value", "temporal", "observed_at", "confidence", "inferred", "confirmed_at", "hedged", "protected", "needs_confirmation"])
        string(data["text"], 4096)
        if not data.get('slot') and str(data.get('key', '')).startswith(('interest:', 'learned-place:')):
            data['inferred'] = True
        require(data["status"] in {"candidate", "active", "suppressed", "rejected", "superseded", "needs_review", "expired"})
        require(record["mode"] == "normal")
        require(isinstance(data["sources"], list) and 1 <= len(data["sources"]) <= 20)
        for ref in data["sources"]:
            fields(ref, ["id", "revision"])
            string(ref["id"], 128)
            require(type(ref["revision"]) is int and ref["revision"] > 0)
            if data["status"] in {"active", "candidate"}:
                require(memory_source_valid(state, ref, record["space"], data.get('inferred', False)), "SOURCE_INVALIDATED", 409)
        if "quote" in data:
            string(data["quote"], 4096)
            if data["status"] in {"active", "candidate"}:
                require(any(data["quote"] in state["records"][ref["id"]]["data"]["text"] for ref in data["sources"]), "INVALID_EVIDENCE")
        if "category" in data:
            require(data["category"] in {"fact", "preference", "goal", "style"})
        from .features import SCENARIOS, timestamp
        require(data.get("scenario", "general") in SCENARIOS)
        require(data.get("goal_state", "open") in {"open", "paused", "done"})
        if "key" in data:
            string(data["key"], 100)
        if 'slot' in data:
            from .profile import slot_spec, memory_key
            spec = slot_spec(data['slot'])
            require(spec is not None and isinstance(data.get('value'), (str, dict)), 'INVALID_PROFILE_SLOT')
            assert spec is not None
            require(data.get('temporal') in spec['allowed_temporal'], 'INVALID_PROFILE_TEMPORAL')
            require(data.get('temporal') != 'state' or bool(data.get('valid_until')), 'STATE_REQUIRES_EXPIRY')
            require(data.get('key') == memory_key(data['slot'], data['value']), 'INVALID_PROFILE_KEY')
        if 'confidence' in data:
            require(type(data['confidence']) in (float, int) and 0 <= data['confidence'] <= 1)
        for field in ('inferred', 'hedged', 'protected', 'needs_confirmation'):
            require(field not in data or type(data[field]) is bool)
        for field in ("valid_from", "valid_until", "observed_at", "confirmed_at"):
            if field in data and data[field] is not None:
                data[field] = timestamp(data[field])
        require(not data.get("valid_from") or not data.get("valid_until")
                or data["valid_from"] < data["valid_until"], "INVALID_TIME_WINDOW")
    elif kind in {"draft", "list"}:
        from .features import validate_extended
        validate_extended(state, record)
    elif kind == 'answer':
        from .recall import validate_answer
        validate_answer(state, record, internal)
    elif kind in {'document','collection'}:
        from .library import validate
        validate(record)
    elif kind in {'archive_note', 'letter', 'legacy_grant'}:
        from .archive import validate as validate_archive
        validate_archive(state, record, internal)
    elif kind in life_system.KINDS:
        life_system.validate(state, record)
    elif kind in KINDS:
        validate_life(state, record, internal)
    elif kind == "reminder":
        fields(data, ["text", "due_at", "status"])
        string(data["text"], 4096)
        require(data["status"] in {"pending", "done", "dismissed"})
        try:
            due = datetime.fromisoformat(data["due_at"].replace("Z", "+00:00"))
            require(due.tzinfo is not None)
            data["due_at"] = due.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, AttributeError):
            raise Problem("INVALID_TIME") from None
    else:
        raise Problem("UNSUPPORTED_KIND")


def dependent_ids(state, source_id):
    """Iterative dependency closure: long conversations must not overflow recursion."""
    children = record_index(state)['sources']
    summaries = state.get('private_items', {}).get('profile_consolidation', {}).get('summaries', {})
    profile_children = {}
    for entry in summaries.values():
        for ref in entry.get('memories', []) + entry.get('sources', []):
            profile_children.setdefault(ref['id'], set()).add(entry['id'])
    tracked = {entry.get('id') for entry in summaries.values()}
    orphan_summaries = {}
    for record in state['records'].values():
        if (record.get('kind') == 'context' and 'profile-summary' in record['data'].get('tags', [])
                and record['id'] not in tracked):
            orphan_summaries.setdefault(record['space'], set()).add(record['id'])
    reached, pending = set(), [source_id] if isinstance(source_id, str) else list(source_id)
    while pending:
        item = pending.pop()
        if item in reached:
            continue
        reached.add(item)
        pending.extend(children.get(item, set()) - reached)
        pending.extend(profile_children.get(item, set()) - reached)
        original = state['records'].get(item)
        if original and original.get('kind') in {'memory', 'message', 'journal', 'event'}:
            # Old backups retain generated contexts but not private bookkeeping.
            pending.extend(orphan_summaries.get(original['space'], set()) - reached)
    return reached


def invalidate_dependents(state, source_id):
    invalid_ids = dependent_ids(state, source_id)
    from .thought_candidates import invalidate_sources
    invalidate_sources(state, invalid_ids)
    index = record_index(state)
    for record_id in invalid_ids - {source_id}:
        record = state["records"].get(record_id)
        if record and record["kind"] == "memory":
            record["data"]["status"] = "suppressed"
            record["revision"] += 1
            record["updated_at"] = now()
        elif record and record['kind'] == 'context' and 'profile-summary' in record['data'].get('tags', []):
            delete_record(state, record_id)
            summaries = state.get('private_items', {}).get('profile_consolidation', {}).get('summaries', {})
            for space, entry in list(summaries.items()):
                if entry.get('id') == record_id:
                    del summaries[space]
    job_ids = {key for identifier in invalid_ids for key in index['job_sources'].get(identifier, ())}
    for key in job_ids:
        job = state['jobs'][key]
        if invalid_ids.intersection(job.get("source_ids", [])):
            job["partial"] = ""
            job["context_invalidated"] = True
            if job["state"] in {"queued", "running"}:
                job["state"], job["error"] = "cancelled", "SOURCE_INVALIDATED"
    state["suggestions"] = {k: v for k, v in state["suggestions"].items()
                            if v["source_id"] not in invalid_ids and not any(ref["id"] in invalid_ids for ref in v.get("sources", []))}


def _retained_memory_evidence(state, record, affected):
    data = record["data"]
    if data["status"] not in {"active", "candidate"}:
        return None
    remaining = []
    separators = r"[。！？.!?；;\n]+"
    for ref in refs(record):
        if ref["id"] not in affected and memory_source_valid(state, ref, record["space"]):
            text = state["records"][ref["id"]]["data"]["text"]
            sentences = {sentence.strip() for sentence in re.split(separators, text)}
            remaining.append((ref, text.strip(), sentences))
    for quote in (data.get("quote"), data["text"]):
        if not quote:
            continue
        sentence = quote.strip().rstrip("。！？.!?；;")
        if not sentence:
            continue
        supported = [ref for ref, text, sentences in remaining
                     if quote in text and (quote == text or sentence in sentences or '我' + sentence in sentences)]
        if supported:
            return quote, supported
    return None


def delete_record(state, record_id):
    if record_id not in state["records"]:
        return
    require(not record_index(state)['parents'].get(record_id),
            'HAS_LINKED_ITEMS', 409)
    affected = dependent_ids(state, record_id)
    from .thought_candidates import invalidate_sources, notify
    invalidate_sources(state, affected)
    notify(state, 'record')
    from .archive import forget_revision
    forget_revision(state, affected)
    index = record_index(state)
    timestamp = now()
    for key in affected:
        record = state["records"].get(key)
        if record:
            if key != record_id and record["kind"] == "memory":
                evidence = _retained_memory_evidence(state, record, affected)
                if evidence:
                    record["data"]["quote"], record["data"]["sources"] = evidence
                    record["revision"] += 1
                    record["updated_at"] = timestamp
                    continue
            state["deleted"][key] = {"space": record["space"], "deleted_at": timestamp,
                                     'store_revision': getattr(state, 'store_revision', 0) + 1}
            if key == record_id and record["kind"] == "memory":
                state["deleted"][key]["memory_source_ids"] = [ref["id"] for ref in refs(record)]
                if record['data'].get('slot'):
                    from .profile import normalize_value
                    state['deleted'][key].update(memory_slot=record['data']['slot'],
                        memory_value=normalize_value(record['data'].get('value', record['data']['text'])))
            del state["records"][key]
    summaries = state.get('private_items', {}).get('profile_consolidation', {}).get('summaries', {})
    for space, entry in list(summaries.items()):
        if entry.get('id') in affected and entry.get('id') != record_id:
            del summaries[space]
    clear_indexes(state)
    state.pop("restore_preview", None)
    for batch in state.get("connector_batches", {}).values():
        if any(item["id"] in affected for item in batch["items"]):
            batch.update(status="invalidated", count=len(batch["items"]), items=[])
    state["suggestions"] = {k: v for k, v in state["suggestions"].items()
                            if v["source_id"] not in affected and not any(ref["id"] in affected for ref in v.get("sources", []))}
    job_ids = {key for identifier in affected for name in ('job_sources', 'job_results') for key in index[name].get(identifier, ())}
    for key in job_ids:
        job = state['jobs'][key]
        if affected.intersection(job.get("source_ids", [])) or job.get("result_id") in affected:
            job["result_id"] = None
            job["partial"] = ""
            job["context_invalidated"] = True
            job["state"], job["error"] = "cancelled", "SOURCE_INVALIDATED"


def mutate(state, device_id, command):
    fields(command, ["operation_id", "action", "id", "base_revision"], ["record", "resolve_conflicts"])
    require(type(command.get("resolve_conflicts", False)) is bool)
    operation_id = string(command["operation_id"], 128)
    string(command["id"], 128)
    require(type(command["base_revision"]) is int and command["base_revision"] >= 0)
    request_hash = hashlib.sha256(canonical(command)).hexdigest()
    command = copy.deepcopy(command)
    if isinstance(command.get('record'), dict):
        command['record']['space'] = normalize_space(command['record'].get('space'))
    normalized_hash = hashlib.sha256(canonical(command)).hexdigest()
    key = "operation:" + operation_id
    previous = state["receipts"].get(key)
    if previous:
        require(normalize_space(previous["space"]) in normalize_spaces(state["devices"][device_id]["spaces"]), "PERMISSION_REVOKED", 403)
        hashes = {request_hash, normalized_hash}
        # Request decoding canonicalizes aliases, but old receipts contain the
        # original wire hash. Retrying an already committed outbox must work.
        if isinstance(command.get('record'), dict) and command['record'].get('space') == 'personal':
            for alias in ('life', 'work'):
                legacy = copy.deepcopy(command)
                legacy['record']['space'] = alias
                hashes.add(hashlib.sha256(canonical(legacy)).hexdigest())
        require(previous["hash"] in hashes, "IDEMPOTENCY_MISMATCH", 409)
        return previous["result"]
    device = state["devices"][device_id]
    device['spaces'] = normalize_spaces(device['spaces'])
    old = state["records"].get(command["id"])
    require(not old or old['kind'] != 'legacy_grant', 'USE_LEGACY_ACTION', 403)
    require(old is None or visible(old, device), "NOT_FOUND", 404)
    require(command["id"] not in state["deleted"], "DELETED_OBJECT", 409)
    command['base_revision'] = migrated_revision(state, command['id'], command['base_revision'])
    require(command["base_revision"] == (old["revision"] if old else 0), "REVISION_CONFLICT", 409)
    if command["action"] == "delete":
        if old is None:
            raise Problem('NOT_FOUND', 404)
        if old['kind'] == 'letter':
            delete_record(state, 'letter-reminder:' + old['id'])
        delete_record(state, command["id"])
        result = {"id": command["id"], "deleted": True}
        receipt_space = old['space']
    else:
        require(command["action"] == "put" and "record" in command)
        record = copy.deepcopy(command["record"])
        fields(record, ["kind", "space", "mode", "data"], ['occurred_at'])
        require(record["space"] in device["spaces"], "PERMISSION_REVOKED", 403)
        if old:
            require(record["kind"] == old["kind"] and record["space"] == normalize_space(old["space"]), "IMMUTABLE_FIELD")
            require(old["kind"] != "message" or old["data"]["role"] == "user", "ROLE_FORBIDDEN", 403)
            require(old["kind"] != "meal" or old["data"]["status"] != "cooked", "USE_KITCHEN_ACTION", 409)
        record.update(id=command["id"], revision=command["base_revision"] + 1, updated_at=now())
        from .features import timestamp
        occurred = timestamp(record.get('occurred_at',
            old.get('occurred_at', old['updated_at']) if old else record['updated_at']))
        if datetime.fromisoformat(occurred) > datetime.fromisoformat(record['updated_at']) + timedelta(minutes=5):
            occurred = record['updated_at']
        record['occurred_at'] = occurred
        confirmed_profile = (old and record['kind'] == 'memory' and record['data'].get('slot')
                             and record['data']['status'] == 'active')
        if confirmed_profile and old is not None:
            if old['data']['status'] != 'active' or record['data'].get('needs_confirmation') is False:
                record['data'].update(confirmed_at=now(), confidence=.9, needs_confirmation=False)
            if old['data']['text'] != record['data']['text']:
                record['data'].update(protected=True, confirmed_at=now())
        validate_record(state, record)
        if (old and record['kind'] == 'memory' and record['data'].get('slot')
                and old['data']['status'] == 'active' and record['data']['status'] == 'rejected'):
            from .profile import slot_spec, observed_at
            spec = slot_spec(record['data']['slot'])
            if spec and spec['slot_kind'] == 'single':
                prior = [r for r in state['records'].values() if r['kind'] == 'memory' and r['space'] == record['space']
                         and r['data'].get('key') == record['data'].get('key') and r['data']['status'] == 'superseded']
                if prior:
                    restored = max(prior, key=observed_at)
                    restored['data'].update(status='active', needs_confirmation=False)
                    restored.update(revision=restored['revision'] + 1, updated_at=now())
        if command.get("resolve_conflicts") or (confirmed_profile and record['data'].get('confirmed_at')):
            require(record["kind"] == "memory" and record["data"]["status"] == "active" and record["data"].get("key"), "INVALID_CONFLICT_SELECTION")
            for other in list(state["records"].values()):
                if (other["id"] != record["id"] and other["kind"] == "memory" and other["space"] == record["space"]
                    and other["data"].get("key") == record["data"]["key"]
                    and other["data"].get("scenario", "general") == record["data"].get("scenario", "general")
                    and other["data"]["status"] in {"active", "candidate", "needs_review"}):
                    invalidate_dependents(state, other["id"])
                    other["data"]["status"] = "superseded" if record['data'].get('slot') else "suppressed"
                    other["revision"] += 1
                    other["updated_at"] = now()
        if old:
            from .archive import remember_revision
            remember_revision(state, old)
            invalidate_dependents(state, old["id"])
        state["records"][record["id"]] = record
        from .thought_candidates import notify
        notify(state, 'record')
        clear_indexes(state)
        result = {"id": record["id"], "revision": record["revision"], "deleted": False}
        receipt_space = record['space']
    state["seq"] += 1
    # Sync can contain automated imports and delayed outbox writes. Only the
    # explicit foreground check-in endpoint may reset an inactivity grant.
    state["receipts"][key] = {"hash": normalized_hash, "result": result,
                              "space": receipt_space, 'created_at': now()}
    return result


def public_preferences(state, device):
    preferences = copy.deepcopy(state['preferences'])
    learning = preferences.pop('dreaming', None)
    if learning is not None and device.get('admin'):
        preferences['dreaming'] = {name: [{**row, 'space': normalize_space(row['space'])} for row in learning.get(name, []) if normalize_space(row['space']) in normalize_spaces(device['spaces'])]
                                   for name in ('rules', 'clues')}
        preferences['dreaming']['last_day'] = learning.get('last_day')
    return preferences


def snapshot(state, device, record_ids=None, *, job_ids=None, deleted_ids=None) -> dict[str, Any]:
    from .features import memory_status
    from .vault import VaultState
    device = {**device, 'spaces': normalize_spaces(device['spaces'])}
    if not isinstance(state, VaultState):
        state = VaultState(state)
    candidates = state['records'].values() if record_ids is None else (state['records'][key] for key in record_ids if key in state['records'])
    records = [copy.deepcopy(r) for r in candidates if visible(r, device)]
    for record in records:
        if record["kind"] == "memory":
            record["effective_status"] = memory_status(state, record)
        if record['kind'] in {'draft', 'list', 'answer', 'meal', 'outfit'}:
            record['evidence_status'] = 'current' if all(state['records'].get(ref['id'], {}).get('revision') == ref['revision']
                for ref in refs(record) + record['data'].get('used_memories', [])) else 'changed'
    jobs = [{k: j.get(k) for k in ("id", "kind", "space", "state", "error", "result_id", "source_ids", "partial", "context_invalidated", "created_at")}
            for key, j in state["jobs"].items() if j.get("kind") != 'thought_association' and j.get("space") in device["spaces"] and (job_ids is None or key in job_ids)]
    return {"protocol": "preview-1", "center_id": state["center_id"], "seq": state["seq"],
            "spaces": device["spaces"], "records": records, "jobs": jobs,
            "deleted": [key for key, value in state["deleted"].items() if value["space"] in device["spaces"] and (deleted_ids is None or key in deleted_ids)],
            "history_suppressed": sorted(forgotten_history(state, device["spaces"])),
            "preferences": public_preferences(state, device),
            'space_names': {key:value['name'] for key,value in state.get('family_spaces',{}).items() if key in device['spaces']},
            "suggestions": [s for s in state["suggestions"].values() if s["space"] in device["spaces"]]}


def model_config(value):
    fields(value, ["base_url", "model", "spaces", "roles", "daily_limit"], ["allow_local_http", "stream"])
    string(value["model"], 128)
    string(value["base_url"], 2048)
    url = urlsplit(value["base_url"])
    require(url.hostname and not url.username and not url.password and not url.query and not url.fragment)
    require(not url.path.rstrip("/").endswith("/chat/completions"), "BASE_URL_REQUIRED")
    require(url.scheme == "https" or (url.scheme == "http" and value.get("allow_local_http") is True
                                     and url.hostname in {"localhost", "127.0.0.1", "::1"}), "HTTPS_REQUIRED")
    require(isinstance(value["spaces"], list) and set(value["spaces"]) <= {"personal", "life", "work"})
    value = {**value, 'spaces': normalize_spaces(value['spaces'])}
    require(isinstance(value["roles"], list) and set(value["roles"]) <= {"chat", "extract", "proactive"})
    require(type(value["daily_limit"]) is int and 0 <= value["daily_limit"] <= 1000)
    require(type(value.get("allow_local_http", False)) is bool)
    require(type(value.get("stream", False)) is bool)
    return copy.deepcopy(value)


def queue_job(state, device_id, value, *, internal=False):
    fields(value, ["operation_id", "kind", "source_id", "source_revision"], ["options"])
    string(value["operation_id"], 128)
    require(internal or not value['operation_id'].startswith('thought:'), 'RESERVED_OPERATION_ID', 403)
    require(value["kind"] in {"chat", "extract", "writing", "checklist", "proactive", "recall", 'thought_association'})
    require(value['kind'] != 'thought_association' or internal, 'INTERNAL_JOB_ONLY', 403)
    options = value.get("options", {})
    fields(options, [], ["scenario", "purpose", "digital_self"])
    require(type(options.get('digital_self', False)) is bool)
    require(not options.get('digital_self') or value['kind'] == 'chat')
    from .features import SCENARIOS
    require(options.get("scenario", "general") in SCENARIOS)
    require(options.get("purpose", "todo") in {"todo", "shopping", "packing", "meal"})
    require(type(value["source_revision"]) is int)
    hashed = hashlib.sha256(canonical(value)).hexdigest()
    key = "job:" + value["operation_id"]
    if key in state["receipts"]:
        old = state["receipts"][key]
        require(old["space"] in state["devices"][device_id]["spaces"], "PERMISSION_REVOKED", 403)
        require(old["hash"] == hashed, "IDEMPOTENCY_MISMATCH", 409)
        return old["result"]
    ref = {"id": value["source_id"], "revision": migrated_revision(state, value["source_id"], value["source_revision"])}
    if value['kind'] == 'thought_association':
        from .thought_candidates import source_access, store_for
        candidate_source = state['records'].get(ref['id'])
        require(source_access(state, candidate_source, device_id, store_for(state, device_id)['grant']) and
                candidate_source['revision'] == ref['revision'], 'SOURCE_INVALIDATED', 409)
    else:
        require(source_valid(state, ref, "personalization" if value["kind"] != "chat" else None), "SOURCE_INVALIDATED", 409)
    source = state["records"][ref["id"]]
    require(visible(source, state["devices"][device_id]), "NOT_FOUND", 404)
    config = state["model"]
    require(config is not None, "MODEL_NOT_CONFIGURED", 409)
    config['spaces'] = normalize_spaces(config['spaces'])
    role = 'proactive' if value['kind'] == 'thought_association' else (
        'chat' if value['kind'] in {'writing', 'checklist', 'recall'} else value['kind'])
    require(source["space"] in config["spaces"] and role in config["roles"], "MODEL_SCOPE_DENIED", 403)
    job_id = uid()
    state["jobs"][job_id] = dict(id=job_id, kind=value["kind"], source_ids=[source["id"]],
        source_revision=source["revision"], space=source["space"], state="queued", error=None, options=options,
        result_id=None, created_at=now(), device_id=device_id, model_hash=hashlib.sha256(canonical(config)).hexdigest())
    result = {"job_id": job_id, "state": "queued"}
    state["receipts"][key] = {"hash": hashed, "result": result, "space": source["space"], 'created_at': now()}
    return result
