# Troubleshooting

## The activity is missing

Confirm the plugin is installed on the affected worker, then restart the
Pioreactor web services or reboot the unit. If the activity is still missing,
reinstall the plugin package on that worker.

## The probe cannot be found

Stop any other job or program using the probe, check its power, and reseat its
data cable at both ends. Only the Turbid Vision Probe job should communicate
with it. A dashboard refresh may briefly be reading the same connection; wait
a few seconds and start again.

## Normalized OD is absent

Normalized OD is produced only when **Turbid Vision Growth Model Enabled** is
set to `1`. On the first start for an experiment, wait for the probe to capture
and for Pioreactor to save a valid starting measurement. Review the job log for
an incomplete starting measurement, an unusable optical measurement, or a
calibration mismatch.

## A saved starting value is rejected

The saved starting measurement belongs to one Pioreactor experiment, unit, and
physical probe. Restore the calibration state and calibration used when it was
saved, or use a new experiment. The plugin will not combine an older starting
density with a materially different calibration.

## A unit dashboard link does not open the probe

Open **Control All Pioreactors** and inspect that unit's dashboard status.
Confirm that the probe has joined a network reachable by the leader and that
its reported `.local` name or IP address is reachable from the leader. Correct
the probe's Wi-Fi or hostname configuration, then refresh the dashboard map.

The probe's Wi-Fi setup hotspot is intentionally unavailable through the
Pioreactor dashboard connection. Connect the probe to a network reachable by
the Pioreactor leader. A dashboard network problem does not by itself prevent
measurements through the data cable.

## Measurements stop unexpectedly

The plugin stops if the probe reports a hardware problem or if another
interface stops measurement. Read the Pioreactor job log and the probe
dashboard's active conditions before restarting. This prevents stale
measurements from entering native Pioreactor tables.
