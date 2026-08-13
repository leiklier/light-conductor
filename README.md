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
- **Night path, TV mode, away-off, outdoor room** — built in. TV mode is
  tri-state: playing dims the room to its TV level, paused (or the TV merely
  switched on) only *caps* it, and a pause grace means a rewind never walks the
  lights up and back down. A balcony can read a lux sensor (even one an indoor
  room already uses, e.g. at the window) and ease in as the light actually
  goes, instead of at a sun-ramp threshold.
- **Calibrated photometry** — a one-button night sweep measures each lamp's
  lux gain and dimming curve at the room's sensor.
- **Manual control is respected** — any wall rotary, HomeKit, or voice change
  latches a per-room override; the conductor backs off until vacancy, sleep,
  away, or a timeout.
- **Whole-home setup** — one config entry for the house, prefilled by
  discovery from your Home Assistant areas (lights, illuminance sensor,
  presence-conductor occupancy), then editable room-by-room under Options.
- **Recorder-friendly** — measurement sensors publish through a quantize +
  rate-limit gate; volatile internals live on the diagnostics platform, never
  in recorded attributes.

Entities: `light.light_conductor_master` (master gain dimmer),
`switch.light_conductor_enabled` / `_away_lighting`, per-outdoor-room
occupational switch, per-trigger-room door-lighting switch (gates the
door-triggered lighting; default on), and per room `sensor.<room>_role`,
`binary_sensor.<room>_overridden`, and — for rooms with a lux sensor —
`sensor.<room>_natural_lux` plus, where a closed loop can run (everything but
outdoor rooms), `sensor.<room>_target_lux`,
`button.<room>_record_light_response`, `event.<room>_calibration`.

Status: implemented, **not yet released**. Install via HACS as a *custom
repository* (`leiklier/light-conductor`); it is not in the default HACS store.
See `docs/ENGINE_SPEC.md` (normative), `docs/DECISION.md` (ADR),
`docs/DISCOVERY.md` (live-instance analysis), and `docs/MIGRATION.md`
(retiring the legacy automations).

## Releases & channels

Behavioral changes ship as `vX.Y.Z-beta.N` pre-releases and soak on the live
install before being promoted unchanged to stable. Same policy as
sonos-conductor.

## Development

```sh
uv sync           # pinned to the production HA version
uv run pytest     # pure-core scenario tests + full HA e2e tests
uv run ruff check .
uv run ruff format --check .
```

The `core/` package must never import `homeassistant` — enforced by ruff
(TID251) and a poisoned-import test (`tests/test_purity.py`). Code and tests
cite the spec's rule numbers; if behavior and spec disagree, one of them is a
bug.
