"""Poll verified doorbell messages and deduplicate them."""

import asyncio
from collections import deque
from hashlib import sha256
import json
import logging
import time

import aiohttp

POLL_INTERVAL = 8
MAX_BACKOFF = 120
RATE_LIMIT_BACKOFF = 60
EVENT_TTL = 180
LOG = logging.getLogger(__name__)


class RingResponseError(RuntimeError):
    """An invalid alarm page with a safe diagnostic reason."""

    def __init__(self, reason, code=""):
        super().__init__(code)
        self.reason = reason


def failure_reason(error):
    """Describe failures without logging responses, URLs or credentials."""
    if isinstance(error, RingResponseError):
        return error.reason
    if isinstance(error, TimeoutError):
        return "request timed out"
    if isinstance(error, aiohttp.ClientResponseError):
        return f"HTTP {error.status}"
    if isinstance(error, aiohttp.ClientError):
        return "network connection or response decoding failed"
    if isinstance(error, RuntimeError):
        return "mobile API rejected the request or returned an invalid envelope"
    return type(error).__name__


def retry_delay(delay, consecutive_failures, error):
    """Keep the first retry responsive and back off during longer failures."""
    if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
        retry_after = (error.headers or {}).get("Retry-After", "")
        if retry_after.isascii() and retry_after.isdigit() and len(retry_after) < 10:
            return max(RATE_LIMIT_BACKOFF, int(retry_after))
        return RATE_LIMIT_BACKOFF
    if consecutive_failures == 1:
        return POLL_INTERVAL
    return min(delay * 2, MAX_BACKOFF)


def ring_events(rows, device_id, earliest):
    events = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("msgSrcId") != device_id:
            continue
        if row.get("msgCode") != "ipc_doorbell":
            continue
        raw_id = row.get("idStr") or row.get("id")
        raw_time = row.get("time")
        if type(raw_time) not in (int, str) or not str(raw_time).isdigit():
            continue
        if type(raw_id) not in (int, str) or not str(raw_id).strip() or len(str(raw_id)) > 128:
            continue
        if len(str(raw_time)) not in (10, 13):
            continue
        stamp = int(raw_time)
        if stamp >= 10**12:
            stamp //= 1000
        if stamp < earliest or stamp > int(time.time()) + 15:
            continue
        identity = sha256(f"{device_id}:{raw_id}".encode()).hexdigest()
        events[identity] = {"event": "ring", "event_id": identity, "timestamp": stamp}
    return sorted(events.values(), key=lambda event: (event["timestamp"], event["event_id"]))


async def fetch_rings(mobile, device_id, earliest):
    query = {"limit": 20, "offset": 0, "bigMsgType": 1, "msgSrcId": device_id}
    response = await mobile._call(
        "tuya.m.msg.list.by.json",
        {"json": json.dumps(query, separators=(",", ":"))},
        version="1.0",
    )
    if not isinstance(response, dict):
        raise RingResponseError("invalid response format")
    result = response.get("result")
    if not response.get("success"):
        code = str(response.get("errorCode") or response.get("code") or "")
        raise RingResponseError("Tuya rejected the message request", code)
    if not isinstance(result, dict) or not isinstance(result.get("datas"), list):
        raise RingResponseError("message response is missing a valid alarm page")
    return ring_events(result["datas"], device_id, earliest)


async def watch_rings(fetch, deliver):
    started = int(time.time())
    clock = asyncio.get_running_loop().time
    pending = deque()
    known = {}
    delay = POLL_INTERVAL
    consecutive_failures = 0
    last_failure = None
    LOG.info("Home Assistant: sledování stisku zvonku každých %s s.", POLL_INTERVAL)
    while True:
        poll_started = clock()
        cutoff = max(started, int(time.time()) - EVENT_TTL)
        known = {key: stamp for key, stamp in known.items() if stamp >= cutoff}
        try:
            events = await fetch(cutoff)
            detected_at = int(time.time())
            for event in events:
                identity = event["event_id"]
                if identity not in known:
                    known[identity] = event["timestamp"]
                    pending.append(event | {"detected_at": detected_at})
            delay = POLL_INTERVAL
            consecutive_failures = 0
            if last_failure is not None:
                LOG.info("Tuya doorbell message polling recovered")
                last_failure = None
        except Exception as exc:
            reason = failure_reason(exc)
            consecutive_failures += 1
            delay = retry_delay(delay, consecutive_failures, exc)
            if reason != last_failure:
                LOG.warning("Tuya doorbell message polling failed: %s. Retry in %s s", reason, delay)
                last_failure = reason

        while pending:
            event = pending[0]
            if event["timestamp"] >= int(time.time()) - EVENT_TTL:
                try:
                    await deliver(event)
                except Exception as exc:
                    LOG.warning("Home Assistant: doručení zatím selhalo (%s).", type(exc).__name__)
                    break
                LOG.debug("Doorbell event delivered.")
            pending.popleft()
        # The wait for a notification photo must not extend the normal polling interval.
        wait = delay if consecutive_failures else max(1, poll_started + delay - clock())
        await asyncio.sleep(wait)
