#!/usr/bin/env python3
"""
useralerts_web.py - small config panel for UserAlerts.

Lets someone open/create their own per-user alert config, link a Telegram
bot (paste token -> scan/tap a QR/deep-link -> chat_id discovered
automatically), and add/edit/delete their alerts -- all without hand-editing
JSON or running tools/get_chat_id.py by hand.

Deliberately decoupled from bin/user/useralerts.py: this process never runs
it as a service, and only ever reads/writes files under `users_dir` (never
`state_dir`, which is owned by the running weewxd service). See that file's
header comment for why that split makes the two processes safe to run
concurrently with no locking. The one exception is the alert editor's "Test"
button, which imports that module read-only to borrow its expression /
template evaluation -- see worker_module() below.

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

# Optional station-wide webcam snapshot URL, from weewx.conf [UserAlerts]
# default_image_url -- pre-fills the alert editor's snapshot box, since a
# household usually points every alert at the same camera. Blank means "no
# default, paste a URL if you want one".
DEFAULT_IMAGE_URL = ''

# [(field_name, unit_label), ...] for every real column in the station's
# archive table, for the expression cheatsheet -- e.g. ('outTemp', '°F').
# None if they couldn't be determined (non-SQLite backend, unusual binding
# setup, ...), in which case the template falls back to a generic example
# list instead.
ARCHIVE_FIELDS = None

# (db_path, table_name) for the station's archive database, from
# resolve_archive_db_path() at startup. Used read-only, both for the
# cheatsheet's field list and for the "Test" button (which needs a real
# record to evaluate an expression/template against). (None, 'archive') if
# the database couldn't be located.
ARCHIVE_DB = (None, 'archive')

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
        edit_alert = {'id': '', 'expression': '', 'template': '', 'subject': '',
                       'channels': [], 'time_wait': DEFAULT_TIME_WAIT,
                       'image_url': DEFAULT_IMAGE_URL, 'image_compress': True}
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
        all_units=ALL_UNITS, default_image_url=DEFAULT_IMAGE_URL)


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


# -- "Test" button: evaluate an expression/template for real ----------------
#
# This is the one place the panel needs the worker's *language*, as opposed
# to its files: re-implementing eval/render here would guarantee the test
# button and the running service eventually disagree about what an
# expression means. So bin/user/useralerts.py is imported (by path -- it
# doesn't have to be on sys.path) and its own Aggregator / UnitConverter /
# render_template are used. That's still read-only towards the service: no
# state file is touched, nothing is sent, and the module is only imported,
# never instantiated as a weewx service.

_WORKER = None


def worker_module():
    """Import bin/user/useralerts.py (a sibling of this repo's web/ dir, or
    wherever it was installed alongside) and cache it. Raises RuntimeError
    with a human-readable reason if it can't be loaded -- e.g. weewx isn't
    importable from this process."""
    global _WORKER
    if _WORKER is not None:
        return _WORKER
    import importlib
    import importlib.util
    try:
        # The installed copy under BIN_ROOT/user -- i.e. the exact code
        # weewxd is running, which is what a test should agree with.
        module = importlib.import_module('user.useralerts')
        if hasattr(module, 'render_template'):   # not some unrelated user/ package
            _WORKER = module
            return _WORKER
    except Exception:
        pass
    # Not installed / not importable from this process: fall back to the
    # copy in this checkout, one directory up from web/.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        '..', 'bin', 'user', 'useralerts.py')
    path = os.path.normpath(path)
    if not os.path.isfile(path):
        raise RuntimeError("Couldn't import user.useralerts, and there's no "
                           "useralerts.py at %s either." % path)
    try:
        spec = importlib.util.spec_from_file_location('useralerts_worker', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        raise RuntimeError("Couldn't load %s: %s" % (path, e))
    _WORKER = module
    return _WORKER


class ReadOnlyDbManager:
    """The slice of a weewx db_manager that Aggregator uses -- table_name
    and getSql() -- backed by a read-only SQLite connection, so avg() and
    friends work in a test run without going anywhere near weewxd's
    write path."""

    def __init__(self, db_path, table_name):
        self.db_path = db_path
        self.table_name = table_name

    def getSql(self, sql, params=()):
        uri = 'file:%s?mode=ro' % urllib.parse.quote(self.db_path)
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()


def latest_archive_record():
    """The most recent archive row as a dict, shaped like the record weewx
    hands the service: NULL columns are dropped, so a field that isn't
    really being reported raises NameError in an expression here exactly as
    it would in production. Returns None if there's no readable database or
    no rows in it."""
    db_path, table_name = ARCHIVE_DB
    if not db_path or not os.path.isfile(db_path):
        return None
    try:
        uri = 'file:%s?mode=ro' % urllib.parse.quote(db_path)
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            cur = conn.execute(
                'SELECT * FROM %s ORDER BY dateTime DESC LIMIT 1' % table_name)
            row = cur.fetchone()
            if row is None:
                return None
            names = [d[0] for d in cur.description]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {name: value for name, value in zip(names, row) if value is not None}


def build_namespace(record):
    """The same eval namespace UserAlerts._process_alert() builds: record
    fields + aggregates + unit conversions + compass() + local time/date."""
    worker = worker_module()
    db_path, table_name = ARCHIVE_DB
    namespace = dict(record)
    if db_path:
        aggregator = worker.Aggregator(ReadOnlyDbManager(db_path, table_name),
                                       record['dateTime'], record.get('usUnits'))
        namespace.update(aggregator.as_namespace())
    namespace.update(worker.UnitConverter(record).as_namespace())
    namespace['compass'] = worker.make_compass(record)
    if 'dateTime' in record:
        for key, value in worker.time_namespace(record['dateTime']).items():
            namespace.setdefault(key, value)
    return namespace


def describe_error(e, source):
    """Turn an exception from an expression into something a code editor
    would show: the message, and for a SyntaxError the line and column it
    points at, plus that line of the user's own source.

    eval_expression() compiles the expression wrapped in "(\n...\n)", so a
    SyntaxError's lineno is one more than the line the user actually typed
    -- undone here, since the whole point is to point at their text."""
    detail = {'error': '%s: %s' % (type(e).__name__, e)}
    if isinstance(e, SyntaxError):
        # e.msg, not str(e): str() tacks on "(<expression>, line 2)", which
        # duplicates the line/column reported separately below.
        detail['error'] = '%s: %s' % (type(e).__name__, e.msg)
        lines = source.splitlines()
        lineno = (e.lineno or 1) - 1          # unwrap the leading "(\n"
        # An error at the very end (e.g. a trailing operator) is reported
        # against the wrapper's closing ")" line -- point at the last line
        # the user actually typed instead.
        lineno = max(1, min(lineno, len(lines)))
        if lines:
            detail['lineno'] = lineno
            detail['line'] = lines[lineno - 1]
            if e.offset:
                # Clamp: the wrapper's closing "\n)" can put the caret one
                # past the end of the real line.
                detail['offset'] = max(1, min(e.offset, len(detail['line']) + 1))
    return detail


def snapshot_from_form(form, worker):
    """Fetch (and usually shrink) the snapshot the editor's form describes,
    reusing the worker's own fetch_image()/compress_image() so the panel and
    the service can't disagree about what actually gets sent.

    Returns (image, info): `image` is the (bytes, content_type, filename)
    tuple the channel senders take, or None; `info` is what the browser
    shows -- the sizes, how long the camera took, and a preview -- or an
    {'error': ...} if the camera couldn't be read. A camera failure is
    reported, never raised: the message still goes without a picture, which
    is what the service does too."""
    url = (form.get('image_url') or '').strip()
    if not url:
        return None, None
    if not hasattr(worker, 'fetch_image'):
        return None, {'error': "The installed useralerts.py is older than this "
                               "panel and doesn't support snapshots yet."}

    def number(key, default):
        raw = (form.get(key) or '').strip()
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    started = time.time()
    try:
        data, content_type = worker.fetch_image(url)
    except Exception as e:
        return None, {'error': '%s: %s' % (type(e).__name__, e)}
    fetch_ms = int((time.time() - started) * 1000)

    original_bytes = len(data)
    compressed = False
    if form.get('image_compress'):
        data, new_type = worker.compress_image(
            data,
            number('image_max_width', worker.DEFAULT_IMAGE_MAX_WIDTH),
            number('image_quality', worker.DEFAULT_IMAGE_QUALITY))
        compressed = new_type is not None
        content_type = new_type or content_type

    ext = 'jpg' if 'jpeg' in content_type or 'jpg' in content_type \
        else content_type.rsplit('/', 1)[-1] or 'jpg'
    info = {
        'original_bytes': original_bytes,
        'bytes': len(data),
        'compressed': compressed,
        'fetch_ms': fetch_ms,
        'content_type': content_type,
        # Inlined so the browser shows the frame that would actually be
        # sent, not whatever the camera serves on a second request.
        'preview': 'data:%s;base64,%s' % (content_type,
                                          base64.b64encode(data).decode('ascii')),
    }
    return (data, content_type, 'snapshot.%s' % ext), info


def render_subject(subject, namespace, alert_id, worker):
    """The subject line as the channels will see it: the alert's own subject
    if it set one, else useralerts.py's default wording, rendered through the
    same template language (so it can carry a reading). Only channels with a
    notion of a subject use it -- email does, telegram doesn't."""
    subject = subject.strip() or 'WeeWX alert: %s' % alert_id
    return worker.render_template(subject, dict(namespace), alert_id)


def eval_expression(expression, namespace, worker):
    """worker.eval_expression() if the loaded useralerts.py has it (it also
    makes multi-line expressions work), else the older inline eval so the
    panel still works against an out-of-date installed copy."""
    if hasattr(worker, 'eval_expression'):
        return worker.eval_expression(expression, namespace)
    return eval(expression, {'__builtins__': worker.SAFE_BUILTINS}, namespace)


@app.route('/u/<user_id>/alerts/test', methods=['POST'])
def test_alert(user_id):
    """Evaluate an expression and/or render a template against the latest
    real archive record, and report what happened -- without saving
    anything, touching the state file, or sending a message anywhere.

    Whichever of `expression` / `template` is posted gets tested, so this
    backs three buttons: "Test expression" and "Test template" in the alert
    editor, and the standalone expression debugger (which posts an
    expression plus include_record=1 to also get the record it was
    evaluated against)."""
    # Only the outer whitespace goes: newlines and indentation inside a
    # multi-line expression are the user's formatting, and are what the
    # error's line/column numbers refer to.
    expression = (request.form.get('expression') or '').strip()
    template = request.form.get('template') or ''
    subject = request.form.get('subject') or ''
    alert_id = (request.form.get('id') or '').strip() or 'test_alert'

    record = latest_archive_record()
    if record is None:
        return jsonify({'ok': False,
                        'error': "No archive record to test against -- couldn't read "
                                 "the station database, or it has no rows yet."})
    try:
        namespace = build_namespace(record)
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)})

    worker = worker_module()
    result = {
        'ok': True,
        'record_time': time.strftime('%Y-%m-%d %H:%M:%S',
                                     time.localtime(record['dateTime']))
        if 'dateTime' in record else None,
    }

    if expression:
        try:
            value = eval_expression(expression, dict(namespace), worker)
            result['expression'] = {'triggered': bool(value),
                                    'value': repr(value),
                                    # For the debugger, where "what did this
                                    # actually return" is the whole question
                                    # -- a float, None, a string, ...
                                    'type': type(value).__name__}
        except Exception as e:
            # Same outcome the service would have -- "didn't trigger" -- but
            # here the reason is the whole point, so it's reported instead of
            # only logged. A NameError is usually a typo, but can also just
            # be a field this particular record doesn't carry.
            result['expression'] = describe_error(e, expression)

    if request.form.get('include_record'):
        # The debugger shows what it evaluated against, so a None/NameError
        # answer is self-explanatory: the field simply isn't in this record.
        result['record'] = [
            {'name': name, 'value': repr(record[name])}
            for name in sorted(record)]

    if template:
        errors = []
        # dateTime_str / alert_id are injected by render_template itself, so
        # the preview shows exactly what would be sent.
        try:
            text = worker.render_template(template, dict(namespace), alert_id, errors)
        except TypeError:
            # An older installed useralerts.py without the errors= argument:
            # still preview the text, just without the per-placeholder
            # reasons (a failed placeholder shows up as literal {...}).
            text = worker.render_template(template, dict(namespace), alert_id)
        result['template'] = {'text': text, 'errors': errors,
                              'subject': render_subject(subject, namespace,
                                                        alert_id, worker)}
        # Previewing the message includes previewing the picture that would
        # ride along with it -- including how big it ends up after shrinking.
        _, snapshot = snapshot_from_form(request.form, worker)
        if snapshot:
            result['snapshot'] = snapshot

    return jsonify(result)


@app.route('/u/<user_id>/alerts/send_test', methods=['POST'])
def send_test(user_id):
    """Render the template against the latest archive record and actually
    send it over the channels ticked in the editor, reporting the outcome
    per channel.

    Deliberately separate from /alerts/test (which only ever previews):
    this one leaves the browser and puts a message on someone's phone, so
    it hangs off its own button rather than happening as a side effect of
    testing. Nothing is saved either way -- the alert doesn't have to exist
    yet, and its cooldown/state is untouched, so a test send never counts
    as the real alert having fired."""
    template = request.form.get('template') or ''
    alert_id = (request.form.get('id') or '').strip() or 'test_alert'
    channels = request.form.getlist('channels')

    if not template.strip():
        return jsonify({'ok': False, 'error': 'Nothing to send -- the template is empty.'})
    if not channels:
        return jsonify({'ok': False,
                        'error': 'No channels ticked -- tick at least one to send a test.'})

    record = latest_archive_record()
    if record is None:
        return jsonify({'ok': False,
                        'error': "No archive record to render against -- couldn't read "
                                 "the station database, or it has no rows yet."})
    try:
        namespace = build_namespace(record)
        worker = worker_module()
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)})

    errors = []
    try:
        text = worker.render_template(template, dict(namespace), alert_id, errors)
    except TypeError:
        text = worker.render_template(template, dict(namespace), alert_id)
    subject = render_subject(request.form.get('subject') or '', namespace,
                             alert_id, worker)

    # The saved config is where channel credentials live -- the editor form
    # only says *which* channels to use.
    cfg = load_json(user_path(user_id)) or {}
    channels_cfg = cfg.get('channels', {})

    image, snapshot = snapshot_from_form(request.form, worker)

    sent = []
    for name in channels:
        sender = worker.Channels.DISPATCH.get(name)
        if sender is None:
            sent.append({'channel': name, 'error': 'Unknown channel.'})
            continue
        chan_cfg = channels_cfg.get(name)
        if not chan_cfg:
            sent.append({'channel': name,
                         'error': "Not set up yet -- no connection settings saved for "
                                  "this channel."})
            continue
        try:
            # Synchronous, unlike the service's background thread: the whole
            # point of a test send is to find out whether it worked.
            try:
                sender(chan_cfg, subject, text, image)
            except TypeError:
                # An older installed useralerts.py whose senders take no
                # image: send the message itself rather than nothing.
                sender(chan_cfg, subject, text)
            sent.append({'channel': name, 'ok': True})
        except Exception as e:
            sent.append({'channel': name, 'error': '%s: %s' % (type(e).__name__, e)})

    return jsonify({'ok': True, 'text': text, 'errors': errors, 'sent': sent,
                    'subject': subject, 'snapshot': snapshot,
                    'record_time': time.strftime('%Y-%m-%d %H:%M:%S',
                                                 time.localtime(record['dateTime']))
                    if 'dateTime' in record else None})


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
    subject = request.form.get('subject', '').strip()
    image_url = request.form.get('image_url', '').strip()
    image_compress = bool(request.form.get('image_compress'))
    image_max_width_raw = request.form.get('image_max_width', '').strip()
    image_quality_raw = request.form.get('image_quality', '').strip()
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

    if not error and image_url and not image_url.lower().startswith(('http://', 'https://')):
        error = "The snapshot URL must start with http:// or https://."
    if not error and image_max_width_raw:
        try:
            if int(image_max_width_raw) < 1:
                raise ValueError
        except ValueError:
            error = "Snapshot width must be a whole number of pixels."
    if not error and image_quality_raw:
        try:
            if not 1 <= int(image_quality_raw) <= 95:
                raise ValueError
        except ValueError:
            error = "Snapshot JPEG quality must be a whole number between 1 and 95."

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
    if subject:
        # Left out entirely when blank, so the alert keeps taking whatever
        # useralerts.py's default is rather than pinning today's wording.
        new_alert['subject'] = subject
    if image_url:
        new_alert['image_url'] = image_url
        new_alert['image_compress'] = image_compress
        if image_compress:
            # Same reasoning as subject: only stored when the user actually
            # chose a number, so the defaults stay the service's to change.
            if image_max_width_raw:
                new_alert['image_max_width'] = int(image_max_width_raw)
            if image_quality_raw:
                new_alert['image_quality'] = int(image_quality_raw)

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
    global USERS_DIR, DEFAULT_BOT_TOKEN, ARCHIVE_FIELDS, ALL_UNITS, ARCHIVE_DB
    global DEFAULT_IMAGE_URL

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
    DEFAULT_IMAGE_URL = ua_dict.get('default_image_url', '')

    host = args.host or web_dict.get('host', '0.0.0.0')
    port = args.port or int(web_dict.get('port', 8081))
    url_path = web_dict.get('url_path', '/')

    db_path, table_name = resolve_archive_db_path(config_dict)
    ARCHIVE_DB = (db_path, table_name)
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
