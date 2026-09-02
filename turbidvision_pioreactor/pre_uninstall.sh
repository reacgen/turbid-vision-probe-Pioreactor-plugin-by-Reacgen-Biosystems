#!/usr/bin/env bash
set -eu

lighttpd_root="${TURBIDVISION_LIGHTTPD_ROOT:-/etc/lighttpd}"
systemd_root="${TURBIDVISION_SYSTEMD_ROOT:-/etc/systemd/system}"
sudoers_root="${TURBIDVISION_SUDOERS_ROOT:-/etc/sudoers.d}"
available="${lighttpd_root}/conf-available/60-turbidvision-probe-ui.conf"
enabled="${lighttpd_root}/conf-enabled/60-turbidvision-probe-ui.conf"

systemctl disable --now turbidvision-dashboard-sync.timer 2>/dev/null || true
systemctl stop turbidvision-dashboard-sync.service 2>/dev/null || true
systemctl disable --now turbidvision-dashboard-discovery.timer 2>/dev/null || true
systemctl stop turbidvision-dashboard-discovery.service 2>/dev/null || true
rm -f -- \
  "${systemd_root}/turbidvision-dashboard-sync.service" \
  "${systemd_root}/turbidvision-dashboard-sync.timer" \
  "${systemd_root}/turbidvision-dashboard-discovery.service" \
  "${systemd_root}/turbidvision-dashboard-discovery.timer"
rm -f -- "${sudoers_root}/turbidvision-dashboard"
systemctl daemon-reload
rm -f -- "${enabled}" "${available}"

if [ -f "${lighttpd_root}/lighttpd.conf" ] && [ -x /usr/sbin/lighttpd ]; then
  /usr/sbin/lighttpd -tt -f "${lighttpd_root}/lighttpd.conf"
  # A graceful reload can inherit a removed listener socket into the new
  # process. Restart so uninstall deterministically closes port 4300.
  systemctl restart lighttpd.service
fi

# Remove registered Flask routes after the uninstalling process has returned.
if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --quiet --collect \
    --unit="turbidvision-plugin-web-restart-$$" --on-active=3s \
    /bin/systemctl restart pioreactor-web.target
fi
