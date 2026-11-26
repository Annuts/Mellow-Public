"""Independent, redacted retry boundaries shared by background loops."""
import time

from .maintenance import log_event
from .vault import now


async def worker_tick(vault, status, steps, clock=None):
    """Isolate failures and back off each step without starving its peers."""
    for name, action in steps.items():
        step = status.setdefault(name, dict(last_ok_at=None, last_error_code=None, consecutive_failures=0))
        instant = time.monotonic() if clock is None else clock
        if instant < step.get('retry_after', 0):
            continue
        started = time.monotonic()
        try:
            await action()
        except Exception as error:
            step['consecutive_failures'] += 1
            step['last_error_code'] = 'STEP_FAILED'
            failed_at = time.monotonic() if clock is None else clock
            step['retry_after'] = failed_at + min(60, 2 ** min(step['consecutive_failures'], 6))
            log_event('STEP_FAILED', name, error, consecutive_failures=step['consecutive_failures'],
                      duration_ms=int((time.monotonic() - started) * 1000))
        else:
            step.update(last_ok_at=now(), last_error_code=None, consecutive_failures=0, retry_after=0)
