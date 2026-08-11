#!/usr/bin/env bash
# install.sh - one-command install of the UserAlerts web panel as a
# background service, ready for gen_nginx_conf.py's output to be pasted
# into nginx.
#
# Does everything web/README.md otherwise walks through by hand:
#   - creates a venv at web/venv (--system-site-packages, so it can still
#     `import weecfg`/`weewx.*` from wherever WeeWX itself is installed)
#     and `pip install`s web/requirements.txt into it, unless one already
#     exists there
#   - generates /etc/systemd/system/useralerts-web.service, with
#     WorkingDirectory/ExecStart/User/Group filled in for *this* checkout
#     and host, instead of a template you hand-edit
#   - systemctl daemon-reload && enable --now's it
#   - optionally creates/updates the nginx basic-auth password file
#     (--htpasswd-user)
#   - prints the matching nginx location block (via gen_nginx_conf.py), the
#     one step that has to happen in a file this script has no business
#     touching on its own
#
# Usage (run as root -- it writes to /etc/systemd/system and, with
# --htpasswd-user, to a system nginx config directory):
#   sudo web/deploy/install.sh --config /etc/weewx/weewx.conf
#   sudo web/deploy/install.sh --config /etc/weewx/weewx.conf \
#       --user weewx --group weewx --htpasswd-user alice
#
# Safe to re-run: skips venv creation if web/venv already exists (delete it
# first to force a rebuild), and always regenerates the systemd unit fresh.

set -euo pipefail

CONFIG_PATH=""
SERVICE_USER="weewx"
SERVICE_GROUP=""
VENV_DIR=""
HTPASSWD_USER=""

usage() {
    cat <<'USAGE'
Usage: install.sh --config PATH [--user NAME] [--group NAME] [--venv PATH] [--htpasswd-user NAME]

  --config PATH        Path to weewx.conf (required).
  --user NAME           systemd User= to run the panel as (default: weewx).
  --group NAME          systemd Group= (default: same as --user).
  --venv PATH           Where to create the venv (default: <this checkout>/web/venv).
  --htpasswd-user NAME  Also create/update this user in the nginx basic-auth
                         password file (htpasswd_file from weewx.conf's
                         [UserAlerts][[Web]], default /etc/nginx/.htpasswd_alerts).
                         Prompts for the password interactively.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --config) CONFIG_PATH="$2"; shift 2 ;;
        --user) SERVICE_USER="$2"; shift 2 ;;
        --group) SERVICE_GROUP="$2"; shift 2 ;;
        --venv) VENV_DIR="$2"; shift 2 ;;
        --htpasswd-user) HTPASSWD_USER="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [ -z "$CONFIG_PATH" ]; then
    echo "error: --config PATH is required (path to weewx.conf)" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "error: must be run as root (writes to /etc/systemd/system)" >&2
    exit 1
fi
if [ ! -f "$CONFIG_PATH" ]; then
    echo "error: no such file: $CONFIG_PATH" >&2
    exit 1
fi
CONFIG_PATH="$(cd "$(dirname "$CONFIG_PATH")" && pwd)/$(basename "$CONFIG_PATH")"

: "${SERVICE_GROUP:=$SERVICE_USER}"

# Resolve this checkout's location from the script's own path
# (web/deploy/install.sh -> repo root is two directories up), so nothing
# here has to be a hand-edited placeholder.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_DIR="$(cd "$DEPLOY_DIR/.." && pwd)"
REPO_DIR="$(cd "$WEB_DIR/.." && pwd)"
: "${VENV_DIR:=$WEB_DIR/venv}"

echo "==> Repo checkout: $REPO_DIR"
echo "==> weewx.conf:    $CONFIG_PATH"
echo "==> Venv:          $VENV_DIR"
echo "==> Service user:  $SERVICE_USER:$SERVICE_GROUP"
echo

if [ -d "$VENV_DIR" ]; then
    echo "==> Venv already exists, leaving it alone (delete it first to rebuild)"
else
    # --system-site-packages so this venv inherits whatever the *system*
    # python3 already has (e.g. an apt-installed weewx's runtime deps like
    # python3-configobj) -- but that alone does NOT make weecfg/weewx.*
    # importable. See the check right below: on apt/rpm installs, WeeWX's
    # own Python source lives under /usr/share/weewx, which is a directory
    # that thing's wrapper scripts (weewxd, weectl) add to sys.path when
    # *they* run it -- it was never installed as an actual site-packages
    # entry, so a plain venv (even --system-site-packages) can't see it.
    echo "==> Creating venv"
    python3 -m venv --system-site-packages "$VENV_DIR"
    echo "==> Installing web/requirements.txt into it"
    "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
    "$VENV_DIR/bin/pip" install -r "$WEB_DIR/requirements.txt"
fi
echo

echo "==> Verifying weecfg/weewx.units are importable from the venv"
if "$VENV_DIR/bin/python3" -c "import weecfg, weewx.units" 2>/dev/null; then
    echo "    OK"
else
    # Fall back to the Debian/apt/rpm weewx package's actual location for
    # its own source (see comment above) -- override with
    # WEEWX_SRC_DIR=/some/other/path if this system puts it somewhere else.
    WEEWX_SRC_DIR="${WEEWX_SRC_DIR:-/usr/share/weewx}"
    if [ -f "$WEEWX_SRC_DIR/weecfg/__init__.py" ]; then
        SITE_DIR="$("$VENV_DIR/bin/python3" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
        echo "$WEEWX_SRC_DIR" > "$SITE_DIR/weewx_bindir.pth"
        if "$VENV_DIR/bin/python3" -c "import weecfg, weewx.units" 2>/dev/null; then
            echo "    OK -- added $WEEWX_SRC_DIR via a .pth file (that's where this system's weewx package keeps weecfg/weewx, not in site-packages)"
        else
            echo "error: still can't import weecfg/weewx.units after adding $WEEWX_SRC_DIR to the venv." >&2
            echo "       Re-run with WEEWX_SRC_DIR=/path/to/weewx/source $0 ..." >&2
            exit 1
        fi
    else
        echo "error: can't import weecfg/weewx.units, and no weewx source found at $WEEWX_SRC_DIR." >&2
        echo "       If this system's weewx Python files live somewhere else, re-run with:" >&2
        echo "         WEEWX_SRC_DIR=/path/to/weewx/source $0 ..." >&2
        exit 1
    fi
fi
echo

echo "==> Writing /etc/systemd/system/useralerts-web.service"
cat > /etc/systemd/system/useralerts-web.service <<EOF
# Generated by web/deploy/install.sh from $REPO_DIR -- re-run that script
# instead of hand-editing this file; it always regenerates it fresh.

[Unit]
Description=UserAlerts config web panel
Documentation=https://github.com/ladismrkolj/weewx-alerts
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$WEB_DIR
ExecStart=$VENV_DIR/bin/python3 $WEB_DIR/useralerts_web.py --config $CONFIG_PATH
Restart=on-failure
RestartSec=5

# Light sandboxing. NOTE: no ProtectSystem= here -- on some install layouts
# (e.g. the Debian/apt weewx package) WEEWX_ROOT is under /etc, so users_dir
# (where this app writes per-user JSON) lives under /etc too.
# ProtectSystem=full/strict would make /etc read-only and silently break
# every save. If your WEEWX_ROOT is elsewhere and you want that hardening,
# add ProtectSystem=full plus a ReadWritePaths= pointing at your actual
# users_dir.
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

echo "==> systemctl daemon-reload && enable --now useralerts-web"
systemctl daemon-reload
systemctl enable --now useralerts-web
echo
systemctl --no-pager status useralerts-web || true
echo

if [ -n "$HTPASSWD_USER" ]; then
    HTPASSWD_FILE="$("$VENV_DIR/bin/python3" - "$CONFIG_PATH" <<'PYEOF'
import sys
import weecfg
_, config_dict = weecfg.read_config(sys.argv[1], [])
print(config_dict.get('UserAlerts', {}).get('Web', {})
      .get('htpasswd_file', '/etc/nginx/.htpasswd_alerts'))
PYEOF
)"
    if ! command -v htpasswd >/dev/null 2>&1; then
        echo "error: htpasswd not found -- install it first:" >&2
        echo "  Debian/Ubuntu: apt-get install apache2-utils" >&2
        echo "  RHEL/Fedora:   dnf install httpd-tools" >&2
        exit 1
    fi
    echo "==> Adding/updating '$HTPASSWD_USER' in $HTPASSWD_FILE (you'll be prompted for a password)"
    mkdir -p "$(dirname "$HTPASSWD_FILE")"
    if [ -f "$HTPASSWD_FILE" ]; then
        htpasswd "$HTPASSWD_FILE" "$HTPASSWD_USER"
    else
        htpasswd -c "$HTPASSWD_FILE" "$HTPASSWD_USER"
    fi
    echo
fi

echo "==> nginx location block (from gen_nginx_conf.py) -- paste this into the"
echo "    server{} block that already serves your WeeWX public_html (must be"
echo "    the HTTPS one), then: nginx -t && systemctl reload nginx"
echo
"$VENV_DIR/bin/python3" "$DEPLOY_DIR/gen_nginx_conf.py" --config "$CONFIG_PATH"
