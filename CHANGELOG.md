# Changelog

## 0.1.0 - 2026-08-31

Initial public-release candidate.

- Added one single-owner Turbid Vision Probe job with optional onboard growth
  modelling.
- Routed calibrated and uncalibrated outputs through native Pioreactor OD,
  normalized-OD, and growth-rate pathways.
- Added experiment-, unit-, and probe-specific starting-measurement storage so
  normalization remains consistent after job restarts.
- Added operator-facing probe diagnostics and safe stale/fault handling.
- Added direct per-unit dashboard links and a selector for controlling all
  assigned Pioreactors.
- Made automatic dashboard routing prefer each probe's `.local` name and fall
  back to its IP address when necessary.
- Removed the redundant per-worker dashboard address configuration.
- Removed development-only custom plots, tables, exports, and split jobs.
