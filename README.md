# imagenai-2api-app

An OpenAI-style **image-to-video** gateway that wraps the Miricanvas video generation
service (`api.imagenai.online`, Video Generation API v3) behind a single self-hosted HTTP
service — complete with an admin console, API-key auth, and automatic device rotation.

Pure Python standard library. No dependencies, no build step.

## Features

- **Image to video** — submit a source image plus a motion prompt, poll for the finished video.
- **HMAC request signing** — algorithm reverse-engineered from the Android APK's
  `libphotoflux_security.so`; the signing secret is self-contained.
- **Automatic device rotation** — each device fingerprint is limited to a fixed number of
  generations per day. When a fingerprint is exhausted the gateway silently rotates to a
  fresh one. No IP rate limiting (25 concurrent requests from one IP tested clean).
- **Device pool** — a background thread keeps a set of fresh fingerprints pre-warmed so
  requests start instantly. Idle fingerprints are recycled, minting is throttled to stay
  light on CPU, and the pool doubles as a concurrency gate.
- **Per-request proxy** — pass an HTTP/SOCKS proxy per request to rotate egress through a pool.
- **Admin console** — dark-themed web UI: overview, playground, API keys, settings, password.
- **API keys** — OpenAI-style `Authorization: Bearer sk-ig-…` auth on `/v1`, toggleable.
- **Self-contained state** — config, sessions, device ledger, and API keys persist under `DATA_DIR`.

## Quick start

### Docker (recommended)

```bash
docker compose up -d --build
# then open http://localhost:8765/admin
```

Or build the image directly:

```bash
docker build -t imagenai-2api .
docker run -d --name imagenai-2api -p 8765:8765 -v "$PWD/data:/data" imagenai-2api
```

### Local (no Docker)

```bash
python3 main.py          # serves on :8765, data in ./data
```

Open the console at `http://127.0.0.1:8765/admin`.
The default admin password is `admin123` — **change it immediately** from the Password tab.

## Configuration

Configuration is read from environment variables at startup, then persisted (and editable)
from the Settings tab.

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `8765` | HTTP listen port |
| `HOST` | `0.0.0.0` | HTTP bind address |
| `DATA_DIR` | `./data` | Directory for `config.json` and `device_ledger.json` |

The device ledger path (`LEDGER_PATH`) is derived from `DATA_DIR` automatically.

## Admin console

| Page | Path | Notes |
|---|---|---|
| Sign in | `/login` | session cookie `ig_admin` |
| Console | `/admin` | gated; redirects to `/login` when signed out |
| API docs | `/docs` | public by default (toggle in Settings) |

Tabs: **Overview** (request stats, device pool, device ledger), **Playground**
(upload/URL/base64 image → generate → preview), **API Keys**, **Settings**, **Password**.

## Device pool

Fingerprints are cheap and local, so the pool is intentionally simple: it is held in memory
and the ledger is the only thing written to disk (and only when a fingerprint is actually
used). On startup the pool adopts any of today's ledger fingerprints that still have
credits, then tops itself up to the minimum.

| Setting | Default | Purpose |
|---|---|---|
| `pool.target` | `16` | Preferred number of warm fingerprints |
| `pool.min` | `8` | Never drop below this many (adopted/minted at boot) |
| `pool.max` | `40` | Hard ceiling; also the concurrency limit |
| `pool.reg_min_interval` | `3` | Minimum seconds between minting new fingerprints |
| `pool.refill_interval` | `5` | Seconds between background refill ticks |
| `pool.session_ttl` | `1500` | Seconds before an idle fingerprint is recycled |
| `pool.acquire_timeout` | `120` | Seconds to wait for a free fingerprint before failing |
| `devices.daily_limit` | `2` | Generations allowed per fingerprint ("free credits per device") |

The background thread mints at most one fingerprint per tick and only when the pool is below
`target`, so an idle instance costs almost nothing. Press **Refill** in the console (or call
`POST /admin/api/pool/refill`) to top up immediately.

## API

Base URL is your deployment origin. Model: `miricanvas-image-to-video`.

### Submit a generation

```bash
curl http://127.0.0.1:8765/v1/video/generations \
  -H "Authorization: Bearer sk-ig-xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "miricanvas-image-to-video",
    "prompt": "Slow camera push-in, gentle parallax, cinematic lighting",
    "image": "data:image/png;base64,...."
  }'
```

`image` accepts a `data:` URL, raw base64, or an `http(s)` image URL
(also `image_url`, `input_image`, or `images[0]`).

Response (asynchronous task):

```json
{ "id": "0e9488a5-2e36-4141-9d60-4747cbab87f9", "status": "queued", "progress": 0 }
```

### Poll for the result

```bash
curl http://127.0.0.1:8765/v1/video/generations/0e9488a5-2e36-4141-9d60-4747cbab87f9 \
  -H "Authorization: Bearer sk-ig-xxxxxxxx"
```

Once complete the response includes `"video_url"`. Typical generation time is 60–90 seconds.

### Other endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/v1/models` | Model list (public) |
| `GET` | `/v1/pool` | Device-pool snapshot (requires a key) |
| `GET` | `/health` | Service + upstream health |

When `require_api_key` is enabled, all `/v1` routes require a valid `sk-ig-…` key.

## Use as a library

```python
from miricanvas_client import generate, status, new_device

r = generate(image_b64, "prompt here", device_id=new_device("script"))
s = status(r["request_id"])   # {"status": "completed", "video_url": "..."}
```

`miricanvas_client` also works as a standalone CLI for quick testing.

## Deploying on Coolify

1. Create a new **Application** → *Dockerfile* build pack, pointing at this repo.
2. Expose port **8765**.
3. Add a **persistent volume** mounted at `/data` and set `DATA_DIR=/data`
   (so config, API keys, sessions, and the device ledger survive redeploys).
4. Optionally set `PORT` / `HOST`; otherwise the defaults apply.
5. Deploy, then open `/admin` and change the default password.

## Benchmarks (measured 2026-10-27)

| Item | Result |
|---|---|
| 25 devices concurrent, same IP | 25/25 accepted (no IP rate limit) |
| 30 tasks with automatic device rotation | 30/30 succeeded |
| Per-device daily limit | 2 (429 → auto-rotate to a new device) |
| Generation time | ~60–90 s per clip |
| Upstream capacity | queue 200 · 24 workers |

## Legacy: `demo_server.py`

The original single-file prototype is kept as `demo_server.py` for reference. It exposes the
same `/v1` routes without auth, admin console, or persistence:

```bash
python3 demo_server.py
```

For anything real, use `main.py` (the packaged app in `app/`).

## License

MIT
