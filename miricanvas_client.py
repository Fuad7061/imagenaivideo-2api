# -*- coding: utf-8 -*-
"""
Miricanvas 视频生成客户端 — 自动设备轮换 + 代理池
- device_id 自动生成 + 本地配额记账（每设备日限 2，用完自动换新）
- 支持 HTTP/SOCKS 代理（每请求可指定，对应网关代理池 Acquire）
- HMAC 签名算法逆向自 libphotoflux_security.so
"""
import hmac, hashlib, json, time, secrets, uuid, os, ssl, urllib.request, urllib.error, threading

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
BASE = "https://api.imagenai.online"
SECRET = "5HvEs9YZ9x7a6zMdGkjzoNettYxF4BUmyyyTHFl1YBU"
DAILY_LIMIT = 2
LEDGER = os.environ.get("LEDGER_PATH") or os.path.join(os.path.dirname(__file__), "device_ledger.json")
_lock = threading.Lock()


def _load_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_ledger(d):
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(d, f)


def _today(): return time.strftime("%Y-%m-%d")


def _prune(d, today=None):
    """账本按天滚动：只保留今天创建的设备（在调用方持锁时使用）。"""
    today = today or _today()
    for did in list(d.keys()):
        if d[did].get("created") != today:
            del d[did]
    return d


def mint_device():
    """生成设备指纹但不写账本（纯内存，零 IO）。"""
    return f"android-{uuid.uuid4().hex[:16]}"


def record_device(did, reason="pool"):
    """把一个已铸造的指纹记入今天的账本（used=0）。设备池用它在预热时落账。"""
    with _lock:
        d = _load_ledger(); today = _today()
        _prune(d, today)
        d[did] = {"created": today, "used": 0, "reason": reason}
        _save_ledger(d)
    return did


def exhaust_device(did):
    """把一个设备标记为当日配额用尽（供 429 后同步账本）。"""
    with _lock:
        d = _load_ledger(); today = _today()
        _prune(d, today)
        info = d.setdefault(did, {"created": today, "used": 0, "reason": "pool"})
        info["used"] = DAILY_LIMIT
        info["last_used"] = today
        _save_ledger(d)
    return did


def forget_device(did):
    """从账本中彻底删除一个设备（回收过期指纹）。"""
    with _lock:
        d = _load_ledger()
        if d.pop(did, None) is not None:
            _save_ledger(d)
    return did


def new_device(reason="fresh"):
    """生成新设备指纹并立即记入本地账本。网关侧可把 device_id 绑定到代理出口 IP。"""
    return record_device(mint_device(), reason)


def pick_device():
    """优先取今天还没用完配额的设备；没有就造新的。"""
    with _lock:
        d = _load_ledger(); today = _today()
        _prune(d, today)
        for did, info in d.items():
            if info.get("used", 0) < DAILY_LIMIT and info.get("created") == today:
                return did
        did = f"android-{uuid.uuid4().hex[:16]}"
        d[did] = {"created": today, "used": 0, "reason": "rotated"}
        _save_ledger(d)
        return did


def _mark_used(did):
    with _lock:
        d = _load_ledger(); today = _today()
        _prune(d, today)
        info = d.setdefault(did, {"created": today, "used": 0})
        info["used"] = info.get("used", 0) + 1
        info["last_used"] = today
        _save_ledger(d)


def _sign(body: bytes):
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    bh = hashlib.sha256(body).hexdigest()
    sig = hmac.new(SECRET.encode(), f"{ts}:{nonce}:{bh}".encode(), hashlib.sha256).hexdigest()
    return {"X-API-Secret": SECRET, "X-Signature": sig, "X-Timestamp": ts,
            "X-Nonce": nonce, "Content-Type": "application/json", "User-Agent": "UnlimAI/1.0"}


def _http(url, body=None, headers=None, proxy=None, timeout=60):
    h = {"User-Agent": "UnlimAI/1.0"}
    if headers: h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    if proxy:
        ph = proxy if "://" in proxy else "http://" + proxy
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": ph, "https": ph}), urllib.request.HTTPSHandler(context=CTX))
    else:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))
    t = time.time()
    try:
        r = opener.open(req, timeout=timeout)
        return r.status, r.read(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.read(), time.time() - t
    except Exception as e:
        return -1, str(e).encode(), time.time() - t


def health(proxy=None):
    st, body, _ = _http(f"{BASE}/api/health", proxy=proxy, timeout=20)
    return json.loads(body) if st == 200 else {"error": st}


def generate(image_b64: str, prompt: str, proxy=None, device_id=None, is_premium=False):
    """提交图生视频。自动换设备直到成功（最多 5 次）。proxy 可传网关代理池取出的代理。

    - 未指定 device_id：每次用全新独立设备（设备零成本），429 时自动换新。
    - 指定 device_id（设备池托管）：429 时把该设备标记为当日用尽后立即返回，
      由上层（网关）决定是否换用池中其它设备重试，避免产生游离设备。
    """
    last = None
    explicit = device_id is not None
    for _ in range(5):
        did = device_id or new_device("auto")
        body = json.dumps({"image_base64": image_b64, "prompt": prompt,
                           "device_id": did, "is_premium": is_premium, "cpm_tier": 0},
                          separators=(",", ":")).encode()
        st, resp, el = _http(f"{BASE}/api/generate", body, _sign(body), proxy=proxy)
        if st == 202:
            d = json.loads(resp)
            _mark_used(did)
            return {"ok": True, "request_id": d["request_id"], "device": did,
                    "remaining": DAILY_LIMIT - (d.get("generations_used", 0) + 1)}
        last = {"ok": False, "http": st, "resp": resp.decode("utf-8", "replace")[:200]}
        if st == 429:  # 该设备日限到顶 → 同步账本后换设备
            exhaust_device(did)
            if explicit:
                break
            device_id = None
            continue
        break
    return last


def status(request_id: str, proxy=None):
    st, body, _ = _http(f"{BASE}/api/status/{request_id}", headers=_sign(b""), proxy=proxy, timeout=30)
    return json.loads(body) if st == 200 else {"status": f"http_{st}", "progress": 0}


if __name__ == "__main__":
    import sys
    print(json.dumps(health(), ensure_ascii=False)[:300])
    PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    r = generate(PNG_1PX, "smoke test from gateway client")
    print(json.dumps(r, ensure_ascii=False))
    if r.get("ok"):
        rid = r["request_id"]
        for _ in range(20):
            time.sleep(8)
            s = status(rid)
            print(s.get("status"), s.get("progress"))
            if s.get("status") in ("completed", "failed"):
                print(s.get("video_url") or s.get("error")); break
