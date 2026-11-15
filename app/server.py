"""HTTP server: routing, auth, admin API, static pages.

Built on the standard library ThreadingHTTPServer with HTTP/1.1 keep-alive
and a thread pool. Data (config, device ledger) lives under ``DATA_DIR``.
"""
import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

# Resolve the persistent data directory *before* importing the upstream client,
# because ``miricanvas_client`` reads its ledger path from the environment at
# import time.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("LEDGER_PATH", os.path.join(DATA_DIR, "device_ledger.json"))

from . import __version__  # noqa: E402
from .config import Config  # noqa: E402
from .security import (  # noqa: E402
    SessionStore,
    api_key_matches,
    generate_api_key,
    hash_password,
    verify_password,
)
from . import upstream  # noqa: E402
from .upstream import UpstreamError  # noqa: E402
from .pool import DevicePool, PoolEmpty  # noqa: E402

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 16 * 1024 * 1024  # base64 images can be large
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}

MODEL = "miricanvas-image-to-video"
TERMINAL = {"completed", "failed", "error", "success"}
DATA_URL_RE = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", re.S)


class RequestError(ValueError):
    """Invalid client input (maps to HTTP 400)."""


def _fetch_b64(url, timeout=30):
    """Download an image URL and return its base64 encoding."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    return base64.b64encode(raw).decode("ascii")


def extract_b64(value):
    """Normalise the many accepted image input shapes into raw base64."""
    if isinstance(value, dict):
        value = value.get("url") or value.get("image_url") or value.get("b64_json")
    if not isinstance(value, str) or not value.strip():
        raise RequestError("image is required")
    value = value.strip()
    m = DATA_URL_RE.match(value)
    if m:
        return m.group(1)
    if value.startswith(("http://", "https://")):
        return _fetch_b64(value)
    return value


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.start = time.time()
        self.requests = 0
        self.success = 0
        self.errors = 0
        self.tasks_completed = 0

    def inc(self, field, n=1):
        with self._lock:
            setattr(self, field, getattr(self, field, 0) + n)

    def snapshot(self):
        with self._lock:
            return {
                "requests": self.requests,
                "success": self.success,
                "errors": self.errors,
                "tasks_completed": self.tasks_completed,
                "uptime_s": round(time.time() - self.start, 1),
            }


class App:
    def __init__(self):
        self.data_dir = DATA_DIR
        self.config = Config(os.path.join(self.data_dir, "config.json"))
        self.config.ensure_admin_password()
        upstream.configure(self.config)
        self.sessions = SessionStore(ttl=self.config.get("session_ttl", default=86400))
        self.stats = Stats()
        self.tasks = {}
        self.tasks_lock = threading.Lock()
        self.pool = DevicePool(self.config, upstream)
        self.pool.start()
        self.static = self._load_static()

    def _load_static(self):
        out = {}
        for f in WEB_DIR.glob("*"):
            if f.suffix in STATIC_TYPES:
                out[f.name] = (STATIC_TYPES[f.suffix], f.read_bytes())
        return out

    # ---- tasks ----
    def submit_task(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise RequestError("prompt is required")
        image = body.get("image")
        if image is None:
            image = body.get("image_url") or body.get("input_image")
        if image is None and isinstance(body.get("images"), list) and body["images"]:
            image = body["images"][0]
        image_b64 = extract_b64(image)

        self.stats.inc("requests")
        try:
            device_id = self.pool.acquire()
        except PoolEmpty as e:
            self.stats.inc("errors")
            raise UpstreamError(str(e))
        try:
            result = upstream.submit(image_b64, prompt, device_id=device_id)
        except Exception:
            self.pool.release(device_id, used=False)
            self.stats.inc("errors")
            raise
        self.pool.release(device_id, used=True)
        tid = os.urandom(16).hex()
        task = {
            "id": tid,
            "status": "queued",
            "progress": 0,
            "request_id": result.get("request_id"),
            "device": result.get("device"),
            "video_url": None,
            "error": None,
            "created": int(time.time()),
            "prompt": prompt,
            "counted": False,
        }
        with self.tasks_lock:
            self.tasks[tid] = task
        return self._view(task)

    def task_view(self, tid):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if not task:
                return None
            snapshot = dict(task)
        if snapshot["status"] not in TERMINAL and snapshot.get("request_id"):
            self._refresh(tid)
            with self.tasks_lock:
                task = self.tasks.get(tid)
                if not task:
                    return None
                snapshot = dict(task)
        return self._view(snapshot)

    def _refresh(self, tid):
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if not task:
                return
            request_id = task.get("request_id")
        if not request_id:
            return
        try:
            st = upstream.poll(request_id)
        except Exception as e:  # noqa: BLE001
            st = {"status": "error", "error": str(e)}
        status = st.get("status") or "processing"
        with self.tasks_lock:
            task = self.tasks.get(tid)
            if not task:
                return
            task["status"] = status
            task["progress"] = st.get("progress", task.get("progress", 0))
            if st.get("video_url"):
                task["video_url"] = st["video_url"]
            if st.get("error"):
                task["error"] = st["error"]
            if status in TERMINAL and not task.get("counted"):
                task["counted"] = True
                if status in ("completed", "success"):
                    self.stats.inc("success")
                    self.stats.inc("tasks_completed")
                else:
                    self.stats.inc("errors")

    @staticmethod
    def _view(task):
        return {
            "id": task["id"],
            "status": task["status"],
            "progress": task.get("progress", 0),
            "video_url": task.get("video_url"),
            "error": task.get("error"),
            "created": task.get("created"),
        }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "imagenai-2api/" + __version__
    app = None  # injected by run()

    # ---- infrastructure ----
    def log_message(self, fmt, *args):
        pass  # silent to avoid log spam from frequent polling

    def log_error(self, fmt, *args):
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None, raw=False):
        if not raw and isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except Exception:
                pass

    def _json(self, code, obj, headers=None):
        self._send(code, obj, "application/json; charset=utf-8", headers)

    def _redirect(self, location):
        self._send(302, b"", "text/plain", {"Location": location})

    def _static(self, name):
        item = self.app.static.get(name)
        if not item:
            self._json(404, {"error": "not found"})
            return
        ctype, data = item
        self._send(200, data, ctype, raw=True)

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _read_raw(self):
        """Read the request body exactly once so HTTP/1.1 keep-alive stays in sync."""
        if getattr(self, "_raw", None) is not None:
            return self._raw
        self._raw = b""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if 0 < length <= MAX_BODY:
            self._raw = self.rfile.read(length)
        return self._raw

    def _body(self):
        raw = self._read_raw()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}

    # ---- auth ----
    def _is_admin(self):
        return self.app.sessions.valid(self._cookies().get(SessionStore.COOKIE))

    def _presented_key(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return self.headers.get("x-api-key", "").strip()

    def _api_ok(self):
        if not self.app.config.get("require_api_key", default=True):
            return True
        return api_key_matches(self._presented_key(), self.app.config.get("api_keys", default=[]))

    # ---- routing ----
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain", {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "Authorization, Content-Type, x-api-key", "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})

    def _dispatch(self, method):
        self._raw = None
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        self._read_raw()
        try:
            if path.startswith("/admin/api"):
                return self._admin_api(method, path, query)
            if path.startswith("/v1"):
                return self._v1(method, path, query)
            return self._pages(method, path)
        except RequestError as e:
            return self._json(400, {"error": {"message": str(e), "type": "invalid_request_error"}})
        except UpstreamError as e:
            return self._json(502, {"error": {"message": str(e), "type": "upstream_error"}})
        except Exception as e:  # noqa: BLE001
            self.log_error("dispatch error: %r", e)
            return self._json(500, {"error": {"message": "internal error", "type": "server_error"}})

    # ---- pages ----
    def _pages(self, method, path):
        if method != "GET":
            return self._json(405, {"error": "method not allowed"})
        if path in ("/", "/index.html", "/admin"):
            if not self._is_admin():
                return self._redirect("/login")
            return self._static("index.html")
        if path == "/login":
            if self._is_admin():
                return self._redirect("/")
            return self._static("login.html")
        if path == "/docs":
            if not self.app.config.get("docs_public", default=True) and not self._is_admin():
                return self._redirect("/login")
            return self._static("docs.html")
        if path == "/logout":
            self.app.sessions.destroy(self._cookies().get(SessionStore.COOKIE))
            return self._redirect("/login")
        if path == "/health":
            snap = self.app.stats.snapshot()
            return self._json(200, {"status": "ok", **snap, "devices": upstream.ledger_summary(), "pool": self.app.pool.snapshot(), "version": __version__})
        if path.startswith("/assets/"):
            return self._static(path.rsplit("/", 1)[-1])
        if path == "/favicon.ico":
            return self._send(204, b"", "text/plain")
        return self._json(404, {"error": "not found"})

    # ---- OpenAI-compatible API ----
    def _v1(self, method, path, query):
        cors = {"Access-Control-Allow-Origin": "*"}
        if path == "/v1/models" and method == "GET":
            return self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "imagenai-2api"}]}, cors)
        if not self._api_ok():
            return self._json(401, {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}, cors)
        if path == "/v1/pool" and method == "GET":
            return self._json(200, self.app.pool.snapshot(), cors)
        if path == "/v1/video/generations" and method == "POST":
            try:
                return self._json(200, self.app.submit_task(self._body()), cors)
            except RequestError as e:
                return self._json(400, {"error": {"message": str(e), "type": "invalid_request_error"}}, cors)
            except UpstreamError as e:
                return self._json(502, {"error": {"message": str(e), "type": "upstream_error"}}, cors)
        if path.startswith("/v1/video/generations/") and method == "GET":
            tid = path.rsplit("/", 1)[-1]
            view = self.app.task_view(tid)
            if not view:
                return self._json(404, {"error": {"message": "task not found", "type": "invalid_request_error"}}, cors)
            return self._json(200, view, cors)
        return self._json(404, {"error": {"message": "unknown endpoint", "type": "invalid_request_error"}}, cors)

    # ---- admin API ----
    def _admin_api(self, method, path, query):
        sub = path[len("/admin/api"):] or "/"
        # No login required
        if sub == "/login" and method == "POST":
            return self._login()
        if sub == "/me" and method == "GET":
            return self._json(200, {"authenticated": self._is_admin(), "must_change_password": self.app.config.get("must_change_password", default=False)})
        if sub == "/logout" and method == "POST":
            self.app.sessions.destroy(self._cookies().get(SessionStore.COOKIE))
            return self._json(200, {"ok": True}, {"Set-Cookie": f"{SessionStore.COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})
        # Login required
        if not self._is_admin():
            return self._json(401, {"error": "unauthorized"})
        if sub == "/config" and method == "GET":
            return self._json(200, {"config": self.app.config.snapshot(), "stats": self.app.stats.snapshot(), "devices": upstream.ledger_summary(), "pool": self.app.pool.snapshot(), "version": __version__})
        if sub == "/config" and method == "PUT":
            applied, errors = self.app.config.apply_settings(self._body())
            upstream.configure(self.app.config)
            return self._json(200, {"ok": not errors, "applied": applied, "errors": errors})
        if sub == "/generate" and method == "POST":
            try:
                return self._json(200, self.app.submit_task(self._body()))
            except RequestError as e:
                return self._json(400, {"error": str(e)})
            except UpstreamError as e:
                return self._json(502, {"error": str(e)})
        if sub == "/task" and method == "GET":
            tid = (query.get("id") or [""])[0]
            view = self.app.task_view(tid)
            if not view:
                return self._json(404, {"error": "task not found"})
            return self._json(200, view)
        if sub == "/password" and method == "POST":
            return self._change_password()
        if sub == "/keys" and method == "GET":
            keys = self.app.config.get("api_keys", default=[])
            return self._json(200, {"keys": keys, "require_api_key": self.app.config.get("require_api_key", default=True)})
        if sub == "/keys" and method == "POST":
            return self._create_key()
        if sub == "/keys/update" and method == "POST":
            return self._update_key()
        if sub == "/keys/delete" and method == "POST":
            return self._delete_key()
        if sub == "/pool" and method == "GET":
            return self._json(200, self.app.pool.snapshot())
        if sub == "/pool/refill" and method == "POST":
            return self._json(200, {"ok": True, "pool": self.app.pool.refill()})
        if sub == "/devices" and method == "GET":
            return self._json(200, upstream.ledger_summary())
        if sub == "/devices/new" and method == "POST":
            try:
                did = upstream.new_device("manual")
                return self._json(200, {"ok": True, "device": did, "devices": upstream.ledger_summary()})
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
        if sub == "/stats" and method == "GET":
            return self._json(200, {"stats": self.app.stats.snapshot(), "devices": upstream.ledger_summary(), "pool": self.app.pool.snapshot()})
        return self._json(404, {"error": "not found"})

    def _login(self):
        body = self._body()
        password = body.get("password", "")
        stored = self.app.config.get("admin_password_hash")
        if not verify_password(password, stored):
            time.sleep(0.6)  # slow down brute-force attempts
            return self._json(401, {"error": "Incorrect password"})
        token = self.app.sessions.create()
        cookie = f"{SessionStore.COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={self.app.config.get('session_ttl', default=86400)}"
        return self._json(200, {"ok": True, "must_change_password": self.app.config.get("must_change_password", default=False)}, {"Set-Cookie": cookie})

    def _change_password(self):
        body = self._body()
        current = body.get("current", "")
        new = body.get("new", "")
        if not verify_password(current, self.app.config.get("admin_password_hash")):
            return self._json(400, {"error": "Current password is incorrect"})
        if len(new) < 6:
            return self._json(400, {"error": "New password must be at least 6 characters"})
        self.app.config.set("admin_password_hash", value=hash_password(new))
        self.app.config.set("must_change_password", value=False)
        self.app.config.save()
        self.app.sessions.destroy_all()
        return self._json(200, {"ok": True})

    def _create_key(self):
        body = self._body()
        name = (body.get("name") or "default").strip()[:40]
        key = {"key": generate_api_key(), "name": name, "enabled": True, "created": int(time.time())}
        keys = self.app.config.get("api_keys", default=[])
        keys.append(key)
        self.app.config.set("api_keys", value=keys)
        self.app.config.save()
        return self._json(200, {"ok": True, "key": key})

    def _update_key(self):
        body = self._body()
        target = body.get("key", "")
        enabled = bool(body.get("enabled", True))
        keys = self.app.config.get("api_keys", default=[])
        found = False
        for k in keys:
            if k.get("key") == target:
                k["enabled"] = enabled
                found = True
        if found:
            self.app.config.set("api_keys", value=keys)
            self.app.config.save()
        return self._json(200 if found else 404, {"ok": found})

    def _delete_key(self):
        body = self._body()
        target = body.get("key", "")
        keys = self.app.config.get("api_keys", default=[])
        remaining = [k for k in keys if k.get("key") != target]
        self.app.config.set("api_keys", value=remaining)
        self.app.config.save()
        return self._json(200, {"ok": len(remaining) != len(keys)})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def run():
    app = App()
    Handler.app = app

    port = int(os.environ.get("PORT") or app.config.get("server", "port") or 8765)
    host = os.environ.get("HOST") or app.config.get("server", "host") or "0.0.0.0"

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    httpd = Server((host, port), Handler)
    print(f"imagenai-2api gateway v{__version__}")
    print(f"  listening:  http://{host}:{port}")
    print(f"  console:    http://{host}:{port}/admin   (default password admin123)")
    print(f"  docs:       http://{host}:{port}/docs")
    print(f"  data dir:   {app.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down…")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
