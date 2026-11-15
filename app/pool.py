"""Device pool: keeps a warm set of fresh device fingerprints ready.

imagenai rotates *device fingerprints* (each limited to a fixed number of
generations per day) rather than logging in to real accounts, so this pool is
intentionally light: fingerprints are minted locally and only written to the
ledger once they are actually used. State lives in memory and a single daemon
thread wakes on a slow tick to refill the pool, keeping CPU and disk use low.
"""
import threading
import time


class PoolEmpty(RuntimeError):
    """Raised when no device can be acquired within the timeout."""


class DevicePool:
    def __init__(self, config, upstream):
        self._config = config
        self._upstream = upstream
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._devices = []  # [{id, credits, inflight, reason, created}]
        self._thread = None
        self._last_mint = 0.0
        self.minted = 0
        self.last_error = None
        self._seed()

    # ---- config (read live so admin edits apply instantly) ----
    def _int(self, key, default, floor=0):
        try:
            return max(floor, int(self._config.get("pool", key, default=default)))
        except Exception:
            return default

    def _float(self, key, default, floor=0.0):
        try:
            return max(floor, float(self._config.get("pool", key, default=default)))
        except Exception:
            return default

    def target(self):
        return self._int("target", 16, 1)

    def minimum(self):
        return self._int("min", 8, 0)

    def maximum(self):
        return max(self.target(), self._int("max", 40, 1))

    def reg_interval(self):
        return self._float("reg_min_interval", 3.0, 0.0)

    def refill_interval(self):
        return self._float("refill_interval", 5.0, 0.5)

    def session_ttl(self):
        return self._float("session_ttl", 1500.0, 0.0)

    def acquire_timeout(self):
        return self._float("acquire_timeout", 120.0, 0.0)

    def _limit(self):
        return max(1, int(self._config.get("devices", "daily_limit", default=2)))

    # ---- internals (call with the lock held) ----
    def _mint(self, reason="pool"):
        did = self._upstream.mint_device()
        self._devices.append(
            {"id": did, "credits": self._limit(), "inflight": 0, "reason": reason, "created": time.time()}
        )
        self._last_mint = time.time()
        self.minted += 1
        return did

    def _prune(self, now):
        ttl = self.session_ttl()
        keep = []
        for d in self._devices:
            if d["inflight"]:
                keep.append(d)
                continue
            if d["credits"] <= 0:
                continue  # exhausted
            if ttl and (now - d["created"]) > ttl and len(self._devices) > self.minimum():
                continue  # stale idle fingerprint, recycle
            keep.append(d)
        self._devices = keep

    def _seed(self):
        with self._cond:
            try:
                for d in self._upstream.ledger_summary().get("devices", []):
                    if d.get("remaining", 0) > 0:
                        self._devices.append(
                            {"id": d["id"], "credits": d["remaining"], "inflight": 0,
                             "reason": d.get("reason") or "adopted", "created": time.time()}
                        )
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
            while len(self._devices) < self.minimum():
                self._mint("seed")
            self._cond.notify_all()

    # ---- lifecycle ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="device-pool", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(self.refill_interval()):
            try:
                self.refill()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)

    def refill(self):
        """Mint at most one fresh fingerprint per tick, gated by the reg interval."""
        with self._cond:
            now = time.time()
            self._prune(now)
            if (len(self._devices) < self.target()
                    and len(self._devices) < self.maximum()
                    and now - self._last_mint >= self.reg_interval()):
                self._mint("refill")
            self._cond.notify_all()
            return self.snapshot()

    # ---- scheduling ----
    def acquire(self, timeout=None):
        timeout = self.acquire_timeout() if timeout is None else timeout
        deadline = time.time() + timeout if timeout else None
        with self._cond:
            while True:
                for d in self._devices:
                    if d["inflight"] == 0 and d["credits"] > 0:
                        d["inflight"] = 1
                        return d["id"]
                if len(self._devices) < self.maximum():
                    return self._mint("ondemand")
                if deadline is not None and time.time() >= deadline:
                    raise PoolEmpty("no device available within the acquire timeout")
                self._cond.wait(1.0)

    def release(self, did, used=True):
        with self._cond:
            for d in self._devices:
                if d["id"] == did:
                    d["inflight"] = 0
                    if used:
                        d["credits"] -= 1
                    break
            self._cond.notify_all()

    def snapshot(self):
        with self._lock:
            idle = sum(1 for d in self._devices if not d["inflight"] and d["credits"] > 0)
            inflight = sum(1 for d in self._devices if d["inflight"])
            credits = sum(d["credits"] for d in self._devices)
            return {
                "size": len(self._devices),
                "idle": idle,
                "inflight": inflight,
                "credits": credits,
                "minted": self.minted,
                "target": self.target(),
                "min": self.minimum(),
                "max": self.maximum(),
                "last_error": self.last_error,
                "devices": [
                    {"id": d["id"], "credits": d["credits"], "reason": d["reason"],
                     "state": "in-flight" if d["inflight"] else ("available" if d["credits"] > 0 else "exhausted")}
                    for d in self._devices
                ],
            }
