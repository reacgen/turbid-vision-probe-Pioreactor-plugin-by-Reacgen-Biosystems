# -*- coding: utf-8 -*-
"""Experiment-scoped authoritative initial references for the probe."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from typing import Literal

from pioreactor.utils import local_persistent_storage

CACHE_NAME = "turbidvision_initial_references"
CACHE_SCHEMA = 2


class InitialCacheError(ValueError):
    """A cache entry exists but cannot safely be used."""


@dataclass(frozen=True)
class InitialReference:
    state: Literal["pending", "ready"]
    experiment: str
    unit: str
    probe_id: str
    captured_at: str | None
    initial_reflectance: float | None
    initial_density: float | None
    schema: int = CACHE_SCHEMA

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def _key(experiment: str, unit: str, probe_id: str) -> tuple[str, str, str]:
    return experiment, unit, probe_id


def _decode(raw: object) -> InitialReference:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise InitialCacheError(
            "The saved starting value for this experiment is unreadable. Pioreactor will "
            "not overwrite it automatically. Reset this experiment's saved starting value "
            "or contact support."
        )
    try:
        payload = json.loads(raw)
        if payload.get("schema") == 1:
            if payload.pop("mode", None) != "od_and_growth":
                raise InitialCacheError(
                    "This experiment has an incompatible starting value saved by an older "
                    "plugin version. Reset the saved starting value or contact support."
                )
            payload["schema"] = CACHE_SCHEMA
        entry = InitialReference(**payload)
    except InitialCacheError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InitialCacheError(
            "The saved starting value for this experiment is damaged or incomplete. "
            "Reset it or contact support before restarting."
        ) from exc

    if entry.schema != CACHE_SCHEMA:
        raise InitialCacheError(
            "This experiment's saved starting value was created by an incompatible plugin "
            f"version (format {entry.schema}). Install a compatible plugin or reset the "
            "saved value."
        )
    if entry.state not in ("pending", "ready"):
        raise InitialCacheError(
            f"The saved starting value has an invalid status ({entry.state}). Reset it or "
            "contact support before restarting."
        )
    if entry.ready:
        if (
            entry.initial_reflectance is None
            or not math.isfinite(entry.initial_reflectance)
            or entry.initial_reflectance <= 0.0
        ):
            raise InitialCacheError(
                "The saved starting reflectance is invalid. Reset this experiment's "
                "starting value and capture a new one."
            )
        if entry.initial_density is not None and (
            not math.isfinite(entry.initial_density) or entry.initial_density <= 0.0
        ):
            raise InitialCacheError(
                "The saved starting density is invalid. Reset this experiment's starting "
                "value and capture a new one with a valid calibration."
            )
        if not entry.captured_at:
            raise InitialCacheError(
                "The saved starting value is incomplete because its capture time is "
                "missing. Reset it or contact support."
            )
    return entry


def load_initial_reference(
    experiment: str, unit: str, probe_id: str
) -> InitialReference | None:
    with local_persistent_storage(CACHE_NAME) as cache:
        raw = cache.get(_key(experiment, unit, probe_id))
    if raw is None:
        return None
    entry = _decode(raw)
    if (entry.experiment, entry.unit, entry.probe_id) != (experiment, unit, probe_id):
        raise InitialCacheError(
            "The saved starting value belongs to a different experiment, Pioreactor, or "
            "probe. It will not be reused. Reset the affected entry or contact support."
        )
    return entry


def _save(entry: InitialReference) -> InitialReference:
    payload = json.dumps(asdict(entry), allow_nan=False, separators=(",", ":"))
    with local_persistent_storage(CACHE_NAME) as cache:
        cache[_key(entry.experiment, entry.unit, entry.probe_id)] = payload
    return entry


def mark_initial_pending(experiment: str, unit: str, probe_id: str) -> InitialReference:
    return _save(
        InitialReference(
            state="pending",
            experiment=experiment,
            unit=unit,
            probe_id=probe_id,
            captured_at=None,
            initial_reflectance=None,
            initial_density=None,
        )
    )


def save_initial_reference(
    *,
    experiment: str,
    unit: str,
    probe_id: str,
    captured_at: str,
    initial_reflectance: float,
    initial_density: float | None,
) -> InitialReference:
    entry = InitialReference(
        state="ready",
        experiment=experiment,
        unit=unit,
        probe_id=probe_id,
        captured_at=captured_at,
        initial_reflectance=initial_reflectance,
        initial_density=initial_density,
    )
    # Validate before writing so NaN/invalid values cannot poison a later start.
    return _save(_decode(json.dumps(asdict(entry), allow_nan=False)))
