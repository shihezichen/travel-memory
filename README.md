<div align="center">

# 🧭 Travel Memory

**Turn a trip into a memory you can revisit — maps, photos, routes, notes, plus a memory API open to AI agents.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-7c3aed)

**English** · [中文](README.zh-CN.md)

</div>

---

## What is this

A month after a trip, the photos are buried in a camera roll of thousands, the route is a vague
impression, and whatever you were thinking at the time is gone. That is the problem Travel Memory
tries to solve: **capture a trip completely, and let you walk through it again whenever you want.**

It is self-hosted — the data lives on your own machine and is uploaded to nobody. Once installed
you get a web app that works well on a phone, plus an endpoint that lets an AI assistant reach
into those memories directly.

## What you can do with it

### 📍 Record a trip

Drop the places you stopped onto a map in order, and the system works out the **real driving
route** between them (not straight lines), labels each leg with its distance and each point with
its elevation. A trip moves through planning → traveling → completed → archived, so you can lay
out where you want to go before you leave and keep editing it on the road.

Routing isn't one-shot. If a leg runs along several numbered highways, all of their numbers get
labelled. And when AMap routes you the long way round because of a seasonal closure or a
restriction — the Duku Highway shuts every October, which turns Nalati → Kuqa into a 1361 km
detour — the system fetches a second route that ignores closures, and makes it the default when
it is clearly shorter. A record of a trip should follow **the road you actually drove**. Whichever
option you pick is stored, so it stays picked; only moving the coordinates invalidates it.

Every place holds photos and videos. Shoot on your phone and upload straight away — the
**capture time and coordinates are read out of the photo automatically**. Big videos don't need
babysitting: pause the upload, go do something else, come back and resume; if the connection
drops, re-pick the same file and it continues from where it stopped. You can play your local copy
while it is still going up.

Every photo takes a caption with **Markdown support**, so you can actually write something rather
than just title it.

### 🔁 Go back through it

- **Route replay** — play the whole trip back along the real route, with speed control. It brings
  the day back in a way a pile of stills doesn't
- **Album** — the trip's photos as one flat wall (iOS Photos style); pinch to change how many per
  row, sort by time, filter by type
- **Photo wall autoplay** — a ▶ on both the album page and the place detail page sets the whole
  wall going by itself: it picks a cell at random, selects it, blows it up in place, fills the
  screen, then shrinks it back, one after another. Good to just leave running. Videos play, with a
  hand-rolled fullscreen button in the corner — native control bars differ so much across
  browsers that building our own is the only way to guarantee one exists, in the same place
- **Photo stacks** — photos cluster onto each place on the map and scale with zoom, so you can see
  at a glance where you shot the most. Videos are in there too, using their first frame as the
  thumbnail
- **Footprint heatmap** — every trip laid over each other: everywhere you have been, over the years
- **Lightbox** — finger-tracking paging, tap for fullscreen, pinch to zoom, drag past the edge to
  keep paging. The same feel as your phone's gallery
- **Save back to your device** — photos download as the original, videos let you pick a quality,
  and the filename is built for you from date, place and index

### 👥 Share with people

Generate a **read-only link**. Whoever you send it to doesn't need an account, and gets the same
interactions you do (layers, replay, album). Set an expiry and a password, revoke it any time.
You can also generate a portrait share card with a QR code, which saves straight to the photo
library on mobile.

A share page contains **only that one trip** — your footprint heatmap and the photos from your
other trips never leak out with it.

Every image and video URL on the page is a **signed, expiring link** (30 minutes by default), so
the bucket holding your media can have public read turned off — no more "forwarded once, public
forever". Expiry is invisible to whoever is looking: images re-fetch a fresh batch of signatures,
and videos re-sign themselves five minutes before the deadline while holding their playback
position. The share link itself is unaffected — post it in a group chat and it still opens two
weeks later.

### 🤖 Let your AI assistant dig through your memories

Connect Claude, Cursor, or any client that speaks **MCP**, and you can just ask:

> "When was I last in Kyoto? Where did I stay that night?"
> "How many cities have I been to in the last two years, and which one do I go back to most?"
> "Organise last week's photos by day and write me a short summary for each."
> "Write an annual report for my 2026 travel."
> "It's September 11th — where was I on this day in previous years?"

The agent reads **your own** data: a key only ever sees the account that issued it, permissions
come in four grades (read / write / share / admin), keys can carry an expiry and be revoked at
any time.

Media URLs handed to an agent are signed and expiring too, with a longer window (24 hours by
default) — agents tend to list everything first and download some of it much later.

To connect: sign an API key in the developer console after logging in, and paste it into your MCP
client. Step-by-step in [MCP-ACCESS.md](MCP-ACCESS.md). The console has a separate shape on
phones (a persistent bottom tab bar and card lists, swipe a key card left to delete); keys carry
an expiry with the remaining days shown in the list, and the call log tells you where a request
came from. **Only the sha256 of a key is stored** — after the one time it is shown to you, the
system can't read it back either, so a lost key has to be reissued.

### 📱 About phones

More effort went into mobile interaction than into features: a panel you drag to resize, trips
that swipe sideways with a sense of direction, long-press-drag and swipe-to-switch-tab that never
fight each other, a back gesture that won't throw you out of the site, a top bar that folds away
and hands the screen back to the map, and icons that stay legible in dark mode and over satellite
imagery.

## Quick start

You need **Python 3.10+**, then one command:

```bash
git clone https://github.com/shihezichen/travel-memory.git
cd travel-memory
./start.sh
```

Open `http://localhost:8084` and you're in. Three entry points:

| Path | Who it serves |
|---|---|
| `/` | Branches on login state: signed-out visitors get the public landing page (a 60-second demo video and a five-panel carousel), signed-in ones go straight into the app |
| `/app` | The app itself, always. Signed out, the sign-in overlay comes up |
| `/welcome` | The landing page, always — for when you're signed in and want another look at the pitch |

(The branch deliberately isn't a 302: a redirect leaves an extra entry in browser history, and
after signing out the back button would bounce between the two pages.)

The first run creates a virtualenv, installs dependencies and initialises the database; every run
after that notices the environment is already there and just starts. No schema to create by hand,
no config file to edit.

That first run also brings **a set of demo data** (3 trips, 18 points, 55 photos, 6 videos) under
the account `test` / `test1234`, with a one-tap button on the sign-in page — so you can see what
the thing looks like without recording a trip first.

## The one thing to configure by hand: an AMap key

It starts fine without one. Trips, photos and sharing all work; the map area is just blank, and a
banner at the top of the page tells you so. Once you set it, the map appears:

1. Get a **Web (JS API)** key from the [AMap console](https://console.amap.com/dev/key/app). You
   also get a paired **securityJsCode** with it;
2. Write both to files and restart:

```bash
echo 'your-key'            > amap_key.txt
echo 'your-security-code'  > amap_security.txt
```

> ⚠️ **Set a domain allowlist on the key in the AMap console.** JS API 2.0 ships the key and the
> security code to the browser by design, so without an allowlist anyone can take it and burn
> through your quota.

> Optional: install `ffmpeg` (`sudo apt install ffmpeg` / `brew install ffmpeg`). It is what
> extracts video poster frames and builds the quality ladder; photos work regardless.

> Elevation comes from the public [OpenTopoData](https://www.opentopodata.org/) instance
> (SRTM 90m), capped at 1000 calls/day. Each point is fetched once and cached, so normal use stays
> far below that; bulk-importing hundreds of points wants your own instance or another source.

## Optional: Jev rerank for photo search

`/api/search/photos` recalls with Chinese-CLIP and then, if configured, puts the top candidates
through a System One model (`jevrerank.py`) that answers typed questions -- "how relevant is this,
on a 0-4 scale", "should this be returned at all", "which category is it" -- and fuses those
calibrated probabilities with the CLIP score. Cosine similarity answers *does it look alike*;
it cannot answer *should this come back*.

It is off until you give it a key:

```bash
echo 'your-key' > jev_key.txt   # or export TM_JEV_API_KEY
```

The default endpoint is a **self-hosted KEV deployment**, not the TypeSafe cloud. Both speak the
same `/v1/systemone` wire format, so switching is one environment variable: `TM_JEV_API_URL` for
another host, `TM_JEV_BACKEND=typesafe` to go back to the official cloud, `TM_JEV_BACKEND=cloudflare`
for the Workers AI relay. Any timeout or error degrades to plain CLIP ordering -- search is what
the user is waiting on, reranking is a bonus and must never take search down with it.

> It only sees **text**: the caption, place name, trip title and filename of each candidate, never
> the pixels. On a library where nobody wrote captions there is nothing for it to judge.

## Keeping it running

The `./start.sh` above is for a first look: plain HTTP, dies with your terminal. Once you actually
start using it, **set up the following from day one** rather than waiting until you "go to
production". Not out of ceremony — these bite while you are still the only user:

1. **Without TLS, half the features are crippled.** Browsers lock the clipboard, geolocation and
   `crypto.subtle` behind a secure context (https or localhost). Reach the site from your phone
   over a LAN IP and copying a share link, locating yourself, and hashing a file on upload all
   take the degraded path.
2. **Without process supervision the site is gone after a reboot**, and so is any video transcode
   that was halfway through.
3. **Two different environments are themselves a bug source** — cookie `Secure`, `X-Forwarded-*`
   and path prefixes all behave differently over plain HTTP than over TLS.

### 1. Supervise the process with systemd

```bash
cp deploy/travel-memory.service ~/.config/systemd/user/
# edit the three placeholders it documents (AMAP_APPNAME / LLM provider / paths)
systemctl --user daemon-reload
systemctl --user enable --now travel-memory
loginctl enable-linger "$USER"      # keep running while you are logged out
```

Logs: `journalctl --user -u travel-memory -f`.

> `deploy/` holds 9 more optional units: certificate renewal, the daily demo reset, daily memory
> push, upload-session reclamation, health checks, a nightly full test run, media sync to object
> storage. Copy whichever you want.

### 2. Set up TLS

**Two routes. Pick one — don't stack them.**

#### A. Terminate TLS in a reverse proxy (recommended for a fresh install)

`deploy/Caddyfile.example` is a minimal working config; Caddy issues and renews certificates on
its own and proxies to the app's **plaintext** port `127.0.0.1:8084`:

```bash
cp deploy/Caddyfile.example /etc/caddy/Caddyfile   # set your domain
sudo systemctl reload caddy
```

Going this way, drop the `--ssl-certfile` / `--ssl-keyfile` lines from
`deploy/travel-memory.service` and put the port back to 8084 — that unit ships configured for
route B below.

No public domain on your dev box? Use Caddy's internal CA (`tls internal`) to sign a certificate
for `localhost` / a LAN IP, install the root on your phone, and you can exercise the full https
path on a real device — see the comments in the example file.

#### B. Terminate TLS in uvicorn itself

`deploy/travel-memory.service` **ships set up for this one**: uvicorn binds
`127.0.0.1:8443` with the certificate directly, no nginx or Caddy in front, and the public entry
point is a tunnel or an upstream proxy. It is what this project's own production host
(top001.xin) runs.

The cost is that certificates are on you: the unit points at `acme/ip.crt` and `acme/renew.sh`,
and **that directory is excluded by `.gitignore`** (it holds TLS private keys and one machine's
hostnames — no use to anyone else), so a fresh clone does not have it. Either sign one yourself
with lego / certbot and fix the paths, or just take route A.
`deploy/travel-memory-cert-renew.timer` and `-domain-renew.timer` are the matching renewal
timers, and depend on the same scripts under `acme/`.

#### If you use MCP

Once the app moves to a TLS port, the address `/mcp` uses to call its own domain API has to move
with it, or every tool reports "domain API unreachable" while the Web UI looks perfectly fine:

```ini
Environment=TRAVEL_BASE_URL=https://127.0.0.1:8443
```

On route A this is the plaintext `http://127.0.0.1:8084` instead — it has to match the protocol
and port `ExecStart` **actually listens on**, not your public address.
(A hostname mismatch on a loopback certificate is expected; `mcp_http.py` skips verification for
loopback only.)

### 3. Backups

Take these and you have everything: `data.db` + `uploads/` + the three secret files
(`amap_key.txt`, `amap_security.txt`, `token.txt`, plus `jev_key.txt` if you set one). `./backup.sh` packages them using SQLite's
`backup()` API, which gets a consistent snapshot even under WAL.

## Optional: make photos and video faster

Using it alone on your LAN? Skip this section entirely. If you put the site on the public internet
and share links with people, **bandwidth** is the only thing that really hurts.

### Downlink: serve media from object storage

By default the app process serves every image and video itself, competing with the API for the
same uplink. It is very visible when your public entry point is a tunnel (frp / cloudflared):
one person watching a video slows the whole site down.

Put your bucket's origin in `media_base.txt` and every media link points at object storage:

```bash
echo 'https://your-bucket.oss-cn-qingdao.aliyuncs.com' > media_base.txt
```

It is **one switch**: blank it out and everything falls back to local serving, no code changes.
`deploy/travel-memory-oss-sync.{service,timer}` keeps the bucket in sync. Note that `/static/` is
deliberately *not* offloaded — the frontend JS/CSS must ship with the code.

**The bucket does not need public read.** Every outward-facing media URL is signed on the fly
with an expiry: 30 minutes for the web and share pages (`TRAVEL_MEDIA_URL_TTL`), 24 hours for
agents (`TRAVEL_AGENT_URL_TTL`). Expiry heals itself on the frontend, invisibly (see "Share with
people" above). With no object storage configured the signing layer is skipped and links fall
back to the in-app `/uploads/` path.

### Uplink: upload straight to object storage

The previous section fixes other people viewing your photos. The uplink is just as congested: the
browser pushes the file to the server and the whole transfer competes for the same uplink — a
150 MB video takes several minutes at best while every API call queues behind it.

With direct upload on, the browser **PUTs parts straight to object storage**; the server only
signs and finalises, and never touches a byte of media. The original is pulled back afterwards by
a background queue that builds thumbnails, transcodes and archives it (`oss_backfill.py`,
concurrency 1, so it never races anyone).

Three things are needed:

1. `media_base.txt` is already filled in (the same switch as the previous section — the bucket
   name and endpoint are derived from it);
2. AK/SK — read from `~/.ossutilconfig` by default, overridable with `TRAVEL_OSS_AK` /
   `TRAVEL_OSS_SK`;
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
upload and never trusts a client-reported ETag, so the frontend does not read that response header
at all — one less CORS setting to get wrong.

Nothing breaks if CORS is missing: the first part fails with a zero-byte error and the frontend
falls the whole upload back to server-relayed mode; the user simply does not get the speed-up. To
turn direct upload off temporarily: `TRAVEL_OSS_DIRECT=0`.

`/health` reports `oss_direct` and the `oss_backfill` queue depth, so you can see at a glance
whether direct upload is live and how many originals are still to be pulled back.

## Other optional settings

### Environment variables

None are required. Add them as you need them:

| Variable | Purpose | Default |
|---|---|---|
| `PORT` / `HOST` | What `./start.sh` listens on (or use `./start.sh --port 9000`) | `8084` / `0.0.0.0` |
| `AMAP_APPNAME` | Whitelist appname for your AMap key; only needed for the administrative-boundary layer, must match what you set in the AMap console | `http://localhost:8084/` |
| `TRAVEL_BASE_URL` | Where the MCP endpoint calls its own domain API. **Must match the scheme/port the process actually listens on** | `http://127.0.0.1:8084` |
| `TRAVEL_LLM_PROVIDER` | Which model backs the AI features: `auto` / `claude-cli` / `anthropic-api` / `deepseek` | `auto` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | Key for the chosen provider; unnecessary if you don't use AI features | empty |
| `TRAVEL_MEDIA_BASE_URL` | Origin that serves media (object storage / CDN). Empty = the app serves `/uploads/` itself | empty (or put it in `media_base.txt`) |
| `TRAVEL_MEDIA_URL_TTL` | Lifetime of signed media URLs on the web and share pages, in seconds. **Change this and you must change `MEDIA_SIG_TTL_MS` in `static/share.js` too** — the frontend uses it to decide when to re-sign | `1800` (30 min) |
| `TRAVEL_AGENT_URL_TTL` | Lifetime of signed media URLs handed to agents (`/api/ai/*`, `/mcp`), in seconds | `86400` (24 h) |
| `TRAVEL_OSS_DIRECT` | Master switch for browser-to-object-storage direct upload. `0` = off, fully back to relaying through the server | `1` (auto-disabled when the config is incomplete) |
| `TRAVEL_OSS_AK` / `TRAVEL_OSS_SK` | AK/SK used to sign direct uploads. Unset = read `~/.ossutilconfig` | empty |
| `TRAVEL_OSS_PART_SIZE` | Direct-upload part size, in bytes | `8388608` (8 MB) |
| `TRAVEL_OSS_BACKFILL_WORKERS` | Concurrency of pulling originals back for archival. Deliberately 1 — do not race someone who is watching a video | `1` |
| `DEMO_USER` | Account name the daily reset targets (see below) | `test` |

### Resetting the demo account daily

If you let other people play with the demo account, a day of that leaves it a mess. The repo ships
a pair of systemd user units that restore it to factory state at 03:00 daily:

```bash
cp deploy/travel-memory-demo-reset.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now travel-memory-demo-reset.timer
```

Reset once by hand: `./start.sh --demo` (touches only the demo account). Don't want demo data at
all: `./start.sh --no-demo`.

## Known limits

- **SQLite is single-writer.** WAL is on, so readers and writers no longer block each other and
  the write lock is only held at commit; that is plenty for personal or small-team use. It is not
  built for heavy write concurrency (dozens of people recording trips at once) — that scale wants
  Postgres.
- Media is served by the app process by default and competes with the API for bandwidth. Point
  `media_base.txt` at object storage (previous section) and that limit disappears.
- Transcoding runs in local ffmpeg background threads. One video's quality rungs are produced by
  a single ffmpeg pass that decodes once and encodes all of them, and concurrency is capped by
  **cores in use** (leaving 20% of the machine by default) — but it still saturates the CPU, so a
  bulk import of dozens of videos takes a while. Without ffmpeg you simply get the original rung
  only.
- Routing depends on AMap/OSRM; some regions outside China fall back to straight lines.
- "Reopen the file picker where you left it" relies on the File System Access API (desktop Chrome
  / Edge). Firefox and Safari get the ordinary file dialog and behave as before; on phones it is
  **deliberately** not used — the system photo picker is better than a file manager.

---

# For developers

Everything below is only needed if you want to change the code. If you just want to use it, the
sections above are enough.

## Stack

**FastAPI + SQLite + vanilla JavaScript.** No frontend build step, no ORM, no Redis — clone it,
install a handful of Python packages, run it. Edit a line of JS and reload the page.

## Layout

```
app.py                 FastAPI app and Web UI routes (100 paths / 117 operations)
db.py                  SQLite schema, migrations and every query (WAL)
config.py              static config shared across modules (AMap key, OSRM, media origin,
                       signed-URL lifetimes)
media.py               thumbnails / EXIF / video frame extraction
media_purge.py         really deletes the bytes, locally and on OSS — shared by six delete paths
transcode.py           video quality ladder (one decode pass emits 1080p/720p/480p, capped by
                       cores in use)
download.py            media "save as": filename building, RFC 5987, quality whitelist
oss.py                 OSS V1 signing + multipart direct upload (browser -> OSS, never via the app)
oss_backfill.py        pulls directly-uploaded objects back for archival (background queue:
                       thumbnails / EXIF / transcode)
agent_media.py         signed media URLs for the agent path (separate, longer-lived than the web's)
formfix.py             normalises form bodies to percent-encoded ASCII (fixes agent-written CJK)
video_gen.py           trip video generation engine (FFmpeg templates)
mcp_http.py            MCP Streamable HTTP endpoint (routing table: tools -> domain API)
mcp_server.py          MCP stdio variant
companion.py           companion process (scheduled jobs) + the single definition of TOOLS
routers/
  agent_api.py         domain API (/api/ai/*) + /mcp — API-key auth, disjoint from the Web UI
  console.py           developer console (API key management), session auth
  share.py             read-only share page and its anonymous endpoints
  deps.py              request-scoped helpers shared between routers (incl. asset versioning)
domain/                domain logic: memory generation, association, search, geocoding,
                       LLM provider, rate limiting, token auth
static/
  landing.html         the public landing page. Fully self-contained — no style.css, app.js or AMap
  index.html app.js    main UI (~6,800 lines of JS)
  share.html share.js  read-only share page (~2,800 lines)
  common.js            shared by both pages (~2,600 lines): map layers, lightbox, quality picker,
                       download, Markdown, photo wall autoplay
  console.html         developer console
  style.css
  promo/               landing page artefacts (screenshot WebPs, demo video, narration subtitles)
assets/landing-src/    **source** images for the landing carousel. Not under static/ — that
                       directory is publicly served
tools/                 make-slides.py (source image -> desktop/mobile WebP), shoot-landing.py
scripts/               seeding, demo reset, secret scanning, promo recording and assembly
ops/                   operational scripts (OSS sync, health check, standby update, daily recall)
deploy/                systemd unit samples (11 services + 9 timers) + a Caddyfile sample
docs/advertising/      positioning, copy and storyboards for the promo material
verify/                site-ownership verification files, served verbatim at the site root
tests/                 automated tests (unit / api / e2e, 39 test files)
.githooks/             pre-commit checks (secret scan + fast suite)
start.sh               install + run, merged into one idempotent script
test.sh                test entry point
```

## Tests

```bash
./test.sh                # fast suite: unit + API, 693 tests in ~30 s (run after every change)
./test.sh all            # everything incl. browser end-to-end, 786 tests in ~2m20s (before committing)
./test.sh unit           # unit layer only
./test.sh api            # API integration layer only
./test.sh e2e            # end-to-end only (needs system Chrome)
./test.sh -k keyword -vv # any other argument is passed straight to pytest
./test.sh secrets        # scan for hardcoded secrets
./test.sh install-hooks  # enable the pre-commit checks (run once per clone, see below)
```

**Run `./test.sh install-hooks` once after cloning.** Git does not carry `core.hooksPath` across a
clone, so without this step the pre-commit checks in `.githooks/` (secret scan + fast suite) are
silently inactive.

First-time prep: `.venv/bin/pip install -r requirements-dev.txt`. E2E drives the already-installed
`/usr/bin/google-chrome`, so no `playwright install` download is needed.

**Tests never touch production data.** Everything runs against a temporary SQLite database and a
temporary uploads directory, and `tests/conftest.py` hijacks `sqlite3.connect` so that any attempt
to open the production `data.db` raises instead of quietly writing to it.

### MCP tool regression

Between `/mcp` and the domain API sits a hand-written routing table (`mcp_http.TOOL_ROUTES`), and
nothing at compile time ties it to the real routes in `routers/agent_api.py`: change an endpoint
path, rename a field, adjust a scope, and a tool goes dead while the Web UI is completely fine
(the frontend never calls `/api/ai/*`). So `tests/api/test_mcp_tools.py` does three things:

1. no orphans in either direction between the catalogue (`companion.TOOLS`) and the routing table,
   and no missing fields in the argument schemas;
2. every `(method, path)` the routing table points at is actually registered on FastAPI;
3. **every tool is really invoked** through `tools/call`, end to end (auth → scope → argument
   assembly → domain API → result); any `isError` counts as a regression.

```bash
./test.sh -k mcp
```

The tool count lives in `EXPECTED_TOOL_COUNT` in `tests/api/test_mcp_tools.py`: add or remove a
tool and that assertion goes red first, reminding you to update the docs with it.

### Pre-commit checks

```bash
./test.sh install-hooks     # once per clone; after that every git commit runs them
```

This points `core.hooksPath` at `.githooks/` in the repo, so each commit runs, in order:

1. `scripts/scan-secrets.sh --staged` — scan the staged diff for hardcoded secrets (milliseconds)
2. `./tests/run.sh` — the fast suite; automatically skipped for docs/image-only changes

Either failing blocks the commit and prints **which test failed and why**. End-to-end is too slow
for a hook and is left to the nightly timer.

Escape hatches:

| How | Skips | Notice |
| --- | --- | --- |
| `git commit --no-verify` | secret scan + tests | `post-commit` prints a loud warning afterwards |
| `TM_SKIP_TESTS=1 git commit` | tests only, secrets still scanned | warns at commit time and after |

**Do not** force past a secret-scan hit with `--no-verify`: once a secret is pushed, the only fix
is to revoke and reissue it.

### About xfail

A test marked `@pytest.mark.xfail` is a **documented known defect**: expected state, not a
failure, and the count changing as defects get fixed is not an incident. Conversely, if an xfail
test suddenly passes (xpass), the defect was probably fixed in passing — drop the marker so it
becomes a real regression guard.

## License

[MIT](LICENSE). Use it, change it, ship it commercially — just keep the copyright notice.
