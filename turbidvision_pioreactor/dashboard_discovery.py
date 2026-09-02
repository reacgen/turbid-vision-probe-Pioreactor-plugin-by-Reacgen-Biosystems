"""Worker-local, single-owner discovery of the probe's network identity."""
from __future__ import annotations

import json
import subprocess
import time
from contextlib import AbstractContextManager
from typing import Self

from pioreactor.paths import get_dot_pioreactor_path
from pioreactor.utils import is_pio_job_running, local_persistent_storage
from pioreactor.utils.timing import current_utc_datetime, to_iso_format
from pioreactor.whoami import get_unit_name

from .dashboard import (
    AP_MODE_MESSAGE,
    BUSY_MESSAGE,
    DISCOVERY_CACHE_NAME,
    LINK_DOWN_MESSAGE,
    Discovery,
)
from .odsensorv2 import (
    LINK_AP,
    LINK_DOWN,
    LINK_STA,
    NetInfo,
    TurbidVisionProbe,
)

JOB_NAME = "turbidvision_probe"
_CACHE_KEY = "network"


class I2COwnershipError(RuntimeError):
    """Another process currently owns the Turbid Vision I2C session."""


class ProbeI2CLock(AbstractContextManager["ProbeI2CLock"]):
    """An advisory inter-process lock shared by discovery and the running job."""

    def __init__(self, timeout: float = 0.0) -> None:
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> Self:
        import fcntl

        path = get_dot_pioreactor_path() / "turbidvision_probe_i2c.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a+")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise I2COwnershipError(BUSY_MESSAGE) from None
                time.sleep(0.05)

    def __exit__(self, *args: object) -> None:
        if self._handle is not None:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def link_name(link: int) -> str:
    return {LINK_DOWN: "down", LINK_STA: "sta", LINK_AP: "ap"}.get(link, "unknown")


def discovery_from_net_info(
    net_info: NetInfo,
    *,
    unit: str,
    job_owned_by_pioreactor: bool,
) -> Discovery:
    link = link_name(net_info.link)
    message = AP_MODE_MESSAGE if link == "ap" else LINK_DOWN_MESSAGE if link != "sta" else None
    return Discovery(
        unit=unit,
        probe_id=net_info.mac or None,
        link=link,
        ip=net_info.ipv4,
        hostname=net_info.hostname or None,
        observed_at=to_iso_format(current_utc_datetime()),
        job_owned_by_pioreactor=job_owned_by_pioreactor,
        message=message,
    )


def save_discovery(discovery: Discovery) -> Discovery:
    with local_persistent_storage(DISCOVERY_CACHE_NAME) as cache:
        cache[_CACHE_KEY] = json.dumps(discovery.as_dict(), allow_nan=False, separators=(",", ":"))
    return discovery


def load_discovery(*, stale: bool = False, message: str | None = None) -> Discovery | None:
    with local_persistent_storage(DISCOVERY_CACHE_NAME) as cache:
        raw = cache.get(_CACHE_KEY)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    record = Discovery.from_dict(json.loads(raw))
    return Discovery(
        **{
            **record.as_dict(),
            "job_owned_by_pioreactor": bool(is_pio_job_running(JOB_NAME)),
            "stale": stale,
            "message": message or record.message,
        }
    )


def refresh_local_probe() -> Discovery:
    """Safely refresh from a hardware-capable service process."""
    running = bool(is_pio_job_running(JOB_NAME))
    cached = load_discovery(stale=running, message=BUSY_MESSAGE if running else None)
    if running:
        if cached is not None:
            return cached
        return Discovery(
            unit=str(get_unit_name()), probe_id=None, link="unknown", ip=None,
            hostname=None, observed_at=to_iso_format(current_utc_datetime()),
            i2c_present=False, job_owned_by_pioreactor=True, stale=True,
            message=BUSY_MESSAGE,
        )
    try:
        with ProbeI2CLock(timeout=0.25):
            # Recheck after acquiring the cross-process lock. The job acquires
            # this same lock before it opens the bus.
            if is_pio_job_running(JOB_NAME):
                cached = load_discovery(stale=True, message=BUSY_MESSAGE)
                if cached is not None:
                    return cached
                raise I2COwnershipError(BUSY_MESSAGE)
            probe = TurbidVisionProbe()
            if not probe.test_connection():
                raise OSError("probe did not answer its connection test")
            net_info = probe.read_net_info()
    except I2COwnershipError:
        cached = load_discovery(stale=True, message=BUSY_MESSAGE)
        if cached is not None:
            return cached
        raise
    except Exception as exc:  # noqa: BLE001 - convert all Blinka/GPIO failures to state
        return Discovery(
            unit=str(get_unit_name()), probe_id=None, link="unknown", ip=None,
            hostname=None, observed_at=to_iso_format(current_utc_datetime()),
            i2c_present=False,
            message=(
                "Pioreactor could not read the probe's identity and network address. "
                f"Check probe power and the cable. Technical detail: {exc}"
            ),
        )

    return save_discovery(
        discovery_from_net_info(
            net_info, unit=str(get_unit_name()), job_owned_by_pioreactor=False
        )
    )


def discover_local_probe(*, refresh: bool = False) -> Discovery:
    """Return cache from the web process; optionally request safe service refresh.

    Pioreactor's Flask worker intentionally lacks GPIO access, so it must never
    instantiate Blinka or touch I2C itself.
    """
    if refresh:
        try:
            subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "start", "turbidvision-dashboard-discovery.service"],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    cached = load_discovery(stale=False)
    if cached is not None:
        return cached
    return Discovery(
        unit=str(get_unit_name()), probe_id=None, link="unknown", ip=None,
        hostname=None, observed_at=to_iso_format(current_utc_datetime()),
        i2c_present=False, stale=True,
        message=(
            "No Turbid Vision network cache exists yet. Wait for the local discovery "
            "service or start the Turbid Vision Probe job, then refresh the dashboard map."
        ),
    )


def main() -> None:
    print(json.dumps(refresh_local_probe().as_dict(), allow_nan=False))


if __name__ == "__main__":
    main()
