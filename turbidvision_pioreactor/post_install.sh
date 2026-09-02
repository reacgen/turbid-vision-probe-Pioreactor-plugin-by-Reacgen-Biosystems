#!/usr/bin/env bash
set -eu

# Upgrades merge new UI assets but do not know about files removed from a later
# wheel. Delete only this plugin's superseded job, chart and export descriptors.
dot_pioreactor="${DOT_PIOREACTOR:-/home/pioreactor/.pioreactor}"
jobs_dir="${dot_pioreactor}/plugins/ui/jobs"
charts_dir="${dot_pioreactor}/plugins/ui/charts"
exports_dir="${dot_pioreactor}/plugins/exportable_datasets"
config_file="${dot_pioreactor}/config.ini"
unit_config_file="${dot_pioreactor}/unit_config.ini"
python_bin="${TURBIDVISION_PYTHON:-/opt/pioreactor/venv/bin/python}"
crudini_bin="$(command -v crudini || true)"
if [ -z "${crudini_bin}" ] && [ -x /opt/pioreactor/venv/bin/crudini ]; then
  crudini_bin=/opt/pioreactor/venv/bin/crudini
fi
rm -f -- \
  "${jobs_dir}/20_turbidvision_od_reading.yaml" \
  "${jobs_dir}/21_turbidvision_growth_rate.yaml" \
  "${charts_dir}/20_turbidvision_reflectance.yaml" \
  "${charts_dir}/21_turbidvision_density.yaml" \
  "${charts_dir}/22_turbidvision_normalized_reflectance.yaml" \
  "${charts_dir}/23_turbidvision_absolute_growth_rate.yaml" \
  "${exports_dir}/20_turbidvision_readings.yaml"

# Remove merged assets and config left by earlier releases. This plugin's
# runtime settings are deliberately cluster-wide, so migrate any old unit
# defaults into the shared config and remove the unit override section.
if [ -n "${crudini_bin}" ] && [ -f "${config_file}" ]; then
  for chart_key in \
    turbidvision_reflectance \
    turbidvision_density \
    turbidvision_normalized_reflectance \
    turbidvision_absolute_growth_rate
  do
    "${crudini_bin}" --del "${config_file}" ui.overview.charts "${chart_key}" 2>/dev/null || true
  done

  enable_growth_model="$("${crudini_bin}" --get "${config_file}" turbidvision_probe.config enable_growth_model 2>/dev/null || true)"
  if [ "${enable_growth_model}" != "0" ] && [ "${enable_growth_model}" != "1" ]; then
    legacy_mode=""
if [ -f "${unit_config_file}" ]; then
      legacy_mode="$("${crudini_bin}" --get "${unit_config_file}" turbidvision_probe.config mode 2>/dev/null || true)"
    fi
    if [ -z "${legacy_mode}" ]; then
      legacy_mode="$("${crudini_bin}" --get "${config_file}" turbidvision_probe.config mode 2>/dev/null || true)"
    fi
    if [ "${legacy_mode}" = "od_and_growth" ]; then
      enable_growth_model=1
    else
      enable_growth_model=0
    fi
  fi

  interval="$("${crudini_bin}" --get "${config_file}" turbidvision_probe.config interval 2>/dev/null || true)"
  if [ -z "${interval}" ] && [ -f "${unit_config_file}" ]; then
    interval="$("${crudini_bin}" --get "${unit_config_file}" turbidvision_probe.config interval 2>/dev/null || true)"
  fi
  if [ -z "${interval}" ]; then
    interval=5
  fi

  "${crudini_bin}" --set "${config_file}" turbidvision_probe.config enable_growth_model "${enable_growth_model}"
  "${crudini_bin}" --set "${config_file}" turbidvision_probe.config interval "${interval}"
  "${crudini_bin}" --del "${config_file}" turbidvision_probe.config mode 2>/dev/null || true
  "${crudini_bin}" --del "${config_file}" turbidvision_probe.config angle 2>/dev/null || true
  "${crudini_bin}" --del "${config_file}" turbidvision_probe.config initial_density_relative_tolerance 2>/dev/null || true
  "${crudini_bin}" --del "${config_file}" turbidvision_od_reading.config 2>/dev/null || true
  "${crudini_bin}" --del "${config_file}" turbidvision_growth_rate.config 2>/dev/null || true
  "${crudini_bin}" --del "${config_file}" turbidvision_probe.dashboard 2>/dev/null || true

  if [ -f "${unit_config_file}" ]; then
    "${crudini_bin}" --del "${unit_config_file}" turbidvision_probe.config 2>/dev/null || true
    "${crudini_bin}" --del "${unit_config_file}" turbidvision_od_reading.config 2>/dev/null || true
    "${crudini_bin}" --del "${unit_config_file}" turbidvision_growth_rate.config 2>/dev/null || true
  fi

  # Pioreactor's parser does not treat inline comments as comments, so place a
  # short explanation immediately above each remaining plugin setting. Remove
  # these exact markers first to keep upgrades idempotent.
  if [ -x "${python_bin}" ]; then
    "${python_bin}" - "${config_file}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
comments = {
    "interval": (
        "# Seconds between Pioreactor polling the TVP for latest measurements. This does "
        "not change how often the probe measures. Minimum 0.5; default 5."
    ),
    "enable_growth_model": (
        "# 0 publishes OD only; 1 also enables measurement normalization and growth rate."
    ),
}
markers = set(comments.values()) | {
    "# Seconds between probe polls. Minimum 1; default 5.",
    (
        "# Seconds between Pioreactor polls. This does not change how often the probe "
        "measures. Minimum 1; default 5."
    ),
    "# 0 publishes OD only; 1 also enables probe normalization and growth rate.",
}
lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line not in markers]
output: list[str] = []
in_section = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        in_section = stripped == "[turbidvision_probe.config]"
    if in_section and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in comments:
            output.append(comments[key])
    output.append(line)
path.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
  fi
fi

# Pioreactor's fleet page keeps the browser at /pioreactors while it opens a
# unit-specific Control dialog, so the URL alone cannot identify the selected
# card. Each worker serves its own job descriptor: stamp that worker's unit into
# the deployed copy. The descriptor's click handler deliberately ignores this
# value in the native Control All dialog, leaving the all-probe selector there.
deployed_job_descriptor="${jobs_dir}/20_turbidvision_probe.yaml"
if [ -x "${python_bin}" ] && [ -f "${deployed_job_descriptor}" ]; then
  local_unit="$(${python_bin} -c 'from pioreactor.whoami import get_unit_name; print(get_unit_name())')"
  case "${local_unit}" in
    ""|*[!A-Za-z0-9.-]*)
      printf 'Invalid Pioreactor unit name for Turbid Vision dashboard link: %s\n' "${local_unit}" >&2
      exit 1
      ;;
  esac
  descriptor_tmp="${deployed_job_descriptor}.tmp.$$"
  sed "s/@@PIOREACTOR_UNIT@@/${local_unit}/g" \
    "${deployed_job_descriptor}" > "${descriptor_tmp}"
  mv -f -- "${descriptor_tmp}" "${deployed_job_descriptor}"
fi

# Dashboard discovery routes belong on every unit, but the registry, proxy map,
# and timer belong only on the leader.
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
systemd_root="${TURBIDVISION_SYSTEMD_ROOT:-/etc/systemd/system}"
sudoers_root="${TURBIDVISION_SUDOERS_ROOT:-/etc/sudoers.d}"
sudo visudo -cf "${script_dir}/sudoers/turbidvision-dashboard" >/dev/null
sudo install -o root -g root -m 0440 \
  "${script_dir}/sudoers/turbidvision-dashboard" \
  "${sudoers_root}/turbidvision-dashboard"
sudo install -o root -g root -m 0644 \
  "${script_dir}/systemd/turbidvision-dashboard-discovery.service" \
  "${systemd_root}/turbidvision-dashboard-discovery.service"
sudo install -o root -g root -m 0644 \
  "${script_dir}/systemd/turbidvision-dashboard-discovery.timer" \
  "${systemd_root}/turbidvision-dashboard-discovery.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now turbidvision-dashboard-discovery.timer
sudo systemctl start turbidvision-dashboard-discovery.service

if [ -x "${python_bin}" ]; then
  is_leader="$(${python_bin} -c 'from pioreactor.whoami import am_I_leader; print(1 if am_I_leader() else 0)' 2>/dev/null || printf 0)"
else
  is_leader=0
fi
if [ "${TURBIDVISION_ASSUME_LEADER:-${is_leader}}" != "1" ]; then
  exit 0
fi

sudo install -o root -g root -m 0644 \
  "${script_dir}/systemd/turbidvision-dashboard-sync.service" \
  "${systemd_root}/turbidvision-dashboard-sync.service"
sudo install -o root -g root -m 0644 \
  "${script_dir}/systemd/turbidvision-dashboard-sync.timer" \
  "${systemd_root}/turbidvision-dashboard-sync.timer"
sudo systemctl daemon-reload
sudo systemctl enable --now turbidvision-dashboard-sync.timer

# Python plugin routes are registered only when Pioreactor's Flask processes
# start. Defer the restart so an install initiated by Huey can finish cleanly,
# then run the first reconciliation after the new routes are available.
if command -v systemd-run >/dev/null 2>&1; then
  sudo systemd-run --quiet --collect \
    --unit="turbidvision-plugin-web-restart-$$" --on-active=3s \
    /bin/systemctl restart pioreactor-web.target
  sudo systemd-run --quiet --collect \
    --unit="turbidvision-dashboard-initial-sync-$$" --on-active=12s \
    /bin/systemctl start turbidvision-dashboard-sync.service
fi
