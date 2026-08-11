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
at `/alerts/` behind nginx with HTTP basic auth in front (since the app itself has none):

- `deploy/useralerts-web.service` -- systemd unit that runs the panel as a background service,
  bound to `127.0.0.1:8081` only (so nginx is the sole entry point).
- `deploy/nginx-alerts.conf` -- nginx `location` block for `/alerts/`, with `auth_basic` and a
  reverse proxy to that loopback port.

Both files have install steps in their own header comments. In short: copy the systemd unit
(editing its paths for your install), `systemctl enable --now` it, create an
`htpasswd`-generated password file, paste the nginx block into your existing site's `server {}`,
then `nginx -t && systemctl reload nginx`. Do this over HTTPS -- basic auth sends credentials
base64-encoded, not encrypted, on every request.

The app itself already knows how to sit behind a proxy like this (see the `ProxyFix` wiring in
`useralerts_web.py`), so `url_for()`-generated links/forms/redirects correctly come out under
`/alerts/...` instead of pointing back at the un-prefixed root.

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
