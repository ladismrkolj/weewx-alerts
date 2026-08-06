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
#       "template": "Sustained wind! 30-min avg windSpeed={windSpeed}, gust now={windGust}",
#       "channels": ["telegram"],
#       "time_wait": 1800
#     }
#   ]
# }
#
# --------------------------------------------------------------------------
# Expression language
#
#   - Any field in the current archive record is available by name directly,
#     e.g. outTemp, windSpeed, dateTime, ...
#   - avg(obs, minutes) / amin(obs, minutes) / amax(obs, minutes) / asum(obs, minutes)
#     compute a rolling aggregate ending at the current record's dateTime,
#     read straight from the archive database. Returns None if there is no
#     data in that window.
#   - A small set of safe builtins are available: abs, round, min, max, len
#   - Expressions that reference a missing field, or that raise any
#     exception, are logged and simply treated as "not triggered" for that
#     pass; they never crash the service or affect other alerts/users.
#
# Template language
#
#   - Plain str.format() syntax against a dict of: every field in the
#     current archive record, plus dateTime_str (human readable time) and
#     alert_id. Missing fields are left as literal "{field}" rather than
#     raising, so a typo in a template never crashes a send.
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


class SafeFormatDict(dict):
    """A dict that leaves '{missing_key}' untouched in str.format_map()
    instead of raising a KeyError, so a bad template field never crashes
    a send."""

    def __missing__(self, key):
        return '{' + key + '}'


def _validate_obs_name(obs):
    """Guard against anything but a plain identifier being interpolated
    into SQL for the avg/amin/amax/asum helpers."""
    if not isinstance(obs, str) or not OBS_NAME_RE.match(obs):
        raise ValueError("Invalid observation type: %r" % (obs,))
    return obs


class Aggregator:
    """Builds the avg()/amin()/amax()/asum() functions bound to a specific
    db_manager and a specific 'as of' timestamp (the current record's
    dateTime), for use inside an alert expression's eval namespace."""

    def __init__(self, db_manager, end_ts):
        self.db_manager = db_manager
        self.end_ts = end_ts

    def _aggregate(self, sql_func, obs, minutes):
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
        return row[0] if row and row[0] is not None else None

    def avg(self, obs, minutes):
        return self._aggregate('AVG', obs, minutes)

    def amin(self, obs, minutes):
        return self._aggregate('MIN', obs, minutes)

    def amax(self, obs, minutes):
        return self._aggregate('MAX', obs, minutes)

    def asum(self, obs, minutes):
        return self._aggregate('SUM', obs, minutes)

    def as_namespace(self):
        return {'avg': self.avg, 'amin': self.amin,
                'amax': self.amax, 'asum': self.asum}


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


def render_template(template, record, alert_id):
    ctx = SafeFormatDict(record)
    ctx['alert_id'] = alert_id
    if 'dateTime' in record:
        ctx['dateTime_str'] = timestamp_to_string(record['dateTime'])
    try:
        return template.format_map(ctx)
    except Exception as e:
        log.warning("Alert '%s': template render failed: %s", alert_id, e)
        return template


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

        aggregator = Aggregator(db_manager, record['dateTime'])

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
        namespace = dict(record)
        namespace.update(aggregator.as_namespace())

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
            self._dispatch(user_id, alert, record, channels_cfg)

        return True

    # -- dispatch (runs in a background thread; never touches state) ----

    def _dispatch(self, user_id, alert, record, channels_cfg):
        alert_id = alert['id']
        template = alert.get('template', 'Alert {alert_id} triggered')
        subject = alert.get('subject', 'WeeWX alert: %s' % alert_id)
        text = render_template(template, record, alert_id)
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
