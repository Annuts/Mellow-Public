"""Explicit weather lookup and offline iCalendar import. No background account access."""
from __future__ import annotations
import hashlib
import math
import re
from typing import Any
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import httpx
from .domain import Problem, fields, require, string
from .features import DEFAULT_TIMEZONE, timestamp
from .vault import now
from .spaces import normalize_space, normalize_spaces


def ical_text(value):
    return re.sub(r'\\([nN,;\\])', lambda m: '\n' if m[1] in 'nN' else m[1], value)


def calendar_preview(state, device, payload):
    fields(payload, ['text', 'calendar_id', 'space'], ['timezone'])
    payload = {**payload, 'space': normalize_space(payload['space'])}
    require(payload['space'] in normalize_spaces(device['spaces']), 'PERMISSION_REVOKED', 403)
    raw = string(payload['text'], 500000); calendar_id = string(payload['calendar_id'], 64)
    try: default_zone = ZoneInfo(payload.get('timezone', DEFAULT_TIMEZONE))
    except (ZoneInfoNotFoundError, ValueError): raise Problem('INVALID_TIMEZONE') from None
    unfolded = re.sub(r'\r?\n[ \t]', '', raw)
    require('BEGIN:VCALENDAR' in unfolded and 'END:VCALENDAR' in unfolded, 'INVALID_CALENDAR')
    events = re.findall(r'BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT', unfolded, re.S)
    require(len(events) <= 500, 'PAYLOAD_TOO_LARGE')
    records, errors, duplicates, seen, warnings = [], [], 0, set(), []
    def parse_time(pair):
        params, value = pair
        all_day = params.get('VALUE') == 'DATE' or bool(re.fullmatch(r'\d{8}', value))
        zone = ZoneInfo(params['TZID'].strip('"')) if 'TZID' in params else default_zone
        if all_day: parsed = datetime.strptime(value, '%Y%m%d').replace(tzinfo=zone)
        elif value.endswith('Z'): parsed = datetime.strptime(value, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.strptime(value, '%Y%m%dT%H%M%S').replace(tzinfo=zone)
            # Ambiguous/nonexistent wall-clock instants need explicit UTC input.
            require(parsed.utcoffset() == parsed.replace(fold=1).utcoffset(), 'AMBIGUOUS_TIME')
        return parsed, all_day
    for index, raw_event in enumerate(events):
        try:
            values = {}
            components = []
            for line in raw_event.splitlines():
                if ':' not in line: continue
                left, value = line.split(':', 1); pieces = left.split(';')
                key = pieces[0].upper()
                if key == 'BEGIN':
                    components.append(value.upper())
                    if value.upper() == 'VALARM': warnings.append({'index': index+1, 'code': 'ALARM_IGNORED'})
                    continue
                if key == 'END':
                    require(components and components.pop() == value.upper(), 'INVALID_COMPONENT')
                    continue
                if components: continue
                if key in {'UID', 'DTSTART', 'DTEND', 'SUMMARY'}: require(key not in values, 'DUPLICATE_FIELD')
                values[key] = ({p.split('=',1)[0].upper():p.split('=',1)[1] for p in pieces[1:] if '=' in p}, value)
            require(not components, 'INVALID_COMPONENT')
            require(not any(key in values for key in {'RRULE', 'RDATE', 'EXDATE', 'RECURRENCE-ID', 'DURATION'}), 'RECURRENCE_UNSUPPORTED')
            source_id = string(values['UID'][1], 180)
            identifier = 'calendar-' + hashlib.sha256((payload['space']+'\0'+calendar_id+'\0'+source_id).encode()).hexdigest()
            if payload['space'] == 'personal':
                for alias in ('personal', 'life', 'work'):
                    candidate = 'calendar-' + hashlib.sha256((alias+'\0'+calendar_id+'\0'+source_id).encode()).hexdigest()
                    if candidate in state['records'] or candidate in state['deleted']:
                        identifier = candidate
                        break
            require(identifier not in seen, 'DUPLICATE_SOURCE'); seen.add(identifier)
            start, all_day = parse_time(values['DTSTART'])
            if 'DTEND' in values: end, end_all_day = parse_time(values['DTEND']); require(all_day == end_all_day)
            else: require(all_day, 'END_TIME_REQUIRED'); end = start + timedelta(days=1)
            require(end > start, 'INVALID_TIME_WINDOW')
            status = values.get('STATUS', ({}, 'CONFIRMED'))[1].lower()
            require(status in {'confirmed', 'tentative', 'cancelled'})
            data: dict[str, Any] = dict(text=ical_text(values['SUMMARY'][1]), start=start.astimezone(timezone.utc).isoformat(),
                end=end.astimezone(timezone.utc).isoformat(), all_day=all_day,
                location=ical_text(values.get('LOCATION', ({}, ''))[1]), status=status, origin='ical:'+identifier)
            string(data['text'], 500); require(len(data['location']) <= 500)
            require(identifier not in state['deleted'], 'DELETED_OBJECT')
            old = state['records'].get(identifier)
            if old:
                if old['data'] == data: duplicates += 1; continue
                raise Problem('IMPORT_CONFLICT')
            records.append(dict(id=identifier,kind='event',space=payload['space'],mode='normal',data=data))
        except (Problem, KeyError, ValueError, ZoneInfoNotFoundError) as error:
            errors.append({'index':index+1, 'code':error.code if isinstance(error,Problem) else 'INVALID_EVENT'})
    return {'records':records,'errors':errors,'duplicates':duplicates, 'total':len(events),'warnings':warnings}


async def weather(latitude, longitude, transport=None):
    for coordinate, limit in ((latitude, 90), (longitude, 180)):
        require(type(coordinate) in {int,float} and math.isfinite(coordinate) and abs(coordinate) <= limit, 'INVALID_LOCATION')
    params = {'latitude':latitude, 'longitude':longitude, 'current':'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
              'daily':'temperature_2m_max,temperature_2m_min,precipitation_probability_max', 'forecast_days':3, 'timezone':DEFAULT_TIMEZONE}
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=False,trust_env=False,transport=transport) as client:
            async with client.stream('GET','https://api.open-meteo.com/v1/forecast',params=params) as response:
                require(response.status_code == 200, 'WEATHER_UNAVAILABLE', 502)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk); require(len(body) <= 100000, 'WEATHER_INVALID_RESPONSE', 502)
        import json
        data = json.loads(body); current = data['current']; daily = data['daily']
        require(data.get('timezone') == DEFAULT_TIMEZONE, 'WEATHER_INVALID_RESPONSE', 502)
        def numeric(value):
            require(type(value) in {int,float} and math.isfinite(value), 'WEATHER_INVALID_RESPONSE', 502)
            return value
        instant = datetime.fromisoformat(current['time']).replace(tzinfo=ZoneInfo(DEFAULT_TIMEZONE))
        result: dict[str, Any] = {'source':'Open-Meteo','source_url':'https://open-meteo.com/','fetched_at':now(), 'observed_at':instant.isoformat(),
                  'temperature':numeric(current['temperature_2m']), 'feels_like':numeric(current['apparent_temperature']),
                  'weather_code':numeric(current['weather_code']), 'wind_kmh':numeric(current['wind_speed_10m']), 'days':[]}
        require(1 <= len(daily['time']) <= 3, 'WEATHER_INVALID_RESPONSE', 502)
        for i, day in enumerate(daily['time']):
            from .life import day as validate_day
            validate_day(day)
            result['days'].append({'date':day,'max':numeric(daily['temperature_2m_max'][i]),'min':numeric(daily['temperature_2m_min'][i]),
                                   'rain_probability':numeric(daily['precipitation_probability_max'][i])})
        return result
    except (httpx.TransportError, KeyError, ValueError, TypeError, IndexError):
        raise Problem('WEATHER_UNAVAILABLE',502) from None
