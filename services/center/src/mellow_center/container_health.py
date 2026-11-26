"""Docker health probe using the existing center certificate."""
import json
import ssl
from urllib.request import urlopen


def main():
    tls = ssl.create_default_context(cafile='/var/lib/mellow/tls.crt')
    with urlopen('https://localhost:8765/api/preview/health', context=tls, timeout=3) as response:
        return 0 if json.load(response).get('status') == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
