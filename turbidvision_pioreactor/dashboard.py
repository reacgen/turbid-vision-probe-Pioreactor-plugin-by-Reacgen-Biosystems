"""Discovery records and leader-side dashboard link planning.

The data-shaping functions in this module deliberately use only the standard
library. That keeps the duplicate and AP-mode safety rules directly testable on
a development machine without Pioreactor installed.
"""
from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass

BASE_PORT = 4300
DISCOVERY_CACHE_NAME = "turbidvision_dashboard_discovery"
REGISTRY_CACHE_NAME = "turbidvision_dashboard_registry"
REGISTRY_SCHEMA = 2
NETWORK_REFRESH_SECONDS = 300.0

AP_MODE_MESSAGE = (
    "The Turbid Vision Probe is in Wi-Fi setup mode. Connect the probe to the "
    "same network as the Pioreactor leader, then refresh the dashboard link. "
    "Optical-density measurement over I2C can continue."
)
LINK_DOWN_MESSAGE = (
    "The Turbid Vision Probe is not connected to Wi-Fi. Configure the probe to "
    "join the same network as the Pioreactor leader, then refresh the dashboard link. "
    "Optical-density measurement over I2C can continue."
)
BUSY_MESSAGE = (
    "The probe is currently measuring, so dashboard discovery is using its last "
    "known network address. The running job will update it automatically."
)

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Discovery:
    unit: str
    probe_id: str | None
    link: str
    ip: str | None
    hostname: str | None
    observed_at: str
    i2c_present: bool = True
    job_owned_by_pioreactor: bool = False
    stale: bool = False
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> Discovery:
        return cls(**value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class DashboardLink:
    unit: str
    probe_id: str | None
    port: int
    backend_address: str | None
    resolved_address: str | None
    host_header: str | None
    status: str
    message: str | None
    updated_at: str

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["ready"] = self.ready
        return result

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> DashboardLink:
        payload = dict(value)
        payload.pop("ready", None)
        # Schema 1 exposed the now-removed manual address setting.
        payload.pop("configured_address", None)
        return cls(**payload)  # type: ignore[arg-type]


def normalize_address(value: str) -> str:
    """Accept a bare hostname/IP only; lighttpd supplies HTTP and port 80."""
    address = value.strip()
    if not address or "://" in address or "/" in address or ":" in address:
        raise ValueError(
            "Enter only a probe hostname or IPv4 address—for example "
            "turbidvision.local or 192.168.1.52—with no http://, path, or port."
        )
    try:
        ipaddress.IPv4Address(address)
    except ipaddress.AddressValueError:
        if not _HOST_RE.fullmatch(address) or ".." in address:
            raise ValueError(
                f"“{value}” is not a valid probe hostname. Use only letters, "
                "numbers, dots, and hyphens."
            ) from None
    return address.lower()


def host_header_for(discovery: Discovery) -> str | None:
    if discovery.hostname:
        hostname = normalize_address(discovery.hostname)
        return hostname if hostname.endswith(".local") else hostname + ".local"
    return discovery.ip


def load_registry_payload(raw: object | None) -> dict[str, DashboardLink]:
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload = json.loads(str(raw))
    if payload.get("schema") not in (1, REGISTRY_SCHEMA):
        raise ValueError(
            "The saved probe-dashboard map was created by an incompatible plugin "
            "version. Install the matching plugin or reset the dashboard map."
        )
    return {
        unit: DashboardLink.from_dict(record)
        for unit, record in payload.get("links", {}).items()
    }


def dump_registry_payload(links: dict[str, DashboardLink]) -> str:
    return json.dumps(
        {
            "schema": REGISTRY_SCHEMA,
            "links": {unit: link.as_dict() for unit, link in sorted(links.items())},
        },
        allow_nan=False,
        separators=(",", ":"),
    )


def _next_port(used: set[int]) -> int:
    port = BASE_PORT
    while port in used:
        port += 1
    return port


def plan_dashboard_links(
    units: Iterable[str],
    discoveries: dict[str, Discovery],
    previous: dict[str, DashboardLink],
    *,
    resolve: Callable[[str], str],
    reachable: Callable[[str], bool],
    updated_at: str,
    relink_units: set[str] | None = None,
) -> dict[str, DashboardLink]:
    """Build a deterministic one-port-per-unit map and fail closed on conflicts."""
    relink_units = relink_units or set()
    ordered_units = sorted(set(units))
    used_ports = {link.port for unit, link in previous.items() if unit in ordered_units}
    links: dict[str, DashboardLink] = {}

    for unit in ordered_units:
        discovery = discoveries.get(unit)
        old = previous.get(unit)
        port = old.port if old is not None else _next_port(used_ports)
        used_ports.add(port)
        def failed(
            status: str,
            message: str,
            *,
            current_unit: str = unit,
            current_discovery: Discovery | None = discovery,
            current_port: int = port,
        ) -> DashboardLink:
            return DashboardLink(
                current_unit,
                current_discovery.probe_id if current_discovery else None,
                current_port,
                None, None, None, status, message, updated_at,
            )

        if discovery is None:
            links[unit] = failed(
                "unavailable",
                "This Pioreactor did not return probe dashboard information. "
                "Confirm the worker is online and refresh the dashboard map.",
            )
            continue
        if not discovery.i2c_present:
            links[unit] = failed(
                "probe_missing",
                discovery.message
                or "No Turbid Vision Probe was detected on this Pioreactor. "
                "Check probe power and the cable.",
            )
            continue
        if discovery.link == "ap":
            links[unit] = failed("ap_mode", AP_MODE_MESSAGE)
            continue
        if discovery.link != "sta" or not discovery.ip:
            links[unit] = failed("wifi_disconnected", discovery.message or LINK_DOWN_MESSAGE)
            continue
        if not discovery.probe_id:
            links[unit] = failed(
                "identity_missing",
                "The probe did not provide a stable device identity, so Pioreactor "
                "cannot link its dashboard safely. Update the probe firmware or "
                "contact support.",
            )
            continue
        if old and old.probe_id and old.probe_id != discovery.probe_id and unit not in relink_units:
            links[unit] = DashboardLink(
                unit, old.probe_id, port, None, None, None,
                "probe_changed",
                f"This Pioreactor was linked to probe {old.probe_id}, but now sees "
                f"{discovery.probe_id}. Confirm Relink before using its dashboard.",
                updated_at,
            )
            continue

        try:
            try:
                host_header = host_header_for(discovery)
            except ValueError:
                host_header = discovery.ip
            # The hostname is the stable identity on a DHCP network. The
            # numeric IP reported over I2C is only a fallback for networks
            # where mDNS is unavailable. No user-maintained address map is
            # required: every worker reports its physically attached probe.
            hostname = host_header if discovery.hostname else None
            if hostname is not None:
                try:
                    resolved = str(ipaddress.IPv4Address(resolve(hostname)))
                    backend = hostname
                except (OSError, ValueError):
                    backend = discovery.ip
                    resolved = str(ipaddress.IPv4Address(resolve(backend)))
            else:
                backend = discovery.ip
                resolved = str(ipaddress.IPv4Address(resolve(backend)))
        except (AssertionError, OSError, ValueError) as exc:
            links[unit] = failed(
                "invalid_address",
                "The probe-reported dashboard hostname and IP could not be found. "
                "Check the probe's Wi-Fi and hostname settings, then refresh the "
                f"dashboard map. Detail: {exc}",
            )
            continue

        links[unit] = DashboardLink(
            unit, discovery.probe_id, port, backend, resolved,
            host_header, "candidate", None, updated_at,
        )

    # Reject ambiguous physical probes, resolved destinations, and persisted
    # ports. Mark every participant so the result is independent of ordering.
    conflict_fields: tuple[tuple[str, Callable[[DashboardLink], object]], ...] = (
        ("probe identity", lambda link: link.probe_id),
        ("resolved dashboard address", lambda link: link.resolved_address),
        ("listener port", lambda link: link.port),
    )
    conflicts: dict[str, list[str]] = {}
    for label, getter in conflict_fields:
        buckets: dict[object, list[str]] = {}
        for unit, link in links.items():
            if link.status not in ("candidate", "ready"):
                continue
            value = getter(link)
            if value is not None:
                buckets.setdefault(value, []).append(unit)
        for value, members in buckets.items():
            if len(members) > 1:
                for unit in members:
                    conflicts.setdefault(unit, []).append(
                        f"{', '.join(members)} point to the same {label}: {value!r}"
                    )

    for unit, reasons in conflicts.items():
        link = links[unit]
        links[unit] = DashboardLink(
            link.unit, link.probe_id, link.port, None, link.resolved_address,
            link.host_header, "duplicate",
            "Dashboard links were disabled because "
            + "; ".join(reasons)
            + ". Correct the mappings or confirm the physical probe connections.",
            updated_at,
        )

    for unit, link in tuple(links.items()):
        if link.status != "candidate":
            continue
        assert link.backend_address is not None
        if reachable(link.backend_address):
            links[unit] = DashboardLink(
                link.unit, link.probe_id, link.port, link.backend_address,
                link.resolved_address, link.host_header,
                "ready", None, updated_at,
            )
        else:
            links[unit] = DashboardLink(
                link.unit, link.probe_id, link.port, None,
                link.resolved_address, link.host_header, "unreachable",
                f"The leader cannot reach the Turbid Vision dashboard at {link.backend_address}. "
                "Check the probe's Wi-Fi and ensure it is on the leader's network.",
                updated_at,
            )
    return links


def render_lighttpd(links: Iterable[DashboardLink]) -> str:
    lines = [
        "# Generated by turbidvision_pioreactor.dashboard_sync. Do not edit.",
        "# One root-mounted listener is required per probe for absolute HTTP and WebSocket paths.",
    ]
    for link in sorted((item for item in links if item.ready), key=lambda item: item.port):
        assert link.backend_address and link.host_header
        # Values passed here have already gone through the strict hostname/IP
        # validator, so they cannot inject lighttpd syntax.
        lines.extend(
            [
                f'$SERVER["socket"] == "0.0.0.0:{link.port}" {{',
                "  proxy.header = (",
                '    "upgrade" => "enable",',
                f'    "map-host-request" => ( "-" => "{link.host_header}" ),',
                "  )",
                "  proxy.server = (",
                f'    "" => ( ( "host" => "{link.backend_address}", "port" => 80 ) ),',
                "  )",
                "}",
            ]
        )
    return "\n".join(lines) + "\n"
