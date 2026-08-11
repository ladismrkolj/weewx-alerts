# installer for weewx-alerts
#
# Lets `weectl extension install` do what README.md's "Installing the worker"
# section otherwise walks through by hand: copy bin/user/useralerts.py into
# BIN_ROOT, register user.useralerts.UserAlerts in report_services, and add
# a [UserAlerts] section to weewx.conf (including [[Web]], pre-filled with
# the defaults web/deploy/gen_nginx_conf.py and useralerts_web.py both
# assume) -- all in one command, on any install layout (pip venv or
# apt/rpm package), since `weectl` resolves BIN_ROOT/WEEWX_ROOT itself.
#
# It does NOT install the optional web panel (web/) -- that's a separate
# Flask process with its own Python deps (flask, qrcode) and, for anything
# beyond your LAN, its own systemd unit + nginx config, none of which
# `weectl extension install` has a mechanism for. See web/README.md.

import os

from weecfg.extension import ExtensionInstaller


def loader():
    return UserAlertsInstaller()


class UserAlertsInstaller(ExtensionInstaller):
    def __init__(self):
        super(UserAlertsInstaller, self).__init__(
            version="1.0",
            name='useralerts',
            description='Per-user, JSON-configured alerting service (Telegram/email) for WeeWX.',
            author="Ladi Smrkolj",
            author_email="",
            report_services='user.useralerts.UserAlerts',
            config={
                'UserAlerts': {
                    'enable': 'true',
                    'users_dir': 'user/useralerts/users',
                    'state_dir': 'user/useralerts/state',
                    'Web': {
                        'url_path': '/alerts',
                        'host': '127.0.0.1',
                        'port': '8081',
                        'htpasswd_file': '/etc/nginx/.htpasswd_alerts',
                    },
                },
            },
            files=[('bin/user', ['bin/user/useralerts.py'])],
        )

    def configure(self, engine):
        # users_dir isn't auto-created by the service itself (it only warns
        # if missing, see useralerts.py), unlike state_dir, which the
        # service creates on its own the first time it runs. Create both
        # here so there's nothing left for a first run to warn about.
        weewx_root = engine.root_dict['WEEWX_ROOT']
        for rel_dir in ('user/useralerts/users', 'user/useralerts/state'):
            path = os.path.join(weewx_root, rel_dir)
            if engine.dry_run:
                engine.printer.out(f"Fake creating directory {path}", level=2)
            else:
                os.makedirs(path, exist_ok=True)
                engine.printer.out(f"Created directory {path}", level=2)
        # We didn't modify config_dict ourselves -- the declarative 'config'
        # above already triggers its own save -- so nothing more to report.
        return False
