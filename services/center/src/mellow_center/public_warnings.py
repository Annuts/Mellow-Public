"""Official warning contract. No news-derived or model-invented warning fallback."""
from datetime import datetime
import re
from urllib.parse import urlsplit

from fastapi import Request

from .domain import require


def validate_warning(row, trusted_issuers):
    require(isinstance(row, dict), 'INVALID_PUBLIC_WARNING')
    required = {'id', 'issuer', 'url', 'region_code', 'level', 'issued_at', 'expires_at',
                'status', 'update_id', 'cancel_id', 'title'}
    require(required <= set(row), 'INVALID_PUBLIC_WARNING')
    require(all(isinstance(row[k], str) and 0 < len(row[k]) <= 4096 for k in required - {'cancel_id'}),
            'INVALID_PUBLIC_WARNING')
    try:
        url = urlsplit(row['url'])
        port = url.port
    except ValueError:
        from .domain import Problem
        raise Problem('INVALID_PUBLIC_WARNING') from None
    hosts = trusted_issuers.get(row['issuer'])
    require(row['issuer'] in trusted_issuers and url.scheme == 'https' and not url.username and
            not url.password and port in (None, 443) and isinstance(hosts, (set, list, tuple)) and
            url.hostname in hosts, 'UNTRUSTED_WARNING_SOURCE')
    require(bool(re.fullmatch(r'[0-9]{6,12}', row['region_code'])) and
            row['level'] in {'blue', 'yellow', 'orange', 'red'} and
            row['status'] in {'active', 'updated', 'cancelled'}, 'INVALID_PUBLIC_WARNING')
    try:
        issued, expiry = (datetime.fromisoformat(row[k]) for k in ('issued_at', 'expires_at'))
        valid = issued.tzinfo is not None and expiry.tzinfo is not None and expiry > issued
    except (ValueError, TypeError):
        valid = False
    require(valid and (row['cancel_id'] is None or isinstance(row['cancel_id'], str) and
                       0 < len(row['cancel_id']) <= 256), 'INVALID_PUBLIC_WARNING')
    require(row['status'] != 'cancelled' or bool(row['cancel_id']), 'INVALID_PUBLIC_WARNING')
    return dict(row)


def warnings(value):
    require(isinstance(value, dict) and set(value) <= {'region_code'}, 'INVALID_WARNING_REGION')
    region = value.get('region_code')
    require(region is None or isinstance(region, str) and bool(re.fullmatch(r'[0-9]{6,12}', region)),
            'INVALID_WARNING_REGION')
    # No authenticated official feed is currently configured or bundled.
    return {'schema_version': 1, 'available': False, 'reason': 'NO_TRUSTWORTHY_SOURCE',
            'region_code': region, 'warnings': []}


def routes(api, vault, identity):
    def get_warnings(request: Request, region_code: str | None = None):
        identity(vault.read(), request)
        return warnings({'region_code': region_code})

    api.add_api_route('/public-warnings', get_warnings, methods=['GET'])
