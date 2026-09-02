# -*- coding: utf-8 -*-
"""Reacgen Biosystems Turbid Vision Probe support for the Pioreactor.

Adds one background job that owns a Turbid Vision Probe session and publishes
compatible quantities through Pioreactor's native readings pathways.

Requires Pioreactor >= 26.5.2 and probe firmware >= 2.2.0.
"""
from __future__ import annotations

__plugin_name__ = "Turbid Vision Probe"
__plugin_author__ = "Reacgen Biosystems"
__plugin_summary__ = (
    "Optical density and optional growth modelling from a Reacgen Turbid Vision Probe."
)
__plugin_version__ = "0.1.0"
__plugin_homepage__ = (
    "https://github.com/reacgen/turbid-vision-probe-Pioreactor-plugin-by-Reacgen-Biosystems"
)

# Imported for the decorators' registration side effects. Pioreactor discovers
# plugin routes while constructing its native leader and unit API blueprints.
from . import web_routes as _web_routes  # noqa: F401
from .odsensorv2 import (
    ODSENSORV2_ADDR,  # noqa: F401
    TurbidVisionProbe,  # noqa: F401
)
from .probe import (
    TurbidVisionProbeJob,  # noqa: F401
    click_turbidvision_probe,  # noqa: F401
)
