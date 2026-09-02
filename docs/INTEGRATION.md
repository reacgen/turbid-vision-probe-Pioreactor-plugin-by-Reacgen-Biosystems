# Integration architecture

This document describes the current runtime behavior for readers familiar with
Pioreactor jobs, experiments, storage, and the frontend. No knowledge of the
probe's internal firmware is required.

## Ownership and startup

`turbidvision_probe` is the only Pioreactor job that communicates with the
probe during a run. It controls measurement and, when selected, the probe's
growth model. Every start applies the selected settings again. Turning an
option off in Pioreactor therefore also turns it off on the probe, even if it
was previously enabled from the probe dashboard.

```mermaid
flowchart TD
    Start[Start Turbid Vision Probe job] --> Reserve[Reserve the worker's probe connection]
    Reserve --> Stop[Stop any measurement already running on the probe]
    Stop --> Configure[Apply and verify the selected measurement options]
    Configure --> Enabled{Growth model enabled?}
    Enabled -- No --> Disabled[Start OD measurement without normalization]
    Enabled -- Yes --> Lookup[Look for this experiment's saved starting measurement]
    Lookup --> Ready{Usable starting measurement found?}
    Ready -- Yes --> Restore[Restore it to sensor memory]
    Restore --> Verify[Confirm that it matches the current calibration]
    Verify --> Run[Start measurement and reuse it]
    Ready -- No --> Pending[Mark starting-measurement setup as incomplete]
    Pending --> Erase[Clear any starting measurement left by another experiment]
    Erase --> Capture[Start measurement and wait for a valid starting measurement]
    Capture --> Save[Save it in Pioreactor experiment storage]
    Save --> Run
```

Pioreactor marks setup as incomplete before clearing the sensor's previous
starting measurement. If power is lost during this process, the next start
takes a fresh measurement instead of accepting one left by another experiment.

## Polling and native Pioreactor routing

The plugin reads one complete measurement at a time, ignores incomplete or
repeated results, and publishes the values through Pioreactor's native data
paths. Native Pioreactor tables and charts remain authoritative.

| Probe state | Native Pioreactor OD Reading | Native Pioreactor Normalized OD Reading | Native Pioreactor Growth Rate |
|---|---|---|---|
| Uncalibrated, growth model off | Optical measurement displayed in ppm | Not produced | Not produced |
| Uncalibrated, growth model on | Optical measurement displayed in ppm | Filtered normalized measurement, dimensionless | Not produced; growth rate requires a calibration in this integration |
| Calibrated, growth model off | Calibrated density in g/L | Not produced | Not produced |
| Calibrated, growth model on | Calibrated density in g/L | Filtered density divided by the starting density saved for the experiment | Specific growth rate in 1/h |

The ppm conversion is only for display and native Pioreactor storage. The
plugin saves the underlying starting measurement in its original units, so a
display conversion can never change normalization.

The plugin reports the native Pioreactor OD channel angle as 180 degrees. It
publishes the same data shapes expected from native OD and growth jobs, so
stock database ingestion and plots continue to work. It does not start
Pioreactor's growth-rate-calculating job; when enabled, the probe supplies the
filtered value and growth estimate.

## Normalization ownership

The growth model needs a starting measurement. Pioreactor owns the
experiment-level copy so restarting the job does not create a new baseline or
accidentally reuse a value from another experiment.

The saved value is associated with:

- the Pioreactor experiment;
- the Pioreactor unit;
- the physical probe attached to that unit.

For an uncalibrated experiment, Pioreactor saves the starting optical
measurement. For a calibrated experiment, it also saves the corresponding
starting density. On a restart, the plugin restores the optical measurement to
sensor memory and reads it back before measurement begins. It also checks that
the saved value still agrees with the active calibration. If the calibration
state or calibration has materially changed, the job stops and asks the user
to restore it or use a new experiment.

```mermaid
flowchart LR
    ProbeInitial[Probe captures a starting optical measurement] --> PiStore[(Pioreactor experiment storage)]
    ProbeDensity[If calibrated, probe also reports starting density] --> PiStore
    PiStore -->|job restart| SensorMemory[Restore starting measurement to sensor memory]
    PiStore -->|starting density| Normalize[Calibrated normalization]
    Filtered[Filtered density] --> Normalize
    Normalize --> NativeNormalized[Native Pioreactor normalized OD]
```

When uncalibrated, the probe already supplies a filtered normalized
measurement, so the plugin publishes it directly. Pioreactor still saves the
starting optical measurement for consistent restarts.

## Dashboard discovery and routing

The probe dashboard runs on the probe and uses Wi-Fi. The measurement data
cable and dashboard network connection are independent.

```mermaid
flowchart LR
    Worker[Pioreactor worker] -->|read attached probe identity and network details| Discovery[(Saved dashboard details)]
    Discovery --> Leader[Pioreactor leader]
    Leader --> Registry[(Worker-to-probe dashboard map)]
    Registry --> Connection[Per-unit dashboard connection]
    UnitControl[Unit-specific Control] --> Direct[Open that unit's dashboard]
    ControlAll[Control All] --> Selector[All-unit selector]
    Direct --> Connection
    Selector --> Connection
    Connection --> SensorUI[Probe dashboard over Wi-Fi]
```

While the job is stopped, a worker service reads the attached probe's dashboard
details. While the job is running, the job refreshes those details itself so
two processes never try to use the probe connection simultaneously. The leader
updates its worker-to-dashboard map every five minutes.

Automatic routing uses the physical probe identity and first tries its reported
`.local` hostname, which remains stable when the router changes its IP address.
It falls back to the reported IP only when the hostname cannot be resolved. No
per-worker address configuration is required. If two workers appear to point
to the same probe or network address, the plugin reports the conflict instead
of guessing.

## Shutdown and fault behavior

When the Pioreactor job stops normally, it stops probe measurement and releases
the worker's probe connection. If another interface stops the probe during a
run, the plugin stops rather than publishing stale values. Sensor conditions
are translated into operator-facing Pioreactor warnings and errors. A dashboard
network failure produces a warning but does not stop measurements through the
data cable.
