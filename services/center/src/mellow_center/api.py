"""Explicit preview API. Draft /v1 contracts remain design artifacts."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from .domain import (Problem, authenticate, delete_record, fields, model_config, mutate, migrated_revision,
                     pair, queue_job, require, snapshot, string, validate_record)
from .vault import canonical, now, open_backup, uid
from .features import DEFAULT_TIMEZONE, VERSION
from .usage import budget_day
from .spaces import normalize_space
from .request_body import body as body, buffer_body as buffer_body, sync_body as sync_body


async def guarded_request(factory, check):
    """HTTP reauthorization does SQLite/crypto work off ASGI's event loop."""
    await run_in_threadpool(check)
    task = asyncio.create_task(factory())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            await run_in_threadpool(check)
            if done:
                return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def identity(state, request, admin=False):
    auth = request.headers.get("authorization", "")
    require(auth.startswith("Bearer ") and len(auth) <= 160, "AUTH_FAILED", 401)
    device_id, device = authenticate(state, auth[7:])
    require(not admin or device["admin"], "ADMIN_REQUIRED", 403)
    return device_id, device


def restore_plan(state, archive):
    from .sharing import valid_space, shared_space
    from .space_migration import migrate_state
    migrate_state(archive)
    require(archive["owner_id"] == state["owner_id"], "BACKUP_OWNER_MISMATCH", 409)
    records = archive["records"]
    require(isinstance(records, dict) and isinstance(archive["deleted"], dict))
    for key, deletion in archive["deleted"].items():
        string(key, 128)
        fields(deletion, ["space", "deleted_at"], ["memory_source_ids", 'store_revision'])
        require(valid_space(deletion['space']))
        string(deletion["deleted_at"], 64)
        sources = deletion.get("memory_source_ids", [])
        require(isinstance(sources, list) and len(sources) <= 20)
        for source in sources:
            string(source, 128)
    # Validate source relationships against the archive itself, then exclude local tombstones.
    for key, value in archive.get('family_spaces',{}).items():
        require(shared_space(key)); fields(value,['name']); string(value['name'],80)
    invalid = set()
    for key, record in records.items():
        require(key == record["id"])
        require(not shared_space(record['space']) or record['space'] in archive.get('family_spaces',{}) or
                record['space'] in state.get('family_spaces',{}), 'UNKNOWN_SHARED_SPACE')
        try:
            validate_record({"records": records}, record, internal=True)
        except Problem as error:
            # A source can legitimately have changed after generating a draft.
            # Keep the archive usable, but do not revive stale derived content.
            origin = record['data'].get('origin', {})
            if (record["kind"] in {"draft", "list", "meal", "habit_log", "outfit", "consumption", "answer"} or isinstance(origin, dict) and origin.get('format') == 'phone-model-v1') and error.code == "SOURCE_INVALIDATED":
                invalid.add(key)
            else:
                raise
    blocked = set(state["deleted"]) | set(archive["deleted"]) | invalid
    additions = {k: copy.deepcopy(r) for k, r in records.items() if k not in state["records"] and k not in blocked}
    for record in additions.values():
        if record['kind'] == 'legacy_grant':
            # Restoring old data never revives a release or stale consent.
            record['data'].update(status='revoked', released_at=None, confirmed_at=None)
        if record['kind'] == 'consumption' and record['data']['status'] == 'applied':
            # Keep historical stock actions, but a restored receipt never authorizes
            # overwriting later stock. Even matching integer revisions can diverge.
            record['data']['status'] = 'archived'
        if record['kind'] == 'meal' and 'recipe_signature' not in record['data']:
            from .life import recipe_signature
            parent = records.get(record['data']['recipe_id'])
            record['data']['recipe_signature'] = recipe_signature(parent) if parent and parent['revision'] == record['data']['recipe_revision'] else '0' * 64
    # Never restore a child whose evidence is deleted, different or unavailable locally.
    for key, record in list(additions.items()):
        if record["kind"] == "message" and record["data"]["role"] == "assistant":
            # The archive intentionally omits job dependencies; don't revive an
            # assistant answer whose full provenance cannot be reconstructed.
            del additions[key]
    # Iterate to a fixed point: a rejected parent also rejects its descendants,
    # regardless of archive ordering. Local records always win over the archive.
    while True:
        combined = {**additions, **state["records"]}
        rejected = []
        for key, record in additions.items():
            parent_id = record['data'].get('parent_id')
            if parent_id and (parent_id not in combined or combined[parent_id]['space'] != record['space']):
                rejected.append(key)
                continue
            origin = record['data'].get('origin', {})
            if isinstance(origin, dict) and origin.get('format') == 'phone-model-v1':
                context_refs = origin.get('context_refs', [{'id':origin['source_id'],'revision':origin['source_revision']}])
                if any(ref['id'] in blocked or ref['id'] not in combined or combined[ref['id']] != records.get(ref['id']) for ref in context_refs):
                    rejected.append(key)
                    continue
            references = record["data"].get("sources", []) + record["data"].get("used_memories", [])
            def invalid_reference(ref):
                parent = combined.get(ref['id'])
                if ref['id'] in blocked or not parent or parent['space'] != record['space'] or parent['mode'] != 'normal':
                    return True
                if record['kind'] in {'habit_log', 'outfit'}:
                    # These are historical user actions, not model conclusions. Current
                    # pause/laundry state does not invalidate the past action.
                    expected = 'habit' if record['kind'] == 'habit_log' else 'wardrobe'
                    return parent['kind'] != expected
                if record['kind'] == 'meal':
                    return parent['kind'] != 'recipe' or parent['id'] != record['data']['recipe_id']
                if record['kind'] == 'consumption':
                    if ref['id'] == record['data']['meal_id']: return parent['kind'] != 'meal'
                    return parent['kind'] != 'pantry' or ref['id'] not in {item['id'] for item in record['data']['changes']}
                return parent['revision'] != ref['revision'] or parent != records.get(ref['id'])
            if any(invalid_reference(ref) for ref in references):
                rejected.append(key)
        if not rejected:
            break
        for key in rejected:
            del additions[key]
    conflicts = [k for k, r in records.items() if k in state["records"] and r != state["records"][k]]
    return additions, conflicts


def router(vault):
    api = APIRouter(prefix="/api/preview", dependencies=[Depends(buffer_body)])

    def authorize(request, admin=False):
        with vault.view() as state:
            return identity(state, request, admin)

    def require_worker(request):
        require(not getattr(request.app.state, "worker_error", None), "WORKER_UNAVAILABLE", 503)

    def check_model_request(request, config, key, space=None, admin=False):
        space = normalize_space(space)
        with vault.view() as state:
            _, device = identity(state, request, admin)
            require(space is None or space in device["spaces"], "PERMISSION_REVOKED", 403)
            require(state["model"] == config and state["credentials"].get("model", "") == key,
                    "MODEL_CONFIG_CHANGED", 409)

    def reserve_model(request, space=None, admin=False):
        space = normalize_space(space)
        with vault.transaction(bump_revision=False) as state:
            actor, device = identity(state, request, admin)
            require(space is None or space in device['spaces'], 'PERMISSION_REVOKED', 403)
            config = copy.deepcopy(state['model'])
            require(config is not None, 'MODEL_NOT_CONFIGURED', 409)
            require(space is None or (space in config['spaces'] and 'chat' in config['roles']), 'MODEL_SCOPE_DENIED', 403)
            day = budget_day(state)
            require(state['budgets'].get(day, 0) < config['daily_limit'], 'BUDGET_EXCEEDED', 429)
            state['budgets'][day] = state['budgets'].get(day, 0) + 1
            return actor, device, config, state['credentials'].get('model', '')

    @api.get("/health")
    def health(request: Request):
        from .certificates import tls_status
        from .semantic_index import semantic_health
        with vault.view() as state:
            worker = getattr(request.app.state, 'worker_status', {})
            degraded = getattr(request.app.state, 'worker_error', None) or any(step['consecutive_failures'] for step in worker.values())
            return {"protocol": "preview-1", "status": "degraded" if degraded else "ready",
                    "center_id": state["center_id"], 'worker': worker,
                    'features': ['occurred_at', 'jobs_delta', 'light_sync', 'event_departure', 'task_place', 'letter_photo', 'pantry_expires_on', 'inventory_batches', 'thought_candidates_v1'], **semantic_health(state), **tls_status(vault.path.parent)}

    @api.post('/devices/heartbeat')
    def heartbeat(request: Request):
        value = sync_body(request)
        fields(value, ['platform', 'version'], ['ack_revision'])
        require(value['platform'] in {'android', 'ios', 'macos', 'windows', 'linux', 'browser'})
        string(value['version'], 40)
        with vault.transaction(bump_revision=False) as state:
            device_id, device = identity(state, request)
            if 'ack_revision' in value:
                require(type(value['ack_revision']) is int and 0 <= value['ack_revision'] <= state.store_revision)
                device['ack_revision'] = max(device.get('ack_revision', 0), value['ack_revision'])
            device.update(last_seen=now(), platform=value['platform'], version=value['version'])
            return {'device_id':device_id, 'last_seen':device['last_seen']}

    @api.get('/hub')
    def hub(request: Request):
        from collections import Counter
        with vault.view() as state:
            device_id, device = identity(state, request)
            records = [r for r in state['records'].values() if r['space'] in device['spaces']]
            return {'center_id':state['center_id'], 'protocol':'preview-1', 'version':VERSION,
                    'status':'degraded' if getattr(request.app.state,'worker_error',None) else 'ready',
                    'counts':dict(Counter(r['kind'] for r in records)), 'spaces':device['spaces'],
                    'device_id':device_id, 'last_seen':device.get('last_seen'),
                    'model_configured':bool(state['model'])}

    @api.get('/voice/status')
    def voice_status(request: Request):
        authorize(request)
        from .speech import capabilities
        return capabilities(vault.path.parent)

    def voice_authorize(request, space):
        with vault.view() as state:
            _, device = identity(state, request)
            require(space in {'personal', 'life', 'work'} and space in device['spaces'], 'PERMISSION_REVOKED', 403)

    @api.post('/voice/transcribe')
    async def voice_transcribe(request: Request):
        import asyncio
        from .speech import decode_audio, transcribe
        await run_in_threadpool(authorize, request)
        value = await body(request, 2_010_000)
        fields(value, ['space', 'audio'])
        await run_in_threadpool(voice_authorize, request, value['space'])
        raw = await run_in_threadpool(decode_audio, value['audio'])
        text = await asyncio.to_thread(transcribe, vault.path.parent, raw)
        await run_in_threadpool(voice_authorize, request, value['space'])
        return {'text': text, 'stores_audio': False}

    @api.post('/voice/speak')
    async def voice_speak(request: Request):
        import asyncio
        from .speech import speak
        await run_in_threadpool(authorize, request)
        value = await body(request, 16000)
        fields(value, ['space', 'text'])
        await run_in_threadpool(voice_authorize, request, value['space']); string(value['text'], 2000)
        audio = await asyncio.to_thread(speak, value['text'])
        await run_in_threadpool(voice_authorize, request, value['space'])
        return {'audio': audio, 'mime': 'audio/wav', 'stores_audio': False}

    @api.post("/pair")
    def pairing(request: Request):
        value = sync_body(request, 4096)
        fields(value, ["pairing_id", "token"])
        string(value["pairing_id"], 128); string(value["token"], 128)
        with vault.transaction() as state:
            from .owner_access import PAIRING_ID, enroll
            if value["pairing_id"] == PAIRING_ID:
                result = enroll(vault, state, value["token"], "小满手机", admin=False)
            else:
                result = pair(state, value["pairing_id"], value["token"])
            if result is None:
                state.bump_revision = False
            elif 'personal' in result['spaces']:
                # Pairing fixes the delivery owner independently of model
                # consent. Offline devices never lose this assignment.
                from .thought_candidates import control
                delivery = control(state)
                if delivery['owner'] is None:
                    delivery['owner'] = result['device_id']
                result['thought_delivery_owner'] = delivery['owner']
        if result is None:
            raise Problem('PAIRING_FAILED', 401)
        from .certificates import tls_status
        return {**result, **{key: value for key, value in tls_status(vault.path.parent).items() if key == 'tls_spki_sha256'}}

    @api.post("/followups/claim-alarm")
    def claim_alarm(request: Request):
        import re
        authorize(request)
        value = sync_body(request, 8192)
        fields(value, ['task','installation','refs','due'])
        require(isinstance(value['task'],str) and re.fullmatch(r'timed-(fixed|flex)-[0-9a-f]{64}',value['task']))
        string(value['installation'],64)
        refs = value['refs']
        require(isinstance(refs,list) and 1 <= len(refs) <= 16)
        string(value['due'],40)
        try:
            due = datetime.fromisoformat(value['due'].replace('Z','+00:00'))
            require(due.tzinfo is not None)
        except ValueError:
            raise Problem('INVALID_ARGUMENT')
        with vault.transaction(bump_revision=False) as state:
            actor, device = identity(state, request)
            instant = datetime.now(timezone.utc)
            owned = state.setdefault('followup_deliveries',{})
            for key in list(owned):
                if owned[key]['expires'] <= instant.isoformat():
                    del owned[key]
            if due <= instant:
                return {'owned':False}
            for ref in refs:
                fields(ref,['id','revision'])
                string(ref['id'],160)
                r = state['records'].get(ref['id'])
                require(r and normalize_space(r['space']) in device['spaces'] and r['mode']=='normal'
                        and r['kind'] in {'journal','message'} and r['revision']==migrated_revision(state, ref['id'], ref['revision']),
                        'SOURCE_INVALIDATED',409)
            key = value['task']
            owner = [actor,value['installation']]
            if key not in owned:
                require(len(owned)<10000,'DELIVERY_LIMIT',409)
                owned[key] = {'owner':owner,'expires':due.astimezone(timezone.utc).isoformat()}
            if owned[key]['owner'] == owner:
                owned[key]['expires'] = due.astimezone(timezone.utc).isoformat()
            return {'owned':owned[key]['owner']==owner}

    @api.post("/suggestions/claim-delivery")
    def claim_delivery(request: Request):
        from .proactive import claim_decision
        authorize(request)
        value = sync_body(request, 2048)
        fields(value, ['id'], ['receipt'])
        string(value['id'], 256)
        with vault.transaction() as state:
            actor, device = identity(state, request)
            item = state['suggestions'].get(value['id'])
            old = item.get('delivery_claim') if item else None
            retry = old and old.get('device') == actor and old.get('retry_at', '~') <= now()
            if retry:
                item.pop('delivery_claim')
            claimed, wait = claim_decision(state, value['id'], actor)
            # Metadata is only for suggestions in the caller's own spaces; ids elsewhere stay opaque.
            meta = {}
            if item and normalize_space(item['space']) in device['spaces']:
                meta['created_at'] = item.get('created_at')
                meta['kind'] = item.get('kind', item.get('type'))
                if item.get('due_record'):
                    meta['due_record'] = item['due_record']
            if claimed and value.get('receipt') is True:
                claim = item['delivery_claim']
                claim.update(attempt=uid(), retry_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(), outcome='pending')
                return {'claimed': True, 'attempt': claim['attempt'], **meta}
            if retry and not claimed:
                item['delivery_claim'] = old
            return {'claimed': claimed, **meta, **({'retry_after': wait} if wait else {})}

    @api.post("/suggestions/delivery-result")
    def delivery_result(request: Request):
        authorize(request)
        value = sync_body(request, 2048)
        fields(value, ['id','attempt','submitted'])
        require(isinstance(value['submitted'], bool))
        with vault.transaction() as state:
            actor, device = identity(state, request)
            item = state['suggestions'].get(value['id'])
            require(item and normalize_space(item['space']) in device['spaces'], 'NOT_FOUND', 404)
            claim = item.get('delivery_claim', {})
            require(claim.get('device') == actor and claim.get('attempt') == value['attempt'], 'STALE_ATTEMPT',409)
            if claim.get('outcome') != 'submitted':
                claim['outcome'] = 'submitted' if value['submitted'] else 'failed'
                if value['submitted']:
                    claim.pop('retry_at', None)
                state['seq'] += 1
        return {'ok': True}

    from .news import routes as news_routes
    news_routes(api, vault, identity)

    from .public_data import routes as public_data_routes
    public_data_routes(api, vault, identity)
    from .thought_candidates import routes as thought_routes
    from .thought_feedback import routes as feedback_routes
    from .public_warnings import routes as warning_routes
    thought_routes(api, vault, identity)
    feedback_routes(api, vault, identity)
    warning_routes(api, vault, identity)

    @api.get("/headlines")
    def news_headlines(request: Request):
        from .news import headlines, read_cache
        with vault.view() as state:
            identity(state, request)
        cache = read_cache(vault)
        return {"updated_at": cache.get("at"), "items": list(headlines(cache, publication_times=True))}

    @api.get("/event-bulletins")
    def event_bulletins(request: Request):
        from .event_bulletins import read_catalog
        authorize(request)
        return {"items": read_catalog(vault)}

    @api.get("/headlines/selected")
    def personal_headlines(request: Request, space: str):
        from .news import headlines, read_cache, selected_news
        with vault.view() as state:
            _, device = identity(state, request)
            require(space in device["spaces"], "PERMISSION_REVOKED", 403)
            cache = read_cache(vault)
            return {"updated_at": cache.get("at"), "items": selected_news(state, headlines(cache), space)}

    @api.get("/suggestions/pending")
    def pending_suggestions(request: Request, space: str):
        from .proactive import pending_report, produce_urgent, urgent_plan
        with vault.view() as state:
            actor, device = identity(state, request)
            require(space in device["spaces"], "PERMISSION_REVOKED", 403)
            # The CLI scheduler only scans while proactive is on and once a minute; a poll must see due items now.
            stale = any(urgent_plan(state, now()))
        if stale:
            with vault.transaction() as state:
                produce_urgent(state, now())
        with vault.view() as state:
            actor, device = identity(state, request)
            require(space in device["spaces"], "PERMISSION_REVOKED", 403)
            return pending_report(state, actor, space, now())

    @api.get("/reminders/upcoming")
    def upcoming_reminders_route(request: Request):
        from .proactive import CATCHUP_BEHIND, UPCOMING_AHEAD, parse_instant, upcoming_reminders
        # Independent of the proactive switch: exact reminders are prearmed on the phone either way.
        with vault.view() as state:
            _, device = identity(state, request)
            instant = now()
            current = parse_instant(instant)
            if current is None:
                raise Problem('INVALID_TIME')
            return {"items": upcoming_reminders(state, device, instant), "generated_at": instant,
                    "window": {"from": (current - CATCHUP_BEHIND).isoformat(), "to": (current + UPCOMING_AHEAD).isoformat()},
                     "store_revision": state.store_revision, "spaces": sorted(device['spaces'])}

    @api.get("/snapshot")
    def get_snapshot(request: Request, since: int | None = None, delta: bool = False,
                     paged: bool = False, cursor: int = 0, at: int | None = None, jobs_delta: bool = False,
                     space: str | None = None, light: bool = False):
        with vault.view() as state:
            _, device = identity(state, request)
            if space is not None:
                space = normalize_space(space)
                require(space in device['spaces'], 'PERMISSION_REVOKED', 403)
                device = {**device, 'spaces': [space]}
            require(cursor >= 0 and (not cursor or paged), 'INVALID_CURSOR')
            require((not cursor and at is None) or at == state.store_revision, 'SNAPSHOT_CHANGED', 409)
            if since == state.store_revision:
                return {"unchanged": True, "store_revision": since, "center_id": state["center_id"], "protocol": "preview-1"}
            incremental = delta and since is not None and state.get('change_floor', state.store_revision) <= since <= state.store_revision
            identifiers = {key for entry in state.get('record_changes', []) if entry['revision'] > since for key in entry['ids']} if incremental else None
            entries = [entry for entry in state.get('record_changes', []) if entry['revision'] > since] if incremental else []
            # Old clients replace jobs wholesale. Changed-only jobs require opt-in.
            job_ids = {key for entry in entries for key in entry.get('jobs', [])} if incremental and jobs_delta and all('jobs' in entry for entry in entries) else None
            deleted_ids = {key for entry in entries for key in entry.get('deleted', [])} if incremental and all('deleted' in entry for entry in entries) else None
            next_cursor = None
            deferred_count = 0
            if light:
                from .sync_policy import light_record_allowed
                visible = [key for key in (identifiers if identifiers is not None else state['records'])
                           if key in state['records'] and state['records'][key]['space'] in device['spaces']]
                identifiers = [key for key in visible if light_record_allowed(state['records'][key])]
                deferred_count = len(visible) - len(identifiers)
            if paged:
                from .vault import canonical
                ordered = sorted(identifiers if identifiers is not None else state['records'])
                require(cursor <= len(ordered), 'INVALID_CURSOR')
                selected, size = [], 0
                for offset in range(cursor, len(ordered)):
                    record = state['records'].get(ordered[offset])
                    if not record or record['space'] not in device['spaces']: continue
                    weight = len(canonical(record))
                    if selected and (len(selected) >= 100 or size + weight > (128 * 1024 if light else 2 * 1024 * 1024)):
                        next_cursor = offset
                        if light: deferred_count += len(ordered) - offset
                        break
                    selected.append(record['id']); size += weight
                identifiers = selected
            result: dict[str, Any] = {**snapshot(state, device, identifiers, job_ids=job_ids, deleted_ids=deleted_ids), "store_revision": state.store_revision,
                    'delta': incremental, 'jobs_delta': job_ids is not None,
                    'deleted_jobs': sorted(job_ids - set(state['jobs'])) if job_ids is not None else [],
                    'next_cursor': next_cursor}
            if light:
                # Auxiliary job partials can contain long generated responses.
                from .vault import canonical
                for key in ('jobs', 'suggestions'):
                    small, size = [], 0
                    for item in result[key]:
                        weight = len(canonical(item))
                        if len(small) < 100 and size + weight <= 64 * 1024:
                            small.append(item)
                            size += weight
                    deferred_count += len(result[key]) - len(small)
                    result[key] = small
                if len(canonical(result['preferences'])) > 64 * 1024:
                    result['preferences'] = {}
                    result['preferences_omitted'] = True
                    deferred_count += 1
                result.update(light=True, deferred_count=deferred_count)
            return result


    @api.post("/operations")
    def operation(request: Request):
        authorize(request)
        value = sync_body(request, 16 * 1024 * 1024)
        with vault.transaction() as state:
            device_id, _ = identity(state, request)
            result = mutate(state, device_id, value)
        return result

    @api.get("/search")
    def search(request: Request, q: str = "", space: str = "personal", purpose: str = "recall", limit: int = 20):
        from .retrieval import search as retrieve
        from .sharing import valid_space
        require(0 < len(q) <= 256 and valid_space(space) and purpose in {"recall", "personalization"}
                and 1 <= limit <= 20)
        with vault.view() as state:
            _, device = identity(state, request)
            require(space in device["spaces"], "PERMISSION_REVOKED", 403)
            records = retrieve(state, q, space, purpose, limit)
            from .semantic_index import index_for
            semantic = index_for(state)
            return {"records": records, "abstained": not records,
                    "method": "hybrid_lexical_semantic" if semantic and semantic.ready else "normalized_lexical"}

    @api.get('/life/dashboard')
    def dashboard(request: Request, space: str, date: str):
        from .life import life_dashboard
        with vault.view() as state:
            _, device = identity(state, request)
            return life_dashboard(state, device, space, date)

    @api.post('/kitchen/preview')
    def prepare_kitchen(request: Request):
        from .life import kitchen_preview
        value = sync_body(request, 4096); fields(value, ['meal_id'])
        with vault.view() as state:
            _, device = identity(state, request)
            return kitchen_preview(state, device, value['meal_id'])

    @api.post('/kitchen/actions')
    def act_kitchen(request: Request):
        from .life import kitchen_action
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            _, device = identity(state, request)
            return kitchen_action(state, device, value)

    @api.post('/calendar/preview')
    def preview_calendar(request: Request):
        from .connectors import calendar_preview
        value = sync_body(request, 600000)
        with vault.view() as state:
            _, device = identity(state, request)
            return calendar_preview(state, device, value)

    @api.post('/travel/hotels')
    @api.post('/travel/search')
    async def travel_search(request: Request):
        from . import flyai
        value = await body(request, 8192)
        hotels = request.url.path.endswith('/hotels')
        if hotels:
            fields(value, ['space', 'city', 'day', 'keyword'])
        else:
            fields(value, ['space', 'query'])
            require(isinstance(value['query'], str) and 0 < len(value['query'].strip()) <= 1500)
        def check_traveler():
            _, device = authorize(request)
            require(value['space'] == 'personal' and 'personal' in device['spaces'],
                    'PERMISSION_REVOKED', 403)
        await run_in_threadpool(check_traveler)
        if hotels:
            result = await run_in_threadpool(flyai.hotel_quotes, value['city'], value['day'], value['keyword'])
        else:
            result = await run_in_threadpool(flyai.search, value['query'].strip())
        await run_in_threadpool(check_traveler)
        return result

    @api.post('/weather')
    async def lookup_weather(request: Request):
        from .connectors import weather
        value = await body(request, 4096)
        fields(value, ['latitude', 'longitude', 'space'])
        def check_space():
            _, device = authorize(request)
            require(value['space'] in device['spaces'], 'PERMISSION_REVOKED', 403)
        await run_in_threadpool(check_space)
        result = await weather(value['latitude'], value['longitude'], getattr(request.app.state, 'weather_transport', None))
        # Revocation during the external request still prevents disclosure.
        await run_in_threadpool(check_space)
        return result

    @api.get('/groups')
    def list_groups(request: Request):
        from .sharing import groups
        with vault.view() as state:
            _, device = identity(state, request)
            return {'groups': groups(state, device)}

    @api.post('/groups')
    def add_group(request: Request):
        from .sharing import create_group
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            device_id, _ = identity(state, request, admin=True)
            return create_group(state, device_id, value)

    @api.post('/groups/{group_id}/invite')
    def group_invite(group_id: str, request: Request):
        from .sharing import invite
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            _, device = identity(state, request, admin=True)
            return invite(state, device, group_id, value)

    @api.get("/capabilities")
    def capabilities(request: Request):
        from .features import capability_catalog
        authorize(request)
        return capability_catalog()

    @api.post("/evidence")
    def evidence(request: Request):
        value = sync_body(request, 16384)
        fields(value, ["refs"])
        require(isinstance(value["refs"], list) and len(value["refs"]) <= 20)
        with vault.view() as state:
            _, device = identity(state, request)
            records = []
            for ref in value["refs"]:
                fields(ref, ["id", "revision"])
                record = state["records"].get(ref["id"])
                require(record and record["space"] in device["spaces"], "NOT_FOUND", 404)
                require(record["revision"] == ref["revision"], "SOURCE_INVALIDATED", 409)
                records.append(record)
            return {"records": records}

    @api.get("/usage")
    def usage(request: Request, start: str = '', end: str = '', zone: str = DEFAULT_TIMEZONE):
        from .usage import summary
        with vault.view() as state:
            _, device = identity(state, request)
            return summary(state, device['spaces'], start=start, end=end, zone=zone)

    @api.put('/usage/prices')
    def prices(request: Request):
        from .usage import validate_prices
        value = sync_body(request, 64000)
        fields(value, ['prices'])
        result = validate_prices(value['prices'])
        with vault.transaction() as state:
            identity(state, request, admin=True)
            state['usage_prices'] = result
        return {'saved':True}

    @api.post('/usage/import')
    def import_usage(request: Request):
        from .usage import import_events
        value = sync_body(request, 256000)
        with vault.transaction(bump_revision=False) as state:
            actor, device = identity(state, request)
            return import_events(state, value, origin='phone', source='phone', spaces=device['spaces'], device_id=actor)

    @api.get("/jobs")
    def list_jobs(request: Request, light: bool = False):
        with vault.view() as state:
            _, device = identity(state, request)
            jobs = [{key: job.get(key) for key in ("id", "kind", "space", "state", "error", "result_id", "source_ids", "partial", "context_invalidated")}
                    for job in state["jobs"].values() if job.get('kind') != 'thought_association' and job["space"] in device["spaces"]]
            if light:
                for job in jobs:
                    if len((job.get('partial') or '').encode('utf-8')) > 4096:
                        job['partial'] = ''
                jobs = jobs[-100:]
            return {"jobs": jobs}

    @api.post("/jobs")
    def start_job(request: Request):
        require_worker(request)
        value = sync_body(request, 4096)
        with vault.transaction() as state:
            device_id, _ = identity(state, request)
            result = queue_job(state, device_id, value)
        return result

    @api.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, request: Request):
        with vault.transaction() as state:
            _, device = identity(state, request)
            job = state["jobs"].get(job_id)
            require(job and job["space"] in device["spaces"], "NOT_FOUND", 404)
            if job["state"] in {"queued", "running"}:
                job.update(state="cancelled", error="CANCELLED_BY_USER")
            result = {"state": job["state"]}
        return result

    @api.get("/model")
    def get_model(request: Request):
        with vault.view() as state:
            identity(state, request, admin=True)
            return {"config": state["model"], "has_key": bool(state["credentials"].get("model"))}

    @api.put("/model")
    def set_model(request: Request):
        value = sync_body(request, 8192)
        fields(value, ["config"], ["key"])
        config = model_config(value["config"])
        if "key" in value:
            require(isinstance(value["key"], str) and len(value["key"]) <= 4096)
        with vault.transaction() as state:
            identity(state, request, admin=True)
            if state["model"] and state["model"]["base_url"] != config["base_url"] and "key" not in value:
                state["credentials"].pop("model", None)
            state["model"] = config
            if "key" in value:
                state["credentials"]["model"] = value["key"]
            # Changed credentials can change the account. Do not silently re-run queued work.
            for job in state["jobs"].values():
                if job["state"] in {"queued", "running"}:
                    job.update(state="cancelled", error="MODEL_CONFIG_CHANGED")
        return {"saved": True}

    @api.post("/model/probe")
    async def probe(request: Request):
        require_worker(request)
        from .worker import completion
        from .usage import tracked
        actor, device, config, key = await run_in_threadpool(reserve_model, request, admin=True)
        import httpx
        try:
            await guarded_request(lambda: tracked(vault, config, lambda: completion(config, key, [{"role": "user", "content": "Reply with OK."}]),
                kind="probe", space=device["spaces"][0], device_id=actor),
                                  lambda: check_model_request(request, config, key, admin=True))
        except httpx.TransportError:
            raise Problem("MODEL_OUTCOME_UNKNOWN", 502) from None
        return {"chat": True, "streaming": "not_tested", "structured_output": "not_tested"}

    @api.post("/photo/analysis")
    @api.post("/temporary")
    async def temporary(request: Request):
        require_worker(request)
        # Only numeric usage metadata is durable. No prompt, answer or job.
        from .worker import completion
        from .usage import tracked
        import httpx
        photo = request.url.path.endswith('/photo/analysis')
        value = await body(request, 2900000 if photo else 100000)
        fields(value, ["text", "space", "image"] if photo else ["text", "space"])
        string(value["text"], 32000)
        content = value["text"]
        if photo:
            from .archive import validate_photo_preview
            require(value['space'] == 'personal', 'MODEL_SCOPE_DENIED', 403)
            validate_photo_preview(value['image'])
            content = [{'type': 'text', 'text': content}, {'type': 'image_url',
                        'image_url': {'url': 'data:image/png;base64,' + value['image']}}]
        actor, device, config, key = await run_in_threadpool(reserve_model, request, value['space'])
        try:
            text, _ = await guarded_request(lambda: tracked(vault, config, lambda: completion(config, key, [{"role": "user", "content": content}]),
                kind="photo" if photo else "temporary", space=value["space"], device_id=actor),
                                            lambda: check_model_request(request, config, key, value["space"]))
        except httpx.TransportError:
            raise Problem("MODEL_OUTCOME_UNKNOWN", 502) from None
        await run_in_threadpool(authorize, request)
        return {"text": text}

    @api.put("/preferences")
    def preferences(request: Request):
        value = sync_body(request, 256 * 1024)
        # Older clients echo snapshot preferences. Learning is read-only here;
        # accept the projection without granting a rule-writing capability.
        value.pop('dreaming', None)
        value.pop('news', None)  # Headlines have their own admin-only settings route.
        value.pop('field_versions', None)  # Read-only per-field sync projection.
        fields(value, [], ["proactive", "daily_limit", "model_proactive", "model_daily_limit", "timezone", "quiet_start", "quiet_end", "snooze_until", "conversation_quiet_seconds", "dreaming_hour"])
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        require(type(value.get("model_proactive", False)) is bool)
        require(type(value.get("model_daily_limit", 3)) is int and 0 <= value.get("model_daily_limit", 3) <= 20)
        require(type(value.get('conversation_quiet_seconds', 180)) is int
                and 0 <= value.get('conversation_quiet_seconds', 180) <= 86400)
        require(type(value.get('dreaming_hour', 3)) is int and 0 <= value.get('dreaming_hour', 3) <= 7)
        for field in ("quiet_start", "quiet_end"):
            require(field not in value or (type(value[field]) is int and 0 <= value[field] <= 23))
        require(("quiet_start" in value) == ("quiet_end" in value))
        require('timezone' not in value or isinstance(value['timezone'], str), 'INVALID_TIMEZONE')
        try: ZoneInfo(value.get("timezone", DEFAULT_TIMEZONE))
        except (ZoneInfoNotFoundError, ValueError): raise Problem("INVALID_TIMEZONE") from None
        if value.get("snooze_until") is not None:
            from .features import timestamp
            value["snooze_until"] = timestamp(value["snooze_until"])
        require("proactive" not in value or type(value["proactive"]) is bool)
        require("daily_limit" not in value or type(value["daily_limit"]) is int and 0 <= value["daily_limit"] <= 10)
        with vault.transaction() as state:
            identity(state, request, admin=True)
            versions = state['preferences'].setdefault('field_versions', {})
            for key in ('proactive', 'daily_limit', 'quiet_start', 'quiet_end', 'timezone'):
                if key in value and (value[key] != state['preferences'].get(key) or len(value) == 1):
                    versions[key] = {'version': versions.get(key, {}).get('version', 0) + 1, 'origin': 'user'}
            # Learned rules are internal, visible preferences, not discarded when
            # a client submits the editable interruption controls.
            state['preferences'].update(value)
            from .thought_candidates import refresh_policy
            refresh_policy(state)
            if 'snooze_until' in value and value['snooze_until'] is None:
                state['preferences'].pop('snooze_until', None)
            if not state['preferences']["proactive"] or not state['preferences'].get("model_proactive", False):
                for job in state["jobs"].values():
                    if job["kind"] == "proactive" and job["state"] in {"queued", "running"}:
                        job.update(state="cancelled", error="PROACTIVE_DISABLED", partial="")
            if not state['preferences']["proactive"]:
                for suggestion in state["suggestions"].values():
                    if suggestion["state"] in {"new", "snoozed"}:
                        suggestion["state"] = "dismissed"
        return {"saved": True}

    @api.get('/preferences/learned-rules')
    def learned_rules(request: Request):
        with vault.view() as state:
            _, device = identity(state, request, admin=True)
            learning = state['preferences'].get('dreaming', {})
            return {'rules': [{**rule, 'space': normalize_space(rule['space'])} for rule in learning.get('rules', []) if normalize_space(rule['space']) in device['spaces']],
                    'clues': [{**clue, 'space': normalize_space(clue['space'])} for clue in learning.get('clues', []) if normalize_space(clue['space']) in device['spaces']],
                    'last_day': learning.get('last_day')}

    @api.delete('/preferences/learned-rules')
    def clear_learned_rules(request: Request, space: str):
        with vault.transaction() as state:
            actor, device = identity(state, request, admin=True)
            require(space in device['spaces'], 'PERMISSION_REVOKED', 403)
            learning = state['preferences'].get('dreaming', {})
            removed = 0
            for collection in ('rules', 'clues'):
                rows = learning.get(collection, [])
                remaining = [row for row in rows if normalize_space(row['space']) != space]
                removed += len(rows) - len(remaining)
                if collection in learning:
                    learning[collection] = remaining
            for job in state['jobs'].values():
                if job.get('dreaming') and normalize_space(job.get('space')) == space and job['state'] in {'queued', 'running'}:
                    job.update(state='cancelled', error='LEARNING_RESET', partial='')
            from .admin import audit
            audit(state, actor, 'preferences.learning.clear', space)
            return {'removed': removed}

    @api.post("/suggestions/{suggestion_id}/dismiss")
    def dismiss(suggestion_id: str, request: Request):
        with vault.transaction() as state:
            _, device = identity(state, request)
            suggestion = state["suggestions"].get(suggestion_id)
            require(suggestion and suggestion["space"] in device["spaces"], "NOT_FOUND", 404)
            suggestion["state"] = "dismissed"
        return {"saved": True}

    @api.post("/suggestions/{suggestion_id}/feedback")
    def suggestion_feedback(suggestion_id: str, request: Request):
        value = sync_body(request, 4096)
        fields(value, ["action"], ["until"])
        require(value["action"] in {"useful", "dismiss", "snooze"})
        with vault.transaction() as state:
            _, device = identity(state, request)
            suggestion = state["suggestions"].get(suggestion_id)
            require(suggestion and suggestion["space"] in device["spaces"], "NOT_FOUND", 404)
            require(suggestion["state"] != "expired" and suggestion.get("expires_at", "~") > now(), "SUGGESTION_EXPIRED", 409)
            if value["action"] == "snooze":
                from .features import timestamp
                until = timestamp(value.get("until"))
                require(until is not None and now() < until < suggestion.get("expires_at", "~"), "INVALID_TIME_WINDOW")
                suggestion.update(state="snoozed", snooze_until=until)
            else:
                suggestion.update(state="used" if value["action"] == "useful" else "dismissed")
        return {"state": suggestion["state"]}

    @api.get("/maintenance")
    def maintenance_status(request: Request):
        with vault.view() as state:
            identity(state, request, admin=True)
            return {"automatic_backup": state.get("backup_policy"), "last_backup": state.get("last_backup")}

    @api.get("/devices")
    def devices(request: Request):
        with vault.view() as state:
            identity(state, request, admin=True)
            return {"devices": [{"id": key, "last_seen":d.get("last_seen"), "platform":d.get("platform"), "version":d.get("version"), **{k: d[k] for k in ("name", "spaces", "admin", "revoked", "expires_at")}}
                                for key, d in state["devices"].items()]}

    def withdraw_device(state, actor, device_id, remove):
        from .admin import audit
        from .thought_candidates import revoke_device
        revoke_device(state, device_id)
        if remove:
            del state["devices"][device_id]
        else:
            state["devices"][device_id]["revoked"] = True
        for job in state["jobs"].values():
            if job["device_id"] == device_id and job["state"] in {"queued", "running"}:
                job.update(state="cancelled", error="PERMISSION_REVOKED", partial="")
        audit(state, actor, "device.remove" if remove else "device.revoke", device_id)

    @api.post("/devices/cleanup")
    def cleanup_devices(request: Request):
        value = sync_body(request, 128 * 1024)
        fields(value, ["ids"])
        require(isinstance(value["ids"], list) and 1 <= len(value["ids"]) <= 1000)
        ids = set(string(identifier, 128) for identifier in value["ids"])
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            instant = now()
            removed = 0
            for device_id in sorted(ids - {actor}):
                device = state["devices"].get(device_id)
                if device and (device["revoked"] or device["expires_at"] <= instant):
                    withdraw_device(state, actor, device_id, remove=True)
                    removed += 1
        return {"removed": removed}

    @api.delete("/devices/{device_id}")
    def revoke(device_id: str, request: Request, remove: bool = False):
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            require(not remove or device_id != actor, "CURRENT_DEVICE", 409)
            if remove and device_id not in state["devices"]:
                return {"revoked": True, "removed": True, "offline_copy_erased": False}
            require(device_id in state["devices"], "NOT_FOUND", 404)
            withdraw_device(state, actor, device_id, remove)
        return {"revoked": True, "offline_copy_erased": False, **({"removed": True} if remove else {})}

    @api.post("/backup")
    def backup(request: Request):
        value = sync_body(request, 4096)
        fields(value, ["password"])
        string(value["password"], 1024)
        authorize(request, admin=True)
        require(len(value["password"]) >= 12, "WEAK_PASSPHRASE")
        return vault.backup(value["password"])

    @api.post("/restore/preview")
    def preview_restore(request: Request):
        value = sync_body(request, 32 * 1024 * 1024)
        fields(value, ["password", "bundle"])
        authorize(request, admin=True)
        string(value["password"], 1024)
        try:
            archive = open_backup(value["bundle"], value["password"])
        except Exception:
            raise Problem("INVALID_BACKUP_OR_PASSPHRASE") from None
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            additions, conflicts = restore_plan(state, archive)
            token = uid()
            state["restore_preview"] = {"token": token, "device_id": actor, "archive": archive, "seq": state["seq"],
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
            result = {"confirmation_token": token, "additions": len(additions), "conflicts": len(conflicts),
                      "deletions": len(set(archive["deleted"]) & set(state["records"]))}
        return result

    @api.post("/restore/commit")
    def commit_restore(request: Request):
        value = sync_body(request, 4096)
        fields(value, ["confirmation_token"])
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            completed = state.setdefault("restore_receipts", {}).get(value["confirmation_token"])
            if completed:
                require(completed["device_id"] == actor, "PREVIEW_REQUIRED", 409)
                return completed["result"]
            preview = state.get("restore_preview")
            require(preview and preview.get("device_id") == actor and preview["token"] == value["confirmation_token"] and preview["expires_at"] > now(),
                    "PREVIEW_REQUIRED", 409)
            require(preview["seq"] == state["seq"], "PREVIEW_STALE", 409)
            archive = preview.get('archive') or state['private_items']['restore_archive']
            additions, conflicts = restore_plan(state, archive)
            pending = dict(archive["deleted"])
            while pending:
                leaves = [key for key in pending if not any(r["data"].get("parent_id") == key for r in state["records"].values())]
                require(leaves, "HAS_LINKED_ITEMS", 409)
                for key in leaves:
                    delete_record(state, key)
                    restored = pending.pop(key)
                    current = state["deleted"].get(key, {})
                    if current.get("memory_source_ids") and not restored.get("memory_source_ids"):
                        restored = {**restored, "memory_source_ids": current["memory_source_ids"]}
                    state["deleted"][key] = {**restored, 'store_revision': state.store_revision + 1}
            state["records"].update(additions)
            for key, event in archive.get("usage_events", {}).items():
                state["usage_events"].setdefault(key, event)
            state.setdefault('family_spaces',{}).update(archive.get('family_spaces',{}))
            for device in state['devices'].values():
                if device['admin']:
                    device['spaces'] = list(dict.fromkeys([*device['spaces'], *state.get('family_spaces',{})]))
            state["seq"] += 1
            state.pop("restore_preview", None)
            result = {"restored": len(additions), "skipped_conflicts": len(conflicts)}
            state["restore_receipts"][value["confirmation_token"]] = {"device_id":actor, "result":result}
            from .admin import audit
            audit(state, actor, "backup.restore")
        return result

    @api.delete("/restore/preview")
    def cancel_restore(request: Request):
        with vault.transaction() as state:
            actor, _ = identity(state, request, admin=True)
            preview = state.get("restore_preview")
            require(not preview or preview.get("device_id") == actor, "PREVIEW_REQUIRED", 409)
            state.pop("restore_preview", None)
        return {"cancelled": True}

    from .web_family import routes as family_routes
    api.include_router(family_routes(vault))
    return api
