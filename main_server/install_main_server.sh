#!/bin/sh
# in-Time main server installer: deploys main_server as a systemd service.
#
#   curl -sSL https://raw.githubusercontent.com/dijey099/in-Time/refs/heads/master/main_server/install_main_server.sh | sh
#
# Installs into /var/lib/in-time/main-server, runs as the system user "in-time" (home /var/lib/in-time)
# with the service in-time-main. Running it again updates the code and keeps .env, photos,
# snapshots, face models and the Python environment.
#
# Options (environment variables):
#   IN_TIME_REPO    GitHub repository        (default dijey099/in-Time)
#   IN_TIME_BRANCH  branch                   (default master)
#   IN_TIME_SRC     install from this local main_server folder instead of downloading
set -eu

REPO="${IN_TIME_REPO:-dijey099/in-Time}"
BRANCH="${IN_TIME_BRANCH:-master}"
BASE=/var/lib/in-time
APP="$BASE/main-server"
SVC_USER=in-time
SERVICE=in-time-main
UNIT="/etc/systemd/system/$SERVICE.service"
# Kept when updating an existing installation
KEEP=".env .venv photos snapshots models app/static/vendor cf_ips.txt"
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

# ------------------------------------------------------------------ python
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || die "python3 is required (3.10 or newer). On Debian/Ubuntu: sudo apt install python3 python3-venv"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required ($("$PYTHON" -V 2>&1) found)"
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        say "Installing python3-venv"
        if ! { $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3-venv >/dev/null; }; then
            die "could not install python3-venv"
        fi
    else
        die "the Python venv module is missing: install it with your package manager (python3-venv)"
    fi
fi

# ---------------------------------------------------------------- download
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
NEW="$TMP/main_server"

if [ -n "${IN_TIME_SRC:-}" ]; then
    say "Copying $IN_TIME_SRC"
    [ -f "$IN_TIME_SRC/main.py" ] || die "$IN_TIME_SRC is not a main_server folder"
    cp -a "$IN_TIME_SRC" "$NEW"
else
    URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH"
    say "Downloading $REPO ($BRANCH)"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" -o "$TMP/src.tar.gz" || die "download failed: $URL"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$TMP/src.tar.gz" "$URL" || die "download failed: $URL"
    else
        die "curl or wget is required"
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
WAS_ACTIVE=no
if [ -d "$APP" ]; then
    say "Updating $APP (keeping .env, photos, snapshots, models and the Python environment)"
    if $SUDO systemctl is-active --quiet "$SERVICE"; then
        WAS_ACTIVE=yes
        $SUDO systemctl stop "$SERVICE"
    fi
    for item in $KEEP; do
        if [ -e "$APP/$item" ]; then
            $SUDO mkdir -p "$NEW/$(dirname "$item")"
            $SUDO rm -rf "$NEW/$item"
            $SUDO mv "$APP/$item" "$NEW/$item"
        fi
    done
    $SUDO rm -rf "$APP.old"
    $SUDO mv "$APP" "$APP.old"
else
    say "Installing into $APP"
fi
$SUDO mv "$NEW" "$APP"
$SUDO rm -rf "$APP.old"

FIRST_ENV=no
if [ ! -f "$APP/.env" ]; then
    FIRST_ENV=yes
    $SUDO cp "$APP/.env.example" "$APP/.env"
    # a random session key instead of the placeholder
    KEY="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    $SUDO sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$KEY|" "$APP/.env"
fi
$SUDO chown -R "$SVC_USER:$SVC_USER" "$BASE"
$SUDO chmod 750 "$BASE" "$APP"
$SUDO chmod 600 "$APP/.env"

# ---------------------------------------------------------- python packages
run_as_user() {  # run a command as the service user
    if [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u "$SVC_USER" -- "$@"
    else
        sudo -u "$SVC_USER" "$@"
    fi
}
if [ ! -x "$APP/.venv/bin/python" ]; then
    say "Creating the Python environment"
    run_as_user "$PYTHON" -m venv "$APP/.venv"
fi
say "Installing dependencies (this can take a few minutes)"
run_as_user env HOME="$BASE" "$APP/.venv/bin/pip" install -q --upgrade pip
run_as_user env HOME="$BASE" "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

# ------------------------------------------------------------------ systemd
say "Installing the $SERVICE service"
$SUDO install -m 644 "$TMP/$SERVICE.service" "$UNIT"
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE.service" >/dev/null 2>&1 || $SUDO systemctl enable "$SERVICE.service"

if [ "$WAS_ACTIVE" = yes ]; then
    say "Restarting $SERVICE"
    $SUDO systemctl start "$SERVICE"
fi

cat <<EOF

in-Time main server installed in $APP

EOF
if [ "$FIRST_ENV" = yes ]; then
    cat <<EOF
Next steps:
  1. Edit the configuration (database URL, admin password, departments...):
       sudo nano $APP/.env
     The main server needs PostgreSQL: set DATABASE_URL to your database.
  2. Start the server:
       sudo systemctl start $SERVICE
EOF
elif [ "$WAS_ACTIVE" = yes ]; then
    echo "Updated and restarted. Your .env was kept (compare it with .env.example for new settings)."
else
    cat <<EOF
Updated. Your .env was kept (compare it with .env.example for new settings).
Check the configuration if needed (sudo nano $APP/.env), then start the server:
  sudo systemctl start $SERVICE
EOF
fi
cat <<EOF

Status and logs:
  sudo systemctl status $SERVICE
  sudo journalctl -u $SERVICE -f
It listens on 127.0.0.1:8000 by default (put nginx in front, or set SERVER in .env).
EOF
