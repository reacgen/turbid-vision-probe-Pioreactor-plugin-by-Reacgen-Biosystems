# -*- coding: utf-8 -*-
"""Human-readable names for the probe's diagnostic codes.

The probe reports failures as 16-bit codes; nothing over I2C carries the
text. Reporting a bare ``0x3002`` to a Pioreactor user is only marginally
better than reporting nothing, so this table carries the probe's own
wording across.

The code set originated in ``main/diagnostics.h`` and ``main/diagnostics.c`` in
the ODSensorV2 firmware repo. Its titles and recovery text are deliberately
worded for a nontechnical Pioreactor user. Preserve these reviewed strings when
updating the catalog from newer firmware.
"""
from __future__ import annotations


# code -> (key, title, recovery). Recovery is empty for the request-scoped
# 0x01xx codes, which are command return values rather than probe conditions.
DIAGNOSTICS: dict[int, tuple[str, str, str]] = {
    0x0000: ("ok", "OK", "No action required."),
    0x0101: ("request.invalid_argument", "The probe rejected an invalid command value", "Check plugin and probe firmware compatibility."),
    0x0102: ("request.invalid_state", "The probe cannot perform this operation in its current state", "Stop other probe activity and try again."),
    0x0103: ("request.busy", "Probe is busy", ""),
    0x0104: ("request.timeout", "Operation timed out", "Try once more. Contact Reacgen Biosystems if it keeps happening."),
    0x0105: ("request.not_found", "The probe could not find information required for this operation", "Check active diagnostics and firmware compatibility."),
    0x0106: ("request.not_supported", "This probe firmware does not support the requested operation", "Update the probe firmware or install a compatible plugin."),
    0x0107: ("request.no_memory", "The probe does not have enough free memory for this operation", "Stop other activity and retry; restart the probe if it persists."),
    0x0108: ("request.io", "The probe encountered a hardware, storage, or communication failure", "Review active diagnostics before retrying."),
    0x0110: ("job.already_running", "Job already running", "Stop the current job before starting another one."),
    0x0111: ("job.must_be_stopped", "Stop probe measurement before changing this setting", "Then try again."),
    0x0112: ("job.start_failed", "Job failed to start", "Fix any active health errors, then start the job again."),
    0x0113: ("job.stop_timeout", "Job stop timed out", "Reboot the sensor if it still appears to be running."),
    0x0120: ("calibration.invalid", "Calibration is not valid", ""),
    0x0121: ("calibration.store_full", "Calibration storage is full", ""),
    0x0122: ("calibration.no_active", "No active calibration", "Select an existing calibration or create one first."),
    0x0130: ("logging.name_invalid", "Invalid log name", "Use letters, digits, '-' or '_' with at most 24 characters."),
    0x0131: ("logging.exists", "Log already exists", "Pick another name or delete the existing log."),
    0x0132: ("logging.active", "Log is active", "Finish or close the active log before continuing."),
    0x0133: ("logging.full", "Log storage full", "Download any logs you need to keep, then delete old ones."),
    0x0134: ("logging.unavailable", "Logging unavailable", "Save readings on the connected computer instead. Contact Reacgen Biosystems for repair."),
    0x0140: ("update.busy", "An update is already running", ""),
    0x0141: ("update.manifest_invalid", "Invalid update information", "Refresh the update list. Contact Reacgen Biosystems if the update source is correct and this keeps happening."),
    0x0142: ("update.connection_failed", "Update connection failed", "Check Wi-Fi, the update address, and that the sensor's date and time are correct."),
    0x0143: ("update.verify_failed", "Update failed its safety check", "Do not install it. Download a valid, correctly signed update."),
    0x0144: ("update.storage_failed", "Update could not be saved", "Reboot and try again. Contact Reacgen Biosystems if this persists."),
    0x0150: ("auth.required", "Requires the probe's admin password", ""),
    0x0151: ("auth.invalid", "Admin password rejected", ""),
    0x1001: ("boot.nvs", "The probe cannot access its saved settings", "Restart and run a health recheck; contact support if it persists."),
    0x1002: ("boot.data_fs", "The probe cannot access its saved data", "Run a health recheck and contact support before reformatting storage."),
    0x1003: ("boot.external_flash", "The probe's external storage is unavailable", "Power-cycle it and check that the flash board is seated; contact support if it persists."),
    0x1004: ("boot.i2c_slave", "The probe cannot communicate with Pioreactor over its wired connection", "Check the configured address and cable, then run a health recheck."),
    0x1005: ("hardware.adc", "The probe's measurement electronics failed their startup check", "Restart the probe and contact support if it persists."),
    0x1006: ("hardware.dac", "The probe's laser-control electronics failed their startup check", "Restart the probe and contact support if it persists."),
    0x1007: ("hardware.status_led", "The probe's status light is unavailable", "Measurements can continue, but use the dashboard for status."),
    0x1008: ("hardware.laser_ntc", "Laser temperature unavailable", "Check the probe cable is fully seated. Contact Reacgen Biosystems if the error stays."),
    0x1009: ("hardware.pd_ntc", "Detector temperature unavailable", "Check the probe cable is fully seated. Contact Reacgen Biosystems if the error stays."),
    0x100A: ("hardware.probe_gpio", "The sensor cannot determine whether the optical probe is installed", "Check the probe connector and contact support if it persists."),
    0x100B: ("hardware.laser_driver", "The laser-control hardware failed its startup check", "Keep measurement stopped and contact support."),
    0x100C: ("hardware.laser_loopback", "Laser self-check failed", "Make sure the probe is installed and stop the OD job, then run a health recheck. Contact Reacgen Biosystems if the check still fails."),
    0x100E: ("recovery.state_invalid", "Automatic recovery information is missing", "Measurements can continue, but recovery storage should be reprovisioned."),
    0x100F: ("recovery.golden_invalid", "Backup firmware missing", "Reprovision the backup firmware so automatic recovery will work. Contact Reacgen Biosystems for guidance."),
    0x1010: ("update.cert_store_invalid", "Secure update certificates are missing", "Measurements can continue, but online updates are unavailable until the probe is reprovisioned."),
    0x1011: ("update.web_bundle_invalid", "The full sensor dashboard is unavailable or does not match this firmware", "Install the matching dashboard bundle."),
    0x1012: ("update.last_attempt_failed", "Update did not complete", "Check Wi-Fi, the update address, and the sensor's clock, then try the update again."),
    0x2001: ("measurement.adc_read", "The probe could not collect an optical measurement", "Check the probe cable; it will retry automatically."),
    0x2002: ("measurement.ambient_invalid", "Unexpected light was detected during the background measurement", "The probe will retry automatically; contact support if it persists."),
    0x2003: ("optics.power_settle_timeout", "Laser power slow to settle", "Let the sensor warm up or lower the target power. The condition clears on its own once readings settle."),
    0x2005: ("measurement.optical_ratio_invalid", "The reflected-light signal is too weak for a valid measurement", "Check that the optical path is not blocked."),
    0x2006: ("measurement.initial_reference_failed", "The probe could not measure a valid starting value", "Resolve other optical warnings, then restart the job."),
    0x2007: ("measurement.laser_temperature_unavailable", "Laser temperature unavailable", "Check the probe cable is fully seated. This clears itself when a good reading returns."),
    0x2008: ("measurement.detector_temperature_unavailable", "Detector temperature unavailable", "Check the probe cable is fully seated. This clears itself when a good reading returns."),
    0x2009: ("measurement.filter_init_failed", "The probe's growth model could not start with the current calibration and starting values", "Check the active calibration and references, then restart."),
    0x200A: ("optics.target_unreachable", "Laser cannot reach target power", "Lower the target power, let the sensor cool, and check the optical path is not obstructed."),
    0x200B: ("optics.laser_enable_failed", "Laser did not switch on", "If the probe is out, install it and try again. Contact Reacgen Biosystems if the laser stays off with the probe fitted."),
    0x3001: ("safety.optical_power_limit", "Optical safety shutdown", "Keep the laser off. Contact Reacgen Biosystems to service the optical path; run a health recheck once serviced."),
    0x3002: ("safety.probe_removed", "Probe not installed", "Install the probe, then start the job again. The sensor rechecks the probe the next time the laser is switched on."),
    0x30FF: ("system.diagnostic_capacity", "The probe has more active problems than it can report at once", "Resolve the visible diagnostics, then run a health recheck to reveal any remaining issue."),
    0x3101: ("system.low_heap", "The probe is running low on working memory", "Stop other activity; restart the probe if it does not recover."),
    0x3102: ("system.heap_fragmented", "The probe's working memory needs a restart", "Reboot the probe, then run a health recheck."),
    0x3201: ("storage.persistence_failed", "The probe could not save a setting permanently", "Check storage health and try the change again."),
    0x3202: ("logging.storage_full", "Log storage full", "Download logs you need to keep, then delete old ones. Logging resumes once enough space is free."),
    0x3203: ("storage.reference_save_failed", "The probe could not save a blank, starting value, or calibration point", "Try again and contact support if it persists."),
    0x3204: ("storage.log_write_failed", "Log write failed", "Check that log storage is not full and delete old logs if needed."),
    0x4001: ("network.wifi_disconnected", "Wi-Fi disconnected", "Check credentials and signal strength. Reconnects automatically when the network returns."),
}


def describe(code: int, *, with_recovery: bool = True) -> str:
    """Return a sentence naming ``code``, always including the raw hex.

    The hex stays in the message even when the code is known: it is what a
    user quotes to support, and what matches the probe's own dashboard.
    """
    entry = DIAGNOSTICS.get(code)
    if entry is None:
        return (
            f"The probe reported an unrecognized problem (support code 0x{code:04X}). "
            "Check the sensor dashboard and plugin/firmware compatibility."
        )
    _key, title, recovery = entry
    text = f"{title} (0x{code:04X})"
    if with_recovery and recovery:
        text += f". {recovery}"
    return text
