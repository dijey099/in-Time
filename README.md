# in-Time

Face-recognition attendance system for multi-site organisations. Employees check **in** and **out**
by looking at a phone camera on their site's Wi-Fi, with the phone's GPS location recorded. Each site runs a small **relay server** that keeps
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

**Web admin console** (main server): dashboard with today's check-ins, collaborators, companies, relays and settings.

![Web admin console](main.png)

**Relay GUI** (one per site): QR code for phones, the relay's public IP, trusted clock, internet and main server status, and today's check-ins.

![Relay GUI](relay.png)

## Features

- **Face recognition on the relay**: the phone only takes the picture; the relay identifies the person (OpenCV YuNet + SFace), so a check-in can't be forged by sending a request with someone's ID.
- **Face check on the main server**: every check-in snapshot is compared again with the collaborator's photo; mismatches, missing faces and missing snapshots are flagged on the dashboard for review.
- **GPS location required**: the phone page starts only when location is allowed and on, every check-in carries the phone's location (signed by the relay), and check-ins made **outside the site area** (a circle picked on a map for each relay) get a warning on the dashboard. They are still recorded.
- **Tamper-proof time**: check-in times come from Google over HTTPS, never from the relay PC's clock, so changing the Windows date/time has no effect.
- **Main-server outages tolerated**: the relay caches collaborators and settings, decides OK/KO locally and syncs check-ins when the main server is reachable again. Without **internet**, new check-ins are refused (no trusted time).
- **Late / early detection**: IN after *max in time* or OUT before *min out time* is flagged KO. Global values with per-site overrides.
- **Relay window**: today's check-ins with OK/KO, sync state per row (blue cloud = synced, grey = not synced), main server status (Reachable / Unreachable / Error), QR code for phones, the relay's public IP.
- **Admin console**: companies and their departments, collaborators (company and department, photo upload or camera capture, face check), relays (tokens, allowed IPs, site area on a map), settings, attendances (check-in history) with filters and CSV export, snapshot and location of every check-in. A **Logs** page records every action made in the console (who, when, from where, what changed), exports included. Console **users, groups and roles** (Viewer, Operator, Technician, Administrator), with local, LDAP or Active Directory authentication. Works on phones too (compact menu).
- **Relay authentication**: bearer token + allowed IP/CIDR list + per-relay signing key; works behind nginx/Apache/Traefik and Cloudflare.
- **Open source (AGPL-3.0) and self-hosted**: no license key, no activation, no limit on collaborators or sites.

## Repository layout

| Folder | Contents |
|---|---|
| `main_server/` | Public API for relays + admin web console (Flask, PostgreSQL) |
| `main_server/postgres/` | PostgreSQL 16 for the main server (Docker Compose) |
| `relay_server/` | Relay: phone web client, private LAN API, sync worker, Qt window (Flask, PySide6, SQLite) |

## How it works

1. **Admin** creates companies and their departments on the **Companies** page, then adds collaborators (ID, full name,
   company, one of that company's departments, photo) on the main server. The server checks that the
   photo has one usable face and keeps its face data as the reference. The photo is saved as `photos/<ID>.jpg|png`.
2. **Relays** sync every 30 s: ping, settings, collaborators and their photos. Each relay computes the face data
   of new or changed photos itself.
3. **Phones** on the site's Wi-Fi open `https://<relay-ip>:8443`. The page first asks for the phone's
   location and starts the camera only once it has a GPS fix (see [GPS location](#gps-location-and-site-area)).
   The phone only detects that a face is in
   front of the camera, then sends the picture and its location to the relay, which recognises the person. The person sees
   their name, IN/OUT is suggested (alternating during the day) and sent after 4 s unless they tap the
   other option.
4. The **relay** accepts the check-in only with the one-time token it issued for that picture, stamps it
   with the trusted (internet) time and the phone's location, shows OK/KO, keeps the picture as the snapshot, stores it in SQLite
   and pushes it to the main server immediately, or later if the main server is unreachable.
5. The **main server** recomputes OK/KO itself (site time zone and settings), compares the snapshot with the
   collaborator's photo again and flags the check-in if they don't match, if the picture is a replay, or if it
   arrived suspiciously late (see [Face check](#face-check-and-anti-fraud)). It also compares the location
   with the relay's site area and warns about check-ins made outside it.

### Face check and anti-fraud

The phone never decides who is checking in, and never receives anyone's face data or photo list:

- `POST /api/identify` (picture) → the relay finds the face and compares it with every collaborator's photo.
  When it matches, it returns the name and a **one-time token** valid 90 s for that phone only.
- `POST /api/checkins` (token, IN/OUT) → accepted only with that token; the stored snapshot is the
  picture the relay recognised. Requests with a collaborator ID, a reused or a fake token are refused.
- Identification requests are rate-limited per phone (15 per 10 s).

On the main server, every snapshot received is compared with the collaborator's photo. The **Face check** column
of the check-ins shows *match*, or flags *mismatch*, *no face*, *no snapshot* or
*replay* (the snapshot is the collaborator's reference photo, practically identical to it, or a picture already
used for another check-in). Hover the result to see the similarity as a percentage (e.g. *72% similar*);
the dashboard counts flagged check-ins of the day and the Attendances page can filter them. Flagged
check-ins are still recorded: review the snapshot and decide. Check-ins received while the models were
loading are checked by a background pass within a minute; `flask --app main verify-checkins` runs it on
demand (`--all` re-checks everything, e.g. after changing `min_similarity` in `[face]`).

The main server trusts the relay as little as possible:

- **OK/KO** is computed on the main server from the time, the site's time zone and the settings. The time
  zone is the relay's own (Relays page, or pinned at its first check-in), never one sent with the check-in.
  When the relay reported something else, the Status column shows it (*relay: OK*).
- Every check-in is **signed by the relay** when it is made (with the trusted time, the collaborator, IN/OUT and the
  SHA-256 of its snapshot), with a private key that never leaves the relay. A check-in created or changed
  anywhere else (relay database edited, stolen relay token) is rejected (`bad_signature`).
- Check-ins are **numbered** by the relay. A missing or reused number means check-ins were deleted on the relay
  before they reached the main server: the next one is flagged **gap**.
- A timestamp in the **future** is refused. A check-in received more than 10 min after it was made while
  the relay was connected is flagged **delayed** (it may have been held back before syncing).
  Check-ins made while the main server was unreachable are not flagged.
- Snapshots must be JPEG images; check-in IDs must be UUIDs (they name the snapshot files).
- The check-in's **GPS location** is part of what the relay signs: removing or changing it on the relay PC
  makes the check-in rejected (`bad_signature`).

Limits: there is no liveness detection, so holding a printed photo or a screen in front of the camera can
still fool it; every check-in keeps its snapshot for review. Photos, snapshots and face data are
**biometric personal data**: keep them out of version control (see `.gitignore`), restrict access, and
check the data-protection rules that apply to you.

### GPS location and site area

**On the phone.** The check-in page asks for the phone's location before it starts the camera. Without
it, nothing can be checked in: the page explains what to do when location is **blocked** for the site
(allow it in the browser settings, then reload) or **switched off** (turn on GPS / location services),
and retries on its own. A location older than 60 s, or lost during use, stops recognition until a new
fix arrives. The relay also refuses `POST /api/identify` and `POST /api/checkins` without a valid location,
so the rule can't be bypassed by skipping the page.

**With the check-in.** The relay stores the latitude, longitude and accuracy (metres) reported by the phone
in the check-in and signs them with it. The main server saves them; the **Location** column of the
check-ins links to the place on OpenStreetMap, and the CSV export has `latitude`, `longitude`,
`location_accuracy_m`, `area_check` and `distance_from_site_m`.

**Site area.** Each relay has a site area: a circle (centre + radius, 10 m to 50 km) picked on a map
when the relay is created (required) or later with **Edit** on the Relays page. Search an address, use
the browser's location, or click the map, then set the radius. A check-in is *outside* when it is
farther from the centre than the radius, even giving the phone's reported GPS accuracy the benefit of the
doubt (up to 100 m, so a huge reported accuracy can't hide a far-away check-in). Location never refuses a
check-in:

| Location column | Meaning |
|---|---|
| *on site* | inside the area |
| *outside* (+ distance beyond its edge) | outside the area: counted in **outside site area** on the dashboard, listed in its warning, filterable on Attendances (*Location → outside site area*) |
| *no location* | the relay has an area but sent no location (relay older than this feature) |
| *map* | location recorded, but the relay had no area when the check-in arrived |

The area is compared when the check-in arrives; changing an area later doesn't re-check older check-ins.
The dashboard also lists enabled relays without an area.

The map uses OpenStreetMap's volunteer-run tile servers and Nominatim address search by default. Map
requests send the console's origin as `Referer`, as their usage policy requires (the rest of the console
sends no referrer). For heavier use, set another provider with `tile_url` / `tile_attribution` in `[map]`
(see [Configuration](#main-server-main_serverenv)).

### Trusted time and internet check

The relay reads the time from the HTTPS `Date` header of Google, Cloudflare or Microsoft (built into the
relay, not configurable; certificates are checked against the bundled CA list) every 60 s. Between two checks it counts with the system's monotonic clock, which does not move when someone
changes the Windows clock, so a changed PC date/time has no effect on check-ins (the relay window only
shows a warning). If the time check fails, the relay shows a red banner, the phones show
"check-in unavailable", and new check-ins are refused until the connection is back (retried every 10 s).
If the PC date is wrong by months, the secure connection itself fails and the relay asks to fix the date.

The site's time zone is set in `relay.conf` (`[time] timezone`), so changing the Windows time zone has no
effect either. Without it the PC time zone is used and the relay window shows a warning.

| Situation | Check-ins |
|---|---|
| Internet OK, main server OK | accepted, synced immediately |
| Internet OK, main server down | accepted, synced when it is back (grey cloud, blue once synced) |
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

The script creates `main.conf` with a random `secret_key` and admin password (printed at the end). **Run it again to
update:** it detects the existing installation first (no PostgreSQL question), saves `main.conf` to
`/var/lib/in-time/conf-backup-<date>`, replaces the code, keeps `main.conf`, photos, snapshots and models, updates `.venv` (rebuilt if broken or made with another Python version) and restarts the
service. If the update fails, the previous version is put back. Set `IN_TIME_BRANCH`
to a release tag and `IN_TIME_SHA256` to its archive checksum to install a verified version.

**Manually:**

```bash
cd main_server
cd postgres && cp .env.example .env && docker compose up -d && cd ..   # set POSTGRES_PASSWORD first
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp main.conf.example main.conf   # set secret_key, the admin password (and the database url)
python main.py                # development: http://127.0.0.1:8000 (FLASK_DEBUG=1 for the debugger)
# production (behind nginx + HTTPS):
gunicorn -w 4 -b 127.0.0.1:8000 main:app
```

The server refuses to start while `secret_key` (32+ characters) or the admin `password` (12+ characters) in
`main.conf` is empty or a default value. Tables are created (and new columns added) automatically. Log in, create your
companies and their departments on the **Companies** page (collaborators can only be added once their company has a
department), then **Relays → New relay**: enter the public IP(s) the site connects from (CIDR allowed, `*` = any, for
testing only; the relay window shows its public IP), pick the site area on the map, and copy the token,
which is shown once. `flask --app main create-relay "SITE A" --ips 203.0.113.10` does the same from the
shell (set the area afterwards with **Edit**).

The **Relays** page lists each relay's name, allowed IPs, time zone and last seen time (with an
online/offline dot and the IP it connects from). **Edit** opens all its settings in a pop-up: name, allowed IPs,
time zone, max in / min out times, enabled, local relay, site area and **Generate new token**.

The **Companies** page lists each company with its departments and their number of collaborators. Companies and
departments can be renamed; one that still has collaborators can't be deleted (move its collaborators first). On the collaborator
page, the Department list only offers the departments of the selected company, and the server refuses a
department of another company. The Collaborators page filters by company and department, and the check-ins CSV
export has `company` and `department` columns.

On a phone or small tablet (860 px wide or less), the console's pages are in the menu button at the top
right, which shows the current page.

The camera option on the collaborator page needs the admin console to be served over HTTPS (or `localhost`).

The **Logs** page lists every action made in the console, newest first: logins (and failed or refused
ones), logouts, collaborators created, edited, deactivated or deleted, photo checks, companies and
departments created, renamed or deleted, relays created, edited, disabled or deleted, new relay tokens,
settings changes, and CSV exports of the attendances and of the logs themselves. Each entry shows who made it
(the console login name), when, from which IP address, the target, and what changed (old → new value).
Filter by period, user, action or text, and export to CSV. Entries are saved in the same transaction as
the action and can't be edited or deleted from the console (table `audit_logs`); relay tokens and
passwords are never logged.

### Console users, groups and roles

Who can do what in the console is set on **Settings**:

| Role | Can |
|---|---|
| **Viewer** | see the Dashboard and Attendances, and export attendances |
| **Operator** | Viewer, plus see and update Collaborators and Companies |
| **Technician** | see the Dashboard and Attendances (no export), see and update Relays, change Settings > General and Authentication, and see (read only) Settings > Users, Groups and Roles |
| **Administrator** | everything, including managing Settings > Users, Groups and Roles, and the Logs |

A person with several roles (their own and their groups') may do what any of them allows.

- **Users** (Settings > Users): the accounts of the authentication source in use (accounts of another source are
  hidden). **Local**: accounts created here, password hashed here (12+ characters with letters and digits); each
  person can change it (click their name, top right). **LDAP / Active Directory**: the directory's accounts are
  read from the server with their groups (when the source is saved, then again every 10 minutes when the Users or
  Groups page is opened, or with the sync button (⟳)), so they are listed with their role before their first
  login. Their password is checked by the directory. AD accounts disabled in the directory are not added; an
  account removed from the directory is flagged and can be deleted. A user's role is the highest of its own role
  and its groups' roles.
- **Groups** (Settings > Groups): with **Local** authentication, groups are created here and members added on
  the Users page. With **LDAP** or **Active Directory**, the page lists the **directory's groups**, read from the
  server (when the source is saved, then again every 10 minutes when the page is opened, or with the sync
  button (⟳)); just choose a role for each group. Members are managed in the directory: an account gets the roles of
  its groups at every login (nested AD groups included). Groups are also added when their members log in, even
  before anyone has a role. A group removed from the directory is flagged and can be deleted.
- **Authentication** (Settings > Authentication): one **authentication source**, chosen in a drop-down; only its
  accounts can log in. **Local** (default, nothing to set up): accounts created on the Users page. **LDAP**:
  server URL, LDAPS / StartTLS, service account, user and group search. **Active Directory**: domain controller URL,
  domain, a reader account (any domain user) to list users and groups, and optional users / groups base DNs; people log in as *alice*, *DOMAIN\\alice* or *alice@domain*. Only the chosen source's settings are
  shown; **Test** checks them before they are saved. With a test username, it also lists every attribute of that
  account and its values, to pick the first name, last name, e-mail (and, for LDAP, login) attributes in one click
  (password attributes are hidden). The console shows the full name as First name + Last name (default `givenName`
  and `sn`; `cn`, or `displayName` on AD, when both are empty). Accounts added to the directory since the last read can log in
  at once when one of their groups gives them a role. Accounts of another source stay listed but can't log in until that
  source is chosen again. The built-in administrator (`username` / `password` in `[admin]` of `main.conf`) always logs
  in, whatever the source. The service account password is stored encrypted with `secret_key` (enter it again if
  `secret_key` changes).
- **Roles** (Settings > Roles): the permission matrix and how many users and groups have each role.

Changes apply at once, even to people already logged in (their role is read at every page). The built-in
administrator of `main.conf` always works: it can't be turned off, disabled or locked from the console, so the
console can always be administered. You can't make a change that removes your own access. Pages and actions a role
doesn't allow are refused, and the attempt is recorded in the Logs. Wrong passwords lock the account they were
typed for, not the whole console (see [Account lock](#account-lock)).

### Relay server (Windows / Linux)

1. Copy `relay_server/relay.conf.example` to `relay.conf` and set the main server `url` and `token`,
   and the site `[time] timezone`.
   At start-up the token is moved to `data/secrets.bin` (encrypted with Windows DPAPI
   for the account running the relay, and with a key tied to this computer and built into the relay) and
   removed from `relay.conf`; type a new value there to replace it.
   The `data/` folder is restricted to that account, SYSTEM and Administrators.

   **Encrypted local cache.** The relay's cache (collaborators, face data, check-ins and settings in `data/relay.db`,
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

**Executable.** [Download the relay server](https://drive.google.com/drive/folders/1LGi-ymOp_Rc3_-FmAAgkOsDYD4yjZSgU?usp=sharing), then put `start_relay.exe` (Windows) or `start_relay` (Linux) in the folder with `relay.conf`
and double-click it. Its `data/` folder is created next to it. Double-clicked, the Windows executable hides
its console and only the relay window shows. Run from a terminal (`start_relay.exe --no-gui`), it runs
headless and prints the log there. The log is always in `data/relay.log`, and start-up errors (missing
`relay.conf`, port already in use...) are also shown in a message box.

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
allow the camera and location over HTTPS): accept it once, then allow the camera and the location. The certificate is regenerated if the relay's LAN IP
changes, so give the relay PC/Server a fixed IP or DHCP reservation.

**Relay window.** Next to the QR code, the window shows the relay's **public IP** as seen from the
internet (from `https://ifconfig.me/ip`, refreshed every 10 min): the address to put in the relay's
*Allowed IPs* on the main server. The **Main server** column of the check-ins shows a blue cloud when the
check-in is synced and a grey cloud when it isn't yet (hover for the sync time or the reason it was
refused; **Retry rejected** sends refused ones again).

## Configuration

### Main server (`main_server/main.conf`)

An INI file next to `main.py`, with sections; copy [`main.conf.example`](main_server/main.conf.example) (every setting is
explained there). Keep it private (`chmod 600`) and restart the service after a change. A missing line takes the
default value.

| Section | Setting | Purpose |
|---|---|---|
| `[security]` | `secret_key` | **Required.** Session key: a long random value (32+ characters) |
| | `session_cookie_secure` | `true` when the console is served over HTTPS |
| `[database]` | `url` | PostgreSQL URL |
| `[admin]` | `username`, `password` | Built-in administrator of the console: always an Administrator, always able to log in (it can't be turned off, disabled or locked). **Required:** password of 12+ characters |
| | `max_failures`, `lock_minutes` | Account lock: this many wrong passwords (default 5) for the same account within this many minutes (default 15) lock it (see [Account lock](#account-lock)) |
| `[server]` | `bind`, `workers` | Address and number of processes of Gunicorn (systemd service; default `127.0.0.1:8000`, 2) |
| | `trusted_proxies` | Number of reverse proxies in front of the app |
| | `trusted_peers` | Addresses of those proxies (default `127.0.0.1/32, ::1/128`): forwarded headers from anyone else are ignored |
| | `cloudflare` | `true` when Cloudflare is in front |
| | `cloudflare_ips_file` | Cloudflare ranges file (default `cf_ips.txt`) |
| `[storage]` | `photos_dir`, `snapshots_dir`, `face_models_dir` | Where collaborator photos, check-in snapshots and the OpenCV face models are stored |
| `[face]` | `min_similarity` | Face check: similarity (0-1) between snapshot and photo required for a match (default `0.363`, higher = stricter) |
| | `replay_similarity` | A snapshot at least this similar to the photo is flagged *replay* (default `0.97`) |
| `[checkins]` | `max_delay_seconds` | Flag *delayed* above this delay while the relay was connected (default `600`) |
| `[map]` | `tile_url`, `tile_attribution` | Map tiles of the site area picker (default: OpenStreetMap, fine for a few admins). Use another provider, with its attribution, for heavier use |
| | `search_url` | Address search of the map (Nominatim API, default OpenStreetMap's). Empty: no search box, the site is clicked on the map |
| `[about]` | `source_url` | Source code link in the admin console footer (default: this repository). Set it to your published source if you run a modified version (AGPL-3.0) |

**`main.conf` is required**: without it the main server does not start. It is the only place the settings come
from (`.env` is no longer read). **Upgrading from a version configured with `.env`:** create `main.conf` from
`main.conf.example` and copy your values into it (e.g. `SECRET_KEY` → `secret_key` in `[security]`, `DATABASE_URL` →
`url` in `[database]`, `ADMIN_PASSWORD` → `password` in `[admin]`, `SERVER` / `WORKERS` → `bind` / `workers` in
`[server]`), then delete `.env`; the install script stops until `main.conf` exists. The PostgreSQL container keeps
its own `postgres/.env` (read by Docker Compose).

Companies and departments are not set in `main.conf`: they are managed on the **Companies** page of the admin console
(a company has any number of departments; every collaborator belongs to one company and one of its departments).
Upgrading from a version without companies: at the first start, the departments of existing collaborators are put
in a company named *Default company* (rename it on the Companies page) and existing collaborators are assigned to it.

Upgrading from a version where the people who check in were called *users*: at the first start the main server
renames the `users` table to `collaborators` and `checkins.user_id` to `collaborator_id` (data, indexes and
constraints kept). The relay API changed with it (`/api/v1/collaborators`, `collaborator_id`, check-in signature
v2): **update every relay** along with the main server, since an older relay can no longer sync. Its first start
updates its local cache and re-signs the check-ins not sent yet, so none are lost.

### Account lock

Wrong passwords are counted **per account**: someone typing wrong passwords for one account never blocks the
others. After `max_failures` (5) wrong passwords for the same account within `lock_minutes` (15) (`[admin]` of `main.conf`):

- a **console account** (local, LDAP or Active Directory) is **locked**: nobody can log in with it, not even
  with the right password, until an Administrator clicks **Unlock** on Settings > Users (the account shows
  *locked*), or runs `flask --app main unlock-user <name>` on the server;
- the **built-in administrator** of `main.conf` is never locked, so it always works: it is only **paused** for
  `lock_minutes`, then works again (`flask --app main unlock-admin` ends the pause at once);
- a name without an account yet (an LDAP / AD person before their first login) is paused the same way.

Only wrong passwords count: a login refused because the account has no role or is disabled, or because the
directory server is unreachable, doesn't. A successful login resets the account's count. Locks, unlocks and
refused logins are recorded in the Logs. (Older versions locked the whole console with an `admin_login.lock`
file: it is removed at the first start.)

### Relay IP behind a reverse proxy / Cloudflare

The allowed-IP check needs the relay's real (public) IP:

| Setup | `[server]` of `main.conf` |
|---|---|
| No proxy | `trusted_proxies = 0`, `cloudflare = false` |
| nginx / Apache / Traefik on the same host | `trusted_proxies = 1` (proxy must send `X-Forwarded-For`) |
| Cloudflare → app, or Cloudflare Tunnel | `cloudflare = true` |
| Cloudflare → nginx → app | `cloudflare = true` and `trusted_proxies = 1` |

With `cloudflare = true` the IP comes from the `CF-Connecting-IP` header, accepted only when the request
arrives from a Cloudflare IP range or a `trusted_peers` address (add the address of a cloudflared tunnel
or proxy running on another machine or in Docker); a forged header sent straight to the server is refused.
`X-Forwarded-For` is likewise only read from `trusted_peers`. With nginx in between, also set `trusted_proxies = 1` so requests that bypass Cloudflare are
detected too (or firewall the server to Cloudflare's ranges). The IP seen for each relay is shown on the
Relays page under *Last seen*.

**Local relays.** A relay on the same network as the main server can be marked *Local relay* (Relays page).
Its IP is then the address that opened the connection: `trusted_proxies` and `cloudflare` are ignored for it,
and forwarded headers are never trusted, so it must connect directly to the app port (not through the proxy),
e.g. gunicorn listening on the LAN (`-b 0.0.0.0:8000`) with its LAN IP in *Allowed IPs*.

Cloudflare's ranges live in `main_server/cf_ips.txt`. Keep them current with `python update_cf_ips.py`:
it rewrites the file only when Cloudflare's list changed (`--check` only reports), and the server reloads
the file on its own. The install script schedules it every 2 hours (`/etc/cron.d/in-time-cf-ips`,
log in `/var/lib/in-time/cf_ips_update.log`). For a manual installation, example cron job (every 2 hours):

```
17 */2 * * * cd /path/to/in-Time/main_server && .venv/bin/python update_cf_ips.py >> cf_ips_update.log 2>&1
```

### Relay (`relay_server/relay.conf`)

Relays of older versions read `relay.ini`: rename it `relay.conf` (same content) when you update the relay, or it
won't start.

See [`relay.conf.example`](relay_server/relay.conf.example): main server URL and token, sync interval,
port, duplicate window, data folder, the `[face]` section (`min_similarity`, 0.363 minimum, higher = stricter; the main server's
`min_similarity` applies when it is higher) and the `[time]` section (site `timezone`, `check_interval`).

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
`GET /ping`, `GET /settings`, `GET /collaborators`, `GET /collaborators/<id>/photo`, `POST /checkins` (idempotent by uuid;
each check-in carries its snapshot, which the main server checks against the collaborator's photo). Relays must
send their machine ID in the `X-Relay-Id` header and their Ed25519 public key in `X-Relay-Key`: a token is
accepted from only one relay server, bound at its first request. Every request is signed: `X-Relay-Time` (Unix time,
within 5 min of the main server's clock) and `X-Relay-Signature` (Ed25519 over method, path, time and
SHA-256 of the body). Check-ins carry `seq`, `snapshot_sha256`, `location` (`{lat, lng, accuracy}`, the
phone's GPS location) and `signature` (made when the check-in was created; `location` is signed when present).

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
(`source_url` in `[about]` of `main.conf`); point it to your own repository if you run a modified version.

Third-party components keep their own licenses: face-api.js (downloaded at run time) is © Vladimir Mandic
and contributors, MIT License; Leaflet (map of the admin console, `main_server/app/static/leaflet/`),
BSD-2-Clause; map data © OpenStreetMap contributors (ODbL); OpenCV, Apache 2.0, and its face models YuNet (MIT) and SFace (Apache 2.0);
Qt for Python (PySide6), LGPL-3.0.
