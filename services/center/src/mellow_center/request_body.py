"""Bounded ASGI receive and thread-friendly JSON decoding shared by routers."""
import json

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from .domain import Problem, require


async def buffer_body(request: Request):
    if request.method in {'POST', 'PUT', 'PATCH'}:
        limit = {
            '/api/preview/pair': 4096,
            '/console/session': 8192,
            '/console/actions': 512 * 1024,
            '/api/connectors/imports': 512 * 1024,
            '/api/connectors/usage': 256000,
            '/api/preview/operations': 16 * 1024 * 1024,
            '/api/preview/restore/preview': 32 * 1024 * 1024,
            '/api/preview/voice/transcribe': 2_010_000,
            '/api/preview/photo/analysis': 2900000,
            '/api/preview/temporary': 100000,
        }.get(request.url.path, 2 * 1024 * 1024)
        length = request.headers.get('content-length')
        if length is not None:
            require(length.isdigit(), 'INVALID_ARGUMENT')
            require(int(length) <= limit, 'PAYLOAD_TOO_LARGE', 413)
        data = bytearray()
        async for part in request.stream():
            require(len(data) + len(part) <= limit, 'PAYLOAD_TOO_LARGE', 413)
            data.extend(part)
        request._body = bytes(data)


def json_object(raw):
    try:
        value = json.loads(raw)
        require(isinstance(value, dict))
        from .spaces import normalize_payload
        normalize_payload(value)
        return value
    except (ValueError, UnicodeError):
        raise Problem('INVALID_JSON') from None


def sync_body(request, limit=2 * 1024 * 1024):
    raw = request._body
    require(len(raw) <= limit, 'PAYLOAD_TOO_LARGE', 413)
    return json_object(raw)


async def body(request, limit=2 * 1024 * 1024):
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        require(len(data) <= limit, 'PAYLOAD_TOO_LARGE', 413)
    return await run_in_threadpool(json_object, bytes(data))
