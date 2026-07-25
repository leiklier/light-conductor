# Light Conductor — Engine Specification

Normative contract for the pure core (`custom_components/light_conductor/core/`).
Numbered rules are binding; tests cite them. The core imports nothing from Home
Assistant (CI-enforced). The adapter (controller + entities) translates HA state
into engine inputs and engine plans into service calls.

Sibling specs: sonos-conductor (audio zones), presence-conductor (occupancy
estimation), grow-conductor (photoperiod). This engine is a *consumer* of
presence-conductor outputs.

## 0. Vocabulary & principles

- **Room** — a controlled space with one or more light channels, optional lux
  sensor, presence inputs, and a profile. Opaque ids; no hardcoded room names.
- **Channel** — one HA `light` entity in a room, with static capabilities
  (dimmable; CT-adjustable with a kelvin range, or fixed CT declared in config,
  e.g. 2700 K) and a photometric model (§4).
- **Role** — the activity classification the FSM assigns a room (§1). Roles
  select an illuminance tier; they never map directly to brightness.
- **Target illuminance `T`** — desired total lux at the room's sensor position.
  All reasoning happens in lux where a sensor exists; brightness is derived.
- **Natural light `N̂`** — estimated lux at the sensor with all controlled
  channels off (§3).
- **Artificial estimate `Â`** — predicted lux contribution of current channel
  outputs, from the photometric model (§4).
- **Single writer** — when enabled, the engine is the only writer to configured
  channels. Every command goes through one funnel (§8) which applies gain, CT
  policy, slew, quantization, and the echo ledger. There is no second path.
- **Fail-safe direction** — uncertainty resolves toward *lights usable, not
  blinding*: unknown presence ⇒ treat per §1.8/§6; unknown lux ⇒ open-loop
  tables (§4.6); unknown home ⇒ home (§6.4).
- **Determinism** — the engine is a pure function of (state, event, now).
  Time enters only through event timestamps and scheduled reviews.

## 1. Room activity FSM

1.1 **Inputs per room:** primary presence entity (presence-conductor room
occupancy), optional fallback occupancy entities (OR-ed with primary, used
alone when the primary is unavailable — blind ≠ absent: an unavailable primary
holds its last definitive value for `presence_blind_hold` before falling back),
optional activity sensor (presence-conductor `room_activity`:
empty/passing/active/settled), optional trigger entities (momentary, e.g. a
door sensor), and the adjacency list (other room ids configured as neighbours).

1.2 **Roles.** Each room is in exactly one role:

| role | meaning |
|---|---|
| `ACTIVE` | room occupied |
| `ADJACENT` | not occupied, but a configured neighbour is ACTIVE |
| `BACKGROUND` | not occupied, no active neighbour, but the house is awake and the living area is in use |
| `OFF` | no light warranted |
| `NIGHT_PATH` | night-movement episode (§6.2) |
| `TV` | room participates in TV mode (§6.3) |

Role priority when multiple qualify: NIGHT_PATH > TV > ACTIVE > ADJACENT >
BACKGROUND > OFF.

1.3 **Hold times.** Occupancy loss starts a vacancy hold of
`hold_seconds[room]` scaled by the activity episode peak (as in
sonos-conductor 1.7): `passing` ⇒ ×`hold_passing_scale`, `settled` ⇒
×`hold_settled_scale`. Only after the hold expires does the room leave ACTIVE.
Re-occupancy during the hold returns to ACTIVE with no visible change.

1.4 **Demotion is gradual, not a cliff.** On leaving ACTIVE the room demotes to
the highest currently-qualifying role (ADJACENT/BACKGROUND/OFF). Each demotion
step changes the illuminance tier (§2) and is executed with the room's
`fade_down` ramp. A room whose profile sets `vacancy: dim` (living-area rooms)
never demotes below BACKGROUND while `living_recently_active` (1.6) holds; a
room with `vacancy: off` (kontor) demotes straight to OFF after its hold.

1.5 **ADJACENT tier scaling.** An ADJACENT room's target is
`adjacent_fraction` of its would-be ACTIVE target (default 0.5), clamped to
`adjacent_cap` lux. This replaces the legacy per-neighbour brightness ladders.

1.6 **`living_recently_active`** — true while any room in the configured
"living group" is ACTIVE or left ACTIVE less than `living_memory` (default
900 s) ago. Gates BACKGROUND (mirrors the legacy standby rule).

1.7 **Corridor rooms** (no presence input, e.g. gang): role is derived —
ADJACENT when any neighbour is ACTIVE, BACKGROUND per 1.6 during evening
(§2.3), else OFF. Trigger entities (1.1) pulse a corridor to ACTIVE for
`trigger_hold` seconds.

1.8 **Unknown presence.** Primary and fallbacks all unavailable ⇒ the room
freezes its current role for `presence_blind_hold`, then demotes one step per
`presence_blind_hold` interval (never straight to OFF from ACTIVE).

1.9 **Door-triggered rooms** (soverom): trigger entity opening ⇒ ACTIVE for
`trigger_hold` (restartable); closing edge ⇒ shortened hold `door_close_hold`.
Sleep mode overrides per §6.1.

## 2. Illuminance targets & circadian shaping

2.1 **Tiered targets.** Each room profile defines target lux per role tier:
`lux_active_day`, `lux_active_evening`, `lux_background`. ACTIVE interpolates
between day and evening values by the circadian factor (2.3); ADJACENT and
BACKGROUND derive from the interpolated ACTIVE target (1.5, and
`background_fraction` with `background_cap`). OFF ⇒ 0.

2.2 **Rooms without a lux sensor** use the same tier machinery but express
targets directly as normalized channel output (open-loop tables, §4.6).

2.3 **Circadian factor `E ∈ [0,1]`** (0 = full day, 1 = full evening):
`E = max(E_sun, E_clock)` where `E_sun` ramps 0→1 as sun elevation falls
from `sun_high_deg` (default +10°) to `sun_low_deg` (default −4°), and
`E_clock` ramps 0→1 between `evening_start` (default 20:00) and
`evening_full` (default 22:30) local time, and back to 0 at `morning_start`
(default 06:00) → `morning_full` (07:30). The clock term guarantees a cozy
ramp-down toward bedtime even in Nordic summer when the sun sets late; the
sun term darkens winter afternoons. No hard steps: E is continuous and
evaluated on a coarse schedule (≤ 1 change per `circadian_tick`, default
300 s) so it never causes visible jumps by itself.

2.4 **Evening cap.** In addition to interpolation, `E ≥ evening_cap_threshold`
imposes the profile's `evening_output_cap` on normalized channel output
(legacy 30 %-after-18:00 semantics, now smooth). The cap applies in the §8
funnel so every path (including manual-nudge reconciliation) honors it.

2.5 **Targets are what the master gain scales** (§7): `T' = T × G` before
allocation, clamped to the room's `lux_max`.

## 3. Natural-light estimator

The lux sensor measures natural + our own artificial light. The estimator
separates them so the controller regulates *natural shortfall* instead of
chasing its own output.

3.1 **Model.** `L = N + Σ_i g_i·f_i(b_i) + ε` per room: `L` measured lux,
`b_i` commanded normalized output of channel i, `f_i` the channel's relative
flux curve (§4.2), `g_i` the calibrated lux gain at the sensor.

3.2 **Estimate.** `N̂ = clamp(L_filt − Â, 0, ∞)` where `L_filt` is the
measured lux after (a) a **write blanking window**: samples arriving within
`write_blank` (default 5 s) after any own channel command in the room are
excluded, and (b) an asymmetric low-pass (rise `tau_lux_up` 30 s, fall
`tau_lux_down` 60 s) — clouds are minutes, not seconds.

3.3 **Night prior.** While sun elevation < `night_prior_deg` (default −6°),
N̂ relaxes toward 0 with `tau_night_prior` (600 s). Street lighting etc. can
hold small values; the prior only pulls, never clamps.

3.4 **Online gain refinement.** When the engine commands a step and the room
is otherwise quiet (no other command within the settle window, sensor fresh),
the observed `ΔL` across the step updates a per-room scalar gain multiplier
via EMA (`gain_learn_rate`, default 0.1), bounded to [0.5, 2.0] of the
calibrated gains. Per-channel calibration only changes via §4.4 recalibration.

3.5 **Sensor trust.** Lux sensor unavailable/stale (> `lux_stale` 120 s) ⇒
room falls back to open-loop tables (§4.6) at the same role tier; recovery is
seamless because both paths share the §8 funnel.

3.6 **Anti-hunting invariant.** The closed loop may not oscillate: control
error uses a **deadband** — no action while `|T' − (N̂ + Â)| <
max(deadband_abs, deadband_rel × T')` (defaults 5 lx, 0.15) — plus
**sustain**: the error must persist for `error_sustain` (default 20 s;
shortened to `error_sustain_fast` 2 s for role changes and mode edges).
Corrections command the *model-predicted* output for the new target (feed
forward), not an incremental nudge, so one write lands near the goal and the
loop settles in ≤ 2 corrections.

## 4. Channels, photometry & allocation

4.1 **Channel config:** entity id, `fixed_ct` kelvin for non-CT channels
(default 2700), optional CT range override, `dim_floor` (minimum useful
normalized output; below it the channel is turned off), allocation `band`
(§4.5), and `curve` (§4.2).

4.2 **Relative flux curve `f(b)`.** Phase dimmers are not linear in light
output. Each channel has a piecewise-linear curve mapping normalized command
`b` → relative flux, defaulting to `b²` (square-law approximation) until
calibrated. Calibration (§4.4) replaces the default with measured points.

4.3 **Perceptual steps.** All ramps and slew limits operate in flux-relative
space so a "small step" looks small at any level.

4.4 **Room calibration routine** (`RecordLightResponse` button per room with a
lux sensor): only runs when sun elevation < `night_prior_deg` and the room's
lux is stable. Sweeps each channel alone: off → each of
`calibration_levels` (default 10/25/50/75/100 %), dwell `calibration_dwell`
(4 s) per level, recording lux. Produces `g_i` and `f_i` points per channel;
commits transactionally (all-or-nothing, like presence-conductor 3.3), fires
a result event, and marks the room `calibrated`. Uncalibrated rooms run
closed-loop with the default curve and a conservative gain estimated from the
first night the lights run (bounded influence via 3.4).

4.5 **Allocation bands.** A room's channels are ordered into bands:
`accent` (fills first, e.g. kjøkken downlights), `primary` (main body, e.g.
taklys), `boost` (only at high demand, e.g. benkebelysning). Demand `D` (lux
to produce) fills bands in order; each band saturates before the next engages,
with `band_overlap` (default 0.15) cross-fade so engagement is never a pop —
this replaces the legacy `base > 62` benkebelysning step and the
`0.8x − 50` mapping. Within a band, channels share proportionally to their
calibrated gains. A `boost` band additionally requires `E < boost_evening_max`
(benkebelysning stays off in the evening, matching legacy kitchen-off
behavior where only the accent band survives sunset).

4.6 **Open-loop tables.** Without a usable lux sensor, role tiers map to
normalized outputs per band: `out_active_day`, `out_active_evening`,
`out_background` (profile), interpolated by E, scaled by master gain, floored
by `dim_floor`.

## 5. Color temperature policy

5.1 CT-capable channels track `ct_target = ct_day − E × (ct_day − ct_evening)`
(defaults 3300 K → 2400 K), clamped to hardware range.

5.2 **Blend anchoring.** In a room that mixes CT channels with fixed-CT
channels, whenever a fixed channel is at ≥ `blend_threshold` of its output the
CT target is clamped to within `blend_delta` (default 300 K) of the fixed
channels' declared kelvin — mixed sources must read as one scene, not two.
When fixed channels are off (evening accent), CT may go fully warm.

5.3 **Low-output warmth.** ct_target is additionally capped by output level:
below `warm_dim_output` (default 0.3 normalized) the cap slides toward
`ct_min_evening` (2200 K) — dim light is always warm ("dim-to-warm").

5.4 CT writes go through the funnel ordered **CT before brightness** (the
DWN-02 OUTPUT_SET can clobber a just-set level; brightness must be the last
mesh write). CT is only rewritten when it moves ≥ `ct_min_delta` (100 K).

## 6. Modes

6.1 **Sleep.** `sleep_entity` on ⇒ all rooms OFF (fade `sleep_fade`), and the
FSM ignores presence except night movement (6.2). Sleep turning off restores
normal evaluation (morning ramp per §2.3).

6.2 **Night path.** While sleep is on, a night trigger (any configured
`night_trigger` entity: bedroom door opening, or presence/pass-by in a living
room) opens a NIGHT_PATH episode: all rooms in the configured `night_path`
set light at their profile's `night_output` (fixed dim warm values, CT forced
to `ct_min_evening`), everything else stays OFF. The episode holds for
`night_hold` (default 600 s) after the last trigger, restartable, then fades
out over `night_fade` (10 s). Master gain does not scale night path (§7.4).

6.3 **TV mode.** `tv_entities` playing (any) ⇒ rooms with a `tv_output`
profile entry switch to role TV at that output (spisebord 15 %→occupied /
5 %→empty semantics become: `tv_output` when ACTIVE, `tv_output_empty`
otherwise; sofakrok keeps a low glow when occupied, 0 when not; gang dims).
TV ending re-evaluates roles — rooms *restore* (the legacy gang light that
stayed dimmed forever now recovers).

6.4 **Away.** `anyone_home is False` ⇒ every room OFF, including balkong
(tunable `balkong_when_away`, default off). `None`/unavailable fails safe as
home (consistent with sonos-conductor 1.8). Arrival re-evaluates immediately;
no flash: normal fades apply.

6.5 **Balkong (outdoor room).** A room may be flagged `outdoor`. It ignores
presence and runs: ON at `E ≥ outdoor_on_threshold` (dusk) at
`out_background` warm CT; OFF when sleep turns on or away per 6.4. Its
`occupational` switch (exposed entity, §10) raises it to `out_active_evening`
at a slightly cooler CT while on — "sitting outside" vs "ambient backdrop".

6.6 **Vacation.** If `vacation_entity` is configured and on, away rules apply
regardless of presence, except an optional simple presence-simulation is out
of scope for v1 (roadmap).

## 7. Master gain

7.1 Exposed as a dimmable `light` entity (HomeKit dimmer). Brightness maps to
gain `G = 2^((pct − 50)/50·gain_range_stops)` with `gain_range_stops` default
1.0 — i.e. 50 % ⇒ ×1.0 (neutral), 100 % ⇒ ×2, 0⁺ % ⇒ ×0.5. Applied to lux
targets (2.5) and open-loop outputs (4.6).

7.2 Master light **off** ⇒ gain 0: all conductor-managed indoor rooms fade
OFF and stay off; turning it back on restores the previous gain. (Balkong
background and night path are exempt.)

7.3 **Neutral drift.** The gain relaxes back to neutral on the morning edge
(sleep off, or `morning_full`), tunable `gain_reset` (on/off, default on) —
a "dimmer tonight" nudge should not permanently darken the house.

7.4 Night path (6.2) and away/sleep OFF states are not scaled by gain —
safety floors and hard-offs are absolute.

## 8. Write governor (actuator discipline)

8.1 **Single funnel.** Every channel command passes: role/mode target →
master gain → caps (2.4) → allocation (4.5) → CT policy (§5) → slew limiter →
quantizer → dim floor → ledger. No other code path writes to lights.

8.2 **Slew limiting.** Output moves toward its goal in steps ≤
`slew_step` flux-relative per `slew_interval` (defaults 0.1 / 1.0 s) while
the room is ACTIVE (occupied eyes present); role demotions and empty rooms may
step `slew_step_empty` (0.25). Transitions the actuator can't do natively are
software ramps emitted by the engine as timed step plans.

8.3 **Write economy.** A channel is commanded only when the quantized goal
differs from the ledger's last commanded value by ≥ `min_delta` (flux-relative
0.03) or crosses on/off. Rate limit ≥ `min_write_interval` (1.0 s) per
channel, latest-value-wins coalescing. Site-wide concurrent command cap
`max_inflight` (default 3) respects the single-gateway BLE bottleneck.

8.4 **Echo ledger.** Every command is recorded (channel, value, timestamp).
Incoming state reports matching a recent command (± `echo_tolerance`, within
`echo_window` 10 s) are consumed as echoes; everything else is a *foreign
change* (§9). Mirrors sonos-conductor's controller ledger.

8.5 **Availability.** A channel (or the whole Plejd gateway) unavailable ⇒
skip its writes this cycle and re-reconcile on recovery — never queue against
a dead link; never mark the room failed. State divergence found at recovery
(the integration reconciles true state every ~3 min) is corrected quietly
with `slew_step_empty` ramps.

8.6 **Off is off.** Goal 0 ⇒ `turn_off` (never brightness 0), after ramping
down to the dim floor when the room was lit.

## 9. Manual override & reconciliation

9.1 **Foreign changes latch an override.** A non-echo state change on a
channel (wall rotary, HomeKit direct, voice) sets the *room* to
`OVERRIDDEN`: the engine adopts the observed levels as the room's goal and
stops adjusting everything except mode hard-offs (sleep/away still win; night
path suspends the override).

9.2 **Override release.** The override clears on: room OFF-worthy vacancy
(hold expiry at OFF tier), sleep on, away, master gain off/on cycle, or
`override_timeout` (default 4 h). Release re-enters normal control with slew
ramps (no jumps).

9.3 **Manual-on respect.** A room turned on manually while the FSM wanted OFF
is an override (9.1) — the legacy "only auto-on if the light is currently
off" behavior falls out of this rule.

9.4 **Plejd wall-event awareness.** Configured `wall_event` entities
(WRT-01/WPH-01 `event.*`) count as foreign changes for their room even if the
resulting state lands inside echo tolerance.

## 10. Entities (adapter contract)

- `light.light_conductor_master` — master gain dimmer (§7), HomeKit-ready.
- `switch.light_conductor_enabled` — master enable; off = observe only
  (no commands; ledger and estimator keep running).
- `switch.light_conductor_<room>_occupational` — only for outdoor rooms (6.5).
- Per room diagnostics: `sensor.<room>_role` (enum), `sensor.<room>_natural_lux`
  (measurement, publish-gated: 5-point buckets + ≥ 10 s interval — recorder
  discipline per presence-conductor lesson), `sensor.<room>_target_lux`,
  `binary_sensor.<room>_overridden`.
- `button.<room>_record_light_response` (4.4) + calibration result event
  entity.
- All volatile values live in engine state / diagnostics platform, never in
  recorded attributes. A recorder-discipline sweep test is mandatory.

## 11. Seeding & startup

11.1 On start the engine seeds from current entity states: existing light
levels are adopted as ledger baselines (no startup flash, mirroring
grow-conductor's restore-before-enforce), roles evaluate from live presence,
and rooms whose lights differ grossly from the computed goal converge with
`slew_step_empty` ramps only after `startup_grace` (30 s).

11.2 Restorable entities: master gain, enabled, occupational switches,
override latches (not restored — cleared on restart).

## 12. Tunables (defaults)

| name | default | rule |
|---|---|---|
| hold_seconds (per room) | 120 s (kontor 90) | 1.3 |
| hold_passing_scale / hold_settled_scale | 0.3 / 4.0 | 1.3 |
| adjacent_fraction / adjacent_cap | 0.5 / 30 lx | 1.5 |
| background_fraction / background_cap | 0.25 / 15 lx | 2.1 |
| living_memory | 900 s | 1.6 |
| trigger_hold / door_close_hold | 300 s / 15 s | 1.7, 1.9 |
| presence_blind_hold | 120 s | 1.1, 1.8 |
| sun_high_deg / sun_low_deg | +10° / −4° | 2.3 |
| evening_start / evening_full | 20:00 / 22:30 | 2.3 |
| morning_start / morning_full | 06:00 / 07:30 | 2.3 |
| circadian_tick | 300 s | 2.3 |
| evening_output_cap | profile (0.3 living) | 2.4 |
| write_blank | 5 s | 3.2 |
| tau_lux_up / tau_lux_down | 30 s / 60 s | 3.2 |
| night_prior_deg / tau_night_prior | −6° / 600 s | 3.3 |
| gain_learn_rate | 0.1 | 3.4 |
| lux_stale | 120 s | 3.5 |
| deadband_abs / deadband_rel | 5 lx / 0.15 | 3.6 |
| error_sustain / error_sustain_fast | 20 s / 2 s | 3.6 |
| calibration_levels / calibration_dwell | 10,25,50,75,100 % / 4 s | 4.4 |
| band_overlap / boost_evening_max | 0.15 / 0.5 | 4.5 |
| ct_day / ct_evening / ct_min_evening | 3300 / 2400 / 2200 K | 5.1, 5.3 |
| blend_threshold / blend_delta | 0.1 / 300 K | 5.2 |
| warm_dim_output | 0.3 | 5.3 |
| ct_min_delta | 100 K | 5.4 |
| sleep_fade / night_hold / night_fade | 4 s / 600 s / 10 s | 6.1, 6.2 |
| outdoor_on_threshold | 0.7 | 6.5 |
| balkong_when_away | off | 6.4 |
| gain_range_stops / gain_reset | 1.0 / on | 7.1, 7.3 |
| slew_step / slew_interval / slew_step_empty | 0.1 / 1.0 s / 0.25 | 8.2 |
| min_delta / min_write_interval / max_inflight | 0.03 / 1.0 s / 3 | 8.3 |
| echo_window | 10 s | 8.4 |
| override_timeout | 4 h | 9.2 |
| startup_grace | 30 s | 11.1 |

## 13. Roadmap (non-normative)

- Plejd fork (`hass_plejd`/`pyplejd`): surface write failures, liveness check
  (`is_connected`), BLEDevice refresh, opt-in double-connect, software
  transitions below HA, group-address writes (one mesh write per room).
  Until then §8 assumes lossy fire-and-forget writes and self-corrects.
- Presence simulation during vacation (6.6).
- Soverom lux once the MTR-1 is back online.
- Rotary-delta decoding from WRT-01 for relative master-gain control.
