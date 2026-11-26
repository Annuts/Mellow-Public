"""Thought v2 identity, matching Dart's sorted-map jsonEncode + SHA-256/24."""
import hashlib
import json
import math
from decimal import Decimal


def _encode(value):
    if isinstance(value, str):
        value = value.encode('utf-16-le', errors='surrogatepass').decode('utf-16-le', errors='surrogatepass')
        encoded = json.dumps(value, ensure_ascii=False)
        return ''.join(f'\\u{ord(char):04x}' if 0xd800 <= ord(char) <= 0xdfff else char for char in encoded)
    if isinstance(value, dict):
        # Dart String.compareTo compares UTF-16 code units, not Unicode codepoints.
        if not all(isinstance(k, str) for k in value):
            raise ValueError('Thought JSON keys must be strings')
        keys = sorted(value, key=lambda k: k.encode('utf-16-be', errors='surrogatepass'))
        return '{' + ','.join(_encode(k) + ':' + _encode(value[k]) for k in keys) + '}'
    if isinstance(value, list):
        return '[' + ','.join(_encode(v) for v in value) + ']'
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite Thought number')
        number = repr(value)
        if 'e' in number:
            mantissa, exponent = number.split('e')
            power = int(exponent)
            if -6 <= power < 21:
                return format(Decimal(number), 'f') + ('.0' if power >= 0 else '')
            return mantissa.removesuffix('.0') + 'e' + ('+' if power >= 0 else '-') + str(abs(power))
        return number
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def thought_id(value: object) -> str:
    encoded = _encode(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
