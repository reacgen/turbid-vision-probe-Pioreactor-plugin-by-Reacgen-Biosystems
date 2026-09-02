"""Native Pioreactor API routes for discovery and dashboard selection."""
from __future__ import annotations

import html
import json
import subprocess
from urllib.parse import urlsplit

from flask import Response, jsonify, redirect, request
from pioreactor.web.plugin_registry import register_api_route, register_unit_api_route

from .dashboard_discovery import discover_local_probe
from .dashboard_sync import load_registry, request_relink


def _start_sync() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/systemctl", "--no-block", "start", "turbidvision-dashboard-sync.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode:
        return False, (result.stderr or result.stdout or "dashboard sync service failed").strip()
    return True, "Dashboard map refresh started. Recheck the page in a few seconds."


@register_unit_api_route("/turbidvision/sensor", methods=["GET"])
def turbidvision_sensor():
    refresh = request.args.get("refresh", "0").lower() in {"1", "true", "yes"}
    return jsonify(discover_local_probe(refresh=refresh).as_dict())


@register_api_route("/turbidvision/sensors", methods=["GET"])
def turbidvision_sensors():
    return jsonify({unit: link.as_dict() for unit, link in load_registry().items()})


def _dashboard_url(port: int) -> str:
    hostname = urlsplit(request.host_url).hostname or request.host.split(":", 1)[0]
    return f"http://{hostname}:{port}/"


def _selector_page(selected_unit: str | None = None) -> str:
    links = load_registry()
    rows: list[str] = []
    for unit, link in sorted(links.items()):
        safe_unit = html.escape(unit)
        if link.ready:
            action = (
                f'<a href="{html.escape(_dashboard_url(link.port))}" target="_blank" '
                f'rel="noopener">Open dashboard on port {link.port}</a>'
            )
        else:
            action = f'<strong>{html.escape(link.status)}</strong>: {html.escape(link.message or "Not ready")}'
        relink = ""
        if link.status == "probe_changed":
            relink = (
                f' <button onclick="relink({html.escape(json.dumps(unit), quote=True)})">'
                "Confirm Relink</button>"
            )
        marker = " class=\"selected\"" if selected_unit == unit else ""
        rows.append(f"<li{marker}><b>{safe_unit}</b> — {action}{relink}</li>")
    if not rows:
        rows.append("<li>No dashboard links have been synchronized yet.</li>")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Turbid Vision Probe dashboards</title>
<style>body{{font:16px system-ui;max-width:850px;margin:3rem auto;padding:0 1rem}}li{{margin:1rem 0}}.selected{{background:#eef;padding:.5rem}}</style></head>
<body><h1>Turbid Vision Probe dashboards</h1>
<p>Each Pioreactor is linked automatically to the probe physically attached to it.</p>
<ul>{''.join(rows)}</ul>
<button onclick="refreshMap()">Refresh dashboard map</button>
<script>
async function post(path) {{
  const response = await fetch(path, {{method:'POST'}});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Request failed');
  alert(body.message); window.setTimeout(() => window.location.reload(), 5000);
}}
function refreshMap() {{ post('/api/turbidvision/dashboard/refresh').catch(e => alert(e)); }}
function relink(unit) {{
  if (confirm('Link '+unit+' to the newly detected probe?'))
    post('/api/turbidvision/dashboard/'+encodeURIComponent(unit)+'/relink').catch(e => alert(e));
}}
</script></body></html>"""


@register_api_route("/turbidvision/dashboard", methods=["GET"])
def turbidvision_dashboard():
    unit = request.args.get("unit")
    links = load_registry()
    if unit and unit in links and links[unit].ready:
        return redirect(_dashboard_url(links[unit].port), code=302)
    return Response(_selector_page(unit), status=200, mimetype="text/html")


@register_api_route("/turbidvision/dashboard/refresh", methods=["POST"])
def turbidvision_dashboard_refresh():
    ok, message = _start_sync()
    return jsonify({"message" if ok else "error": message}), 200 if ok else 503


@register_api_route("/turbidvision/dashboard/<unit>/relink", methods=["POST"])
def turbidvision_dashboard_relink(unit: str):
    if unit not in load_registry():
        return jsonify({"error": f"Unknown Pioreactor unit {unit!r}."}), 404
    request_relink(unit)
    ok, message = _start_sync()
    return jsonify({"message" if ok else "error": message}), 200 if ok else 503
