# useralerts web panel

A small Flask app for editing per-user UserAlerts configs (`users_dir/<id>.json`) instead of
hand-editing JSON, and for linking a Telegram bot without running `tools/get_chat_id.py` by hand.

## Running

```bash
pip install -r requirements.txt        # into the same venv/ as weewx
python3 useralerts_web.py --config /path/to/weewx.conf
```

From the dev environment repo root, `./serve-panel.sh` does both the venv activation and the
`--config` path for you.

## Security model -- read this before exposing it beyond your own LAN

This is deliberately simple for v1:

- **No password.** Typing a name opens or creates that person's config. Anyone who can reach
  this panel can read and edit *anyone's* config, including their Telegram bot token.
- It's meant to run next to `weewxd` on a home network only -- the same trust model as WeeWX's
  own generated reports (`public_html/`).
- Flask's built-in dev server is used directly. Fine on a LAN, not meant for the open internet.

If this ever needs to be reachable outside your LAN, it needs real access control first (at
minimum, a per-user secret link instead of a bare name) -- don't just port-forward it as-is.

## Deploying behind nginx with a password

`deploy/` has a ready-made setup for running this on a public-facing station server, mounted
behind nginx with HTTP basic auth in front (since the app itself has none). Where it's mounted
and where it listens is configured once, in `weewx.conf`, not hand-copied into nginx:

```ini
[UserAlerts]
    [[Web]]
        url_path = /alerts                        # mount point behind the reverse proxy
        host = 127.0.0.1                           # loopback only -- nginx is the public entry point
        port = 8081
        htpasswd_file = /etc/nginx/.htpasswd_alerts # consumed by gen_nginx_conf.py only
```

- `useralerts_web.py` reads `host`/`port` from here as defaults (a CLI `--host`/`--port` still
  overrides, so `./serve-panel.sh` keeps working with no `[[Web]]` section at all).

**One command does the rest:**

```bash
sudo web/deploy/install.sh --config /path/to/weewx.conf --htpasswd-user someuser
```

This creates a venv and installs `requirements.txt` into it (handling the apt/rpm-package
gotcha where `weecfg`/`weewx.*` live under `/usr/share/weewx`, not real site-packages, so a
plain venv can't see them -- see the script's own comments), generates and installs
`useralerts-web.service` for wherever this checkout/venv actually are (no placeholder paths to
hand-edit), enables the service, prompts to set `someuser`'s password in the `htpasswd` file, and
finally prints the nginx `location` block from `deploy/gen_nginx_conf.py`. Paste that into the
`server {}` block that already serves your WeeWX `public_html` (must be the HTTPS one -- basic
auth sends credentials base64-encoded, not encrypted), then `nginx -t && systemctl reload nginx`
-- the one step left that has to happen in a file this script has no business touching itself.

Safe to re-run (e.g. after changing `[[Web]]`): it leaves an existing venv alone and always
regenerates the systemd unit and nginx block fresh, so they can't drift out of sync with
`weewx.conf`. Run `web/deploy/install.sh --help` for all options, or see `deploy/useralerts-web.service`
and `deploy/gen_nginx_conf.py`'s own header comments for what it's doing and how to do it by hand
instead (e.g. under config management like Ansible).

The app itself already knows how to sit behind a proxy like this (see the `ProxyFix` wiring in
`useralerts_web.py`), so `url_for()`-generated links/forms/redirects correctly come out under
`url_path` instead of pointing back at the un-prefixed root.

## How it works

- Reads `users_dir` out of the same `weewx.conf` / `[UserAlerts]` section the plugin itself
  uses, via `weecfg.read_config` -- one source of truth, nothing hardcoded.
- Never imports or runs `bin/user/useralerts.py`. It only reads/writes the JSON files under
  `users_dir`, atomically (temp file + `os.replace`) -- the same contract described in that
  file's own header comment, which is what makes it safe to run this alongside a live `weewxd`
  with no locking. It never touches `state_dir`, which only the running service owns.
- Telegram connect: validates the pasted bot token via `getMe`, shows a
  `t.me/<bot>?start=<code>` deep link + QR code, then polls `getUpdates` looking for a matching
  `/start <code>` message to learn the resulting `chat_id`. No incoming webhook needed, so this
  works even with the panel only reachable on your LAN (no public HTTPS endpoint required).
