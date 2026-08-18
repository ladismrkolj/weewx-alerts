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

The app itself already knows how to sit behind a proxy like this (see the `ProxyFix` wiring in
`useralerts_web.py`), so `url_for()`-generated links/forms/redirects correctly come out under
`url_path` instead of pointing back at the un-prefixed root, regardless of which install method
below you use.

### Automated: `deploy/install.sh` (recommended)

```bash
sudo web/deploy/install.sh --config /path/to/weewx.conf --htpasswd-user someuser
```

This creates a venv and installs `requirements.txt` into it (handling the apt/rpm-package
gotcha where `weecfg`/`weewx.*` live under `/usr/share/weewx`, not real site-packages, so a
plain venv can't see them -- see "Manual install" below, and the script's own comments), generates
and installs `useralerts-web.service` for wherever this checkout/venv actually are (no
placeholder paths to hand-edit), enables the service, prompts to set `someuser`'s password in the
`htpasswd` file, and finally prints the nginx `location` block from `deploy/gen_nginx_conf.py`.
Paste that into the `server {}` block that already serves your WeeWX `public_html` (must be the
HTTPS one -- basic auth sends credentials base64-encoded, not encrypted), then
`nginx -t && systemctl reload nginx` -- the one step left that has to happen in a file this
script has no business touching itself.

Safe to re-run (e.g. after changing `[[Web]]`): it leaves an existing venv alone and always
regenerates the systemd unit and nginx block fresh, so they can't drift out of sync with
`weewx.conf`. Run `web/deploy/install.sh --help` for all options.

### Manual install (without `install.sh`)

Everything `install.sh` does, spelled out, for anyone who'd rather run it by hand or drive it
through config management (Ansible etc.) instead. All paths below assume the Debian/apt `weewx`
package's layout (`weewx.conf` at `/etc/weewx/weewx.conf`, user/group `weewx`) with this repo
checked out at `/opt/weewx-alerts` -- adjust for your own layout (e.g. a `pip`-installed WeeWX
has its own venv already, see step 1).

**1. Create a venv and install dependencies:**

```bash
sudo python3 -m venv --system-site-packages /opt/weewx-alerts/web/venv
sudo /opt/weewx-alerts/web/venv/bin/pip install -r /opt/weewx-alerts/web/requirements.txt
```

`--system-site-packages` pulls in the apt package's runtime deps (e.g. `python3-configobj`), but
**not** `weecfg`/`weewx.*` themselves -- on an apt/rpm install those live under
`/usr/share/weewx`, which is never installed as an actual site-packages entry (only
`weewxd`'s/`weectl`'s own wrapper scripts add it to `sys.path` when *they* run something). Check:

```bash
/opt/weewx-alerts/web/venv/bin/python3 -c "import weecfg, weewx.units"
```

If that fails, add `/usr/share/weewx` explicitly (adjust the path if this system's weewx source
lives elsewhere, e.g. inside a `pip`-installed venv's own site-packages, in which case just reuse
that venv directly instead of creating a new one):

```bash
echo /usr/share/weewx | sudo tee /opt/weewx-alerts/web/venv/lib/python3*/site-packages/weewx_bindir.pth
```

**2. Install the systemd unit.** Copy `deploy/useralerts-web.service`, edit its `User`/`Group`,
`WorkingDirectory`, `ExecStart` (venv/script paths), and `--config` to match your layout if they
differ from the defaults already in the file, then:

```bash
sudo cp deploy/useralerts-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now useralerts-web
systemctl status useralerts-web   # confirm it's up
```

**3. Create the basic-auth password file** (pick any username):

```bash
sudo apt-get install apache2-utils   # provides htpasswd; RHEL/Fedora: httpd-tools
sudo htpasswd -c /etc/nginx/.htpasswd_alerts someuser
```

(Drop `-c` for additional users on later runs -- it truncates the file.)

**4. Generate and install the nginx block:**

```bash
/opt/weewx-alerts/web/venv/bin/python3 deploy/gen_nginx_conf.py --config /etc/weewx/weewx.conf
```

Paste the output into the `server {}` block that already serves your WeeWX `public_html` (must
be the HTTPS one), then:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## How it works

- Reads `users_dir` out of the same `weewx.conf` / `[UserAlerts]` section the plugin itself
  uses, via `weecfg.read_config` -- one source of truth, nothing hardcoded.
- Only ever reads/writes the JSON files under `users_dir`, atomically (temp file +
  `os.replace`) -- the same contract described in `bin/user/useralerts.py`'s own header
  comment, which is what makes it safe to run this alongside a live `weewxd` with no
  locking. It never touches `state_dir`, which only the running service owns, and it never
  runs the service.
- **Test button** (in the alert editor): evaluates the expression you've typed and renders
  the template against your station's *latest archive record*, and reports whether the alert
  would fire, what the message would look like, and the exact error behind any placeholder
  that failed. Nothing is saved and no message is sent -- it's a dry run of one evaluation
  pass. To keep the panel's answer and the service's behaviour from drifting apart, it
  imports `useralerts.py` (the installed `user.useralerts` if importable, otherwise the copy
  in this checkout) and reuses its `Aggregator` / `UnitConverter` / `render_template`, so
  `avg()`, `to_C()`, `compass()` and the rest mean exactly what they mean in production. The
  archive database is opened read-only (SQLite `mode=ro`), so this never contends with
  `weewxd`; on a non-SQLite backend the test button reports that it has no record to test
  against, and everything else in the panel works as before.
- Telegram connect: validates the pasted bot token via `getMe`, shows a
  `t.me/<bot>?start=<code>` deep link + QR code, then polls `getUpdates` looking for a matching
  `/start <code>` message to learn the resulting `chat_id`. No incoming webhook needed, so this
  works even with the panel only reachable on your LAN (no public HTTPS endpoint required).
