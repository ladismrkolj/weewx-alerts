# weewx-alerts

A per-user, JSON-configured alerting service for [WeeWX](https://weewx.com). Define one or
more alerts per person -- each a Python-like expression evaluated against every new archive
record (`outTemp < 32.0`, a rolling `avg('windSpeed', 30) > 20`, ...) -- and get notified over
Telegram and/or email when it fires, with a templated message built from live station data.

Two independent pieces, deliberately decoupled so they're safe to run at the same time with no
locking (see the header comment in `bin/user/useralerts.py` for why):

- **`bin/user/useralerts.py`** -- the worker. A WeeWX service that evaluates alerts and sends
  notifications. This is the only piece that's *required*.
- **`web/`** -- an optional small Flask config panel for creating/editing per-user alert configs
  and linking a Telegram bot without hand-editing JSON. See [web/README.md](web/README.md) for
  what it does, its security model, and deploying it beyond your own LAN.

This README covers installing the worker on a WeeWX station. For the web panel, do that first,
then follow [web/README.md](web/README.md).

## Requirements

- WeeWX 5.x, Python 3.7+ (same requirement as WeeWX itself). No third-party Python packages --
  `useralerts.py` only uses the standard library plus WeeWX's own `weewx`/`weeutil`.
- A Telegram bot token (free, via [@BotFather](https://t.me/BotFather)) and/or SMTP credentials,
  for whichever channels you actually want to send over -- neither is required just to install
  the service (an alert with no channels configured just evaluates + logs).

## Installing the worker

This repo has an `install.py`, so `weectl extension install` -- WeeWX's own extension
installer -- does the whole thing in one command: copies `bin/user/useralerts.py` into
`BIN_ROOT`, registers `user.useralerts.UserAlerts` in `report_services`, adds a `[UserAlerts]`
section to `weewx.conf` (pre-filled with sane defaults, including `[[Web]]` for the web panel
below), and creates `users_dir`/`state_dir`. It figures out `BIN_ROOT`/`WEEWX_ROOT` itself, so
this is the same command regardless of whether WeeWX was installed via `pip` or as an
apt/rpm package:

```bash
weectl extension install /path/to/weewx-alerts --config /path/to/weewx.conf
# or straight from GitHub, no local clone needed:
weectl extension install https://github.com/ladismrkolj/weewx-alerts/archive/refs/heads/main.zip \
    --config /path/to/weewx.conf
```

(Omit `--config` to use WeeWX's default config path for your install. Add `--dry-run` to preview
first, `-y`/`--yes` to skip the confirmation prompt.)

Then restart `weewxd` (however it's normally started/restarted on your system -- e.g.
`sudo systemctl restart weewx`) and check its log for a clean startup with no import errors.

`weectl extension list` shows it's installed; `weectl extension uninstall useralerts` reverses
everything it did (file, `report_services` entry, `[UserAlerts]` section) except your actual
`users_dir`/`state_dir` data, which it leaves alone.

**Add your first user config.** Either use the web panel below, or drop a file by hand into
`users_dir` (`user/useralerts/users/<user_id>.json` under `WEEWX_ROOT`, see `bin/user/useralerts.py`'s
header comment for the full schema, expression language, and template language reference):

```json
{
  "enabled": true,
  "channels": {
    "telegram": { "bot_token": "123456:ABC-DEF...", "chat_id": "987654321" }
  },
  "alerts": [
    {
      "id": "freeze_warning",
      "expression": "outTemp < 32.0",
      "template": "Freeze warning! outTemp={outTemp:.1f}F at {dateTime_str}",
      "subject": "Freeze warning: {outTemp:.0f}F",
      "channels": ["telegram"],
      "time_wait": 3600
    },
    {
      "id": "daytime_gust",
      "expression": "to_kts('windGust') is not None and to_kts('windGust') > 25 and 6 <= hour < 20 and weekday >= 5",
      "template": "Gust {to_kts('windGust'):.0f}kt from {compass(windDir)} at {dateTime_str}",
      "channels": ["telegram"],
      "time_wait": 1800
    }
  ]
}
```

The second alert shows three things worth knowing:

- `hour`, `weekday`, `yday` and friends are available as bare names, in local time on the
  WeeWX host -- so `6 <= hour < 20` really does mean daytime at the station.
- `compass(windDir)` turns degrees into `NW`. Pass a second argument for how many points:
  `compass(windDir, 4)` for N/E/S/W, 8 (the default) for NE/SE/SW/NW as well, 16 for
  NNE/ENE/... too.
- Each `{...}` is a full expression, not just a field name, so `A if cond else B` gives you
  if/else in a message -- e.g. `{'Overnight' if hour >= 22 or hour < 6 else 'Daytime'} gust`.

`subject` is optional and rendered with the same template language as `template`. Only
channels that have a subject line use it -- email puts it in the Subject header, Telegram
has no such thing and ignores it. Left out, it defaults to `WeeWX alert: <id>`.

If you do put a string literal in a template, prefer **single** quotes, as in that last
example. A template is not JSON-aware; it's just the string you store. Inside a JSON file
you'd have to write double quotes as `\"`, and if you then copy that escaped form into the
web panel (which takes the string directly, not JSON) the backslashes become literal, the
expression won't parse, and the placeholder is emitted verbatim as `{\"N\" if ...}`. Single
quotes need no escaping in either place, so the exact same text works in both.

```bash
sudo systemctl restart weewx     # or however weewxd is run/restarted on your system
journalctl -u weewx -f           # confirm it starts with no import errors, then evaluates
```

Writing `users/<user_id>.json` by hand works fine, but hand-editing JSON gets old fast once
you're managing more than one alert or user -- that's what the web panel (below) is for.

## The web config panel (optional)

`web/useralerts_web.py` is a small Flask app for creating/editing those per-user JSON configs
through a browser instead of by hand, and for linking a Telegram bot (paste a token, scan a QR
code) without running `tools/get_chat_id.py` yourself. It reads the same `[UserAlerts]` section
above -- nothing to configure twice.

See [web/README.md](web/README.md) for running it and its security model (**no password by
default** -- read this before it's reachable beyond your own LAN). For a public-facing station
server, `web/deploy/` has a ready-made nginx + basic-auth + systemd setup: `sudo
web/deploy/install.sh --config /path/to/weewx.conf --htpasswd-user someuser` does the whole
install in one command (venv, deps, systemd unit, password file, nginx config generation);
[web/README.md](web/README.md) also has the exact manual steps if you'd rather not run that
script.

## Uninstalling

```bash
weectl extension uninstall useralerts --config /path/to/weewx.conf
```

This removes `bin/user/useralerts.py`, the `user.useralerts.UserAlerts` entry in
`report_services`, and the `[UserAlerts]` section it added to `weewx.conf`, then restart
`weewxd`. It leaves `users_dir`/`state_dir` (your actual alert configs/data) untouched --
delete those by hand if you want them gone too.

If the web panel was deployed (see below), also `systemctl disable --now useralerts-web`, remove
its nginx `location` block, and `nginx -t && systemctl reload nginx`.
