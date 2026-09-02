# -*- coding: utf-8 -*-
"""Single-owner Pioreactor job for the Reacgen Turbid Vision Probe."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import ClassVar

import click
from pioreactor import structs
from pioreactor.background_jobs.base import BackgroundJobContrib
from pioreactor.cli.run import run
from pioreactor.config import config
from pioreactor.exc import HardwareNotFoundError
from pioreactor.utils import is_pio_job_running, timing
from pioreactor.whoami import get_assigned_experiment_name, get_unit_name

from .dashboard import AP_MODE_MESSAGE, LINK_DOWN_MESSAGE, NETWORK_REFRESH_SECONDS
from .dashboard_discovery import (
    I2COwnershipError,
    ProbeI2CLock,
    discovery_from_net_info,
    save_discovery,
)
from .diagnostics import describe
from .initial_cache import (
    InitialCacheError,
    InitialReference,
    load_initial_reference,
    mark_initial_pending,
    save_initial_reference,
)
from .odsensorv2 import (
    ODSENSORV2_ADDR,
    STATUS_CALIBRATED,
    STATUS_ERROR,
    STATUS_EXT_JOB_STOPPED_BY_FAULT,
    STATUS_HAS_INITIAL,
    STATUS_JOB_RUNNING,
    ProbeCommandError,
    ProbeError,
    TurbidVisionProbe,
)
from .routing import REFLECTANCE_TO_PPM, native_normalized_od_value, native_od_value

PLUGIN_NAME = "turbidvision_pioreactor"
JOB_NAME = "turbidvision_probe"

DEFAULT_INTERVAL_SECONDS = 5.0
MIN_INTERVAL_SECONDS = 0.5
DEFAULT_ANGLE = "180"
DEFAULT_CHANNEL = "1"
INITIAL_DENSITY_REL_TOL = 0.005
LEGACY_JOB_NAMES = ("turbidvision_od_reading", "turbidvision_growth_rate")


def _available(value: float) -> float | None:
    return value if math.isfinite(value) else None


@dataclass
class ProbeSnapshot:
    status: int
    quality: int
    diagnostic_code: int
    reading_number: int
    measured_reflectance: float | None
    calibrated_density: float | None
    specific_growth_rate: float | None
    filtered_reflectance: float | None
    filtered_calibrated_density: float | None
    laser_power_mw: float | None

    @property
    def calibrated(self) -> bool:
        return bool(self.status & STATUS_CALIBRATED)


class TurbidVisionProbeJob(BackgroundJobContrib):
    job_name = JOB_NAME

    published_settings: ClassVar[dict] = {
        "interval": {"datatype": "float", "unit": "s", "settable": True},
        "enable_growth_model": {"datatype": "boolean", "settable": False},
        "od_unit": {"datatype": "string", "settable": False},
        "initial_state": {"datatype": "string", "settable": False},
        "reflectance_ppm": {"datatype": "float", "unit": "ppm", "settable": False},
        "density": {"datatype": "float", "unit": "g/L", "settable": False},
        "normalized_od": {"datatype": "float", "settable": False},
        "growth_rate": {"datatype": "float", "unit": "h^-1", "settable": False},
        "laser_power_mw": {"datatype": "float", "unit": "mW", "settable": False},
    }

    # BackgroundJob can call teardown while its own constructor is failing.
    probe = None
    record_timer = None
    _expect_running = False
    _stopping_for_fault = False
    _i2c_lock = None

    def __init__(
        self,
        unit: str,
        experiment: str,
        *,
        enable_growth_model: int | bool | str | None = None,
        interval: float | None = None,
    ) -> None:
        super().__init__(unit=unit, experiment=experiment, plugin_name=PLUGIN_NAME)

        self.record_timer = None
        self.probe = None
        self._expect_running = False
        self._stopping_for_fault = False
        self._last_reading_number: int | None = None
        self._warned_negative_density = False
        self._warned_torn_snapshot = False
        self._warned_stale_reading = False
        self._active_diagnostic_signatures: set[tuple[int, int]] = set()
        self._warned_diagnostic_read_failure = False
        self._i2c_lock = None
        self._next_network_refresh = 0.0
        self._network_link: str | None = None

        self.enable_growth_model = self._coerce_binary_flag(
            enable_growth_model
            if enable_growth_model is not None
            else config.get(
                f"{self.job_name}.config",
                "enable_growth_model",
                fallback="0",
            )
        )
        self.interval = self._coerce_float(
            interval
            if interval is not None
            else config.get(
                f"{self.job_name}.config",
                "interval",
                fallback=DEFAULT_INTERVAL_SECONDS,
            ),
            minimum=MIN_INTERVAL_SECONDS,
            fallback=DEFAULT_INTERVAL_SECONDS,
            name="interval",
        )
        # The Turbid Vision optical geometry is fixed at 180 degrees. This is
        # native-reading metadata, not a user-selectable Pioreactor channel.
        self.angle = DEFAULT_ANGLE
        self.channel = DEFAULT_CHANNEL

        self.initial_state = "pending" if self.enable_growth_model else "unused"
        self.od_unit = "unknown"
        self.reflectance_ppm: float | None = None
        self.density: float | None = None
        self.normalized_od: float | None = None
        self.growth_rate: float | None = None
        self.laser_power_mw: float | None = None
        self._initial: InitialReference | None = None

        legacy = [name for name in LEGACY_JOB_NAMES if is_pio_job_running(name)]
        if legacy:
            message = (
                "Stop the legacy Turbid Vision jobs before starting the new single-owner "
                f"job: {', '.join(legacy)}"
            )
            self.logger.error(message)
            self.clean_up()
            raise ProbeError(message)

        try:
            self._i2c_lock = ProbeI2CLock(timeout=3.0)
            self._i2c_lock.__enter__()
        except I2COwnershipError as exc:
            self.clean_up()
            raise ProbeError(
                "Another Pioreactor task is communicating with the probe. Wait a few "
                "seconds for dashboard discovery to finish, then start the job again."
            ) from exc

        try:
            self.probe = self._open_probe()
            net_info = self.probe.read_net_info()
            self.probe_id = net_info.mac
            self._save_network_info(net_info)
        except Exception:
            self.clean_up()
            raise

        try:
            self._prepare_and_start_probe()
        except Exception as exc:
            self.logger.error(f"The Turbid Vision Probe could not start safely: {exc}")
            self.clean_up()
            raise

        self.record_timer = timing.RepeatedTimer(
            self.interval,
            self.record_from_probe,
            job_name=self.job_name,
            run_immediately=True,
            logger=self.logger,
        )
        self.record_timer.start()

    def _open_probe(self) -> TurbidVisionProbe:
        not_found = (
            "Pioreactor cannot communicate with the Turbid Vision Probe. Check that the "
            "probe has power and that its cable is fully connected."
        )
        try:
            probe = TurbidVisionProbe()
        except (OSError, ValueError) as exc:
            self.logger.error(f"{not_found} Technical detail: {exc}")
            raise HardwareNotFoundError(not_found) from exc
        try:
            connected = probe.test_connection()
        except OSError as exc:
            self.logger.error(f"{not_found} Technical detail: {exc}")
            raise HardwareNotFoundError(not_found) from exc
        if not connected:
            raise HardwareNotFoundError(not_found)
        major, minor = probe.protocol_version()
        if major != 2:
            raise ProbeError(
                "The probe firmware is not compatible with this Pioreactor plugin. "
                f"Probe interface version: {major}.{minor}; required version: 2.x. "
                "Update the probe firmware or install a matching plugin."
            )
        # Feature-detect the Phase 2R surface. Protocol 2.0 is intentionally
        # unchanged for additive registers, so the version tuple is insufficient.
        probe.read_initial_reflectance()
        probe.read_initial_density()
        return probe

    def _prepare_and_start_probe(self) -> None:
        assert self.probe is not None
        status = self.probe.read_status()
        if status & STATUS_JOB_RUNNING:
            self.logger.info("Stopping the probe job started from another interface before takeover.")
            self.probe.stop_job()

        wants_growth = self.enable_growth_model
        # Both directions are deliberate: dashboard changes while this job is
        # stopped must not leak into the next Pioreactor run.
        self.probe.set_growth_model(wants_growth)
        self.probe.set_initial_capture(wants_growth)

        if not wants_growth:
            self.probe.start_job()
            self._expect_running = True
            self.logger.info("Probe started in OD-only mode; growth model and capture are off.")
            return

        try:
            cached = load_initial_reference(self.experiment, self.unit, self.probe_id)
        except InitialCacheError as exc:
            raise ProbeError(str(exc)) from exc

        if cached is not None and cached.ready:
            assert cached.initial_reflectance is not None
            self.probe.set_initial_reflectance(cached.initial_reflectance)
            self._initial = cached
            self.initial_state = "ready"
            self.probe.start_job()
            self._expect_running = True
            try:
                self._verify_cached_density(bool(self.probe.read_status() & STATUS_CALIBRATED))
            except Exception:
                self._expect_running = False
                self.probe.stop_job()
                raise
            self.logger.info(
                "Reused the saved starting value for this experiment. No new starting "
                "measurement was needed."
            )
            return

        # Missing and interrupted/pending entries both restart the capture from
        # a known state. Record pending before erasing so a crash can never make
        # the old sensor NVS value authoritative.
        self._initial = mark_initial_pending(self.experiment, self.unit, self.probe_id)
        self.initial_state = "pending"
        self.probe.erase_initial()
        self.probe.start_job()
        self._expect_running = True
        self.logger.info(
            "No saved starting value was available for this experiment. The probe is "
            "measuring a new starting value now."
        )

    def _verify_cached_density(self, calibrated: bool) -> None:
        assert self.probe is not None and self._initial is not None and self._initial.ready
        cached_density = self._initial.initial_density
        sensor_density = _available(self.probe.read_initial_density())
        if calibrated != (cached_density is not None):
            raise ProbeError(
                "The probe's calibration state has changed since this experiment's "
                "starting value was saved. Restore the previous calibration state or use "
                "a new experiment."
            )
        if calibrated:
            if cached_density is None or sensor_density is None:
                raise ProbeError(
                    "The probe could not convert this experiment's saved starting value "
                    "with the active calibration. Restore the original calibration or use "
                    "a new experiment."
                )
            if not math.isclose(
                sensor_density,
                cached_density,
                rel_tol=INITIAL_DENSITY_REL_TOL,
                abs_tol=1e-6,
            ):
                raise ProbeError(
                    "The active calibration no longer matches this experiment's saved "
                    f"starting value ({sensor_density:.9g} g/L now versus "
                    f"{cached_density:.9g} g/L when saved). Restore the original "
                    "calibration or use a new experiment."
                )

    def _coerce_binary_flag(self, value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip()
        if text == "0":
            return False
        if text == "1":
            return True
        raise ProbeError(
            "Turbid Vision Growth Model Enabled must be 0 for off or 1 for on. "
            f"Received: {value}."
        )

    def _coerce_float(
        self, value: object, *, minimum: float, fallback: float, name: str
    ) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            self.logger.warning(
                f"The polling interval {value!r} is invalid. It must be a number of at "
                f"least {minimum:g} seconds, so this run will use {fallback:g} seconds."
            )
            return fallback
        if not math.isfinite(parsed) or parsed < minimum:
            self.logger.warning(
                f"The polling interval {value!r} is invalid. It must be a number of at "
                f"least {minimum:g} seconds, so this run will use {fallback:g} seconds."
            )
            return fallback
        return parsed

    def set_interval(self, value: object) -> None:
        interval = self._coerce_float(
            value,
            minimum=MIN_INTERVAL_SECONDS,
            fallback=DEFAULT_INTERVAL_SECONDS,
            name="interval",
        )
        if self.record_timer is not None:
            self.record_timer.interval = interval
        self.interval = interval

    def on_sleeping(self) -> None:
        if self.record_timer is not None:
            self.record_timer.pause()

    def on_sleeping_to_ready(self) -> None:
        if self.record_timer is not None:
            self.record_timer.unpause()

    def on_disconnected(self) -> None:
        if self.record_timer is not None:
            try:
                self.record_timer.cancel()
            except Exception:
                pass
        if self.probe is not None and self._expect_running and not self._stopping_for_fault:
            self._expect_running = False
            try:
                self.probe.stop_job()
                self.logger.info("Probe measurement job stopped.")
            except Exception as exc:
                self.logger.debug(
                    "Pioreactor disconnected, but could not confirm that the probe "
                    f"stopped: {exc}. Check whether the probe is still measuring from "
                    "its dashboard before restarting."
                )
        if self._i2c_lock is not None:
            try:
                self._i2c_lock.__exit__(None, None, None)
            finally:
                self._i2c_lock = None

    def _save_network_info(self, net_info) -> None:
        discovery = discovery_from_net_info(
            net_info, unit=self.unit, job_owned_by_pioreactor=True
        )
        save_discovery(discovery)
        if discovery.link != self._network_link:
            if discovery.link == "ap":
                self.logger.warning(AP_MODE_MESSAGE)
            elif discovery.link != "sta":
                self.logger.warning(LINK_DOWN_MESSAGE)
            elif self._network_link is not None:
                self.logger.info(
                    f"The Turbid Vision dashboard is available again at {discovery.ip}."
                )
        self._network_link = discovery.link
        self._next_network_refresh = monotonic() + NETWORK_REFRESH_SECONDS

    def _refresh_network_info_if_due(self) -> None:
        assert self.probe is not None
        if monotonic() < self._next_network_refresh:
            return
        try:
            self._save_network_info(self.probe.read_net_info())
        except (OSError, ProbeError) as exc:
            # Network discovery is ancillary. It must never stop optical-density
            # acquisition through the same physical I2C session.
            self._next_network_refresh = monotonic() + NETWORK_REFRESH_SECONDS
            self.logger.warning(
                f"Could not update the saved dashboard address: {exc}. Measurements will "
                "continue, but the dashboard link may be outdated until the next refresh."
            )

    def _abort(self, message: str) -> None:
        self._stopping_for_fault = True
        self._expect_running = False
        self.logger.error(message)
        self.clean_up()

    def _check_probe_still_running(self, status: int) -> bool:
        assert self.probe is not None
        if not self._expect_running or (status & STATUS_JOB_RUNNING):
            return True
        try:
            ext = self.probe.read_status_ext()
        except Exception:
            ext = 0
        if ext & STATUS_EXT_JOB_STOPPED_BY_FAULT:
            detail = ""
            try:
                records = self.probe.active_diagnostics()
                if records:
                    detail = " Active: " + "; ".join(
                        self._describe_diagnostic(record) for record in records
                    )
            except Exception:
                pass
            if detail:
                self._abort("The probe stopped because of a fault." + detail)
            else:
                self._abort(
                    "The probe stopped measurement because of a fault, but Pioreactor "
                    "could not read the cause. Open the sensor dashboard and review its "
                    "active diagnostics before restarting."
                )
        else:
            self._abort("The probe was stopped from another interface; no stale data was published.")
        return False

    @staticmethod
    def _describe_diagnostic(record) -> str:
        detail = describe(record.code, with_recovery=True)
        if record.native_code >= 0:
            detail += f" Native detail: {record.native_code}."
        return detail

    def _surface_active_diagnostics(self, status: int) -> None:
        """Log each warning-or-higher condition once while it remains active."""
        assert self.probe is not None
        if not (status & STATUS_ERROR):
            self._active_diagnostic_signatures.clear()
            self._warned_diagnostic_read_failure = False
            return

        try:
            records = self.probe.active_diagnostics()
        except (OSError, ProbeError) as exc:
            if not self._warned_diagnostic_read_failure:
                self._warned_diagnostic_read_failure = True
                self.logger.warning(
                    f"The probe reports an active warning or error, but its diagnostic "
                    f"details could not be read: {exc}"
                )
            return

        self._warned_diagnostic_read_failure = False
        current = {
            (record.code, record.native_code)
            for record in records
            if record.severity >= 1
        }
        for record in records:
            signature = (record.code, record.native_code)
            if record.severity >= 1 and signature not in self._active_diagnostic_signatures:
                self.logger.warning(
                    "Turbid Vision Probe diagnostic active: "
                    + self._describe_diagnostic(record)
                )
        self._active_diagnostic_signatures = current

    def _read_optional(self, reader: Callable[[], float]) -> float | None:
        try:
            return _available(reader())
        except (OSError, ProbeError):
            return None

    def _read_snapshot(self) -> ProbeSnapshot | None:
        assert self.probe is not None
        for _attempt in range(3):
            status = self.probe.read_status()
            if not self._check_probe_still_running(status):
                return None
            self._surface_active_diagnostics(status)
            quality = self.probe.read_reading_quality()
            if not quality.usable:
                return None
            before = self.probe.read_reading_number()
            calibrated = bool(status & STATUS_CALIBRATED)

            measured = self._read_optional(self.probe.read_measured_reflectance)
            density = self._read_optional(self.probe.read_calibrated_density)
            laser = self._read_optional(self.probe.read_laser_power_mw)

            specific = filtered_refl = filtered_density = None
            if self.enable_growth_model:
                if calibrated:
                    specific = self._read_optional(self.probe.read_growth_rate)
                    filtered_density = self._read_optional(
                        self.probe.read_filtered_calibrated_density
                    )
                else:
                    filtered_refl = self._read_optional(self.probe.read_filtered_reflectance)

            after = self.probe.read_reading_number()
            if before == after:
                self._warned_torn_snapshot = False
                return ProbeSnapshot(
                    status=status,
                    quality=quality.quality,
                    diagnostic_code=quality.diagnostic_code,
                    reading_number=after,
                    measured_reflectance=measured,
                    calibrated_density=density,
                    specific_growth_rate=specific,
                    filtered_reflectance=filtered_refl,
                    filtered_calibrated_density=filtered_density,
                    laser_power_mw=laser,
                )
        if not self._warned_torn_snapshot:
            self._warned_torn_snapshot = True
            self.logger.warning(
                "The probe produced a new measurement while Pioreactor was reading the "
                "previous one. One incomplete update was skipped; the job will continue."
            )
        return None

    def _complete_initial_capture(self, status: int, captured_at: str) -> bool:
        assert self.probe is not None
        if not self.enable_growth_model or (self._initial is not None and self._initial.ready):
            return True
        if not (status & STATUS_HAS_INITIAL):
            return False
        reflectance = _available(self.probe.read_initial_reflectance())
        if reflectance is None or reflectance <= 0.0:
            return False
        calibrated = bool(status & STATUS_CALIBRATED)
        density = _available(self.probe.read_initial_density())
        if calibrated and (density is None or density <= 0.0):
            return False
        if not calibrated:
            density = None
        self._initial = save_initial_reference(
            experiment=self.experiment,
            unit=self.unit,
            probe_id=self.probe_id,
            captured_at=captured_at,
            initial_reflectance=reflectance,
            initial_density=density,
        )
        self.initial_state = "ready"
        self.logger.info(
            "The new starting value was saved for this experiment and will be reused "
            "after restarts."
        )
        return True

    def record_from_probe(self) -> None:
        self._refresh_network_info_if_due()
        try:
            snapshot = self._read_snapshot()
        except Exception as exc:
            self.logger.debug(
                "Could not read this probe measurement; this poll was skipped: "
                f"{exc}. The job is still running.",
                exc_info=True,
            )
            return
        if snapshot is None:
            return
        if self._last_reading_number == snapshot.reading_number:
            if not self._warned_stale_reading:
                self._warned_stale_reading = True
                self.logger.warning(
                    "The probe reading number has not advanced since the previous poll. "
                    "The job will continue; this is expected when Pioreactor polls faster "
                    "than the probe measures."
                )
            return

        timestamp = timing.current_utc_datetime()
        timestamp_text = timing.to_iso_format(timestamp)
        try:
            initial_ready = self._complete_initial_capture(snapshot.status, timestamp_text)
        except Exception as exc:
            self._abort(
                "The probe measured a starting value, but Pioreactor could not save it "
                f"safely: {exc}. No normalized readings will be recorded until this is "
                "resolved."
            )
            return

        self._last_reading_number = snapshot.reading_number
        self.reflectance_ppm = (
            snapshot.measured_reflectance * REFLECTANCE_TO_PPM
            if snapshot.measured_reflectance is not None
            else None
        )
        self.density = snapshot.calibrated_density
        self.od_unit = "g/L" if snapshot.calibrated else "ppm"
        self.laser_power_mw = snapshot.laser_power_mw

        initial_density = self._initial.initial_density if initial_ready and self._initial else None
        native_od = native_od_value(
            calibrated=snapshot.calibrated,
            measured_reflectance=snapshot.measured_reflectance,
            calibrated_density=snapshot.calibrated_density,
        )
        if native_od is not None:
            od_value, self.od_unit = native_od
            self._publish_native_od(od_value, timestamp)

        normalized_od = None
        if self.enable_growth_model and initial_ready:
            normalized_od = native_normalized_od_value(
                calibrated=snapshot.calibrated,
                filtered_reflectance=snapshot.filtered_reflectance,
                filtered_calibrated_density=snapshot.filtered_calibrated_density,
                initial_density=initial_density,
            )
        if normalized_od is not None and math.isfinite(normalized_od):
            self.normalized_od = normalized_od
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/od_filtered",
                structs.ODFiltered(od_filtered=normalized_od, timestamp=timestamp),
                retain=True,
            )

        if snapshot.calibrated and snapshot.specific_growth_rate is not None:
            self.growth_rate = snapshot.specific_growth_rate
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/growth_rate_calculating/growth_rate",
                structs.GrowthRate(
                    growth_rate=snapshot.specific_growth_rate,
                    timestamp=timestamp,
                ),
                retain=True,
            )

    def _publish_native_od(self, od: float, timestamp) -> None:
        if od < 0.0:
            if not self._warned_negative_density:
                self._warned_negative_density = True
                self.logger.warning(
                    "The probe produced a negative optical-density value, which Pioreactor "
                    "cannot display. This point was skipped; check the active calibration "
                    "and signal quality."
                )
            return
        reading = structs.RawODReading(
            timestamp=timestamp,
            angle=self.angle,
            od=od,
            channel=self.channel,
            ir_led_intensity=self.laser_power_mw or 0.0,
        )
        readings = structs.ODReadings(timestamp=timestamp, ods={self.channel: reading})
        base = f"pioreactor/{self.unit}/{self.experiment}/od_reading"
        self.publish(f"{base}/od{self.channel}", reading, retain=True)
        self.publish(f"{base}/ods", readings, retain=True)


@run.command(name=JOB_NAME)
@click.option("--enable-growth-model", type=click.IntRange(0, 1), default=None)
@click.option("--interval", type=float, default=None, help="seconds between probe polls")
def click_turbidvision_probe(
    enable_growth_model: int | None, interval: float | None
) -> None:
    """Run the single-owner Turbid Vision Probe integration."""
    unit = get_unit_name()
    try:
        job = TurbidVisionProbeJob(
            unit=unit,
            experiment=get_assigned_experiment_name(unit),
            enable_growth_model=enable_growth_model,
            interval=interval,
        )
    except (HardwareNotFoundError, ProbeCommandError, ProbeError) as exc:
        raise SystemExit(str(exc)) from None
    job.block_until_disconnected()
