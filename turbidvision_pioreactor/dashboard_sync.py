"""Leader-side reconciliation of Pioreactor units to probe dashboard ports."""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from pioreactor.cluster_management import get_workers_in_inventory
from pioreactor.config import leader_hostname
from pioreactor.pubsub import get_from
from pioreactor.utils import local_persistent_storage
from pioreactor.utils.networking import resolve_to_address
from pioreactor.utils.timing import current_utc_datetime, to_iso_format
from pioreactor.whoami import am_I_leader

from .dashboard import (
    REGISTRY_CACHE_NAME,
    DashboardLink,
    Discovery,
    dump_registry_payload,
    load_registry_payload,
    plan_dashboard_links,
    render_lighttpd,
)

_REGISTRY_KEY = "registry"
_RELINK_CACHE_NAME = "turbidvision_dashboard_relink_requests"
CONFIG_FILENAME = "60-turbidvision-probe-ui.conf"


def load_registry() -> dict[str, DashboardLink]:
    with local_persistent_storage(REGISTRY_CACHE_NAME) as cache:
        raw = cache.get(_REGISTRY_KEY)
    return load_registry_payload(raw)


def save_registry(links: dict[str, DashboardLink]) -> None:
    with local_persistent_storage(REGISTRY_CACHE_NAME) as cache:
        cache[_REGISTRY_KEY] = dump_registry_payload(links)


def request_relink(unit: str) -> None:
    with local_persistent_storage(_RELINK_CACHE_NAME) as cache:
        cache[unit] = "1"


def _pending_relinks() -> set[str]:
    with local_persistent_storage(_RELINK_CACHE_NAME) as cache:
        return {str(key) for key in cache.iterkeys()}


def _clear_relinks(units: set[str]) -> None:
    with local_persistent_storage(_RELINK_CACHE_NAME) as cache:
        for unit in units:
            try:
                del cache[unit]
            except KeyError:
                pass


def _inventory() -> tuple[str, ...]:
    units = {str(unit) for unit in get_workers_in_inventory()}
    units.add(str(leader_hostname))
    return tuple(sorted(units))


def _discoveries(units: tuple[str, ...]) -> dict[str, Discovery]:
    result: dict[str, Discovery] = {}
    for unit in units:
        try:
            response = get_from(
                resolve_to_address(unit),
                "/unit_api/turbidvision/sensor?refresh=1",
                timeout=5,
            )
            response.raise_for_status()
            result[unit] = Discovery.from_dict(response.json())
        except Exception as exc:  # noqa: BLE001 - isolate one unreachable/malformed worker
            # Keep the missing record out of the map: the planner converts it
            # into an unavailable link while preserving its stable port.
            print(
                "Could not obtain probe dashboard information from Pioreactor "
                f"{unit}: {exc}. Other units will continue updating."
            )
    return result


def _resolve(address: str) -> str:
    return socket.gethostbyname(address)


def _reachable(address: str) -> bool:
    try:
        with urllib.request.urlopen(f"http://{address}/mdns.json", timeout=3) as response:
            return 200 <= response.status < 400
    except (OSError, urllib.error.URLError, ValueError):
        return False


def _listener_ports(payload: bytes | str | None) -> set[int]:
    if payload is None:
        return set()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    return {int(value) for value in re.findall(r'0\.0\.0\.0:(\d+)', payload)}


def _install_lighttpd_config(text: str) -> str:
    root = Path(os.environ.get("TURBIDVISION_LIGHTTPD_ROOT", "/etc/lighttpd"))
    available = root / "conf-available" / CONFIG_FILENAME
    enabled = root / "conf-enabled" / CONFIG_FILENAME
    old = available.read_bytes() if available.exists() else None
    if old == text.encode("utf-8") and enabled.is_symlink():
        return "unchanged"
    removed_ports = _listener_ports(old) - _listener_ports(text)

    available.parent.mkdir(parents=True, exist_ok=True)
    enabled.parent.mkdir(parents=True, exist_ok=True)
    old_link = os.readlink(enabled) if enabled.is_symlink() else None
    fd, temp_name = tempfile.mkstemp(prefix=CONFIG_FILENAME + ".", dir=available.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, available)
        if enabled.exists() or enabled.is_symlink():
            enabled.unlink()
        enabled.symlink_to("../conf-available/" + CONFIG_FILENAME)
        subprocess.run(
            ["/usr/sbin/lighttpd", "-tt", "-f", str(root / "lighttpd.conf")],
            check=True,
        )
        # A graceful reload preserves active UI/API connections and is enough
        # for address changes or added ports. If a listener disappeared,
        # restart: lighttpd otherwise inherits and keeps that stale socket.
        action = "restart" if removed_ports else "reload"
        subprocess.run(["systemctl", action, "lighttpd.service"], check=True)
    except Exception:
        if old is None:
            available.unlink(missing_ok=True)
        else:
            available.write_bytes(old)
        enabled.unlink(missing_ok=True)
        if old_link is not None:
            enabled.symlink_to(old_link)
        subprocess.run(
            ["systemctl", "restart", "lighttpd.service"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        raise
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return "restarted" if removed_ports else "reloaded"


def sync_dashboard_links(*, relink_units: set[str] | None = None) -> dict[str, DashboardLink]:
    if not am_I_leader():
        raise RuntimeError("Turbid Vision dashboard synchronization must run on the Pioreactor leader")
    units = _inventory()
    discoveries = _discoveries(units)
    previous = load_registry()
    effective_relinks = (relink_units or set()) | _pending_relinks()
    links = plan_dashboard_links(
        units,
        discoveries,
        previous,
        resolve=_resolve,
        reachable=_reachable,
        updated_at=to_iso_format(current_utc_datetime()),
        relink_units=effective_relinks,
    )
    proxy_action = _install_lighttpd_config(render_lighttpd(links.values()))
    save_registry(links)
    _clear_relinks(effective_relinks)
    ready = sum(link.ready for link in links.values())
    print(
        f"Probe dashboard links updated: {ready} of {len(links)} available; "
        f"web proxy {proxy_action}."
    )
    for link in links.values():
        if not link.ready:
            print(f"{link.unit}: {link.status}: {link.message}")
    return links


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relink", action="append", default=[], metavar="UNIT")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    links = sync_dashboard_links(relink_units=set(args.relink))
    if args.json:
        print(json.dumps({unit: link.as_dict() for unit, link in links.items()}, indent=2))


if __name__ == "__main__":
    main()
