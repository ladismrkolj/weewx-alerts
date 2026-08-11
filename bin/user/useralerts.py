# useralerts.py
#
# UserAlerts - a per-user, JSON-configured alerting service for WeeWX.
#
# This module is the WORKER only. It:
#   - reads one JSON config file per user  (config in)
#   - evaluates each alert's expression against every new archive record
#   - renders the alert's template and sends it over the alert's channels
#   - writes back ONLY a small "last evaluated" state file per user
#
# It never writes to the user's config file. A future useralerts_web.py +
# index.html can freely read/write the config files without ever racing
# against this service.
#
# --------------------------------------------------------------------------
# weewx.conf
#
#   [UserAlerts]
#       enable = true
#       users_dir = user/useralerts/users     # one <user_id>.json per user
#       state_dir = user/useralerts/state     # one <user_id>.json per user
#
# Install:
#   1. Copy this file to BIN_ROOT/user/useralerts.py   (i.e. bin/user/)
#   2. Add the [UserAlerts] section above to weewx.conf
#   3. Add user.useralerts.UserAlerts to report_services (or a services list)
#      in [Engine][[Services]], e.g.:
#
#      [Engine]
#         [[Services]]
#              report_services = weewx.engine.StdPrint, weewx.engine.StdReport, user.useralerts.UserAlerts
#
#   4. Create users_dir / state_dir (relative to WEEWX_ROOT) and drop in a
#      user config file, e.g. user/useralerts/users/user1.json :
#
# --------------------------------------------------------------------------
# Example user config file  (users/user1.json)
#
# {
#   "enabled": true,
#   "channels": {
#     "telegram": {
#       "bot_token": "123456:ABC-DEF...",
#       "chat_id": "987654321"
#     },
#     "email": {
#       "smtp_host": "smtp.example.com",
#       "smtp_user": "myusername",
#       "smtp_password": "mypassword",
#       "from": "alerts@example.com",
#       "to": ["me@example.com"]
#     }
#   },
#   "alerts": [
#     {
#       "id": "freeze_warning",
#       "expression": "outTemp < 32.0",
#       "template": "Freeze warning! outTemp={outTemp:.1f}F at {dateTime_str}",
#       "subject": "WeeWX: freeze warning",
#       "channels": ["telegram", "email"],
#       "time_wait": 3600
#     },
#     {
#       "id": "wind_gust_avg",
#       "expression": "avg('windSpeed', 30) is not None and avg('windSpeed', 30) > 20",
#       "template": "Sustained wind! 30-min avg was {avg('windSpeed', 30, unit='kts'):.1f} kts, gust now {to_kts('windGust'):.1f} kts",
#       "channels": ["telegram"],
#       "time_wait": 1800
#     }
#   ]
# }
#
# --------------------------------------------------------------------------
# Expression language -- shared by "expression" AND by each {...} placeholder
# inside "template" (see "Template language" below)
#
#   - Any field in the current archive record is available by name directly,
#     e.g. outTemp, windSpeed, dateTime, ... Raw field values are already in
#     whatever unit system weewx.conf's [StdConvert] target_unit is set to
#     (US/METRIC/METRICWX) -- that's the unit archive records are stored in.
#   - avg(obs, minutes) / amin(obs, minutes) / amax(obs, minutes) / asum(obs, minutes)
#     compute a rolling aggregate ending at the current record's dateTime,
#     read straight from the archive database. Returns None if there is no
#     data in that window. Same unit system as the raw field, unless you
#     pass unit=..., e.g. amax('outTemp', 30, unit='C') for the 30-minute
#     max in Celsius regardless of the station's configured unit system.
#   - to_C(obs) / to_F(obs) / to_kts(obs) / to_mps(obs) convert a field's
#     *current* value to Celsius / Fahrenheit / knots / meters-per-second,
#     regardless of the station's configured unit system -- handy for a
#     fixed-unit threshold (e.g. "always alert at 0 C") or for a unit
#     (knots) that isn't one of weewx's three standard target_unit systems.
#     Returns None if the field is missing or the conversion doesn't apply
#     (e.g. to_kts() on a temperature field).
#   - convert(obs, unit_name) is the general form behind those four --
#     any weewx unit name works, e.g. convert('barometer', 'hPa'),
#     convert('rain', 'mm'). Optionally convert(obs, unit_name, value) to
#     convert an explicit value (e.g. an avg()/amax() result without using
#     its unit= kwarg) instead of the current record's, using obs only to
#     look up its unit group.
#   - A small set of safe builtins are available: abs, round, min, max, len
#   - dateTime_str (human readable time of the record) and alert_id are also
#     available -- handy inside a template placeholder, e.g. {dateTime_str}.
#   - Expressions that reference a missing field, or that raise any
#     exception, are logged and simply treated as "not triggered" for that
#     pass (in "expression") or left as the literal "{original text}" (in a
#     template placeholder); they never crash the service or affect other
#     alerts/users.
#
# Template language
#
#   - Each {...} in a template is evaluated at send time as an expression in
#     the language above -- not just a field name. A plain field still works
#     the same as before, e.g. {outTemp}, but so does a function call, e.g.
#     {avg('windSpeed', 30, unit='kts')} or {to_kts('windGust')}.
#   - Optionally follow the expression with ':' and a str.format() format
#     spec, e.g. {avg('windSpeed', 30, unit='kts'):.1f} or {outTemp:.1f}.
#   - {{ and }} are literal braces, same as str.format().
#   - If a placeholder's expression raises (missing field, bad syntax, ...),
#     that one placeholder is left as the literal "{original text}" rather
#     than raising, so a typo or a transient missing field never crashes a
#     send -- everything else in the template still renders normally.
#
# --------------------------------------------------------------------------

import glob
import json
import logging
import os
import re
import smtplib
import threading
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

import weewx
import weewx.units
from weeutil.weeutil import timestamp_to_string, to_bool
from weewx.engine import StdService

log = logging.getLogger(__name__)

DEFAULT_TIME_WAIT = 3600        # seconds between repeat sends of the same alert
DEFAULT_NET_TIMEOUT = 10        # seconds, for telegram/email network calls
OBS_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# A deliberately small set of builtins made available inside alert
# expressions. Everything else (import, open, __builtins__, ...) is blocked.
SAFE_BUILTINS = {
    'abs': abs, 'round': round, 'min': min, 'max': max, 'len': len,
    'True': True, 'False': False, 'None': None,
}


def _validate_obs_name(obs):
    """Guard against anything but a plain identifier being interpolated
    into SQL for the avg/amin/amax/asum helpers."""
    if not isinstance(obs, str) or not OBS_NAME_RE.match(obs):
        raise ValueError("Invalid observation type: %r" % (obs,))
    return obs


# Named unit shortcuts usable anywhere a unit name is expected (to_C(),
# convert(obs, unit=...), the aggregate functions' unit= kwarg, ...) -- the
# handful of units alert authors are likely to want by name regardless of
# the station's configured unit system. Any actual weewx unit name (e.g.
# 'hPa', 'mm', 'inHg') works too, this is just convenient shorthand for four
# common ones.
NAMED_UNITS = {'C': 'degree_C', 'F': 'degree_F',
               'kts': 'knot', 'mps': 'meter_per_second'}


def _convert_value(unit_system, obs, value, unit_name):
    """Convert `value` -- assumed to already be obs's value under
    unit_system (a weewx.US/METRIC/METRICWX constant) -- to unit_name
    (either a NAMED_UNITS shortcut or any real weewx unit name). obs is
    used only to look up its unit group, e.g. 'windSpeed' -> group_speed.
    Returns None (never raises) if anything about that doesn't apply --
    missing value, unknown unit_system, or a unit_name that isn't valid for
    obs's group (e.g. converting a temperature field to knots)."""
    if value is None or unit_system is None:
        return None
    obs = _validate_obs_name(obs)
    unit_name = NAMED_UNITS.get(unit_name, unit_name)
    try:
        src_unit, src_group = weewx.units.getStandardUnitType(unit_system, obs)
        if src_unit is None:
            return None
        vt = weewx.units.ValueTuple(value, src_unit, src_group)
        return weewx.units.convert(vt, unit_name)[0]
    except (KeyError, TypeError, ValueError) as e:
        log.debug("Could not convert %s to %s: %s", obs, unit_name, e)
        return None


class Aggregator:
    """Builds the avg()/amin()/amax()/asum() functions bound to a specific
    db_manager, a specific 'as of' timestamp (the current record's
    dateTime), and that record's unit system (for the optional unit=
    conversion), for use inside an alert expression's eval namespace."""

    def __init__(self, db_manager, end_ts, unit_system=None):
        self.db_manager = db_manager
        self.end_ts = end_ts
        self.unit_system = unit_system

    def _aggregate(self, sql_func, obs, minutes, unit=None):
        obs = _validate_obs_name(obs)
        minutes = float(minutes)
        start_ts = self.end_ts - minutes * 60.0
        sql = "SELECT %s(%s) FROM %s WHERE dateTime > ? AND dateTime <= ?" % (
            sql_func, obs, self.db_manager.table_name)
        try:
            row = self.db_manager.getSql(sql, (start_ts, self.end_ts))
        except Exception as e:
            log.debug("Aggregate query failed for %s(%s, %s min): %s",
                       sql_func, obs, minutes, e)
            return None
        value = row[0] if row and row[0] is not None else None
        if unit is not None and value is not None:
            value = _convert_value(self.unit_system, obs, value, unit)
        return value

    def avg(self, obs, minutes, unit=None):
        return self._aggregate('AVG', obs, minutes, unit)

    def amin(self, obs, minutes, unit=None):
        return self._aggregate('MIN', obs, minutes, unit)

    def amax(self, obs, minutes, unit=None):
        return self._aggregate('MAX', obs, minutes, unit)

    def asum(self, obs, minutes, unit=None):
        return self._aggregate('SUM', obs, minutes, unit)

    def as_namespace(self):
        return {'avg': self.avg, 'amin': self.amin,
                'amax': self.amax, 'asum': self.asum}


class UnitConverter:
    """Builds the to_C()/to_F()/to_kts()/to_mps()/convert() functions bound
    to a specific archive record, for use inside an alert expression's eval
    namespace. Each converts a field's value out of whatever unit system the
    record was stored in (record['usUnits']) into a specific target unit,
    independent of the station's configured [StdConvert] target_unit --
    e.g. to_C('outTemp') always means Celsius, even on a US-unit station."""

    def __init__(self, record):
        self.record = record

    def convert(self, obs, unit_name, value=None):
        obs = _validate_obs_name(obs)
        if value is None:
            value = self.record.get(obs)
        return _convert_value(self.record.get('usUnits'), obs, value, unit_name)

    def to_C(self, obs, value=None):
        return self.convert(obs, 'C', value)

    def to_F(self, obs, value=None):
        return self.convert(obs, 'F', value)

    def to_kts(self, obs, value=None):
        return self.convert(obs, 'kts', value)

    def to_mps(self, obs, value=None):
        return self.convert(obs, 'mps', value)

    def as_namespace(self):
        return {'to_C': self.to_C, 'to_F': self.to_F,
                'to_kts': self.to_kts, 'to_mps': self.to_mps,
                'convert': self.convert}


class Channels:
    """Static senders for each supported alert channel. Each takes the
    channel's connection settings (from the user's config) and the
    already-rendered message text."""

    @staticmethod
    def send_telegram(chan_cfg, subject, text):
        bot_token = chan_cfg.get('bot_token')
        chat_id = chan_cfg.get('chat_id')
        if not bot_token or not chat_id:
            raise ValueError("telegram channel missing bot_token/chat_id")

        url = "https://api.telegram.org/bot%s/sendMessage" % bot_token
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=DEFAULT_NET_TIMEOUT) as resp:
            body = resp.read()
            if resp.status != 200:
                raise IOError("Telegram API returned status %s: %s"
                               % (resp.status, body))

    @staticmethod
    def send_email(chan_cfg, subject, text):
        smtp_host = chan_cfg['smtp_host']
        smtp_user = chan_cfg.get('smtp_user')
        smtp_password = chan_cfg.get('smtp_password')
        from_addr = chan_cfg.get('from', 'alerts@example.com')
        to_addrs = chan_cfg['to']
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]

        msg = MIMEText(text)
        msg['Subject'] = subject
        msg['From'] = from_addr
        msg['To'] = ','.join(to_addrs)

        try:
            s = smtplib.SMTP_SSL(smtp_host, timeout=DEFAULT_NET_TIMEOUT)
        except (AttributeError, OSError):
            s = smtplib.SMTP(smtp_host, timeout=DEFAULT_NET_TIMEOUT)
            try:
                s.ehlo()
                s.starttls()
                s.ehlo()
            except smtplib.SMTPException as e:
                log.debug("No STARTTLS, sending unencrypted. Reason: %s", e)

        try:
            if smtp_user:
                s.login(smtp_user, smtp_password)
            s.sendmail(from_addr, to_addrs, msg.as_string())
        finally:
            try:
                s.quit()
            except Exception:
                pass

    DISPATCH = {
        'telegram': send_telegram.__func__,
        'email': send_email.__func__,
    }


def _split_field_spec(content):
    """Split a template placeholder's inner text into (expr, spec) on the
    first top-level ':' -- one that isn't nested inside (), [], {} or a
    quoted string, e.g. "avg('windSpeed', 30):.1f" -> ("avg('windSpeed',
    30)", ".1f"). Returns (content, None) if there's no top-level ':'."""
    depth = 0
    quote = None
    i = 0
    while i < len(content):
        c = content[i]
        if quote:
            if c == '\\':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in ("'", '"'):
            quote = c
        elif c in '([{':
            depth += 1
        elif c in ')]}':
            depth -= 1
        elif c == ':' and depth == 0:
            return content[:i], content[i + 1:]
        i += 1
    return content, None


def render_template(template, namespace, alert_id):
    """Render a template string. Each {...} placeholder is evaluated as a
    Python expression against `namespace` (record fields + avg/amin/amax/
    asum + to_C/to_F/to_kts/to_mps/convert -- the same namespace used for
    "expression"), optionally followed by ':' and a str.format() format
    spec, e.g. {avg('windSpeed', 30, unit='kts'):.1f}. A plain field name
    like {outTemp} still works too, since it's just a name lookup. {{ and
    }} are literal braces, like str.format(). If a placeholder's expression
    raises (missing field, bad syntax, ...), that placeholder is left as
    the literal "{original text}" rather than raising, so one bad field
    never crashes the whole send."""
    ns = dict(namespace)
    ns['alert_id'] = alert_id
    if 'dateTime' in ns:
        ns['dateTime_str'] = timestamp_to_string(ns['dateTime'])

    out = []
    i, n = 0, len(template)
    while i < n:
        c = template[i]
        if c == '{' and template[i:i + 2] == '{{':
            out.append('{')
            i += 2
        elif c == '}' and template[i:i + 2] == '}}':
            out.append('}')
            i += 2
        elif c == '{':
            end = template.find('}', i + 1)
            if end == -1:
                out.append(template[i:])
                break
            content = template[i + 1:end]
            expr, spec = _split_field_spec(content)
            try:
                value = eval(expr, {'__builtins__': SAFE_BUILTINS}, ns)
                out.append(format(value, spec) if spec else str(value))
            except Exception as e:
                log.debug("Alert '%s': template field '{%s}' failed: %s",
                          alert_id, content, e)
                out.append('{' + content + '}')
            i = end + 1
        else:
            out.append(c)
            i += 1
    return ''.join(out)


class UserAlerts(StdService):
    """WeeWX service that evaluates per-user, JSON-defined alerts on every
    new archive record, and dispatches triggered alerts over one or more
    channels (telegram, email, ...)."""

    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)

        ua_dict = config_dict.get('UserAlerts', {})

        if not to_bool(ua_dict.get('enable', True)):
            log.info("UserAlerts: disabled in weewx.conf")
            return

        root = config_dict.get('WEEWX_ROOT', '.')
        self.users_dir = os.path.join(root, ua_dict.get(
            'users_dir', 'user/useralerts/users'))
        self.state_dir = os.path.join(root, ua_dict.get(
            'state_dir', 'user/useralerts/state'))

        if not os.path.isdir(self.users_dir):
            log.warning("UserAlerts: users_dir '%s' does not exist; "
                        "no alerts will be evaluated until it does",
                        self.users_dir)
        os.makedirs(self.state_dir, exist_ok=True)

        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        log.info("UserAlerts: watching '%s' for user alert configs",
                  self.users_dir)

    # -- main entry point, called on every new archive record ------------

    def new_archive_record(self, event):
        record = event.record
        db_manager = self.engine.db_binder.get_manager()

        for path in sorted(glob.glob(os.path.join(self.users_dir, '*.json'))):
            user_id = os.path.splitext(os.path.basename(path))[0]
            try:
                self._process_user(user_id, path, record, db_manager)
            except Exception as e:
                log.error("UserAlerts: unexpected error processing user "
                          "'%s': %s", user_id, e)

    # -- per user ----------------------------------------------------------

    def _process_user(self, user_id, config_path, record, db_manager):
        config = self._load_json(config_path)
        if config is None:
            return
        if not to_bool(config.get('enabled', True)):
            return

        channels_cfg = config.get('channels', {})
        alerts = config.get('alerts', [])
        if not alerts:
            return

        state = self._load_json(self._state_path(user_id)) or {}
        changed = False

        aggregator = Aggregator(db_manager, record['dateTime'], record.get('usUnits'))

        for alert in alerts:
            alert_id = alert.get('id')
            if not alert_id:
                log.warning("UserAlerts: user '%s' has an alert with no "
                            "'id'; skipping", user_id)
                continue
            try:
                if self._process_alert(user_id, alert, record, aggregator,
                                        channels_cfg, state):
                    changed = True
            except Exception as e:
                log.error("UserAlerts: user '%s' alert '%s' failed: %s",
                          user_id, alert_id, e)
                st = state.setdefault(alert_id, {})
                st['last_error'] = str(e)
                st['last_checked'] = time.time()
                changed = True

        if changed:
            self._save_json(self._state_path(user_id), state)

    # -- per alert -----------------------------------------------------

    def _process_alert(self, user_id, alert, record, aggregator,
                        channels_cfg, state):
        alert_id = alert['id']
        expression = alert.get('expression')
        if not expression:
            return False

        # Build the eval namespace: record fields + avg()/amin()/amax()/asum()
        # + to_C()/to_F()/to_kts()/to_mps()/convert()
        namespace = dict(record)
        namespace.update(aggregator.as_namespace())
        namespace.update(UnitConverter(record).as_namespace())

        try:
            triggered = bool(eval(expression,
                                   {'__builtins__': SAFE_BUILTINS},
                                   namespace))
        except NameError as e:
            # Record is missing a field the expression needs -- not an
            # error, just can't evaluate this pass.
            log.debug("UserAlerts: user '%s' alert '%s': %s",
                      user_id, alert_id, e)
            return False
        except Exception as e:
            log.warning("UserAlerts: user '%s' alert '%s': bad expression: %s",
                        user_id, alert_id, e)
            return False

        now = time.time()
        st = state.setdefault(alert_id, {'active': False, 'last_sent': None})
        st['last_checked'] = now
        st['last_error'] = None
        time_wait = alert.get('time_wait', DEFAULT_TIME_WAIT)

        should_send = False
        if triggered:
            never_sent = st.get('last_sent') is None
            cooled_down = (not never_sent and
                           (now - st['last_sent']) >= time_wait)
            if never_sent or cooled_down:
                should_send = True
                st['last_sent'] = now
            st['active'] = True
        else:
            st['active'] = False

        if should_send:
            self._dispatch(user_id, alert, namespace, channels_cfg)

        return True

    # -- dispatch (runs in a background thread; never touches state) ----

    def _dispatch(self, user_id, alert, namespace, channels_cfg):
        alert_id = alert['id']
        template = alert.get('template', 'Alert {alert_id} triggered')
        subject = alert.get('subject', 'WeeWX alert: %s' % alert_id)
        text = render_template(template, namespace, alert_id)
        channel_names = alert.get('channels', [])

        t = threading.Thread(
            target=self._send_all,
            args=(user_id, alert_id, channel_names, channels_cfg,
                  subject, text),
            daemon=True)
        t.start()

    def _send_all(self, user_id, alert_id, channel_names, channels_cfg,
                  subject, text):
        for name in channel_names:
            sender = Channels.DISPATCH.get(name)
            if sender is None:
                log.warning("UserAlerts: user '%s' alert '%s': unknown "
                            "channel '%s'", user_id, alert_id, name)
                continue
            chan_cfg = channels_cfg.get(name)
            if not chan_cfg:
                log.warning("UserAlerts: user '%s' alert '%s': channel "
                            "'%s' has no connection settings configured",
                            user_id, alert_id, name)
                continue
            try:
                sender(chan_cfg, subject, text)
                log.info("UserAlerts: user '%s' alert '%s' sent via %s",
                          user_id, alert_id, name)
            except Exception as e:
                log.error("UserAlerts: user '%s' alert '%s': failed to "
                          "send via %s: %s", user_id, alert_id, name, e)

    # -- small json helpers ----------------------------------------------

    def _state_path(self, user_id):
        return os.path.join(self.state_dir, '%s.json' % user_id)

    @staticmethod
    def _load_json(path):
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            log.error("UserAlerts: could not read '%s': %s", path, e)
            return None

    @staticmethod
    def _save_json(path, data):
        tmp_path = path + '.tmp'
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, path)   # atomic on POSIX and Windows
        except OSError as e:
            log.error("UserAlerts: could not write '%s': %s", path, e)


# ---------------------------------------------------------------------
# Stand-alone test harness. Lets you sanity-check a user's config and
# expressions against a fake record, WITHOUT sending real notifications
# and without needing the full weewx engine running.
#
#   PYTHONPATH=/home/weewx/bin python3 useralerts.py --config /etc/weewx/weewx.conf --user user1
# ---------------------------------------------------------------------
if __name__ == '__main__':
    from optparse import OptionParser
    import weecfg
    import weewx.engine

    usage = """Usage: python useralerts.py --config CONFIG_FILE --user USER_ID [--dry-run]"""
    parser = OptionParser(usage=usage)
    parser.add_option("--config", dest="config_path", metavar="CONFIG_FILE",
                       help="Path to weewx.conf")
    parser.add_option("--user", dest="user_id", metavar="USER_ID",
                       help="User id (i.e. filename without .json) to test")
    parser.add_option("--dry-run", action="store_true", dest="dry_run",
                       default=False,
                       help="Evaluate and print, but never send notifications")
    (options, args) = parser.parse_args()

    if not options.user_id:
        parser.error("--user is required")

    config_path, config_dict = weecfg.read_config(options.config_path, args)
    print("Using configuration file %s" % config_path)

    # Build a slim engine so UserAlerts has a real db_binder to query.
    config_dict['Engine']['Services'] = {}
    engine = weewx.engine.StdEngine(config_dict)

    ua = UserAlerts(engine, config_dict)

    if options.dry_run:
        Channels.DISPATCH = {
            'telegram': lambda cfg, subj, text: print("[DRY RUN telegram]", text),
            'email': lambda cfg, subj, text: print("[DRY RUN email]", subj, text),
        }

    db_manager = engine.db_binder.get_manager()
    # Use the most recent real archive record as the test record.
    record = db_manager.getRecord(db_manager.lastGoodStamp())
    if record is None:
        exit("No archive records found to test against.")

    print("Testing against record at %s" % timestamp_to_string(record['dateTime']))
    ua._process_user(options.user_id,
                      os.path.join(ua.users_dir, options.user_id + '.json'),
                      record, db_manager)
    print("State written to %s" % ua._state_path(options.user_id))

    # _process_user() may have kicked off background daemon threads to
    # actually send the alerts (see UserAlerts._dispatch). Those threads
    # get killed the instant this script's main thread exits, which (unlike
    # the long-running weewxd service) happens almost immediately here --
    # often before a network send has finished. Wait for them so this
    # stand-alone run reflects real delivery, not just "thread started".
    for t in threading.enumerate():
        if t is not threading.main_thread():
            t.join(timeout=DEFAULT_NET_TIMEOUT + 5)
