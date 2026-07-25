# Discovery — live instance & legacy behavior (2026-07-25)

Reference snapshot backing DECISION.md. Live config fetched over SSH; entity
registry, statistics, and Plejd/pyplejd source reviewed the same day.

## Controlled lights

| entity | device model | area | capabilities | notes |
|---|---|---|---|---|
| light.gang_taklys | DIM-01-2P | gang | brightness | corridor, no sensing |
| light.kjokken_taklys | DIM-01-2P | kjokken | brightness | 2700 K plafond, primary |
| light.kjokken_benkebelysning | DIM-01-2P | kjokken | brightness | 2700 K, powerful → boost band |
| light.kjokken_downlights | 4× DWN-02 | kjokken | CT 2200–4000 K | accent band, evening survivor |
| light.kontor_taklys | DIM-01-2P | kontor | brightness | vacancy: off |
| light.sofakrok_taklys | DIM-01-2P | stue(!) | brightness | living group |
| light.spisebord_taklys | DIM-01-2P | stue(!) | brightness | living group |
| light.soverom_taklys | DIM-01-2P | soverom | brightness | door-triggered |
| light.balkong_taklys | 4× DWN-02 | balkong | CT 2200–4000 K | outdoor room, no automation today |

All Plejd outputs are dimmers (no relays). Wall hardware: ~5 WRT-01
rotary/button controllers (event entities, e.g. `event.gang_taklys_3`), one
WMS-01 PIR in kontor (`binary_sensor.kontor_pir_sensor`). Out of scope:
soverom garderobeskap (z2m IKEA driver), Apollo/UniFi RGB LEDs.

## Sensors & inputs

- Lux (LTR390, ~1 Hz deduped): kjøkken `sensor.apollo_msr_2_29abc4_ltr390_light`,
  kontor `..._f77c08_...`, sofakrok `..._f79794_...`, spisebord `..._fadea8_...`.
  Soverom MTR-1 offline, no LTS. 7-day hourly stats (min/max/avg, night avg):
  kjøkken 0/147/23 (night ~0), kontor 0/55/8 (night ~0), sofakrok 0/17/4.5
  (night 2.4 — TV/grow-light glow), spisebord 0/540/118 (night ~0.1).
  Spisebord's sensor sits close to its lamp: 68 lx at brightness 20/255.
- Presence: presence-conductor v0.5.3+ per-room
  `binary_sensor.presence_conductor_<room>_room_occupancy` / `_room_motion` /
  `_room_settled`, `sensor.presence_conductor_<room>_room_activity`,
  `event.<room>_room_pass_by`, global `binary_sensor.presence_conductor_anyone_home`;
  all four rooms calibrated (`ready`). Legacy template sensors
  `binary_sensor.{kjokken,spisebord,kontor,sofakrok}_occupancy` (raw radar
  zones) remain active — become fallbacks.
- `binary_sensor.household_sleep_mode` = person.leik home AND
  input_boolean.leik_sleep_mode. `binary_sensor.quiet_hours` (23:00–06:00 wd).
  `input_boolean.vacation_mode`. `zone.home` person count.
  TV: `media_player.sofakrok_tv` (LG webOS, unavailable when off),
  `media_player.sofakrok_apple_tv`.
- Sun: ~59.9° N — summer nights only ~6 h below horizon (clock term in the
  circadian model is essential; sun-only would never dim July evenings).

## Legacy automation semantics (what we replace)

- **Kitchen ON (edge-triggered only):** occupancy on + light off + not
  sleep. lux<60 & sun down → taklys+downlights 45 % @2700 + benke via
  `0.8x−50` map; sun up → `min(−0.74·lux + 103.7, 100)` only if > 62.
  No continuous adaptation after the edge.
- **Kitchen OFF:** vacancy 150 s or sunset; sun down leaves downlights 15 %
  @2300 K as evening accent, else all off.
- **Kontor:** ON at edge (night/sleep 1 %, sun down 18 %, day
  `100 − 0.5·lux` if > 10); OFF after 90 s vacancy. No away guard.
- **Spisebord/Sofakrok "Lysstyring" (mode restart, rich):** triggers on own
  occupancy (off-delay 120 s), TV state, neighbours (90 s), lux thresholds
  50/80 (30 s) — both rooms read the *sofakrok* sensor f79794 (spisebord's own
  sensor unusable for linear maps due to self-feedback) — and every sun
  change. Target ladder: TV modes (15/8/5 spisebord; 5/0 sofakrok) →
  occupied `base = clamp(100 − 0.5·lux, 0, evening?30:100)` → neighbour
  fractions/caps → standby `clamp(base·0.25, 2, 8)` when someone home &
  recently-active(15 min)|evening|base≥20 → else 0. Write gate |Δ| ≥ 4 %;
  transitions 1.2 s up / 5 s down / 6 s to off (all silently no-ops — see
  Plejd findings).
- **Living Area off** on zone.home=0 or sleep. **Gang TV dim** to 5 % with
  no restore path. **Night movement**: soverom door or living occupancy
  while sleep ⇒ input_boolean 10 min; path scene sofakrok 4 / gang 5 /
  spisebord 1 / downlights 20 % @2300. **Soverom door**: elevation-tiered
  100/70/40 %, off on close+15 s or 5 min.
- Scripts: `set_plejd_brightness` (pct→255, ≤0→turn_off, CT-then-brightness
  split, queued mode) — correct except transition passthrough is dead and a
  kelvin-only call turns the light off. `set_benkebelysning_brightness`
  (`clamp(0.8·base − 50, 0, 100)` via sort|median — correct).
  `night_path_lighting_on` per above.

## Plejd integration findings (hass_plejd 0.21.3 / pyplejd 0.21.3)

Single-gateway GATT bridge (highest-RSSI mains device; RSSI never decays,
BLEDevice handle never refreshed). Site metadata + crypto key from Plejd
cloud, cached. Commands: one encrypted write per command through one lock;
dim = cmd 0x0098, CT = 0x0420 TLV; ~30–100 ms per write (5–20 writes/s
ceiling). State: LASTDATA notifications are a *command echo* (optimistic);
true state poll only on 3-min ping. Reconnect does connect→disconnect→
sleep(5)→connect unconditionally (firmware throttling workaround), holding
the write lock ≥5–7 s.

Defects relevant to us: no transition support (`LightEntityFeature.TRANSITION`
never declared — HA strips the arg); writes silently dropped when
disconnected (no error, no retry); `connected` is `client is not None`
(stale-link blindness — matches upstream #162/#125/#147); all-or-nothing
availability flaps every entity; CT state can go permanently stale (not in
lightlevel poll); Plejd's native dim-speed/dim-curve device settings
(dimSpeed, dimMin/Max, dimCurve) exist in cloud data but are read-only
mirrors — the app's settings-write protocol is not reverse-engineered, so
"native fade" means software ramps. Group/room mesh addresses are parsed
upstream but no group-write API is exposed (one-write-per-room is feasible).

Consequences adopted: ENGINE_SPEC §8 (lossy actuator discipline), §5.4
(CT before brightness), D4 (fork strategy).

## Housekeeping found (fix during migration, needs user)

- `scene.tv_kveld` references renamed `light.spisebord_lampe` (broken half).
- Metadata HomeKit bridge exposes two deleted input_booleans
  (`homekit_leik_er_hjemme`, `homekit_scene_god_natt`).
- `binary_sensor.sofakrok_audio_zone` references nonexistent kjokken/spisebord
  audio-zone templates (stale since sonos-conductor migration).
- sofakrok/spisebord taklys devices sit in area `stue`.
- Soverom MTR-1 offline since last HA restart.
- `automation.soverom_turn_on_light_when_door_opens_dynamic` is currently
  disabled in HA (semantics preserved in spec 1.9 regardless).
