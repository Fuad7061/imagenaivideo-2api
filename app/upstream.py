"""Upstream adapter around the low-level ``miricanvas_client``.

The reverse-engineered client lives at the repository root so it can also be
used standalone. This module is a thin, testable facade: it normalises return
values, maps failures to ``UpstreamError`` and exposes a device-ledger summary
for the admin console.
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import miricanvas_client as _mc  # noqa: E402


class UpstreamError(RuntimeError):
    """Raised when the upstream gateway rejects or fails a request."""


def configure(config):
    """Apply admin-configured values to the underlying client."""
    try:
        limit = int(config.get("devices", "daily_limit", default=_mc.DAILY_LIMIT))
        _mc.DAILY_LIMIT = max(1, limit)
    except Exception:
        pass


def submit(image_b64, prompt, device_id=None, proxy=None):
    """Submit an image-to-video job. Returns ``{request_id, device, remaining}``."""
    result = _mc.generate(image_b64, prompt, proxy=proxy, device_id=device_id)
    if not result or not result.get("ok"):
        detail = ""
        if isinstance(result, dict):
            detail = result.get("resp") or result.get("http") or ""
        raise UpstreamError(f"upstream rejected the request: {detail}" if detail else "upstream request failed")
    return result


def poll(request_id, proxy=None):
    """Fetch the current status of a job. Returns the raw status dict."""
    return _mc.status(request_id, proxy=proxy)


def health(proxy=None):
    """Ping the upstream health endpoint."""
    return _mc.health(proxy=proxy)


def new_device(reason="manual"):
    """Force creation of a fresh device fingerprint."""
    return _mc.new_device(reason)


def ledger_summary():
    """Summarise the local device ledger for the dashboard."""
    today = _mc._today()
    try:
        ledger = _mc._load_ledger()
    except Exception:
        ledger = {}
    limit = int(getattr(_mc, "DAILY_LIMIT", 2))
    today_devices = [(d, info) for d, info in ledger.items() if info.get("created") == today]
    used_today = sum(int(info.get("used", 0)) for _, info in today_devices)
    capacity = len(today_devices) * limit
    devices = [
        {
            "id": d,
            "used": int(info.get("used", 0)),
            "remaining": max(0, limit - int(info.get("used", 0))),
            "reason": info.get("reason", ""),
            "last_used": info.get("last_used", ""),
        }
        for d, info in today_devices
    ]
    devices.sort(key=lambda x: x["id"])
    return {
        "today": today,
        "daily_limit": limit,
        "total_devices": len(ledger),
        "today_devices": len(today_devices),
        "used_today": used_today,
        "capacity_today": capacity,
        "remaining_today": max(0, capacity - used_today),
        "devices": devices,
    }
