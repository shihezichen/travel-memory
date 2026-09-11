<div align="center">

# 🧭 Travel Memory

**Turn a trip into a memory you can revisit — maps, photos, routes, notes, plus a memory API open to AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-34%20tools-7c3aed)

**English** · [中文](README.zh-CN.md)

</div>

---

## What is this

Travel Memory is a self-hosted, multi-user travel memory system with two faces:

- **For humans**: a mobile-first web app. Pin every stop of a trip on a map, attach photos and videos, write down what you were thinking. Later, replay the whole route, browse the trip's album, or hand a friend a read-only share link.
- **For agents**: an **MCP (Model Context Protocol) Streamable HTTP** endpoint exposing 34 tools. Any remote agent connects with its own API key, fully tenant-isolated. Let an AI read your travel memories, find patterns, write an annual report, or answer "when was I last in Kyoto?"

The stack is deliberately boring: **FastAPI + SQLite + vanilla JavaScript**. No frontend build step, no ORM, no Redis. Clone it, install a handful of Python packages, run it.

## Features

**Trips & map**

- Multiple trips, each an ordered list of points; four statuses (planning / traveling / completed / archived)
- Real driving routes (AMap Driving, OSRM fallback), per-segment clickable with alternate routes, results cached
- Distance labelled at the midpoint of every segment; selecting a point highlights the inbound leg (green) and outbound leg (orange)
- Elevation shown next to each point (OpenTopoData / SRTM 90m, fetched once then cached)
- Layers: administrative boundaries / scenic POIs / satellite imagery / live traffic / photos & videos / footprint heatmap
- Route replay animation along the real route, with speed control
- Map fullscreen, one-tap fit-whole-trip
- My location: blue dot with accuracy ring, one tap to recentre on yourself (uses AMap geolocation so the coordinate system matches the tiles)

**Photos & video**

- Upload photos/videos, automatic thumbnails, EXIF time and coordinates extracted
- Files over 8 MB switch to chunked upload: parallel chunks, exponential-backoff retries, an
  aggregate progress ring. An upload in flight can be **paused, resumed or cancelled**, and you
  can play the local copy of the file while it is still going up. If the connection drops, the
  point keeps an "unfinished upload" placeholder — **re-pick the same file** and it continues
  from where it stopped (browsers do not let a page persist local file handles, so fully
  automatic resume is impossible). Sessions idle for 24 h are reclaimed together with their
  temp chunks (`scripts/prune_upload_sessions.py`)
- **Video quality ladder**: after upload a background worker remuxes the original for faststart
  and transcodes 1080p / 720p / 480p rungs by short edge (never upscaling). The player picks a
  rung from network + screen + DPR, you can switch manually and the choice is remembered; rungs
  still encoding are greyed out as "transcoding" and appear as soon as they are ready
- Photos cluster into "stacks" at each point on the map, scaling with zoom level; the 📷 badge
  next to a point carries a media count that updates the moment you add or delete something
- Album: a flat whole-trip grid (iOS Photos style), pinch to change columns, filter by type, sort by time
- Point detail: a WeChat-moments-style grid — long-press to drag-reorder, edit a caption right in the cell
- Save to device: a download button in the lightbox corner — photos download the original,
  videos let you pick a quality. Filenames are built from date/place/index
  (`旅行记忆-20260910-京都-01.mp4`). Anonymous share-page visitors can download too,
  with exactly the same permissions as viewing. WeChat's in-app browser blocks downloads,
  so there it degrades to a "long-press to save" hint rather than pretending
- Lightbox: finger-tracking carousel, tap for fullscreen, pinch zoom and pan, drag past the edge
  to page (Android gallery behaviour). Controls (close / download / quality / paging) fade out
  after a few idle seconds and come back on the next tap
- Every photo can carry a caption **with Markdown support** (bold/italic, lists, quotes, links, inline code)

**Sharing**

- Read-only share links with optional expiry and password, revocable at any time
- The share page mirrors the main UI (layers, fullscreen, replay, album, lightbox) but deliberately **excludes cross-trip data** — the footprint heatmap and the global photo layer never appear on a public page
- Generates a portrait share card with a QR code; on mobile it saves straight to the photo library

**AI & open platform**

- **34 MCP tools** covering trip CRUD, memory generation, pattern discovery, annual reports, time
  travel, memory recall and semantic search. The catalogue, the argument schemas, the scope
  grading and **whether each individual tool actually runs** are all guarded by automated tests
  (`./test.sh -k mcp`) — break one and it goes red immediately
- API key management (scopes: read / write / share / admin, with expiry and revocation) built into the developer console
- Pluggable LLM provider: Claude CLI / Anthropic API / DeepSeek (`TRAVEL_LLM_PROVIDER`)

**Mobile polish**

More effort went into mobile interaction than into features: draggable panel height, card-carousel tab swiping, directional trip-switch animation, gesture arbitration (long-press drag vs. tab swipe vs. pinch zoom), back-navigation interception (edge swipe won't drop you out of the site), a collapsible top bar that hands its height back to the map, and icon legibility under dark mode and satellite basemaps.

## Quick start

All you need is **Python 3.10+**. Two commands:

```bash
git clone https://github.com/shihezichen/travel-memory.git
cd travel-memory

./setup.sh      # venv, dependencies, database, and a set of demo data
./start.sh      # run (development only — see "Running it" below)
```

Open `http://localhost:8084`. `data.db` is created and migrated automatically — no schema
setup, no config files to edit.

`setup.sh` also seeds a demo dataset (3 trips, 12 points, 28 synthetic photos, 1 video,
24 Markdown captions) under the account **`test` / `test1234`**, with a one-tap login button
on the sign-in page — so you can see what the thing looks like without recording a trip
first. Skip it with `./setup.sh --no-demo`; add it later (or reset it to factory state)
with `./setup.sh --demo`.

**The one thing you must configure by hand: an AMap key.** It starts fine without one —
trips, photos and sharing all work — the map area is just blank, and a banner at the top
of the page tells you where to put the key. Once you have it:

```bash
echo 'your-key'            > amap_key.txt
echo 'your-security-code'  > amap_security.txt
# restart to apply
```

> Optional: `ffmpeg`. Needed to extract poster frames and to build the video quality ladder;
> photos work regardless. `sudo apt install ffmpeg` / `brew install ffmpeg`.

### Optional environment variables

None are required. Add them as you need them:

| Variable | Purpose | Default |
|---|---|---|
| `AMAP_APPNAME` | Whitelist appname for your AMap key; only needed for the administrative-boundary layer, must match what you set in the AMap console | `http://localhost:8084/` |
| `TRAVEL_BASE_URL` | Where the MCP endpoint calls its own domain API. **Must match the scheme/port the process actually listens on** (see "Running it") | `http://127.0.0.1:8084` |
| `TRAVEL_LLM_PROVIDER` | Which model backs the AI features: `auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | Key for the chosen provider; unnecessary if you don't use AI features | empty |
| `TRAVEL_MEDIA_BASE_URL` | Origin that serves media (object storage / CDN). Empty = the app serves `/uploads/` itself | empty (or put it in `media_base.txt`) |
| `TRAVEL_OSS_DIRECT` | Master switch for browser-to-object-storage direct upload. `0` = off, fully back to relaying through the server | `1` (auto-disabled when the config is incomplete) |
| `TRAVEL_OSS_AK` / `TRAVEL_OSS_SK` | AK/SK used to sign direct uploads. Unset = read `~/.ossutilconfig` | empty |
| `TRAVEL_OSS_PART_SIZE` | Direct-upload part size, in bytes | `8388608` (8 MB) |
| `TRAVEL_OSS_BACKFILL_WORKERS` | Concurrency of pulling originals back for archival. Deliberately 1 — do not race someone who is watching a video | `1` |
| `DEMO_USER` | Account name the daily reset targets (see below) | `test` |

### Daily demo-account reset (optional)

A public demo account gets edited by everyone; after a day the data is a mess. The repo
ships a pair of systemd user units that restore it to factory state at 03:00 daily —
nothing from that day is kept:

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

Run it once by hand: `./.venv/bin/python scripts/reset_demo.py`. Don't install the timer if
you don't want a demo account.

## About the AMap key

The map uses AMap JS API 2.0. Get a **Web (JS API)** key from the [AMap console](https://console.amap.com/dev/key/app) along with its paired **securityJsCode**.

Both are delivered to the browser (that's how JS API 2.0 works), so:

> ⚠️ **Configure a domain allowlist for your key in the AMap console**, or anyone can lift it and burn your quota.

Elevation comes from the public [OpenTopoData](https://www.opentopodata.org/) instance (SRTM 90m), rate-limited to 1000 calls/day. Points are few and results are cached, so you won't hit it in normal use; self-host or swap the source if you bulk-import trips.

## Running it

`./start.sh` is only good for a first look: plain HTTP, dies with your terminal.
**Set up the real thing from your development environment onwards** — not because it is tidier,
but because these three things bite during development, not after launch:

1. **Without TLS, half the app is running in a degraded mode.** Browsers gate the clipboard API,
   geolocation and `crypto.subtle` behind a secure context (https or localhost). Debug on a
   phone over a LAN IP and copying a share link, "locate me" and the upload fingerprint all take
   the fallback branch. You test the fallback; your users get the real path; the difference only
   shows up in production.
2. **Without process supervision, every code change is a manual restart** — and when the service
   dies nothing brings it back, taking any in-flight transcode with it.
3. **Two different environments are themselves a bug source.** Production is uvicorn on loopback
   behind a TLS-terminating proxy; `0.0.0.0:8084` in the clear behaves differently for path
   prefixes, `X-Forwarded-*` and the `Secure` cookie attribute.

### 1. Supervise the process with a systemd user service

```bash
cp deploy/travel-memory.service ~/.config/systemd/user/
# edit the three placeholders it documents (AMAP_APPNAME / LLM provider / paths)
systemctl --user daemon-reload
systemctl --user enable --now travel-memory
systemctl --user status travel-memory
loginctl enable-linger "$USER"      # keep running when you are not logged in
```

Logs: `journalctl --user -u travel-memory -f`.

> `deploy/` carries nine more unit pairs: certificate renewal, demo reset, the daily recall
> push, upload-session pruning, health checks, a nightly full test run, media sync to object
> storage. All optional — copy what you need.

### 2. Put Caddy / Nginx in front for TLS

`deploy/Caddyfile.example` is a minimal working config (Caddy issues and renews certificates
for you):

```bash
cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # set your domain
sudo systemctl reload caddy
```

No public domain on your dev box? Use Caddy's internal CA (`tls internal`) to sign a
certificate for `localhost` or your LAN IP, install the root cert on your phone, and you get
the full https path on a real device — see the comments in the example file.

**If you use MCP**: once the app moves to a TLS port, the address the `/mcp` endpoint uses to
call its own domain API has to move with it, or all 34 tools report "domain API unreachable":

```ini
Environment=TRAVEL_BASE_URL=https://127.0.0.1:8443
```

(A loopback certificate never matches the hostname; `mcp_http.py` skips verification for
loopback only.)

### 3. Backups

Three things are enough: `data.db` + `uploads/` + the key files (`amap_key.txt`,
`amap_security.txt`, `token.txt`). `./backup.sh` packages them using SQLite's `backup()` API,
which gives a consistent snapshot even under WAL.

### 4. Serve media from object storage (optional, recommended)

By default every image and video is served by the application process out of `/uploads/`.
That competes with the API for the same uplink — very visible if your public entry point is a
tunnel (frp / cloudflared), where one person watching a video slows the whole site down.

Put your bucket's origin in `media_base.txt` (or `TRAVEL_MEDIA_BASE_URL`) and every
`/uploads/` link points at object storage instead, taking media traffic off the app entirely:

```bash
echo 'https://your-bucket.oss-cn-qingdao.aliyuncs.com' > media_base.txt
```

It is **one switch**: blank it out and everything falls back to local serving, no other code
changes. `deploy/travel-memory-oss-sync.{service,timer}` keeps the bucket in sync. Note that
`/static/` is deliberately *not* offloaded — the frontend JS/CSS must ship with the code.

### 5. Direct media upload to object storage (faster uploads, optional)

The previous section fixes the **downlink** (other people viewing your photos and video). The
uplink is just as congested: the browser pushes the file to the server, and the whole transfer
competes for the same uplink — lethal behind a tunnel, where a 150 MB video takes several
minutes at best while every API call queues behind it.

With direct upload on, the browser **PUTs parts straight to OSS**; the server only signs and
finalises, and never touches a byte of media. The original is pulled back to the box afterwards
by a background queue that builds thumbnails, transcodes and archives it
(`oss_backfill.py`, concurrency 1, so it never races anyone).

Three things are needed:

1. `media_base.txt` is already filled in (the same switch as the previous section — the bucket
   name and endpoint are derived from it);
2. AK/SK — read from `~/.ossutilconfig` by default (the same credentials the media sync script
   uses), overridable with `TRAVEL_OSS_AK` / `TRAVEL_OSS_SK`;
3. **CORS enabled on the bucket.** This is the only hard prerequisite:

The easiest route is the console: **OSS -> your bucket -> Permissions -> CORS -> create rule**,
origin = your site's domain, methods `PUT` / `GET` / `HEAD`, header `*`, rest default.

The command-line equivalent (needs an AK with `oss:PutBucketCors`; a restricted sub-account is
rejected):

```bash
cat > cors.xml <<'XML'
<?xml version="1.0" encoding="UTF-8"?>
<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>https://your-domain</AllowedOrigin>
    <AllowedMethod>PUT</AllowedMethod>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>HEAD</AllowedMethod>
    <AllowedHeader>*</AllowedHeader>
    <MaxAgeSeconds>600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>
XML
ossutil cors --method put oss://your-bucket cors.xml

# verify: this must return 200, not "CORS is not enabled for this bucket"
curl -i -X OPTIONS "https://your-bucket.oss-cn-qingdao.aliyuncs.com/uploads/_probe" \
     -H "Origin: https://your-domain" \
     -H "Access-Control-Request-Method: PUT" \
     -H "Access-Control-Request-Headers: content-type"
```

`ETag` does not need to be exposed: the server queries ListParts when completing the multipart
upload and never trusts a client-reported ETag, so the frontend does not read that response
header at all — one less CORS setting to get wrong.

Nothing breaks if CORS is missing: the first part fails with a zero-byte error and the frontend
falls the whole upload back to server-relayed mode (`ossDirectGiveUp`); the user simply does not
get the speed-up. To turn direct upload off temporarily: `TRAVEL_OSS_DIRECT=0`.

`/health` reports `oss_direct` and the `oss_backfill` queue depth, so you can see at a glance
whether direct upload is live and how many originals are still to be pulled back.

## Layout

```
app.py                 FastAPI app and Web UI routes (90 paths / 105 operations)
db.py                  SQLite schema, migrations and every query (WAL)
config.py              static config shared across modules (AMap key, OSRM, media origin)
media.py               thumbnails / EXIF / video frame extraction
transcode.py           video quality ladder (faststart + 1080p/720p/480p, background thread)
download.py            media "save as": filename building, RFC 5987, quality whitelist
oss.py                 OSS V1 signing + multipart direct upload (browser -> OSS, never via the app)
oss_backfill.py        pulls directly-uploaded objects back for archival (background queue:
                       thumbnails / EXIF / transcode)
mcp_http.py            MCP Streamable HTTP endpoint (routing table: 34 tools → domain API)
mcp_server.py          MCP stdio variant
companion.py           companion process (scheduled jobs) + the single definition of TOOLS
routers/
  agent_api.py         domain API (/api/ai/*) + /mcp — API-key auth, disjoint from the Web UI
  console.py           developer console (API key management), session auth
  share.py             read-only share page and its anonymous endpoints
  deps.py              request-scoped helpers shared between routers
domain/                domain logic: memory generation, association, search, geocoding,
                       LLM provider, rate limiting, token auth
static/
  index.html app.js    main UI (~5,600 lines of JS)
  share.html share.js  read-only share page
  common.js            shared by both pages: map layers, lightbox, quality picker, download, Markdown
  console.html         developer console
  style.css
scripts/               seeding, demo reset, secret scanning and utilities
deploy/                systemd unit samples (11 services + 9 timers) + a Caddyfile sample
tests/                 automated tests (unit / api / e2e)
.githooks/             pre-commit checks (secret scan + fast suite)
test.sh setup.sh start.sh   test / one-command install / run
```

## Tests

```bash
./test.sh                # fast suite: unit + API, ~20s (run this after any change)
./test.sh all            # everything incl. end-to-end browser tests, ~90s
./test.sh unit           # unit layer only
./test.sh api            # API integration layer only
./test.sh e2e            # end-to-end only (needs system Chrome)
./test.sh -k keyword -vv # remaining args are passed straight to pytest
./test.sh secrets        # scan for hard-coded credentials
./test.sh install-hooks  # enable the pre-commit checks (run once per clone)
```

**Run `./test.sh install-hooks` once after cloning.** Git does not carry
`core.hooksPath` across a clone, so without this step the pre-commit checks
(secret scan + fast suite) silently never run.

One-time setup: `.venv/bin/pip install -r requirements-dev.txt`. E2E reuses the
system `/usr/bin/google-chrome` via Playwright's `channel="chrome"`, so there is
no `playwright install` download step.

**Tests never touch production data.** Everything runs against a temporary
SQLite file and a temporary uploads directory, and `tests/conftest.py` patches
`sqlite3.connect` so that any attempt to open the production `data.db` raises
instead of silently writing to it.

### MCP tool regression

Between `/mcp` and the domain API sits a hand-written routing table
(`mcp_http.TOOL_ROUTES`), and nothing at compile time ties it to the real routes in
`routers/agent_api.py`. Rename an endpoint, change a field, adjust a scope, and the tool goes
mute while the Web UI is entirely unaffected (the frontend never calls `/api/ai/*`). So
`tests/api/test_mcp_tools.py` does three things:

1. neither the catalogue (`companion.TOOLS`) nor the routing table may contain orphans, and no
   declared argument may be missing from a tool's schema;
2. every `(method, path)` the table points at must actually be registered on the FastAPI app;
3. **all 34 tools are really invoked** through `tools/call`, end to end (auth → scope →
   argument assembly → domain API → result); any `isError` is a regression.

```bash
./test.sh -k mcp
```

### Pre-commit checks

```bash
./test.sh install-hooks     # run once after cloning
```

This points `core.hooksPath` at the in-repo `.githooks/`. Every commit then runs:

1. `scripts/scan-secrets.sh --staged` — hard-coded credentials in staged lines
2. `./tests/run.sh` — the fast suite, skipped for docs-only commits

Either failure blocks the commit and prints **which test failed and why**. E2E is
too slow for a hook; the nightly job covers it.

Escape hatches: `git commit --no-verify` skips both (a `post-commit` hook prints a
loud warning afterwards), `TM_SKIP_TESTS=1 git commit` skips only the tests and
still scans for secrets. Never force past a secret-scan hit — a pushed credential
has to be revoked and rotated.

### About xfail

Tests marked `@pytest.mark.xfail` are documented known defects. They are an
expected state: never counted as failures, never alerted on, and the count
changing as defects get fixed is not an incident. If an xfail test starts
passing (xpass), the defect was probably fixed — drop the marker so the test
becomes a real regression guard.

## Known limits

- **SQLite is single-writer.** WAL is on, so readers and writers no longer block each other and
  the write lock is only held at commit; that is plenty for personal or small-team use. It is
  not built for heavy write concurrency (dozens of people recording trips at once) — that scale
  wants Postgres.
- Media is served by the app process by default and competes with the API for bandwidth. Point
  `media_base.txt` at object storage (see "Running it · 4") and that limit disappears.
- Transcoding is a local ffmpeg background thread handling one video at a time; a bulk import of
  dozens of videos takes a while. Without ffmpeg you simply get the original rung only.
- Routing depends on AMap/OSRM; some regions outside China fall back to straight lines.
- End-to-end tests drive the system Chrome (`channel="chrome"`); without it only the unit and API layers can run.

## License

[MIT](LICENSE). Use it, change it, ship it commercially — just keep the copyright notice.
