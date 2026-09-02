# -*- coding: utf-8 -*-
"""I2C driver for the Reacgen Turbid Vision Probe.

Address 0x69, 400 kHz, little-endian floats. The authoritative interface
reference is ``user_guides/i2c.md`` in the ODSensorV2 repo; this file requires
firmware **2.2.0** (probe API 2.0).

Transaction shape: write the register address, then read with a repeated START.
Every response is ``[result byte][payload...]``.

    result byte   0x00  ok
                  0x01  unknown register (also: this firmware is too old)
                  0x02  no data yet / cursor past end
                  0x03  no usable value yet (0x27 only)

**The result byte is not the status flags.** Older integration notes described
every read as returning "[status][value]", which led to the belief that the live
status bitfield rides along on every read. It does not. Status comes only from an
explicit read of register 0x20.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from time import monotonic, sleep

from adafruit_bus_device.i2c_device import I2CDevice
from busio import I2C  # type: ignore
from pioreactor import hardware

from .diagnostics import describe

ODSENSORV2_ADDR = 0x69

# --- Result bytes ---
RESP_OK = 0x00
RESP_UNKNOWN_REGISTER = 0x01
RESP_NO_DATA = 0x02
RESP_NO_ADDRESS = 0x03

# --- Read registers ---
REG_PROTOCOL_VERSION = 0x00
REG_MEASURED_REFLECTANCE = 0x10
REG_NORMALIZED_REFLECTANCE = 0x11
REG_CALIBRATED_DENSITY = 0x12  # unfiltered density observation (g/L)
REG_GROWTH_RATE = 0x13  # UKF mu, 1/h on density
REG_LASER_POWER_MW = 0x14
# 0x15 (filtered_reflectance) is NOT deleted, despite what older integration
# notes claim. It carries UKF-smoothed reflectance for the *uncalibrated*
# fallback, and is NaN whenever a calibration is active.
REG_FILTERED_REFLECTANCE = 0x15
REG_FILTERED_CALIBRATED_DENSITY = 0x16  # UKF-filtered density (g/L)
REG_FILTERED_CALIBRATED_GROWTH_RATE = 0x17  # g/L/h
REG_CALIBRATED_ABSOLUTE_GROWTH_RATE = 0x18  # g/L/h, unfiltered
REG_READING_QUALITY = 0x19
REG_STATUS = 0x20
REG_HEALTH_SUMMARY = 0x21
REG_DIAGNOSTIC_CURSOR = 0x22
REG_DIAGNOSTIC_RECORD = 0x23
REG_HEALTH_RECHECK = 0x24
REG_SELFTEST_RESULTS = 0x25
REG_STATUS_EXT = 0x26
REG_NET_INFO = 0x27  # firmware 2.2.0+
REG_INITIAL_REFLECTANCE = 0x28  # firmware 2.2.0+
REG_INITIAL_DENSITY = 0x29  # firmware 2.2.0+
REG_READING_NUMBER = 0x30
REG_COMMAND_RESULT = 0x41

# --- Write registers ---
REG_CONTROL = 0x40
REG_LASER_POWER = 0x50
REG_INITIAL_REFLECTANCE_SET = 0x52

# --- Control commands (written to REG_CONTROL) ---
CMD_START = 0x01
CMD_STOP = 0x02
CMD_BLANK = 0x03
CMD_CAL_READ = 0x04
CMD_DEACTIVATE_LASER = 0x05  # stops the constant-power controller AND the laser
CMD_ERASE_INITIAL = 0x06
CMD_HEALTH_RECHECK = 0x07
CMD_ENABLE_UKF = 0x08
CMD_DISABLE_UKF = 0x09  # probe firmware 2.2.0+
CMD_ENABLE_INITIAL_CAPTURE = 0x0A  # probe firmware 2.2.0+
CMD_DISABLE_INITIAL_CAPTURE = 0x0B  # probe firmware 2.2.0+

# Used only in error messages. Keep these as verb phrases so command failures
# read as plain-language actions instead of exposing protocol command numbers.
COMMAND_NAMES = {
    CMD_START: "start its measurement job",
    CMD_STOP: "stop its measurement job",
    CMD_BLANK: "take a blank",
    CMD_CAL_READ: "take a calibration reading",
    CMD_DEACTIVATE_LASER: "deactivate the laser",
    CMD_ERASE_INITIAL: "erase the starting value",
    CMD_HEALTH_RECHECK: "recheck its health",
    CMD_ENABLE_UKF: "enable its growth model",
    CMD_DISABLE_UKF: "disable its growth model",
    CMD_ENABLE_INITIAL_CAPTURE: "enable starting-value capture",
    CMD_DISABLE_INITIAL_CAPTURE: "disable starting-value capture",
    REG_INITIAL_REFLECTANCE_SET: "restore the experiment's starting value",
}

# --- Status flags (register 0x20). Eight bits, not six. ---
STATUS_JOB_RUNNING = 1 << 0
STATUS_HAS_BLANK = 1 << 1
STATUS_HAS_INITIAL = 1 << 2  # initial-reference capture is OFF by default
STATUS_LASER_PWRCTL_RUNNING = 1 << 3
STATUS_LASER_STABLE = 1 << 4
STATUS_REFLECTANCE_STABLE = 1 << 5
STATUS_ERROR = 1 << 6  # warning-or-higher diagnostic active; read 0x21/0x23
STATUS_CALIBRATED = 1 << 7  # usable LUT present, so 0x12 is meaningful

# --- Extended status (register 0x26) ---
STATUS_EXT_JOB_STOPPED_BY_FAULT = 1 << 0
# The growth model is OFF by default on the probe, and while it is off every
# filtered register reads NaN -- identical to "still converging". Before firmware
# 2.2.0 there was no way to tell those apart over I2C at all, so this bit reads 0
# on older firmware and callers must treat that as "unknown", not "off".
STATUS_EXT_UKF_ENABLED = 1 << 1  # probe firmware 2.2.0+
STATUS_EXT_INITIAL_CAPTURE_ENABLED = 1 << 2  # probe firmware 2.2.0+

# --- Network link states (register 0x27) ---
LINK_DOWN = 0
LINK_STA = 1
LINK_AP = 2

# --- Reading quality (register 0x19) ---
QUALITY_UNAVAILABLE = 0
QUALITY_VALID = 1
QUALITY_DEGRADED = 2
QUALITY_INVALID = 3

# --- Command result phases (register 0x41) ---
PHASE_IDLE = 0
PHASE_PENDING = 1
PHASE_COMPLETE = 2
PHASE_NAMES = {
    PHASE_IDLE: "idle",
    PHASE_PENDING: "pending",
    PHASE_COMPLETE: "complete",
}

# How long to wait for the probe's own verdict on a command.
#
# These must EXCEED the firmware's internal budget for the same command, or the
# client gives up exactly when the probe might still be finishing and we never
# see the probe's own result -- turning a specific diagnostic into a generic
# client-side timeout, which is the failure mode the 0x41 latch exists to avoid.
#
# od_job_stop() waits up to 10 s for the reading task to exit and only then
# returns 0x0113 job.stop_timeout (od_reading_job.c:1364). A 10 s client timeout
# races that exactly, so STOP gets headroom. Everything else is prompt.
DEFAULT_COMMAND_TIMEOUT = 10.0
STOP_COMMAND_TIMEOUT = 20.0
# After the probe reports 0x0113, how long to keep checking whether it stopped
# anyway. It took about a second on the bench; this is generous.
STOP_CONFIRM_SECONDS = 15.0

# The one diagnostic we second-guess. See stop_job().
DIAG_JOB_STOP_TIMEOUT = 0x0113


class ProbeError(Exception):
    """The probe answered, but not with what was asked for."""


class ProbeCommandError(ProbeError):
    """A control command was refused or failed, with the probe's own reason."""

    def __init__(self, message: str, code: int = 0, native_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.native_code = native_code


@dataclass
class CommandResult:
    sequence: int
    command: int
    phase: int
    code: int
    native_code: int

    @property
    def complete(self) -> bool:
        return self.phase == PHASE_COMPLETE

    @property
    def ok(self) -> bool:
        return self.complete and self.code == 0


@dataclass
class ReadingQuality:
    quality: int
    diagnostic_code: int

    @property
    def usable(self) -> bool:
        # DEGRADED is still a real measurement, just a noisier one. Only INVALID
        # and UNAVAILABLE should be kept out of the experiment record.
        return self.quality in (QUALITY_VALID, QUALITY_DEGRADED)


@dataclass
class NetInfo:
    link: int
    ipv4: str | None
    mac: str
    hostname: str

    @property
    def connected(self) -> bool:
        return self.link == LINK_STA


@dataclass
class HealthSummary:
    schema_major: int
    schema_minor: int
    highest_severity: int
    active_count: int
    impacts: int
    generation: int


@dataclass
class Diagnostic:
    index: int
    code: int
    severity: int
    source: int
    lifecycle: int
    impacts: int
    native_code: int
    occurrences: int


class TurbidVisionProbe:
    """I2C driver for the Turbid Vision Probe."""

    def __init__(self, address: int = ODSENSORV2_ADDR) -> None:
        comm = I2C(hardware.SCL, hardware.SDA)
        self._dev = I2CDevice(comm, address)
        self.address = address

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _read(self, reg: int, n_data_bytes: int) -> tuple[int, bytes]:
        """Write the register address, then read ``1 + n_data_bytes``.

        Repeated START keeps the bus held between the address write and the read,
        matching ``Wire.endTransmission(false)`` + ``requestFrom()`` on Arduino.
        The register bank is pre-populated, so reads are served immediately.
        """
        out = bytearray(1 + n_data_bytes)
        with self._dev as i2c:
            i2c.write_then_readinto(bytes([reg]), out)
        return out[0], bytes(out[1:])

    def _read_float(self, reg: int) -> float:
        result, data = self._read(reg, 4)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not provide a requested measurement "
                f"(support register 0x{reg:02X}, code 0x{result:02X})."
            )
        value = struct.unpack("<f", data)[0]
        # Reject implausible floats from torn or short reads. NaN passes through:
        # the firmware uses it to mean "value not available yet".
        if math.isinf(value) or (0.0 < abs(value) < 1e-30):
            raise ProbeError(
                "The probe returned a corrupted or incompatible measurement "
                f"({value}; support register 0x{reg:02X}). Check the cable and "
                "probe firmware."
            )
        return value

    def _write(self, payload: bytes) -> None:
        with self._dev as i2c:
            i2c.write(payload)

    # ------------------------------------------------------------------
    # identity and health
    # ------------------------------------------------------------------

    def protocol_version(self) -> tuple[int, int]:
        result, data = self._read(REG_PROTOCOL_VERSION, 2)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not report its compatibility version "
                f"(support code 0x{result:02X}). Check the cable and probe firmware."
            )
        return data[0], data[1]

    def test_connection(self) -> bool:
        try:
            self.read_status()
            return True
        except Exception:
            return False

    def read_status(self) -> int:
        """Read the live status flags (register 0x20).

        These do **not** come along with data reads; this is a separate
        transaction, and the only way to see the flags.
        """
        result, data = self._read(REG_STATUS, 1)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not report its current status "
                f"(support code 0x{result:02X})."
            )
        return data[0]

    def read_status_ext(self) -> int:
        result, data = self._read(REG_STATUS_EXT, 1)
        if result != RESP_OK:
            raise ProbeError(
                "The probe stopped, but Pioreactor could not determine why "
                f"(support code 0x{result:02X})."
            )
        return data[0]

    def read_reading_quality(self) -> ReadingQuality:
        result, data = self._read(REG_READING_QUALITY, 3)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not report whether this measurement is valid "
                f"(support code 0x{result:02X}). This update was skipped."
            )
        return ReadingQuality(data[0], struct.unpack("<H", data[1:3])[0])

    def read_health_summary(self) -> HealthSummary:
        result, data = self._read(REG_HEALTH_SUMMARY, 10)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not provide its health summary "
                f"(support code 0x{result:02X})."
            )
        impacts = struct.unpack("<H", data[4:6])[0]
        generation = struct.unpack("<I", data[6:10])[0]
        return HealthSummary(data[0], data[1], data[2], data[3], impacts, generation)

    def read_diagnostic(self, index: int) -> Diagnostic | None:
        """Read one active diagnostic record. Returns None past the end."""
        self._write(bytes([REG_DIAGNOSTIC_CURSOR, index & 0xFF]))
        result, data = self._read(REG_DIAGNOSTIC_RECORD, 30)
        if result == RESP_NO_DATA:
            return None
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not provide details for an active problem "
                f"(support code 0x{result:02X})."
            )
        return Diagnostic(
            index=data[0],
            code=struct.unpack("<H", data[1:3])[0],
            severity=data[3],
            source=data[4],
            lifecycle=data[5],
            impacts=struct.unpack("<H", data[6:8])[0],
            native_code=struct.unpack("<i", data[8:12])[0],
            occurrences=struct.unpack("<H", data[20:22])[0],
        )

    def active_diagnostics(self) -> list[Diagnostic]:
        summary = self.read_health_summary()
        records = []
        for i in range(summary.active_count):
            record = self.read_diagnostic(i)
            if record is None:
                break
            records.append(record)
        return records

    def read_net_info(self) -> NetInfo:
        """Read the probe's network address (register 0x27, firmware 2.2.0+).

        Raises :class:`ProbeError` on older firmware, which answers ``0x01`` for a
        register it does not have. Feature-detecting this way is more reliable
        than comparing ``protocol_version``, which does not move for an additive
        register.
        """
        result, data = self._read(REG_NET_INFO, 43)
        if result == RESP_UNKNOWN_REGISTER:
            raise ProbeError(
                "The probe firmware is too old for this plugin. Install probe "
                "firmware 2.2.0 or newer."
            )
        if result not in (RESP_OK, RESP_NO_ADDRESS):
            raise ProbeError(
                "The probe could not report its identity and network address "
                f"(support code 0x{result:02X})."
            )
        link = data[0]
        ipv4 = ".".join(str(b) for b in data[1:5]) if link == LINK_STA else None
        mac = ":".join(f"{b:02x}" for b in data[5:11])
        host = data[11:43]
        nul = host.find(0)
        hostname = host[: nul if nul >= 0 else len(host)].decode("utf-8", "replace")
        return NetInfo(link, ipv4, mac, hostname)

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def read_command_result(self) -> CommandResult:
        result, data = self._read(REG_COMMAND_RESULT, 10)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not report whether the requested operation succeeded "
                f"(support code 0x{result:02X})."
            )
        sequence = struct.unpack("<H", data[0:2])[0]
        code = struct.unpack("<H", data[4:6])[0]
        native = struct.unpack("<i", data[6:10])[0]
        return CommandResult(sequence, data[2], data[3], code, native)

    def send_command(self, cmd: int) -> None:
        """Fire a control command without waiting for its outcome."""
        self._write(bytes([REG_CONTROL, cmd]))

    def _wait_for_command_result(
        self,
        command: int,
        previous_sequence: int,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> CommandResult:
        """Wait for the command-result latch to identify a completed request."""
        deadline = monotonic() + timeout
        last = None
        while monotonic() < deadline:
            try:
                last = self.read_command_result()
            except (OSError, ProbeError):
                sleep(0.1)
                continue
            if (
                last.sequence != previous_sequence
                and last.command == command
                and last.complete
            ):
                if last.code != 0:
                    operation = COMMAND_NAMES.get(command, "complete the requested operation")
                    raise ProbeCommandError(
                        f"The probe could not {operation}: {describe(last.code)}",
                        code=last.code,
                        native_code=last.native_code,
                    )
                return last
            sleep(0.1)
        operation = COMMAND_NAMES.get(command, "complete the requested operation")
        phase = (
            f" Last reported step: {PHASE_NAMES.get(last.phase, str(last.phase))}."
            if last is not None
            else ""
        )
        raise ProbeCommandError(
            f"The probe did not finish {operation} within {timeout:.0f} seconds."
            f"{phase} Try again; restart the probe if it remains busy."
        )

    def send_command_and_confirm(self, cmd: int, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        """Fire a control command and wait for the probe's own verdict.

        Poll the command-result latch (0x41), **not** a status bit. A command the
        probe refuses never sets its status bit, so bit-polling turns a specific
        failure -- "the probe is not fitted" -- into an unexplained timeout. That
        was a real defect in the previous integration.
        """
        previous = self.read_command_result()
        self.send_command(cmd)
        return self._wait_for_command_result(cmd, previous.sequence, timeout)

    def start_job(self, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        return self.send_command_and_confirm(CMD_START, timeout)

    def stop_job(self, timeout: float = STOP_COMMAND_TIMEOUT) -> CommandResult:
        """Stop the probe's measurement job.

        Treats the probe's own ``0x0113 job.stop_timeout`` as provisional. That
        code means "I gave up waiting", not "the job is still running": a stop
        requested during start-up initialisation -- before the first reading,
        roughly the first 18 s -- reliably exceeds the firmware's internal 10 s
        wait and then completes about a second later anyway. Measured on the
        bench: clean 0.1-0.6 s stops once readings are flowing, and a 10 s
        "failure" followed by an actual stop when the job had produced none.

        Reporting that as a failure would be wrong twice over -- the job did
        stop, and the diagnostic's recovery text tells the user to reboot the
        sensor. So confirm against the status bit before believing it.
        """
        try:
            return self.send_command_and_confirm(CMD_STOP, timeout)
        except ProbeCommandError as e:
            if e.code != DIAG_JOB_STOP_TIMEOUT:
                raise
            deadline = monotonic() + STOP_CONFIRM_SECONDS
            while monotonic() < deadline:
                sleep(0.5)
                try:
                    if not (self.read_status() & STATUS_JOB_RUNNING):
                        # It stopped after all.
                        return CommandResult(0, CMD_STOP, PHASE_COMPLETE, 0, 0)
                except (OSError, ProbeError):
                    continue
            raise

    def enable_growth_model(self, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        """Turn the probe's UKF on. The probe's job must be stopped."""
        return self.send_command_and_confirm(CMD_ENABLE_UKF, timeout)

    def disable_growth_model(self, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> CommandResult:
        """Turn the probe's UKF off. The probe's job must be stopped.

        Needs probe firmware 2.2.0+; older firmware refuses with
        ``0x0106 request.not_supported`` and can only be switched off from the
        probe's dashboard or USB console.
        """
        return self.send_command_and_confirm(CMD_DISABLE_UKF, timeout)

    def growth_model_enabled(self) -> bool:
        """Is the probe's growth model (UKF) on? Register 0x26 bit 1.

        A **set** bit is conclusive. A **clear** bit means either "off" or "this
        probe's firmware predates 2.2.0 and cannot report it" -- the bit reads 0
        in both cases and nothing on the I2C bus distinguishes them. That
        ambiguity costs nothing in practice: the fix is the same either way,
        because enabling an already-enabled growth model just re-saves the
        setting. Word user-facing messages to cover both readings.
        """
        return bool(self.read_status_ext() & STATUS_EXT_UKF_ENABLED)

    def set_growth_model(self, enabled: bool) -> None:
        command = CMD_ENABLE_UKF if enabled else CMD_DISABLE_UKF
        self.send_command_and_confirm(command)
        if self.growth_model_enabled() != enabled:
            raise ProbeError(
                "The probe did not keep the requested growth-model setting "
                f"({'on' if enabled else 'off'}). Check probe firmware compatibility "
                "and try again."
            )

    def initial_capture_enabled(self) -> bool:
        return bool(self.read_status_ext() & STATUS_EXT_INITIAL_CAPTURE_ENABLED)

    def set_initial_capture(self, enabled: bool) -> None:
        command = CMD_ENABLE_INITIAL_CAPTURE if enabled else CMD_DISABLE_INITIAL_CAPTURE
        self.send_command_and_confirm(command)
        if self.initial_capture_enabled() != enabled:
            raise ProbeError(
                "The probe did not keep the requested starting-value capture setting "
                f"({'on' if enabled else 'off'}). Check probe firmware compatibility "
                "and try again."
            )

    def erase_initial(self) -> None:
        self.send_command_and_confirm(CMD_ERASE_INITIAL)
        if self.read_status() & STATUS_HAS_INITIAL:
            raise ProbeError(
                "The probe could not clear its previous starting value. No new value "
                "was captured; restart the probe and try again or contact support."
            )

    def read_initial_reflectance(self) -> float:
        return self._read_float(REG_INITIAL_REFLECTANCE)

    def read_initial_density(self) -> float:
        return self._read_float(REG_INITIAL_DENSITY)

    def set_initial_reflectance(self, value: float) -> None:
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(
                "The saved starting reflectance is invalid and cannot be restored. "
                "Reset this experiment's starting value."
            )
        previous = self.read_command_result()
        self._write(bytes([REG_INITIAL_REFLECTANCE_SET]) + struct.pack("<f", value))
        self._wait_for_command_result(REG_INITIAL_REFLECTANCE_SET, previous.sequence)
        restored = self.read_initial_reflectance()
        if not math.isclose(restored, value, rel_tol=1e-6, abs_tol=1e-9):
            raise ProbeError(
                "The probe did not correctly restore this experiment's saved starting "
                f"value. Expected {value}; read back {restored}. Restart the probe "
                "and try again or contact support."
            )

    def set_laser_power(self, power_pct: float) -> None:
        self._write(bytes([REG_LASER_POWER]) + struct.pack("<f", power_pct))

    # ------------------------------------------------------------------
    # measurements
    # ------------------------------------------------------------------

    def read_reading_number(self) -> int:
        result, data = self._read(REG_READING_NUMBER, 4)
        if result != RESP_OK:
            raise ProbeError(
                "The probe could not identify the current measurement "
                f"(support code 0x{result:02X}). This update was skipped."
            )
        return struct.unpack("<I", data)[0]

    def read_measured_reflectance(self) -> float:
        return self._read_float(REG_MEASURED_REFLECTANCE)

    def read_normalized_reflectance(self) -> float:
        return self._read_float(REG_NORMALIZED_REFLECTANCE)

    def read_calibrated_density(self) -> float:
        return self._read_float(REG_CALIBRATED_DENSITY)

    def read_growth_rate(self) -> float:
        return self._read_float(REG_GROWTH_RATE)

    def read_laser_power_mw(self) -> float:
        return self._read_float(REG_LASER_POWER_MW)

    def read_filtered_reflectance(self) -> float:
        return self._read_float(REG_FILTERED_REFLECTANCE)

    def read_filtered_calibrated_density(self) -> float:
        return self._read_float(REG_FILTERED_CALIBRATED_DENSITY)

    def read_filtered_calibrated_growth_rate(self) -> float:
        return self._read_float(REG_FILTERED_CALIBRATED_GROWTH_RATE)

    def read_calibrated_absolute_growth_rate(self) -> float:
        return self._read_float(REG_CALIBRATED_ABSOLUTE_GROWTH_RATE)
