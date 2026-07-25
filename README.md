# Light Conductor

Adaptive whole-home lighting for Home Assistant, built for Plejd-dimmed rooms
with mmWave presence and lux sensing. Part of the conductor family
([sonos-conductor](https://github.com/leiklier/sonos-conductor),
[presence-conductor](https://github.com/leiklier/presence-conductor),
[grow-conductor](https://github.com/leiklier/grow-conductor)).

What it does:

- **Regulates illuminance, not brightness** — a per-room estimator separates
  natural light from the lights' own contribution to the lux sensor, so the
  control loop doesn't chase its own output.
- **Circadian shaping** — targets and color temperature drift continuously
  toward cozy warm evenings; no hard clock steps.
- **Follow-me with room personalities** — living-area rooms make subtle
  adjustments and never go dark while the area is in use; utility rooms turn
  fully off after vacancy.
- **Master gain dimmer** — one HomeKit dimmer scales the whole automation
  relative to what it would do on its own.
- **Night path, TV mode, away-off, outdoor room** — built in.
- **Calibrated photometry** — a one-button night sweep measures each lamp's
  lux gain and dimming curve at the room's sensor.

Status: design phase. See `docs/ENGINE_SPEC.md` (normative),
`docs/DECISION.md` (ADR), `docs/DISCOVERY.md` (live-instance analysis).

## Releases & channels

Behavioral changes ship as `vX.Y.Z-beta.N` pre-releases and soak on the live
install before being promoted unchanged to stable. Same policy as
sonos-conductor.
