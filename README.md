# in-Time

Face-recognition attendance system for multi-site organisations. Employees check **in** and **out**
by looking at a phone camera on their site's Wi-Fi. Each site runs a small **relay server** that keeps
working offline, and a central **main server** collects everything.

```
 Phones (browser) ──LAN / HTTPS──▶ RELAY SERVER  (Windows, one per site)
                                    Flask + Qt window + SQLite cache
                                          │  HTTPS, token + IP authentication
                                          ▼
                                   MAIN SERVER  (Linux)
                                   Flask + PostgreSQL + admin web console
```

![Global architecture of in-Time](archi.png)

*Global architecture. The editable source is [`archi.drawio`](archi.drawio) (open it with [draw.io](https://app.diagrams.net)).*

## Screenshots

**Web admin console** (main server): dashboard with today's check-ins, users, relays and settings.

![Web admin console](main.png)

**Relay GUI** (one per site): QR code for phones, trusted clock, internet and main server status, and today's check-ins.

![Relay GUI](relay.png)

## Features

- **Face recognition on the relay**: the phone only takes the picture; the relay identifies the person (OpenCV YuNet + SFace), so a check-in can't be forged by sending a request with someone's ID.
- **Face check on the main server**: every check-in snapshot is compared again with the user's photo; mismatches, missing faces and missing snapshots are flagged on the dashboard for review.
- **Tamper-proof time**: check-in times come from Google over HTTPS, never from the relay PC's clock, so changing the Windows date/time has no effect.
- **Main-server outages tolerated**: the relay caches users and settings, decides OK/KO locally and syncs check-ins when the main server is reachable again. Without **internet**, new check-ins are refused (no trusted time).
- **Late / early detection**: IN after *max in time* or OUT before *min out time* is flagged KO. Global values with per-site overrides.
- **Relay window**: today's check-ins with OK/KO, sync state per row, main server status (Reachable / Unreachable / Error), QR code for phones.
- **Admin console**: users (photo upload or camera capture, face check), relays (tokens, allowed IPs), settings, check-in history with filters and CSV export, snapshot of every check-in.
- **Relay authentication**: bearer token + allowed IP/CIDR list + per-relay signing key; works behind nginx/Apache/Traefik and Cloudflare.
- **Open source (AGPL-3.0) and self-hosted**: no license key, no activation, no limit on users or sites.

## Repository layout

| Folder | Contents |
|---|---|
| `main_server/` | Public API for relays + admin web console (Flask, PostgreSQL) |
| `main_server/postgres/` | PostgreSQL 16 for the main server (Docker Compose) |
| `relay_server/` | Relay: phone web client, private LAN API, sync worker, Qt window (Flask, PySide6, SQLite) |

## How it works

1. **Admin** adds users (ID, full name, department, photo) on the main server. The server checks that the
   photo has one usable face and keeps its face data as the reference. The photo is saved as `photos/<ID>.jpg|png`.
2. **Relays** sync every 30 s: ping, settings, users and their photos. Each relay computes the face data
   of new or changed photos itself.
3. **Phones** on the site's Wi-Fi open `https://<relay-ip>:8443`. The phone only detects that a face is in
   front of the camera, then sends the picture to the relay, which recognises the person. The person sees
   their name, IN/OUT is suggested (alternating during the day) and sent after 4 s unless they tap the
   other option.
4. The **relay** accepts the check-in only with the one-time token it issued for that picture, stamps it
   with the trusted (internet) time, shows OK/KO, keeps the picture as the snapshot, stores it in SQLite
   and pushes it to the main server immediately, or later if the main server is unreachable.
5. The **main server** recomputes OK/KO itself (site time zone and settings), compares the snapshot with the
   user's photo again and flags the check-in if they don't match, if the picture is a replay, or if it
   arrived suspiciously late (see [Face check](#face-check-and-anti-fraud)).

### Face check and anti-fraud

The phone never decides who is checking in, and never receives anyone's face data or photo list:

- `POST /api/identify` (picture) → the relay finds the face and compares it with every user's photo.
  When it matches, it returns the name and a **one-time token** valid 90 s for that phone only.
- `POST /api/checkins` (token, IN/OUT) → accepted only with that token; the stored snapshot is the
  picture the relay recognised. Requests with a user ID, a reused or a fake token are refused.
- Identification requests are rate-limited per phone (15 per 10 s).

On the main server, every snapshot received is compared with the user's photo. The **Face check** column
of the check-ins shows *match* (with the similarity), or flags *mismatch*, *no face*, *no snapshot* or
*replay* (the snapshot is the user's reference photo, practically identical to it, or a picture already
used for another check-in);
the dashboard counts flagged check-ins of the day and the Check-ins page can filter them. Flagged
check-ins are still recorded: review the snapshot and decide. Check-ins received while the models were
loading are checked by a background pass within a minute; `flask --app main verify-checkins` runs it on
demand (`--all` re-checks everything, e.g. after changing `FACE_MIN_SIMILARITY`).

The main server trusts the relay as little as possible:

- **OK/KO** is computed on the main server from the time, the site's time zone and the settings. The time
  zone is the relay's own (Relays page, or pinned at its first check-in), never one sent with the check-in.
  When the relay reported something else, the Status column shows it (*relay: OK*).
- Every check-in is **signed by the relay** when it is made (with the trusted time, the user, IN/OUT and the
  SHA-256 of its snapshot), with a private key that never leaves the relay. A check-in created or changed
  anywhere else (relay database edited, stolen relay token) is rejected (`bad_signature`).
- Check-ins are **numbered** by the relay. A missing or reused number means check-ins were deleted on the relay
  before they reached the main server: the next one is flagged **gap**.
- A timestamp in the **future** is refused. A check-in received more than 10 min after it was made while
  the relay was connected is flagged **delayed** (it may have been held back before syncing).
  Check-ins made while the main server was unreachable are not flagged.
- Snapshots must be JPEG images; check-in IDs must be UUIDs (they name the snapshot files).

Limits: there is no liveness detection, so holding a printed photo or a screen in front of the camera can
still fool it; every check-in keeps its snapshot for review. Photos, snapshots and face data are
**biometric personal data**: keep them out of version control (see `.gitignore`), restrict access, and
check the data-protection rules that apply to you.

### Trusted time and internet check

The relay reads the time from the HTTPS `Date` header of Google, Cloudflare or Microsoft (built into the
relay, not configurable; certificates are checked against the bundled CA list) every 60 s. Between two checks it counts with the system's monotonic clock, which does not move when someone
changes the Windows clock, so a changed PC date/time has no effect on check-ins (the relay window only
shows a warning). If the time check fails, the relay shows a red banner, the phones show
"check-in unavailable", and new check-ins are refused until the connection is back (retried every 10 s).
If the PC date is wrong by months, the secure connection itself fails and the relay asks to fix the date.

The site's time zone is set in `relay.ini` (`[time] timezone`), so changing the Windows time zone has no
effect either. Without it the PC time zone is used and the relay window shows a warning.

| Situation | Check-ins |
|---|---|
| Internet OK, main server OK | accepted, synced immediately |
| Internet OK, main server down | accepted, synced when it is back (grey cloud) |
| No internet | refused |

## Getting started

Requirements: Python 3.10+, Docker (for PostgreSQL) on the main server, Windows or Linux for the relay.
Both servers download their models automatically on first start: face-api.js (npm registry, used by the
admin pages and the phone page) and the OpenCV face models (~39 MB, OpenCV model zoo, checked by SHA-256).
On a machine without internet, copy `app/static/vendor/` and `models/` (main server) or `data/vendor/`
and `data/models/` (relay) from another installation.

### Main server (Linux)

**As a systemd service (recommended).** On the server, with sudo rights:

```bash
curl -sSL https://raw.githubusercontent.com/dijey099/in-Time/refs/heads/master/main_server/install_main_server.sh | sh
```

The script generates `SECRET_KEY` and a random admin password (printed at the end). Set `IN_TIME_BRANCH`
to a release tag and `IN_TIME_SHA256` to its archive checksum to install a verified version.

**Manually:**

```bash
cd main_server
cd postgres && cp .env.example .env && docker compose up -d && cd ..   # set POSTGRES_PASSWORD first
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set SECRET_KEY, ADMIN_PASSWORD, DEPARTMENTS (and DATABASE_URL)
python main.py                # development: http://127.0.0.1:8000 (FLASK_DEBUG=1 for the debugger)
# production (behind nginx + HTTPS):
gunicorn -w 4 -b 127.0.0.1:8000 main:app
```

The server refuses to start while `SECRET_KEY` (32+ characters) or `ADMIN_PASSWORD` (12+ characters) is
empty or a default value. Tables are created automatically. Log in, then **Relays → Add a relay**: enter the public IP(s) the site
connects from (CIDR allowed, `*` = any, for testing only) and copy the token, which is shown once.
`flask --app main create-relay "SITE A" --ips 203.0.113.10` does the same from the shell.

The camera option on the user page needs the admin console to be served over HTTPS (or `localhost`).

### Relay server (Windows / Linux)

1. Copy `relay_server/relay.ini.example` to `relay.ini` and set the main server `url` and `token`,
   and the site `[time] timezone`.
   At start-up the token is moved to `data/secrets.bin` (encrypted with Windows DPAPI
   for the account running the relay, and with a key tied to this computer and built into the relay) and
   removed from `relay.ini`; type a new value there to replace it.
   The `data/` folder is restricted to that account, SYSTEM and Administrators.

   **Encrypted local cache.** The relay's cache (users, face data, check-ins and settings in `data/relay.db`,
   photos in `data/photos`, snapshots in `data/snapshots`) is encrypted with AES-256-GCM using a data key kept
   in `data/secrets.bin`. Each value is bound to its row or file, so it can only be read and written by the
   relay: anything changed outside it is ignored (and a check-in changed or deleted before it was sent is
   flagged *gap* on the main server). A relay of an older version encrypts its existing cache at the first
   start. **Never delete `data/secrets.bin`:** without it the cache can't be read (it is moved to
   `data/unreadable-<date>`; check-ins not yet sent are lost) and the relay gets a new signing key, so the main server refuses it
   (`bad_signature`) until you generate a new token for it on the Relays page.
2. Allow phones to reach the relay on TCP port 8443, once:
   - Windows (as administrator): `netsh advfirewall firewall add rule name="in-Time relay" dir=in action=allow protocol=TCP localport=8443`
   - Linux (ufw): `sudo ufw allow 8443/tcp`
3. Start the relay in one of two ways.

**Executable.** Put `start_relay.exe` (Windows) or `start_relay` (Linux) in the folder with `relay.ini`
and double-click it. Its `data/` folder is created next to it. Double-clicked, the Windows executable hides
its console and only the relay window shows. Run from a terminal (`start_relay.exe --no-gui`), it runs
headless and prints the log there. The log is always in `data/relay.log`, and start-up errors (missing
`relay.ini`, port already in use...) are also shown in a message box.

**From source** (development). With Python 3.10+:

```bash
cd relay_server
python -m venv venv
venv\Scripts\activate          # Linux: . venv/bin/activate
pip install -r requirements.txt
python start_relay.py           # --no-gui runs it headless
```

**Building the executable.** In the activated venv, run `pip install pyinstaller`, then `build.bat` (Windows)
or `sh build.sh` (Linux): it creates `dist/start_relay(.exe)` from [`relay.spec`](relay_server/relay.spec).
Build on the OS you target, from a venv that only has `requirements.txt` installed (PyInstaller bundles
every package it can import). OpenCV (face recognition) makes up a large part of the executable
(about 130 MB on Linux); the face models are not bundled but downloaded into `data/models` on first start.

On first visit, phones show a certificate warning (self-signed certificate, needed because browsers only
allow the camera over HTTPS): accept it once. The certificate is regenerated if the relay's LAN IP
changes, so give the relay PC/Server a fixed IP or DHCP reservation.

## Configuration

### Main server (`main_server/.env`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | **Required.** Flask session key: a long random value (32+ characters) |
| `DATABASE_URL` | PostgreSQL URL |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Admin console login. **Required:** password of 12+ characters. After `LOGIN_MAX_FAILURES` (5) failed logins in a row the login is locked (see [Admin login lock](#admin-login-lock)) |
| `ADMIN_LOCK_FILE` | Lock file of the admin login (default `admin_login.lock` next to `.env`) |
| `SESSION_COOKIE_SECURE` | `true` when the console is served over HTTPS |
| `DEPARTMENTS` | **Required.** Comma-separated departments, e.g. `Sales,Direction,Manager`; every user must belong to one. The server refuses to start without it |
| `PHOTOS_DIR`, `SNAPSHOTS_DIR` | Where user photos and check-in snapshots are stored |
| `TRUSTED_PROXIES` | Number of reverse proxies in front of the app |
| `TRUSTED_PEERS` | Addresses of those proxies (default `127.0.0.1/32,::1/128`): forwarded headers from anyone else are ignored |
| `CF_PROXY` | `true` when Cloudflare is in front |
| `SOURCE_URL` | Source code link in the admin console footer (default: this repository). Set it to your published source if you run a modified version (AGPL-3.0) |
| `CF_IPS_FILE` | Cloudflare ranges file (default `cf_ips.txt`) |
| `FACE_MIN_SIMILARITY` | Face check: similarity (0-1) between snapshot and photo required for a match (default `0.363`, higher = stricter) |
| `FACE_REPLAY_SIMILARITY` | A snapshot at least this similar to the photo is flagged *replay* (default `0.97`) |
| `CHECKIN_MAX_DELAY_SECONDS` | Flag *delayed* above this delay while the relay was connected (default `600`) |
| `FACE_MODELS_DIR` | Where the OpenCV face models are stored (default `models`) |

### Admin login lock

Failed admin logins are counted in a row, from any address. At the 5th (`LOGIN_MAX_FAILURES`) the server
creates the file `admin_login.lock` (next to `.env`, or `ADMIN_LOCK_FILE`), listing the time and address of
each attempt, and logs an error. While the file exists **nobody can log in, not even with the right
password**. It is never removed automatically: to unlock, on the main server, delete the file

```bash
sudo rm /var/lib/in-time/main-server/admin_login.lock      # installed with install_main_server.sh
```

or run `flask --app main unlock-admin` (prints the attempts, then unlocks). A successful login resets the
count. If the file can't be written, the counter in the database keeps the login locked until
`unlock-admin` is run.

### Relay IP behind a reverse proxy / Cloudflare

The allowed-IP check needs the relay's real (public) IP:

| Setup | `.env` |
|---|---|
| No proxy | `TRUSTED_PROXIES=0`, `CF_PROXY=false` |
| nginx / Apache / Traefik on the same host | `TRUSTED_PROXIES=1` (proxy must send `X-Forwarded-For`) |
| Cloudflare → app, or Cloudflare Tunnel | `CF_PROXY=true` |
| Cloudflare → nginx → app | `CF_PROXY=true` and `TRUSTED_PROXIES=1` |

With `CF_PROXY=true` the IP comes from the `CF-Connecting-IP` header, accepted only when the request
arrives from a Cloudflare IP range or a `TRUSTED_PEERS` address (add the address of a cloudflared tunnel
or proxy running on another machine or in Docker); a forged header sent straight to the server is refused.
`X-Forwarded-For` is likewise only read from `TRUSTED_PEERS`. With nginx in between, also set `TRUSTED_PROXIES=1` so requests that bypass Cloudflare are
detected too (or firewall the server to Cloudflare's ranges). The IP seen for each relay is shown on the
Relays page under *Last seen*.

**Local relays.** A relay on the same network as the main server can be marked *Local relay* (Relays page).
Its IP is then the address that opened the connection: `TRUSTED_PROXIES` and `CF_PROXY` are ignored for it,
and forwarded headers are never trusted, so it must connect directly to the app port (not through the proxy),
e.g. gunicorn listening on the LAN (`-b 0.0.0.0:8000`) with its LAN IP in *Allowed IPs*.

Cloudflare's ranges live in `main_server/cf_ips.txt`. Keep them current with `python update_cf_ips.py`:
it rewrites the file only when Cloudflare's list changed (`--check` only reports), and the server reloads
the file on its own. The install script schedules it every 2 hours (`/etc/cron.d/in-time-cf-ips`,
log in `/var/lib/in-time/cf_ips_update.log`). For a manual installation, example cron job (every 2 hours):

```
17 */2 * * * cd /path/to/in-Time/main_server && .venv/bin/python update_cf_ips.py >> cf_ips_update.log 2>&1
```

### Relay (`relay_server/relay.ini`)

See [`relay.ini.example`](relay_server/relay.ini.example): main server URL and token, sync interval,
port, duplicate window, data folder, the `[face]` section (`min_similarity`, 0.363 minimum, higher = stricter; the main server's
`FACE_MIN_SIMILARITY` applies when it is higher) and the `[time]` section (site `timezone`, `check_interval`).

**Relay ID and relay key.** Each relay has a stable ID derived from the machine (stored in
`data/server.id` and recreated with the same value if the file is deleted). On first start it also creates
its own key pair (Ed25519, private key in `data/secrets.bin`). The relay sends its machine ID
(`X-Relay-Id`) and public key (`X-Relay-Key`) with every request; the main server records both with the
first request made with a token, and from then on only accepts requests and check-ins from that token
that are signed with the matching private key. A stolen token, or a machine ID copied to another
computer, is therefore useless without the relay's key.

The main server binds each relay **token** to the first relay that uses it and refuses the token from any
other relay (`token_in_use`), and a relay to a single token (`relay_id_in_use`). Generate a new token
(Relays page) to move it to a replacement relay, or after a relay lost its `data/secrets.bin`: this frees
the recorded machine ID and key.

## Relay API

`/api/v1`, header `Authorization: Bearer <token>`:
`GET /ping`, `GET /settings`, `GET /users`, `GET /users/<id>/photo`, `POST /checkins` (idempotent by uuid;
each check-in carries its snapshot, which the main server checks against the user's photo). Relays must
send their machine ID in the `X-Relay-Id` header and their Ed25519 public key in `X-Relay-Key`: a token is
accepted from only one relay server, bound at its first request. Every request is signed: `X-Relay-Time` (Unix time,
within 5 min of the main server's clock) and `X-Relay-Signature` (Ed25519 over method, path, time and
SHA-256 of the body). Check-ins carry `seq`, `snapshot_sha256` and `signature` (made when the check-in was
created).

## Contributing

in-Time is open source under the AGPL-3.0. Bug reports and feature requests go through GitHub issues; changes go through pull
requests using the templates in `.github/`. By opening a pull request you agree that your contribution is
licensed under the AGPL-3.0. Never include real configuration, photos, snapshots
or databases.

## License

Copyright (C) 2026 Dijey.

in-Time is free software: you can redistribute it and/or modify it under the terms of the
**GNU Affero General Public License, version 3** ([`LICENSE`](LICENSE)). It is distributed in the hope that it
will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE.

In short: you may use, study, modify and share it, including commercially. If you distribute it, or let
people use a modified version over a network (for example a hosted main server), you must offer them the
complete source code of that version under the same license. The admin console links to the source
(`SOURCE_URL` in `.env`); point it to your own repository if you run a modified version.

Third-party components keep their own licenses: face-api.js (downloaded at run time) is © Vladimir Mandic
and contributors, MIT License; OpenCV, Apache 2.0, and its face models YuNet (MIT) and SFace (Apache 2.0);
Qt for Python (PySide6), LGPL-3.0.
