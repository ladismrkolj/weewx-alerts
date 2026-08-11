#!/usr/bin/env python3
"""
useralerts_web.py - small config panel for UserAlerts.

Lets someone open/create their own per-user alert config, link a Telegram
bot (paste token -> scan/tap a QR/deep-link -> chat_id discovered
automatically), and add/edit/delete their alerts -- all without hand-editing
JSON or running tools/get_chat_id.py by hand.

Deliberately decoupled from bin/user/useralerts.py: this process never
imports or runs it, and only ever reads/writes files under `users_dir`
(never `state_dir`, which is owned by the running weewxd service). See that
file's header comment for why that split makes the two processes safe to
run concurrently with no locking.

Security model: NO PASSWORD. Typing a name opens or creates that config.
This is only safe on a trusted home/LAN deployment -- see web/README.md
before ever exposing this beyond your own network.

Configuration: reads weewx.conf's [UserAlerts] section (users_dir,
state_dir, default_telegram_bot_token) plus an optional [[Web]]
sub-section for this panel specifically:

    [UserAlerts]
        [[Web]]
            url_path = /alerts   # informational -- must match nginx's mount
            host = 127.0.0.1     # default for --host if not passed on the CLI
            port = 8081          # default for --port if not passed on the CLI
            htpasswd_file = /etc/nginx/.htpasswd_alerts  # read only by deploy/gen_nginx_conf.py

--host/--port on the command line always win over weewx.conf; both are
optional to keep `./serve-panel.sh` (no [[Web]] section needed) working.
See web/deploy/ for turning [[Web]] into a running, password-protected
nginx-fronted deployment.

Usage:
    python3 useralerts_web.py --config /path/to/weewx.conf [--host H] [--port P]
"""

import argparse
import base64
import glob
import io
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

import qrcode
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

DEFAULT_TIME_WAIT = 3600
NET_TIMEOUT = 10
PENDING_TTL = 600            # seconds a Telegram "connecting" attempt stays valid
ID_RE = re.compile(r'^[A-Za-z0-9_-]+$')
SLUG_STRIP_RE = re.compile(r'[^a-z0-9]+')
SAFE_USER_ID_RE = re.compile(r'[^a-z0-9_-]')

# Channel names this panel offers as checkboxes -- kept in sync by hand with
# Channels.DISPATCH in bin/user/useralerts.py (not imported, see module
# docstring for why).
AVAILABLE_CHANNELS = ['telegram', 'email']

app = Flask(__name__)
app.secret_key = secrets.token_bytes(32)   # only signs flash-message cookies

# If this is put behind a reverse proxy (nginx) -- e.g. to mount it at
# https://station.example/alerts/ with HTTPS + basic auth handled by nginx,
# see web/deploy/ -- trust exactly one hop of X-Forwarded-* so url_for()
# builds URLs with the right scheme/host and, when nginx sends
# X-Forwarded-Prefix, the right sub-path prefix too. Harmless when run
# standalone on a LAN: with no proxy in front, these headers are just never
# sent, and ProxyFix falls back to the real connection info.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)

USERS_DIR = None   # set in main() from weewx.conf

# In-memory Telegram "connect in progress" state: user_id -> dict. Fine for a
# single-process dev server (see web/README.md); not meant to survive a
# restart or scale past one process.
PENDING = {}

# Optional station-wide bot token, from weewx.conf [UserAlerts]
# default_telegram_bot_token -- pre-fills the connect form when everyone in
# the household links their own chat to the same shared bot. Blank means
# "no default, always paste a token by hand".
DEFAULT_BOT_TOKEN = ''

# [(field_name, unit_label), ...] for every real column in the station's
# archive table, for the expression cheatsheet -- e.g. ('outTemp', '°F').
# None if they couldn't be determined (non-SQLite backend, unusual binding
# setup, ...), in which case the template falls back to a generic example
# list instead.
ARCHIVE_FIELDS = None

# [(unit_name, unit_label), ...] for every unit convert()/unit= will accept,
# e.g. ('degree_C', '°C'). Set once at startup in main() -- see
# list_all_units().
ALL_UNITS = None


# -- small json helpers (atomic write, mirrors UserAlerts._save_json) -------

def load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)   # atomic on POSIX and Windows


def default_config(display_name):
    return {
        'enabled': True,
        'display_name': display_name,
        'channels': {},
        'alerts': [],
    }


def slugify(name):
    return SLUG_STRIP_RE.sub('-', name.strip().lower()).strip('-')


def user_path(user_id):
    # Defensive: user_id can come straight off a URL, not just our own
    # slugify() output, so make sure it can never escape USERS_DIR.
    safe = SAFE_USER_ID_RE.sub('', user_id.lower())
    return os.path.join(USERS_DIR, '%s.json' % safe)


def list_users():
    users = []
    for path in sorted(glob.glob(os.path.join(USERS_DIR, '*.json'))):
        uid = os.path.splitext(os.path.basename(path))[0]
        cfg = load_json(path) or {}
        users.append({'user_id': uid, 'display_name': cfg.get('display_name', uid)})
    return users


def mask_token(token):
    if not token:
        return ''
    if len(token) <= 10:
        return token[0] + '…' + token[-1]
    return token[:6] + '…' + token[-4:]


# -- archive schema introspection (for the cheatsheet's field list) ---------
#
# Best-effort only: this is purely to show real field names in the help
# text, not something any alert actually depends on. Any failure (non-SQLite
# backend, unusual binding, db momentarily locked by weewxd, ...) just falls
# back to a generic example list in the template -- it never blocks startup
# or a page load.

def resolve_archive_db_path(config_dict):
    """Figure out the on-disk path (and table name) of the archive database
    from weewx.conf, the same way weewx itself resolves [[wx_binding]] ->
    [Databases] -> [DatabaseTypes]. Returns (path_or_None, table_name)."""
    table_name = 'archive'
    try:
        binding_name = config_dict.get('StdArchive', {}).get('data_binding', 'wx_binding')
        binding = config_dict['DataBindings'][binding_name]
        table_name = binding.get('table_name', table_name)
        db_section = config_dict['Databases'][binding['database']]
        db_type = config_dict['DatabaseTypes'][db_section['database_type']]
        if db_type.get('driver') != 'weedb.sqlite':
            return None, table_name   # MySQL etc -- not attempting that here
        root = config_dict.get('WEEWX_ROOT', '.')
        path = os.path.join(root, db_type.get('SQLITE_ROOT', ''), db_section['database_name'])
        return path, table_name
    except (KeyError, TypeError):
        return None, table_name


def fetch_archive_fields(db_path, table_name):
    """Returns a list of (field_name, unit_label) tuples for every column in
    the archive table -- e.g. ('outTemp', '°F') -- or None if the schema
    couldn't be read at all. unit_label is '' for columns with no known
    physical unit (dateTime, batteryStatus1, forecast, ...)."""
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        # Read-only URI connection: never takes a write lock, so this is
        # safe to run even while weewxd has the same file open.
        uri = 'file:%s?mode=ro' % urllib.parse.quote(db_path)
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            # table_name comes from weewx.conf (ours to trust, not request
            # input), and sqlite has no parameter placeholder for identifiers.
            cols = [row[1] for row in conn.execute('PRAGMA table_info(%s)' % table_name)]
            if not cols:
                return None
            unit_system = None
            if 'usUnits' in cols:
                row = conn.execute(
                    'SELECT usUnits FROM %s WHERE usUnits IS NOT NULL LIMIT 1' % table_name
                ).fetchone()
                unit_system = row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None

    return [(name, _unit_label_for(unit_system, name)) for name in cols]


def _normalize_label(label):
    """weewx label lookups are sometimes a plain string, sometimes a
    [singular, plural] pair (e.g. 'mile' -> [' mile', ' miles']) -- either
    way, reduce to one displayable string."""
    if isinstance(label, (list, tuple)):
        label = label[0] if label else ''
    return (label or '').strip()


def _unit_label_for(unit_system, obs_type):
    """'outTemp' -> '°F', 'batteryStatus1' -> '' (no known physical unit)."""
    if unit_system is None:
        return ''
    try:
        import weewx.defaults
        import weewx.units
        unit_name, _ = weewx.units.getStandardUnitType(unit_system, obs_type)
        if not unit_name:
            return ''
        return _normalize_label(weewx.defaults.defaults['Units']['Labels'].get(unit_name, ''))
    except Exception:
        return ''


def list_all_units():
    """Every unit name valid anywhere in weewx -- i.e. everything convert()
    and the aggregate unit= kwarg will accept -- with a human label where
    one exists, e.g. ('degree_C', '°C'). None if weewx.units can't be
    introspected for some reason (falls back to no unit list in the
    cheatsheet, same as the other best-effort helpers above)."""
    try:
        import weewx.defaults
        import weewx.units
    except ImportError:
        return None
    names = set()
    for unit_dict in (weewx.units.USUnits, weewx.units.MetricUnits, weewx.units.MetricWXUnits):
        names.update(unit_dict.values())
    for src_unit, targets in weewx.units.conversionDict.items():
        names.add(src_unit)
        names.update(targets.keys())
    labels = weewx.defaults.defaults['Units']['Labels']
    return [(name, _normalize_label(labels.get(name, ''))) for name in sorted(names)]


# -- Telegram API helpers (plain urllib, same style as useralerts.py) -------

def telegram_api(bot_token, method, params=None):
    url = "https://api.telegram.org/bot%s/%s" % (bot_token, method)
    if params:
        url += '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=NET_TIMEOUT) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        raise ValueError("Telegram rejected the request (HTTP %s)" % e.code)
    except Exception as e:
        raise ValueError("Could not reach Telegram: %s" % e)
    if not body.get('ok'):
        raise ValueError("Telegram API error: %s" % body.get('description', body))
    return body['result']


def telegram_get_me(bot_token):
    return telegram_api(bot_token, 'getMe')


def telegram_get_updates(bot_token, offset=None):
    params = {'offset': offset} if offset is not None else None
    return telegram_api(bot_token, 'getUpdates', params)


def make_qr_data_uri(data):
    img = qrcode.make(data, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


# -- routes: name entry ------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', users=list_users())


@app.route('/open', methods=['POST'])
def open_user():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Enter a name first.', 'error')
        return redirect(url_for('index'))
    slug = slugify(name)
    if not slug:
        flash("That name doesn't have any usable characters -- try adding letters or numbers.",
              'error')
        return redirect(url_for('index'))
    path = user_path(slug)
    if not os.path.isfile(path):
        save_json(path, default_config(name))
    return redirect(url_for('dashboard', user_id=slug))


# -- routes: dashboard --------------------------------------------------------

@app.route('/u/<user_id>')
def dashboard(user_id):
    path = user_path(user_id)
    cfg = load_json(path)
    if cfg is None:
        cfg = default_config(user_id)
        save_json(path, cfg)

    tg = cfg.get('channels', {}).get('telegram', {})
    telegram_connected = bool(tg.get('bot_token') and tg.get('chat_id'))

    pending = PENDING.get(user_id)
    if pending and time.time() - pending['created'] > PENDING_TTL:
        PENDING.pop(user_id, None)
        pending = None

    show_connect_form = (not telegram_connected) or request.args.get('telegram') == 'edit'

    deep_link = qr_data_uri = None
    if pending:
        deep_link = "https://t.me/%s?start=%s" % (pending['username'], pending['code'])
        qr_data_uri = make_qr_data_uri(deep_link)

    edit_id = request.args.get('edit')
    edit_alert = None
    if edit_id == 'new':
        edit_alert = {'id': '', 'expression': '', 'template': '', 'channels': [],
                       'time_wait': DEFAULT_TIME_WAIT}
    elif edit_id:
        edit_alert = next((a for a in cfg.get('alerts', []) if a.get('id') == edit_id), None)

    return render_template(
        'dashboard.html',
        user_id=user_id, display_name=cfg.get('display_name', user_id),
        telegram_connected=telegram_connected, telegram_info=tg,
        masked_token=mask_token(tg.get('bot_token', '')),
        show_connect_form=show_connect_form,
        pending=pending, deep_link=deep_link, qr_data_uri=qr_data_uri,
        default_bot_token=DEFAULT_BOT_TOKEN,
        alerts=cfg.get('alerts', []), edit_id=edit_id, edit_alert=edit_alert,
        available_channels=AVAILABLE_CHANNELS, archive_fields=ARCHIVE_FIELDS,
        all_units=ALL_UNITS)


# -- routes: telegram connect --------------------------------------------------

@app.route('/u/<user_id>/telegram/connect', methods=['POST'])
def telegram_connect(user_id):
    bot_token = request.form.get('bot_token', '').strip()
    if not bot_token:
        flash('Paste a bot token first.', 'error')
        return redirect(url_for('dashboard', user_id=user_id))
    try:
        info = telegram_get_me(bot_token)
    except ValueError as e:
        flash("Couldn't validate that bot token: %s" % e, 'error')
        return redirect(url_for('dashboard', user_id=user_id, telegram='edit'))

    PENDING[user_id] = {
        'bot_token': bot_token,
        'code': secrets.token_urlsafe(6),
        'username': info.get('username'),
        'created': time.time(),
    }
    return redirect(url_for('dashboard', user_id=user_id))


@app.route('/u/<user_id>/telegram/status')
def telegram_status(user_id):
    pending = PENDING.get(user_id)
    if not pending:
        return jsonify({'connected': False, 'pending': False})

    # Telegram only reliably re-sends "/start <code>" the *first* time someone
    # ever starts a given bot. Anyone reconnecting to a bot they've already
    # chatted with before (e.g. the shared station bot) instead gets a bare
    # "/start" or a plain "Start" button press with no payload -- so also
    # accept the code sent back as a plain message (see dashboard.html,
    # which tells the user to do this if tapping the button doesn't work).
    target_texts = {'/start %s' % pending['code'], pending['code']}
    try:
        updates = telegram_get_updates(pending['bot_token'])
    except ValueError as e:
        return jsonify({'connected': False, 'pending': True, 'error': str(e)})

    for update in updates:
        msg = update.get('message')
        if not msg or (msg.get('text') or '').strip() not in target_texts:
            continue

        path = user_path(user_id)
        cfg = load_json(path) or default_config(user_id)
        cfg.setdefault('channels', {})['telegram'] = {
            'bot_token': pending['bot_token'],
            'chat_id': str(msg['chat']['id']),
            # Cosmetic only -- send_telegram() in useralerts.py never reads
            # this, it only needs bot_token/chat_id above.
            'username': pending['username'],
        }
        save_json(path, cfg)

        # Acknowledge this (and everything older) so future getUpdates calls
        # -- for this bot or any future reconnect -- don't keep re-seeing it.
        try:
            telegram_get_updates(pending['bot_token'], offset=update['update_id'] + 1)
        except ValueError:
            pass

        PENDING.pop(user_id, None)
        return jsonify({'connected': True})

    return jsonify({'connected': False, 'pending': True})


# -- routes: alert CRUD --------------------------------------------------------

@app.route('/u/<user_id>/alerts/save', methods=['POST'])
def save_alert(user_id):
    path = user_path(user_id)
    cfg = load_json(path) or default_config(user_id)
    alerts = cfg.setdefault('alerts', [])

    orig_id = request.form.get('orig_id', '').strip()
    new_id = request.form.get('id', '').strip()
    expression = request.form.get('expression', '').strip()
    template = request.form.get('template', '').strip()
    channels = request.form.getlist('channels')
    time_wait_raw = request.form.get('time_wait', '').strip()

    error = None
    if not new_id or not ID_RE.match(new_id):
        error = "Alert id must be non-empty and use only letters, numbers, '-' and '_'."
    elif any(a.get('id') == new_id and a.get('id') != orig_id for a in alerts):
        error = "Alert id '%s' is already used by another alert." % new_id

    time_wait = DEFAULT_TIME_WAIT
    if not error:
        try:
            time_wait = int(time_wait_raw) if time_wait_raw else DEFAULT_TIME_WAIT
            if time_wait < 0:
                raise ValueError
        except ValueError:
            error = "time_wait must be a non-negative whole number of seconds."

    if error:
        flash(error, 'error')
        return redirect(url_for('dashboard', user_id=user_id, edit=orig_id or 'new'))

    new_alert = {
        'id': new_id,
        'expression': expression,
        'template': template,
        'channels': channels,
        'time_wait': time_wait,
    }

    existing_index = next((i for i, a in enumerate(alerts) if a.get('id') == orig_id), None) \
        if orig_id else None
    if existing_index is not None:
        alerts[existing_index] = new_alert
    else:
        alerts.append(new_alert)

    save_json(path, cfg)
    flash("Alert '%s' saved." % new_id, 'success')
    return redirect(url_for('dashboard', user_id=user_id))


@app.route('/u/<user_id>/alerts/delete', methods=['POST'])
def delete_alert(user_id):
    path = user_path(user_id)
    cfg = load_json(path) or default_config(user_id)
    alert_id = request.form.get('id', '')
    cfg['alerts'] = [a for a in cfg.get('alerts', []) if a.get('id') != alert_id]
    save_json(path, cfg)
    flash("Alert '%s' deleted." % alert_id, 'success')
    return redirect(url_for('dashboard', user_id=user_id))


# -- entry point ----------------------------------------------------------------

def main():
    global USERS_DIR, DEFAULT_BOT_TOKEN, ARCHIVE_FIELDS, ALL_UNITS

    parser = argparse.ArgumentParser(description="UserAlerts config web panel")
    parser.add_argument('--config', dest='config_path', metavar='CONFIG_FILE',
                         help='Path to weewx.conf')
    # No defaults here for --host/--port: None means "not passed on the CLI",
    # so weewx.conf's [UserAlerts][[Web]] host/port (see module docstring)
    # can supply them instead, without one silently overriding the other.
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    import weecfg
    config_path, config_dict = weecfg.read_config(args.config_path, [])
    ua_dict = config_dict.get('UserAlerts', {})
    web_dict = ua_dict.get('Web', {})
    root = config_dict.get('WEEWX_ROOT', '.')
    USERS_DIR = os.path.join(root, ua_dict.get('users_dir', 'user/useralerts/users'))
    os.makedirs(USERS_DIR, exist_ok=True)
    DEFAULT_BOT_TOKEN = ua_dict.get('default_telegram_bot_token', '')

    host = args.host or web_dict.get('host', '0.0.0.0')
    port = args.port or int(web_dict.get('port', 8081))
    url_path = web_dict.get('url_path', '/')

    db_path, table_name = resolve_archive_db_path(config_dict)
    ARCHIVE_FIELDS = fetch_archive_fields(db_path, table_name)
    ALL_UNITS = list_all_units()

    print("Using configuration file %s" % config_path)
    print("Serving users_dir '%s' on http://%s:%s (mount point when proxied: %s)"
          % (USERS_DIR, host, port, url_path))
    if DEFAULT_BOT_TOKEN:
        print("Telegram connect form will default to the configured station bot token")
    if ARCHIVE_FIELDS:
        print("Cheatsheet: found %d real archive fields (%s)" % (len(ARCHIVE_FIELDS), db_path))
    else:
        print("Cheatsheet: could not read the archive schema (%s) -- "
              "falling back to a generic field list" % (db_path or 'no SQLite db found'))
    if ALL_UNITS:
        print("Cheatsheet: listing %d valid unit names" % len(ALL_UNITS))
    app.run(host=host, port=port, threaded=True, debug=args.debug)


if __name__ == '__main__':
    main()
