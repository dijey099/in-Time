#!/bin/sh
# in-Time main server installer: deploys main_server as a systemd service.
#
#   curl -sSL https://raw.githubusercontent.com/dijey099/in-Time/refs/heads/master/main_server/install_main_server.sh | sh
#
# Installs into /var/lib/in-time/main-server, runs as the system user "in-time" (home /var/lib/in-time)
# with the service in-time-main. Running it again detects the existing installation first and
# updates it: the code is replaced, the Python environment (.venv) is updated, and .env (backed up
# first), photos, snapshots, face models and the database are kept. If the update fails, the
# previous version is put back.
#
# Options (environment variables):
#   IN_TIME_BRANCH  branch or tag            (default master; prefer a release tag)
#   IN_TIME_SHA256  expected SHA-256 of the downloaded archive: the install stops if it differs
#   IN_TIME_GITHUB_TOKEN  GitHub token, only for a private fork of the repository
#   IN_TIME_SRC     install from this local main_server folder instead of downloading
#   IN_TIME_POSTGRES  "existing" (a PostgreSQL server is ready) or "install" (install it here):
#                     answers the PostgreSQL question of a new installation, needed when there is
#                     no terminal (not asked when updating an existing installation)
set -eu

REPO="${IN_TIME_REPO:-dijey099/in-Time}"
BRANCH="${IN_TIME_BRANCH:-master}"
BASE=/var/lib/in-time
APP="$BASE/main-server"
SVC_USER=in-time
SERVICE=in-time-main
UNIT="/etc/systemd/system/$SERVICE.service"
CRON_FILE=/etc/cron.d/in-time-cf-ips
# Kept when updating an existing installation (admin_login.lock: a locked admin login stays locked)
KEEP=".env .venv photos snapshots models app/static/vendor cf_ips.txt admin_login.lock"
# Not needed to run the server
REMOVE="install_main_server.sh $SERVICE.service postgres __pycache__ venv .venv-build *.zip"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
else
    command -v sudo >/dev/null 2>&1 || die "run this script as root or install sudo"
    SUDO="sudo"
    say "Administrator rights are needed: sudo may ask for your password"
    { $SUDO -v </dev/tty || $SUDO -v; } 2>/dev/null || die "sudo failed"
fi

command -v systemctl >/dev/null 2>&1 || die "systemd is required (systemctl not found)"
command -v tar >/dev/null 2>&1 || die "tar is required"

# Tests on the installation run with administrator rights: its folders are only readable by the
# in-time user (750), so a plain [ -e ... ] run as another user would see nothing.
ptest() { $SUDO test "$@"; }

# ------------------------------------------------------- existing installation
EXISTING=no
if ptest -f "$APP/main.py" || ptest -f "$APP/.env" || [ -f "$UNIT" ]; then
    EXISTING=yes
fi
if [ -f "$UNIT" ]; then
    UNIT_DIR="$(sed -n 's/^WorkingDirectory=//p' "$UNIT" | head -n 1)"
    if [ -n "$UNIT_DIR" ] && [ "$UNIT_DIR" != "$APP" ]; then
        die "the $SERVICE service runs from $UNIT_DIR, not $APP: this script only updates installations in $APP"
    fi
fi
if [ "$EXISTING" = yes ]; then
    ptest -f "$APP/.env" || die "an installation exists in $APP but has no .env: restore it (or remove $APP) first"
    say "Existing in-Time main server found in $APP: it will be updated"
    echo "    kept: .env (a backup is made first), photos, snapshots, face models, database"
    echo "    updated: code and Python environment (.venv)"
else
    # another server already on port 8000 (e.g. a manual installation started by hand)
    if command -v ss >/dev/null 2>&1 && ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq '(^|[:.])8000$'; then
        warn "port 8000 is already in use on this machine: if it is another in-Time main server (manual installation), stop it, or set SERVER in .env to another port"
    fi
fi

# ---------------------------------------------------------------- packages
# apt can be locked by automatic updates (unattended-upgrades), often right after a server is
# created: wait for it (up to 15 min) instead of failing.
APT_UPDATED=no
apt_run() {  # apt-get arguments...
    tries=0
    while :; do
        if out="$($SUDO env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
                apt-get -o DPkg::Lock::Timeout=30 -qq "$@" 2>&1)"; then
            return 0
        fi
        case "$out" in
            *"Could not get lock"*|*"Unable to acquire"*|*"Unable to lock"*)
                tries=$((tries + 1))
                [ "$tries" -le 30 ] || break
                [ "$tries" -eq 1 ] && say "Waiting for another package installation to finish (automatic updates)..."
                sleep 15
                ;;
            *) break ;;
        esac
    done
    printf '%s\n' "$out" >&2
    return 1
}
apt_install() {  # packages...
    if [ "$APT_UPDATED" = no ]; then
        apt_run update || die "apt-get update failed"
        APT_UPDATED=yes
    fi
    apt_run install -y "$@"
}

# --------------------------------------------------------------- postgresql
# The question is read from the terminal: stdin is the script itself with "curl ... | sh".
ask_yes_no() {  # $1 = question; returns 0 for yes
    while :; do
        printf '%s [y/n] ' "$1" >/dev/tty
        read -r answer </dev/tty || return 1
        case "$answer" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo]) return 1 ;;
        esac
    done
}

PG_INSTALL=no
if [ "$EXISTING" = no ] || [ "${IN_TIME_POSTGRES:-}" = install ]; then
cat <<EOF

The in-Time main server stores its data in PostgreSQL (version 12 or newer).
It can use an existing PostgreSQL server or this script can install 
PostgreSQL here and create the "in-time" database for you.

EOF
if command -v psql >/dev/null 2>&1 || [ -d /etc/postgresql ] || [ -d /var/lib/pgsql ]; then
    echo "(PostgreSQL seems to be installed on this machine already.)"
fi
case "${IN_TIME_POSTGRES:-}" in
    existing) PG_INSTALL=no ;;
    install) PG_INSTALL=yes ;;
    "")
        if ! (: </dev/tty) 2>/dev/null; then
            die "no terminal to ask: run again with IN_TIME_POSTGRES=existing or IN_TIME_POSTGRES=install"
        fi
        if ask_yes_no "Do you already have a working PostgreSQL server?"; then PG_INSTALL=no; else PG_INSTALL=yes; fi
        ;;
    *) die "IN_TIME_POSTGRES must be 'existing' or 'install'" ;;
esac
fi  # new installation

if [ "$PG_INSTALL" = yes ]; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing PostgreSQL (apt)"
        apt_install postgresql || die "could not install PostgreSQL"
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        PKG="$(command -v dnf || command -v yum)"
        say "Installing PostgreSQL ($(basename "$PKG"))"
        # on Fedora/RHEL the "postgresql" package is only the client: the server is postgresql-server
        $SUDO "$PKG" install -y -q postgresql-server postgresql >/dev/null || die "could not install PostgreSQL"
        if ! $SUDO test -f /var/lib/pgsql/data/PG_VERSION; then
            say "Initialising the database cluster"
            $SUDO postgresql-setup --initdb >/dev/null || die "postgresql-setup --initdb failed"
        fi
    else
        die "unsupported package manager: install PostgreSQL yourself, then run this script again and answer y"
    fi
    $SUDO systemctl enable --now postgresql >/dev/null 2>&1 \
        || die "could not start PostgreSQL (systemctl enable --now postgresql)"
    command -v psql >/dev/null 2>&1 || die "PostgreSQL installed but psql not found"
fi

# ------------------------------------------------------------------ python
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 is required (3.10 or newer). On Debian/Ubuntu: sudo apt install python3 python3-venv"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required ($("$PYTHON" -V 2>&1) found)"
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing python3-venv"
        apt_install python3-venv || die "could not install python3-venv"
    else
        die "the Python venv module is missing: install it with your package manager (python3-venv)"
    fi
fi

# ---------------------------------------------------------------- download
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
trap 'exit 130' INT TERM
NEW="$TMP/main_server"

if [ -n "${IN_TIME_SRC:-}" ]; then
    say "Copying $IN_TIME_SRC"
    [ -f "$IN_TIME_SRC/main.py" ] || die "$IN_TIME_SRC is not a main_server folder"
    cp -a "$IN_TIME_SRC" "$NEW"
else
    # GitHub API tarball: works for branches and tags (and for a private fork with a token)
    URL="https://api.github.com/repos/$REPO/tarball/$BRANCH"
    say "Downloading $REPO ($BRANCH)"
    if command -v curl >/dev/null 2>&1; then
        if [ -n "${IN_TIME_GITHUB_TOKEN:-}" ]; then
            curl -fsSL -H "Authorization: Bearer $IN_TIME_GITHUB_TOKEN" "$URL" -o "$TMP/src.tar.gz" \
                || die "download failed: $URL (check IN_TIME_GITHUB_TOKEN)"
        else
            curl -fsSL "$URL" -o "$TMP/src.tar.gz" \
                || die "download failed: $URL (private fork: set IN_TIME_GITHUB_TOKEN)"
        fi
    elif command -v wget >/dev/null 2>&1; then
        if [ -n "${IN_TIME_GITHUB_TOKEN:-}" ]; then
            wget -q --header="Authorization: Bearer $IN_TIME_GITHUB_TOKEN" -O "$TMP/src.tar.gz" "$URL" \
                || die "download failed: $URL (check IN_TIME_GITHUB_TOKEN)"
        else
            wget -qO "$TMP/src.tar.gz" "$URL" || die "download failed: $URL (private fork: set IN_TIME_GITHUB_TOKEN)"
        fi
    else
        die "curl or wget is required"
    fi
    if [ -n "${IN_TIME_SHA256:-}" ]; then
        command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required to check IN_TIME_SHA256"
        GOT="$(sha256sum "$TMP/src.tar.gz" | cut -d' ' -f1)"
        [ "$GOT" = "$IN_TIME_SHA256" ] || die "the downloaded archive does not match IN_TIME_SHA256 ($GOT)"
        say "Archive checksum verified"
    else
        warn "IN_TIME_SHA256 not set: the downloaded archive is not verified"
    fi
    mkdir "$TMP/src"
    tar -xzf "$TMP/src.tar.gz" -C "$TMP/src"
    TOP="$(find "$TMP/src" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    [ -f "$TOP/main_server/main.py" ] || die "main_server not found in the downloaded archive"
    mv "$TOP/main_server" "$NEW"
fi
[ -f "$NEW/$SERVICE.service" ] || die "$SERVICE.service missing from main_server"
cp "$NEW/$SERVICE.service" "$TMP/$SERVICE.service"

# Clean up: only what the server needs stays
for item in $REMOVE; do
    # shellcheck disable=SC2086  # $item may be a glob (*.zip)
    rm -rf "${NEW:?}"/$item
done
find "$NEW" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$NEW" -name '*.pyc' -delete
# Runtime data never comes from the source: keep it out of a fresh install too
rm -rf "$NEW/.env" "$NEW/photos" "$NEW/snapshots" "$NEW/models" "$NEW/app/static/vendor"

# -------------------------------------------------------------------- user
if ! id "$SVC_USER" >/dev/null 2>&1; then
    say "Creating system user $SVC_USER (home $BASE)"
    $SUDO useradd --system --home-dir "$BASE" --create-home --shell /usr/sbin/nologin \
        --comment "in-Time main server" "$SVC_USER"
fi
$SUDO mkdir -p "$BASE"

# --------------------------------------------------------- install / update
run_as_user() {  # run a command as the service user
    if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u "$SVC_USER" -- "$@"
    else
        sudo -u "$SVC_USER" "$@"
    fi
}

STAMP="$(date +%Y%m%d-%H%M%S)"
ENV_BACKUP=""
WAS_ACTIVE=no
ROLLBACK=no   # yes from the moment the service is stopped until the new version is in place
STAGE="$APP.new"  # new version prepared next to the installation (same disk: moves are instant)

rollback() {
    [ "$ROLLBACK" = yes ] || return 0
    ROLLBACK=no
    set +e
    warn "update failed: putting the previous version back"
    if ptest -d "$APP.old"; then
        for src in "$APP" "$STAGE"; do
            ptest -d "$src" || continue
            # data moved into the new version goes back to the old one (.venv: its snapshot below)
            for item in $KEEP; do
                [ "$item" = .venv ] && continue
                if ptest -e "$src/$item"; then
                    $SUDO rm -rf "${APP:?}.old/$item"
                    $SUDO mkdir -p "$APP.old/$(dirname "$item")"
                    $SUDO mv "$src/$item" "$APP.old/$item"
                fi
            done
            $SUDO rm -rf "$src"
        done
        if ptest -d "$BASE/venv.rollback"; then
            $SUDO rm -rf "$APP.old/.venv"
            $SUDO mv "$BASE/venv.rollback" "$APP.old/.venv"
        fi
        $SUDO mv "$APP.old" "$APP"
    else
        $SUDO rm -rf "$STAGE"
    fi
    if [ "$WAS_ACTIVE" = yes ]; then $SUDO systemctl start "$SERVICE"; fi
    warn "the previous version is back in $APP (copy of .env: $ENV_BACKUP)"
}
# shellcheck disable=SC2154  # status is set inside the trap
trap 'status=$?; rollback; rm -rf "$TMP"; exit $status' EXIT

if [ "$EXISTING" = yes ]; then
    ENV_BACKUP="$BASE/env-backup-$STAMP"
    $SUDO cp -p "$APP/.env" "$ENV_BACKUP"
    $SUDO chmod 600 "$ENV_BACKUP"
    say "Saved a copy of .env: $ENV_BACKUP"

    $SUDO rm -rf "$APP.old" "$STAGE" "$BASE/venv.rollback"
    $SUDO mv "$NEW" "$STAGE"   # the new code only (data never goes through $TMP)
    if $SUDO systemctl is-active --quiet "$SERVICE"; then
        WAS_ACTIVE=yes
        say "Stopping $SERVICE"
        $SUDO systemctl stop "$SERVICE"
    fi
    ROLLBACK=yes
    # left over by an earlier version of this script (new code moved inside the folder)
    if ptest -f "$APP/main_server/main.py"; then $SUDO rm -rf "${APP:?}/main_server"; fi

    say "Updating $APP"
    $SUDO mv "$APP" "$APP.old"
    # snapshot of the Python environment to roll back to (hard links: instant, no extra space;
    # pip replaces files, it never writes into them)
    if ptest -d "$APP.old/.venv"; then $SUDO cp -al "$APP.old/.venv" "$BASE/venv.rollback"; fi
    for item in $KEEP; do
        if ptest -e "$APP.old/$item"; then
            $SUDO mkdir -p "$STAGE/$(dirname "$item")"
            $SUDO rm -rf "${STAGE:?}/$item"
            $SUDO mv "$APP.old/$item" "$STAGE/$item"
        fi
    done
    $SUDO mv "$STAGE" "$APP"
else
    if ptest -e "$APP"; then  # an empty folder left over: anything else would be overwritten
        $SUDO rmdir "$APP" 2>/dev/null || die "$APP exists but is not an in-Time installation (no main.py, no .env): move it away first"
    fi
    say "Installing into $APP"
    $SUDO mv "$NEW" "$APP"
fi

FIRST_ENV=no
if ! ptest -f "$APP/.env"; then
    FIRST_ENV=yes
    $SUDO cp "$APP/.env.example" "$APP/.env"
    # a random session key and admin password instead of the placeholders (the server refuses them)
    KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    $SUDO sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$KEY|" "$APP/.env"
    ADMIN_PW="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(15))')"
    $SUDO sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$ADMIN_PW|" "$APP/.env"
fi
# PostgreSQL installed by this script: role and database "in-time", reached through the local
# socket with peer authentication (system user in-time = database role in-time, no password).
DB_URL="postgresql+psycopg://in-time@/in-time?host=/var/run/postgresql"
if [ "$PG_INSTALL" = yes ]; then
    say "Creating the PostgreSQL role and database \"in-time\""
    as_postgres() {
        if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
            (cd / && runuser -u postgres -- "$@")
        else
            (cd / && sudo -u postgres "$@")
        fi
    }
    i=0
    until as_postgres psql -qAt -c 'SELECT 1' >/dev/null 2>&1; do  # server still starting
        i=$((i + 1)); [ "$i" -lt 30 ] || die "PostgreSQL does not answer"
        sleep 1
    done
    if [ -z "$(as_postgres psql -qAt -c "SELECT 1 FROM pg_roles WHERE rolname = 'in-time'")" ]; then
        as_postgres psql -q -v ON_ERROR_STOP=1 -c 'CREATE ROLE "in-time" LOGIN' || die "could not create the role"
    fi
    if [ -z "$(as_postgres psql -qAt -c "SELECT 1 FROM pg_database WHERE datname = 'in-time'")" ]; then
        as_postgres psql -q -v ON_ERROR_STOP=1 -c 'CREATE DATABASE "in-time" OWNER "in-time"' \
            || die "could not create the database"
    fi
    if [ "$FIRST_ENV" = yes ]; then
        $SUDO sed -i "s|^DATABASE_URL=.*|DATABASE_URL=$DB_URL|" "$APP/.env"
    fi
fi

$SUDO chown -R "$SVC_USER:$SVC_USER" "$BASE"
$SUDO chmod 750 "$BASE" "$APP"
$SUDO chmod 600 "$APP/.env"

# ---------------------------------------------------------- python packages
# The environment is rebuilt when it is missing, broken or made with another Python version
# (e.g. after a system upgrade); otherwise it is updated in place.
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
VENV_VER="$(run_as_user "$APP/.venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
if [ -z "$VENV_VER" ] || [ "$VENV_VER" != "$PY_VER" ]; then
    if ptest -d "$APP/.venv"; then
        say "Rebuilding the Python environment (Python ${VENV_VER:-unusable} -> $PY_VER)"
        $SUDO rm -rf "$APP/.venv"
    else
        say "Creating the Python environment"
    fi
    run_as_user "$PYTHON" -m venv "$APP/.venv"
fi
say "Updating dependencies in .venv (this can take a few minutes)"
run_as_user env HOME="$BASE" "$APP/.venv/bin/pip" install -q --upgrade pip
run_as_user env HOME="$BASE" "$APP/.venv/bin/pip" install -q --upgrade -r "$APP/requirements.txt"
run_as_user "$APP/.venv/bin/python" -c 'import flask, flask_sqlalchemy, psycopg, cryptography, cv2, numpy' \
    || die "the Python environment is incomplete after the update"

# the new version is in place: the previous one is no longer needed
ROLLBACK=no
$SUDO rm -rf "$APP.old" "$BASE/venv.rollback"

# ------------------------------------------------------------------ systemd
say "Installing the $SERVICE service"
$SUDO install -m 644 "$TMP/$SERVICE.service" "$UNIT"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE.service" >/dev/null 2>&1 || $SUDO systemctl enable "$SERVICE.service"

# ------------------------------------------------- Cloudflare IP ranges (cron)
# Refresh of cf_ips.txt every 2 hours (used when CF_PROXY=true); the server reloads the file on its own.
if [ ! -d /etc/cron.d ] || ! { command -v cron >/dev/null 2>&1 || command -v crond >/dev/null 2>&1; }; then
    say "Installing cron"
    if command -v apt-get >/dev/null 2>&1; then
        apt_install cron || die "could not install cron"
        $SUDO systemctl enable --now cron >/dev/null 2>&1 || true
    elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        $SUDO "$(command -v dnf || command -v yum)" install -y -q cronie >/dev/null || die "could not install cronie"
        $SUDO systemctl enable --now crond >/dev/null 2>&1 || true
    else
        warn "cron not found: install it to refresh the Cloudflare IP ranges every 2 hours ($APP/update_cf_ips.py)"
    fi
fi
if [ -d /etc/cron.d ]; then
    say "Scheduling the Cloudflare IP ranges update every 2 hours ($CRON_FILE)"
    $SUDO tee "$CRON_FILE" >/dev/null <<EOF
# in-Time main server: refresh the Cloudflare IP ranges (cf_ips.txt) every 2 hours.
# The file is only rewritten when Cloudflare's list changed. Installed by install_main_server.sh.
SHELL=/bin/sh
PATH=/usr/local/bin:/usr/bin:/bin
17 */2 * * * $SVC_USER cd $APP && .venv/bin/python update_cf_ips.py >> $BASE/cf_ips_update.log 2>&1
EOF
    $SUDO chmod 644 "$CRON_FILE"
    # refresh once now (not fatal: e.g. no internet access yet)
    (cd "$APP" && run_as_user env HOME="$BASE" .venv/bin/python update_cf_ips.py) >/dev/null 2>&1 \
        || warn "could not update the Cloudflare IP ranges now (retried every 2 hours by cron)"
fi

if [ "$WAS_ACTIVE" = yes ]; then
    say "Restarting $SERVICE"
    $SUDO systemctl start "$SERVICE"
fi
trap 'rm -rf "$TMP"' EXIT

cat <<EOF

in-Time main server installed in $APP

EOF
if [ "$PG_INSTALL" = yes ] && [ "$FIRST_ENV" = yes ]; then
    DB_NOTE="     DATABASE_URL already points to the local \"in-time\" database created by this script."
elif [ "$PG_INSTALL" = yes ]; then
    DB_NOTE="     For the local database created by this script: DATABASE_URL=$DB_URL"
else
    DB_NOTE="     Set DATABASE_URL to your PostgreSQL database."
fi
if [ "$FIRST_ENV" = yes ]; then
    cat <<EOF
Admin console login (generated, also in $APP/.env):
  user:     admin
  password: $ADMIN_PW
Keep it somewhere safe; change ADMIN_PASSWORD in .env if you prefer your own (12+ characters).

Next steps:
  1. Edit the configuration (proxy settings...):
       sudo nano $APP/.env
$DB_NOTE
  2. Start the server:
       sudo systemctl start $SERVICE
EOF
elif [ "$WAS_ACTIVE" = yes ]; then
    echo "Updated and restarted. Your .env was kept (copy: $ENV_BACKUP); compare it with .env.example for new settings."
else
    cat <<EOF
Updated. Your .env was kept (copy: $ENV_BACKUP); compare it with .env.example for new settings.
Check the configuration if needed (sudo nano $APP/.env), then start the server:
$DB_NOTE
  sudo systemctl start $SERVICE
EOF
fi
cat <<EOF

Status and logs:
  sudo systemctl status $SERVICE
  sudo journalctl -u $SERVICE -f
It listens on 127.0.0.1:8000 by default (put nginx in front, or set SERVER in .env).
EOF
