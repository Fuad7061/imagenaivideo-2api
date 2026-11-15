"""Device pool: keeps a warm set of fresh device fingerprints ready.

imagenai rotates *device fingerprints* (each limited to a fixed number of
generations per day) rather than logging in to real accounts, so this pool is
intentionally light: fingerprints are minted locally and persisted to the
on-disk ledger immediately.

The **ledger is the single source of truth**: the pool mirrors today's ledger
entries, so ``pool.size`` always equals the number of devices in today's
ledger. Credits/remaining are always read back from the ledger (never tracked
independently), which keeps the dashboard's pool and ledger views in sync by
construction. State lives in memory and a single daemon thread wakes on a slow
tick to refill the pool, keeping CPU and disk use low.
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
        self._devices = []  # [{id, inflight, reason, created}] — mirrors today's ledger
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

    # ---- ledger mirroring (call with the lock held) ----
    def _states(self):
        """Read today's ledger as ``{id: {remaining, used, reason}}``.

        Returns ``None`` if the ledger cannot be read so callers never mistake a
        transient error for "every device vanished".
        """
        try:
            summary = self._upstream.ledger_summary()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return None
        out = {}
        for d in summary.get("devices", []):
            out[d["id"]] = {
                "remaining": int(d.get("remaining", 0)),
                "used": int(d.get("used", 0)),
                "reason": d.get("reason", ""),
            }
        return out

    def _sync(self, now):
        """Align the pool with today's ledger. Returns the state map (or None).

        - adopt every device present in today's ledger (including exhausted ones)
        - drop pool devices that are no longer in the ledger
        - recycle stale idle fingerprints beyond the minimum pool size
        """
        states = self._states()
        if states is None:
            return None
        existing = {d["id"]: d for d in self._devices}
        keep = []
        for did, st in states.items():
            d = existing.get(did)
            if d is None:
                d = {"id": did, "inflight": 0,
                     "reason": st.get("reason") or "adopted", "created": now}
            keep.append(d)
        # TTL-prune idle, still-usable fingerprints once we exceed the minimum.
        ttl = self.session_ttl()
        if ttl:
            for d in list(keep):
                if len(keep) <= self.minimum():
                    break
                if d["inflight"]:
                    continue
                if states.get(d["id"], {}).get("remaining", 0) <= 0:
                    continue  # keep exhausted devices for ledger history
                if (now - d["created"]) > ttl:
                    keep.remove(d)
                    states.pop(d["id"], None)
                    try:
                        self._upstream.forget_device(d["id"])
                    except Exception as e:  # noqa: BLE001
                        self.last_error = str(e)
        self._devices = keep
        return states

    def _available(self, states):
        n = 0
        for d in self._devices:
            if d["inflight"]:
                continue
            if states.get(d["id"], {}).get("remaining", 0) > 0:
                n += 1
        return n

    def _mint(self, reason="pool"):
        did = self._upstream.mint_device()
        try:
            self._upstream.record_device(did, reason)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
        now = time.time()
        self._devices.append({"id": did, "inflight": 0, "reason": reason, "created": now})
        self._last_mint = now
        self.minted += 1
        return did

    def _mark_inflight(self, did):
        for d in self._devices:
            if d["id"] == did:
                d["inflight"] = 1
                return

    def _seed(self):
        with self._cond:
            now = time.time()
            states = self._sync(now)
            if states is None:
                while len(self._devices) < self.minimum():
                    self._mint("seed")
            else:
                while self._available(states) < self.minimum():
                    did = self._mint("seed")
                    states[did] = {"remaining": self._limit(), "used": 0, "reason": "seed"}
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
            states = self._sync(now)
            can_mint = (len(self._devices) < self.maximum()
                        and now - self._last_mint >= self.reg_interval())
            if can_mint:
                if states is None:
                    if len(self._devices) < self.target():
                        self._mint("refill")
                elif self._available(states) < self.target():
                    did = self._mint("refill")
                    states[did] = {"remaining": self._limit(), "used": 0, "reason": "refill"}
            self._cond.notify_all()
            return self.snapshot()

    def add(self, reason="manual"):
        """Mint and register a device on demand (persists to the ledger)."""
        with self._cond:
            did = self._mint(reason)
            self._cond.notify_all()
            return did

    # ---- scheduling ----
    def acquire(self, timeout=None):
        timeout = self.acquire_timeout() if timeout is None else timeout
        deadline = time.time() + timeout if timeout else None
        with self._cond:
            while True:
                now = time.time()
                states = self._sync(now)
                if states is not None:
                    for d in self._devices:
                        if d["inflight"] == 0 and states.get(d["id"], {}).get("remaining", 0) > 0:
                            d["inflight"] = 1
                            return d["id"]
                # no idle device → mint a fresh one if we have room
                if len(self._devices) < self.maximum():
                    did = self._mint("ondemand")
                    if states is not None:
                        states[did] = {"remaining": self._limit(), "used": 0, "reason": "ondemand"}
                    self._mark_inflight(did)
                    return did
                if deadline is not None and time.time() >= deadline:
                    raise PoolEmpty("no device available within the acquire timeout")
                self._cond.wait(1.0)

    def release(self, did, used=True):
        """Release an in-flight device. Usage is already recorded in the ledger."""
        with self._cond:
            for d in self._devices:
                if d["id"] == did:
                    d["inflight"] = 0
                    break
            self._cond.notify_all()

    def snapshot(self):
        with self._lock:
            now = time.time()
            states = self._sync(now)
            devs = []
            idle = inflight = exhausted = credits = 0
            for d in self._devices:
                if states is None:
                    remaining = 0 if d["inflight"] else self._limit()
                    state = "in-flight" if d["inflight"] else "unknown"
                else:
                    remaining = int(states.get(d["id"], {}).get("remaining", 0))
                    if d["inflight"]:
                        state = "in-flight"
                    elif remaining > 0:
                        state = "available"
                    else:
                        state = "exhausted"
                if state == "in-flight":
                    inflight += 1
                elif state == "exhausted":
                    exhausted += 1
                elif state == "available":
                    idle += 1
                credits += remaining
                devs.append({"id": d["id"], "credits": remaining,
                             "reason": d["reason"], "state": state})
            return {
                "size": len(self._devices),
                "idle": idle,
                "inflight": inflight,
                "exhausted": exhausted,
                "credits": credits,
                "minted": self.minted,
                "target": self.target(),
                "min": self.minimum(),
                "max": self.maximum(),
                "last_error": self.last_error,
                "devices": devs,
            }
