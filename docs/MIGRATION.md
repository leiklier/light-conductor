# Migration — from the legacy automations to Light Conductor

This guide retires the ~10 lighting automations and 3 scripts on the live
instance (see `DISCOVERY.md`) in favour of the `light_conductor` integration.
Entity ids below are the live-instance ids as of 2026-07-25; adjust if yours
differ. **Do not delete anything until the shadow period (below) is done.**

## 0. Install & configure

1. Add the repo as a HACS custom repository and download **Light Conductor**
   (not yet in the default HACS store — see README). Restart Home Assistant.
2. Settings → Devices & Services → **Add Integration** → *Light Conductor*.
   Setup proposes one room per HA area from its lights + illuminance sensor +
   `*_room_occupancy` sensor. Review under **Configure** (Options):
   - Fix the two living-area lights that sit in area `stue`
     (`light.sofakrok_taklys`, `light.spisebord_taklys`) — put them in the
     right rooms and set `living_group` on the living rooms.
   - `gang` → shape **corridor**; `soverom` → shape **door** with trigger
     `binary_sensor.soverom_dor`; `balkong` → shape **outdoor**.
   - Assign bands: kjøkken downlights → `accent`, taklys → `primary`,
     benkebelysning → `boost`.
   - Global signals: sleep `binary_sensor.household_sleep_mode`, anyone-home
     `binary_sensor.presence_conductor_anyone_home`, vacation
     `input_boolean.vacation_mode`, TV `media_player.sofakrok_tv` +
     `media_player.sofakrok_apple_tv`, night triggers
     `binary_sensor.soverom_dor` + living-room pass-by events, night-path rooms
     {sofakrok, gang, spisebord, kjøkken-downlights}.
3. Leave `switch.light_conductor_enabled` **off** for the shadow period.

## 1. Shadow period (observe-only)

With `enabled` off the engine runs, estimates natural light, classifies roles,
and publishes diagnostics **without writing any light** — the legacy
automations stay in control. Watch the per-room `sensor.<room>_role`,
`sensor.<room>_natural_lux`, `binary_sensor.<room>_overridden` for a day or two
and confirm they track reality. When satisfied, disable the legacy automations
(next section) and flip `enabled` on.

## 2. Automations to disable / retire

Disable (then delete once happy) these automations:

| # | automation (live id) | replaced by |
|---|---|---|
| 1 | `automation.kitchen_lighting_on` | kjøkken room, closed-loop + bands (§3/§4.5) |
| 2 | `automation.kitchen_lighting_off` | vacancy hold + evening accent (§1.4/§2.4) |
| 3 | `automation.kontor_lighting` | kontor room, `vacancy: off` (§1.4) |
| 4 | `automation.spisebord_lysstyring` | spisebord room, roles + TV mode (§1/§6.3) |
| 5 | `automation.sofakrok_lysstyring` | sofakrok room, roles + TV mode (§1/§6.3) |
| 6 | `automation.living_area_off_when_away` | away-off (§6.4) |
| 7 | `automation.gang_tv_dim` | gang corridor + TV mode restore (§6.3) |
| 8 | `automation.night_movement_on` | night path (§6.2) |
| 9 | `automation.night_movement_off` | night-path hold expiry (§6.2) |
| 10 | `automation.soverom_turn_on_light_when_door_opens_dynamic` (already disabled in HA) | door-triggered room (§1.9) |

Scripts to retire:

| script (live id) | note |
|---|---|
| `script.set_plejd_brightness` | superseded by the §8 write governor; its dead `transition` passthrough and kelvin-only-turns-off bug are fixed here |
| `script.set_benkebelysning_brightness` | superseded by banded allocation + calibrated curves (§4.5); the `0.8x − 50` map goes away |
| `script.night_path_lighting_on` | superseded by night path (§6.2); its output values are the defaults copied into the night-path profiles |

## 3. `input_boolean.night_movement`

The night-movement flag and its two automations move into the engine (D10).
After #8/#9 above are disabled, **delete `input_boolean.night_movement`**.
Note: grow-conductor keeps its *own* trigger input_boolean — do **not** delete
that one.

## 4. HomeKit exposure

Expose `light.light_conductor_master` to HomeKit as the whole-home dimmer
(50 % = neutral, 100 % = ×2, off = all managed indoor lights off). Suggested:
add it to your HomeKit bridge's include list and give it a room like "Whole
home". The per-room conductor entities are diagnostics — keep them off HomeKit.

## 5. Calibration (per lux-sensor room)

After a couple of nights, press `button.<room>_record_light_response` for each
room with a lux sensor (kjøkken, kontor, sofakrok, spisebord) — at night, lights
otherwise idle. Watch the room's `event.<room>_calibration` entity for a
`committed` result. Rooms stay usable (square-law default) until then.

## Appendix — housekeeping found during discovery (not blocking)

These pre-existed and are unrelated to the migration, but are worth fixing:

- `scene.tv_kveld` references the renamed `light.spisebord_lampe` (broken half)
  — TV mode (§6.3) subsumes it; delete the scene.
- The Metadata HomeKit bridge still exposes two deleted input_booleans
  (`homekit_leik_er_hjemme`, `homekit_scene_god_natt`) — remove from its config.
- `binary_sensor.sofakrok_audio_zone` references nonexistent kjøkken/spisebord
  audio-zone templates (stale since the sonos-conductor migration).
- The sofakrok/spisebord taklys **devices** sit in area `stue` — reassign so
  discovery/room grouping is clean.
- Soverom MTR-1 lux sensor is offline; soverom runs door-triggered without a
  lux sensor until it returns.
