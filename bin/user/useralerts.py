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
#       "image_url": "http://192.168.1.47:1984/api/frame.jpeg?src=cam1",
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
#   - compass(windDir) turns degrees into a direction name: 'NW'. Pass a
#     second argument for how many points you want -- compass(windDir, 4)
#     for N/E/S/W, 8 (the default) for NE/SE/SW/NW as well, 16 for
#     NNE/ENE/... too. Takes either the degrees, compass(windDir), or the
#     name of a field to read them from, compass('windDir'). Rounds to the
#     nearest sector and wraps at 360, so 350 and 10 are both 'N'. Returns
#     None (never raises) for a missing or non-numeric value. Mostly useful
#     in a template, e.g. "Wind from {compass(windDir)}".
#   - Time and date of the record are available as bare names, in *local time*
#     on the WeeWX host (the same clock dateTime_str is rendered in, so DST is
#     handled for you): hour (0-23), minute (0-59), minute_of_day (0-1439),
#     weekday (0=Monday .. 6=Sunday), day (of month), month (1-12), yday (day
#     of year, 1-366), year. These describe the *record's* timestamp, not
#     wall-clock "now" -- normally the same thing, but not when catching up on
#     backlogged records. Examples:
#       nights only     hour >= 22 or hour < 6
#       weekends only   weekday >= 5
#       a season        100 <= yday <= 250
#     combine with any other condition, e.g.
#       to_kts('windGust') is not None and to_kts('windGust') > 25 and 6 <= hour < 20
#     (If your station's archive schema happens to have a field with one of
#     these names, the real field wins and the time value is not injected.)
#   - An expression may span several lines: it is compiled wrapped in
#     parentheses, so a long condition can be broken up and indented the way
#     it would be in real code --
#         7 <= hour < 19
#             and avg('windGust', 30) is not None
#             and avg('windGust', 30, unit='knot') > 2
#     is the same as writing it all on one line. (In a users/<id>.json file
#     the line breaks have to be written as \n, since JSON strings can't
#     contain a literal newline; typed into the web panel they're just typed.)
#   - dateTime_str (human readable time of the record) and alert_id are
#     injected for template placeholders only -- they are NOT available in
#     "expression". The raw dateTime field is available in both.
#   - Expressions that reference a missing field, or that raise any
#     exception, are logged and simply treated as "not triggered" for that
#     pass (in "expression") or left as the literal "{original text}" (in a
#     template placeholder); they never crash the service or affect other
#     alerts/users.
#
# Snapshot image  (optional)
#
#   - "image_url" attaches a picture to the alert: it is fetched at send
#     time and goes out with the message -- as a photo on telegram, as an
#     attachment on email. Typically a webcam's still-frame endpoint, e.g.
#     "http://192.168.1.47:1984/api/frame.jpeg?src=cam1" (go2rtc). Any URL
#     that returns an image works.
#   - The frame is fetched once per alert and shared by every channel, in
#     the same background thread that does the sending, so a slow camera
#     never holds up the archive loop.
#   - A camera that is unreachable, slow, or serving something that isn't an
#     image is logged and the message is sent anyway, without the picture --
#     losing a freeze warning because a webcam is down would be a bad trade.
#   - By default the frame is shrunk before sending: scaled down to
#     "image_max_width" (default 1280, never scaled up) and re-encoded as
#     JPEG at "image_quality" (default 70). Set "image_compress": false to
#     send the camera's own bytes untouched. Compression needs Pillow, which
#     is NOT a dependency of this plugin -- without it the original bytes are
#     sent and a debug line is logged.
#
#     "image_url": "http://192.168.1.47:1984/api/frame.jpeg?src=cam1",
#     "image_max_width": 1280,
#     "image_quality": 70
#
# Subject
#
#   - "subject" is optional, and is used by channels that have a notion of
#     one -- email puts it in the Subject header; telegram ignores it, since
#     a Telegram message has no subject line. Defaults to
#     "WeeWX alert: <alert id>".
#   - It is rendered with the same template language as "template" below, so
#     it can carry a reading rather than being a fixed string, e.g.
#     "Freeze warning: {outTemp:.0f}F at {dateTime_str}".
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
#   - Because a placeholder is a full expression, Python's conditional
#     expression gives you if/else in a message, and it chains for a
#     multi-way choice:
#       Wind from {'N' if windDir >= 330 or windDir < 30 else
#                  'E' if windDir < 120 else
#                  'S' if windDir < 210 else
#                  'W' if windDir < 300 else 'NW'}
#       {'Overnight' if hour >= 22 or hour < 6 else 'Daytime'} freeze warning
#     (written on one line -- a placeholder can't span lines usefully). Two
#     limits: a placeholder ends at the *first* '}', so no dicts or sets
#     inside one, and only expressions work -- no loops, no statements.
#     Prefer SINGLE quotes for string literals in a template. A template is
#     just a stored string, not JSON: written into a users/<id>.json file by
#     hand, double quotes have to be escaped as \" -- and if that escaped
#     form is then pasted into the web panel (which takes the string
#     directly, not JSON), the backslashes are literal, the expression won't
#     parse, and you get the placeholder back verbatim in your message.
#     Single quotes need no escaping in either place.
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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
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


# Compass point names, by how many points the caller asked for. Each tuple
# starts at North and runs clockwise.
_COMPASS_POINTS = {
    4: ('N', 'E', 'S', 'W'),
    8: ('N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'),
    16: ('N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
         'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'),
}


def make_compass(record):
    """Builds the compass() function for the eval namespace, bound to a
    record so that it can take a field name as well as a bare number."""

    def compass(value, points=8):
        """Wind direction as a name instead of degrees:
        compass(windDir) -> 'NW'. points is 4 (N/E/S/W), 8 (adds NE/SE/SW/NW,
        the default) or 16 (adds NNE/ENE/...). Takes either the degrees
        themselves, compass(windDir), or the name of a field to read them
        from, compass('windDir') -- both work, so whichever habit you have
        from the other helpers is fine. Rounds to the nearest sector and
        wraps at 360, so 350 and 10 are both 'N'. Returns None rather than
        raising for a missing or non-numeric value, same as to_kts() and
        convert(), so a gap in the data never crashes a send."""
        try:
            names = _COMPASS_POINTS[points]
        except (KeyError, TypeError):
            raise ValueError("compass() points must be 4, 8 or 16, not %r"
                             % (points,))
        if isinstance(value, str):
            # A field name -- look up the record's current value for it.
            value = record.get(_validate_obs_name(value))
        sector = 360.0 / len(names)
        try:
            index = int(float(value) % 360 / sector + 0.5)
        except (TypeError, ValueError, OverflowError):
            # Missing, non-numeric, or NaN/infinity (which survive float()
            # but blow up in int()) -- all just "no direction to report".
            return None
        return names[index % len(names)]

    return compass


def time_namespace(ts):
    """Calendar values for a record's timestamp, as bare names for use in
    expressions and templates: hour, minute, minute_of_day, weekday, day,
    month, yday, year. Local time on the WeeWX host -- the same clock
    dateTime_str is rendered in -- so DST is handled for free and
    "hour >= 22" means 10pm at the station, not 10pm UTC."""
    lt = time.localtime(ts)
    return {
        'hour': lt.tm_hour,
        'minute': lt.tm_min,
        'minute_of_day': lt.tm_hour * 60 + lt.tm_min,
        'weekday': lt.tm_wday,          # 0 = Monday .. 6 = Sunday
        'day': lt.tm_mday,
        'month': lt.tm_mon,
        'yday': lt.tm_yday,
        'year': lt.tm_year,
    }


# -- snapshot images ---------------------------------------------------------
#
# An alert may name an "image_url" -- typically a webcam's still-frame
# endpoint, e.g. http://192.168.1.47:1984/api/frame.jpeg?src=cam1 on a
# go2rtc/frigate box. When it does, that frame is fetched at send time and
# goes out with the message: as a photo on telegram, as an attachment on
# email. A camera that's unreachable, slow or serving something that isn't
# an image is never fatal -- the message still goes, just without a picture.

DEFAULT_IMAGE_TIMEOUT = 10       # seconds to wait for the camera
MAX_IMAGE_BYTES = 20 * 1024 * 1024   # refuse anything absurd from a bad URL
DEFAULT_IMAGE_MAX_WIDTH = 1280   # resized down to this before sending
DEFAULT_IMAGE_QUALITY = 70       # JPEG quality when recompressing


def fetch_image(url, timeout=DEFAULT_IMAGE_TIMEOUT):
    """GET `url` and return (bytes, content_type), or raise. Reads at most
    MAX_IMAGE_BYTES so a misconfigured URL (a video stream, say) can't sit
    there filling memory."""
    req = urllib.request.Request(url, headers={'User-Agent': 'weewx-useralerts'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip()
        data = resp.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image at %s is larger than %d bytes"
                          % (url, MAX_IMAGE_BYTES))
    if not data:
        raise ValueError("Image at %s was empty" % url)
    if content_type and not content_type.startswith('image/'):
        raise ValueError("%s returned %s, not an image" % (url, content_type))
    return data, content_type or 'image/jpeg'


def compress_image(data, max_width=DEFAULT_IMAGE_MAX_WIDTH,
                    quality=DEFAULT_IMAGE_QUALITY):
    """Shrink a snapshot for sending: scale down to max_width (never up) and
    re-encode as JPEG at `quality`. Returns (bytes, content_type).

    Needs Pillow, which is NOT a dependency of this plugin -- without it (or
    if the image can't be decoded, or if compressing made it bigger, which
    happens with an already-tiny frame) the original bytes are returned
    unchanged. Shrinking is a nicety; sending the picture at all is the
    feature."""
    try:
        import io
        from PIL import Image
    except ImportError:
        log.debug("Pillow not installed; sending the snapshot uncompressed")
        return data, None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        if max_width and img.width > max_width:
            height = max(1, round(img.height * max_width / img.width))
            img = img.resize((max_width, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=int(quality), optimize=True)
    except Exception as e:
        log.debug("Could not compress snapshot, sending it as-is: %s", e)
        return data, None
    out = buf.getvalue()
    if len(out) >= len(data):
        return data, None
    return out, 'image/jpeg'


def alert_image(alert, timeout=DEFAULT_IMAGE_TIMEOUT):
    """The snapshot to send with `alert`, as (bytes, content_type, filename),
    or None if the alert has no image_url. Raises if the fetch itself failed
    -- the caller decides whether that's worth aborting a send over (it
    isn't: see _send_all)."""
    url = (alert.get('image_url') or '').strip()
    if not url:
        return None
    data, content_type = fetch_image(url, timeout)
    if to_bool(alert.get('image_compress', True)):
        data, new_type = compress_image(
            data,
            alert.get('image_max_width', DEFAULT_IMAGE_MAX_WIDTH),
            alert.get('image_quality', DEFAULT_IMAGE_QUALITY))
        content_type = new_type or content_type
    ext = 'jpg' if 'jpeg' in content_type or 'jpg' in content_type \
        else content_type.rsplit('/', 1)[-1] or 'jpg'
    return data, content_type, 'snapshot.%s' % ext


def encode_multipart(fields, file_field, filename, content_type, data):
    """Build a multipart/form-data body by hand -- urllib has no equivalent
    of requests' files=, and this plugin deliberately has no third-party
    dependencies. Returns (body_bytes, content_type_header)."""
    boundary = '----weewx%s' % os.urandom(12).hex()
    out = []
    for name, value in fields.items():
        out.append(('--%s\r\n'
                    'Content-Disposition: form-data; name="%s"\r\n\r\n'
                    '%s\r\n' % (boundary, name, value)).encode())
    out.append(('--%s\r\n'
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                'Content-Type: %s\r\n\r\n' % (boundary, file_field, filename,
                                                content_type)).encode())
    out.append(data)
    out.append(('\r\n--%s--\r\n' % boundary).encode())
    return b''.join(out), 'multipart/form-data; boundary=%s' % boundary


# Telegram rejects a photo caption longer than this; a longer message is
# sent as its own text message after the photo instead of being truncated.
TELEGRAM_CAPTION_LIMIT = 1024


class Channels:
    """Static senders for each supported alert channel. Each takes the
    channel's connection settings (from the user's config), the
    already-rendered message text, and optionally a snapshot image as
    (bytes, content_type, filename) -- see alert_image()."""

    @staticmethod
    def _telegram_post(bot_token, method, body, content_type=None):
        url = "https://api.telegram.org/bot%s/%s" % (bot_token, method)
        headers = {'Content-Type': content_type} if content_type else {}
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=DEFAULT_NET_TIMEOUT) as resp:
            payload = resp.read()
            if resp.status != 200:
                raise IOError("Telegram API returned status %s: %s"
                               % (resp.status, payload))

    @staticmethod
    def send_telegram(chan_cfg, subject, text, image=None):
        bot_token = chan_cfg.get('bot_token')
        chat_id = chan_cfg.get('chat_id')
        if not bot_token or not chat_id:
            raise ValueError("telegram channel missing bot_token/chat_id")

        if image is not None:
            data, content_type, filename = image
            # A caption over Telegram's limit would be truncated, so anything
            # longer goes as its own message after the photo instead.
            caption = text if len(text) <= TELEGRAM_CAPTION_LIMIT else ''
            body, body_type = encode_multipart(
                {'chat_id': chat_id, 'caption': caption},
                'photo', filename, content_type, data)
            Channels._telegram_post(bot_token, 'sendPhoto', body, body_type)
            if caption:
                return
            # fall through and send the long text separately

        body = urllib.parse.urlencode({'chat_id': chat_id, 'text': text}).encode()
        Channels._telegram_post(bot_token, 'sendMessage', body)

    @staticmethod
    def send_email(chan_cfg, subject, text, image=None):
        smtp_host = chan_cfg['smtp_host']
        smtp_user = chan_cfg.get('smtp_user')
        smtp_password = chan_cfg.get('smtp_password')
        from_addr = chan_cfg.get('from', 'alerts@example.com')
        to_addrs = chan_cfg['to']
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]

        if image is None:
            msg = MIMEText(text)
        else:
            data, content_type, filename = image
            msg = MIMEMultipart()
            msg.attach(MIMEText(text))
            subtype = content_type.rsplit('/', 1)[-1] or 'jpeg'
            part = MIMEImage(data, _subtype=subtype)
            part.add_header('Content-Disposition', 'attachment', filename=filename)
            msg.attach(part)
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


def eval_expression(expression, namespace):
    """Evaluate one alert expression against `namespace`, with only
    SAFE_BUILTINS available.

    The expression is wrapped in parentheses before compiling, which is what
    lets it span several lines: bare

        outTemp < 32
            and windSpeed > 10

    is an IndentationError to Python, but inside parentheses -- exactly like
    a long condition wrapped across lines in real code -- it's just one
    expression. Wrapping changes nothing else: any expression in parentheses
    is the same expression. Exceptions propagate to the caller, which
    decides what a failure means (the service: "didn't trigger"; the web
    panel's debugger: the thing to show you)."""
    code = compile('(\n%s\n)' % expression, '<expression>', 'eval')
    return eval(code, {'__builtins__': SAFE_BUILTINS}, namespace)


def render_template(template, namespace, alert_id, errors=None):
    """Render a template string. Each {...} placeholder is evaluated as a
    Python expression against `namespace` (record fields + avg/amin/amax/
    asum + to_C/to_F/to_kts/to_mps/convert -- the same namespace used for
    "expression"), optionally followed by ':' and a str.format() format
    spec, e.g. {avg('windSpeed', 30, unit='kts'):.1f}. A plain field name
    like {outTemp} still works too, since it's just a name lookup. {{ and
    }} are literal braces, like str.format(). If a placeholder's expression
    raises (missing field, bad syntax, ...), that placeholder is left as
    the literal "{original text}" rather than raising, so one bad field
    never crashes the whole send.

    If `errors` is a list, every placeholder that failed is appended to it as
    {'field': <original text>, 'error': <message>} -- used by the web panel's
    "Test" button to show *why* a placeholder came back as literal text.
    Nothing in the service itself passes it."""
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
                value = eval_expression(expr, ns)
                out.append(format(value, spec) if spec else str(value))
            except Exception as e:
                log.debug("Alert '%s': template field '{%s}' failed: %s",
                          alert_id, content, e)
                if errors is not None:
                    # e.msg for a SyntaxError: str() would append
                    # "(<expression>, line 2)", which is about the wrapper
                    # eval_expression() compiles, not the user's text.
                    message = e.msg if isinstance(e, SyntaxError) else e
                    errors.append({'field': content,
                                   'error': '%s: %s' % (type(e).__name__, message)})
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
        # + to_C()/to_F()/to_kts()/to_mps()/convert() + compass() + local
        # time/date values
        namespace = dict(record)
        namespace.update(aggregator.as_namespace())
        namespace.update(UnitConverter(record).as_namespace())
        namespace['compass'] = make_compass(record)
        if 'dateTime' in record:
            # setdefault, not update: if a station's schema happens to have a
            # field named e.g. 'day', the real field wins and nothing silently
            # changes meaning.
            for key, value in time_namespace(record['dateTime']).items():
                namespace.setdefault(key, value)

        try:
            triggered = bool(eval_expression(expression, namespace))
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
        subject = alert.get('subject') or 'WeeWX alert: %s' % alert_id
        text = render_template(template, namespace, alert_id)
        # The subject is a template too, so it can carry the actual reading:
        # "Freeze warning: {outTemp:.0f}F" reads better in a mailbox than a
        # fixed string. Same rules, same failure behaviour.
        subject = render_template(subject, namespace, alert_id)
        channel_names = alert.get('channels', [])

        t = threading.Thread(
            target=self._send_all,
            args=(user_id, alert_id, channel_names, channels_cfg,
                  subject, text, alert),
            daemon=True)
        t.start()

    def _send_all(self, user_id, alert_id, channel_names, channels_cfg,
                  subject, text, alert=None):
        # Fetched once here, in the sending thread, and shared by every
        # channel -- one camera hit per alert, not one per channel. A camera
        # that's down, slow or serving junk must never cost you the alert
        # itself, so a failure here is logged and the message goes without
        # the picture.
        image = None
        if alert:
            try:
                image = alert_image(alert)
            except Exception as e:
                log.warning("UserAlerts: user '%s' alert '%s': could not fetch "
                            "the snapshot from %s (%s) -- sending without it",
                            user_id, alert_id, alert.get('image_url'), e)

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
                sender(chan_cfg, subject, text, image)
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
            'telegram': lambda cfg, subj, text, image=None: print(
                "[DRY RUN telegram]", text,
                "(+%d byte snapshot)" % len(image[0]) if image else ''),
            'email': lambda cfg, subj, text, image=None: print(
                "[DRY RUN email]", subj, text,
                "(+%d byte snapshot)" % len(image[0]) if image else ''),
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
