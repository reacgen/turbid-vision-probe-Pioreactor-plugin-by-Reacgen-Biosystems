"""Pure signal-routing rules for native Pioreactor publications."""
from __future__ import annotations

import math

REFLECTANCE_TO_PPM = 1_000_000.0


def native_od_value(
    *,
    calibrated: bool,
    measured_reflectance: float | None,
    calibrated_density: float | None,
) -> tuple[float, str] | None:
    """Return the value and display unit for Pioreactor's native OD pathway."""
    if calibrated:
        if calibrated_density is None or not math.isfinite(calibrated_density):
            return None
        return calibrated_density, "g/L"
    if measured_reflectance is None or not math.isfinite(measured_reflectance):
        return None
    return measured_reflectance * REFLECTANCE_TO_PPM, "ppm"


def native_normalized_od_value(
    *,
    calibrated: bool,
    filtered_reflectance: float | None,
    filtered_calibrated_density: float | None,
    initial_density: float | None,
) -> float | None:
    """Return the sensor-derived dimensionless value for native normalized OD."""
    if calibrated:
        if (
            filtered_calibrated_density is None
            or initial_density is None
            or not math.isfinite(filtered_calibrated_density)
            or not math.isfinite(initial_density)
            or initial_density <= 0.0
        ):
            return None
        return filtered_calibrated_density / initial_density
    if filtered_reflectance is None or not math.isfinite(filtered_reflectance):
        return None
    return filtered_reflectance
