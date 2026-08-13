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
| `TV` | a TV is playing and the room joins TV mode (§6.3) |

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
Sleep mode overrides per §6.1. A per-room **door-lighting** switch (§10,
restorable, default on) gates trigger *ingestion* in the engine: while it is
off, no pulse — opening or closing — mints a hold, so the room simply has no
trigger input. Its falling edge clears any live `trigger_hold` at once and the
room re-resolves in that same recompute, leaving down the normal demotion/fade
path (exactly what hold expiry would have done). Its rising edge is not
retroactive — the next door edge behaves normally. The gate changes nothing
else: sleep/away/night-path/master still win as before (§6.1/6.2/6.4/§7), and a
manual override in the room keeps its §9.2 blind-room protection (the toggle is
not a release condition).

1.10 **Occupational presence** (outdoor rooms). An outdoor room's occupational
switch is a *declaration* of presence: while on AND the room's own dusk ramp is
at least `outdoor_presence_factor` deep (§6.5a — ungated, a switch left on would
light the interior in full daylight; with no lux sensor the ramp is binary and
this is exactly `E >= outdoor_on_threshold`), the room is self-active — its
neighbours qualify for ADJACENT and, when flagged `living_group`, it keeps
`living_recently_active` alive (§1.6) so the interior does not go dark around
an occupant the sensors cannot see (the balcony-sitting incident). The falling
edge stamps the normal `living_memory` decay. The switch need not be flipped
from HomeKit: a manual light action on the room makes the same declaration
(§6.5b). Away/sleep hard-offs (§6.1/§6.4)
still win, and §6.5's away-mode handling of the switch is unchanged.

## 2. Illuminance targets & circadian shaping

2.1 **Tiered targets.** Each room profile defines target lux per role tier:
`lux_active_day`, `lux_active_evening`, `lux_background`. ACTIVE interpolates
between day and evening values by the circadian factor (2.3); ADJACENT and
BACKGROUND derive from the interpolated ACTIVE target (1.5, and
`background_fraction` with `background_cap`). An explicit nonzero
`lux_background` acts as a floor under the derived BACKGROUND target. OFF ⇒ 0.

A tier value of **0 means UNSET**, not "0 lx". An unset tier on a
closed-loop room falls back to a fraction of that room's calibrated capacity
`C = Σ_i g_i · f_i(1)` (the sum of the channels' calibrated lux gains at full
output, scaled by the online gain multiplier so a bootstrap-confident room
uses its learned scale): `lux_active_day → lux_day_frac · C`,
`lux_active_evening → lux_evening_frac · C`, `lux_background →
lux_background_frac · C`. This keeps a freshly calibrated room targeting real
light instead of 0 lx (which would leave it dark). An explicit nonzero tier
always wins. Uncalibrated/untrusted rooms never reach the closed loop — they
run the daylight-scaled open-loop tables (§4.6/§4.7), where capacity is
meaningless before calibration — so the fallback never applies to them.

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
300 s) so it never causes visible jumps by itself. The engine self-schedules
its own re-evaluations: while E is inside a ramp it reviews every
`circadian_tick`, and from a plateau (E = 0 or 1) it schedules the next
clock-ramp boundary (the next local `evening_start`/`morning_start`) so it can
start the 20:00 and 06:00 clock ramps unprompted. Entry into the *sun* ramp
additionally relies on the adapter's `SunElevationChanged` events (push cadence
~1–2 min); the clock term needs no external cadence.

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
flux curve (§4.2), `g_i` the calibrated lux gain at the sensor. `g_i` is
strictly an observation model: sensors sit in idiosyncratic spots (under a
speaker, behind a curtain, next to one particular lamp), so per-channel gains
in the same room can differ by orders of magnitude and say nothing about a
channel's contribution to the room as experienced — they are never used for
aesthetic allocation (§4.5). Target tiers `T` are therefore sensor-relative
per room, tuned per profile, and never comparable across rooms.

3.2 **Estimate.** `N̂ = clamp(L_filt − Â, 0, ∞)` where `L_filt` is the
measured lux after (a) a **write blanking window**: samples arriving within
`write_blank` (default 5 s) after any own channel command in the room are
excluded, and (b) an asymmetric low-pass (rise `tau_lux_up` 30 s, fall
`tau_lux_down` 60 s) — clouds are minutes, not seconds. `Â` in this residual
is passed through the *same* low-pass as `L` (both lag together), so the
residual stays consistent through an own-command transient instead of spiking
when `Â` jumps ahead of the still-settling measurement.

3.3 **Night prior.** While sun elevation < `night_prior_deg` (default −6°),
N̂ relaxes toward 0 with `tau_night_prior` (600 s). Street lighting etc. can
hold small values; the prior only pulls, never clamps.

3.4 **Online gain refinement.** When the engine commands a step and the room
is otherwise quiet (no other command within the settle window, sensor fresh),
the observed `ΔL` across the step updates a per-room scalar gain multiplier
via EMA (`gain_learn_rate`, default 0.1), bounded to [0.5, 2.0] of the
calibrated gains. Per-channel calibration only changes via §4.4 recalibration.

3.5 **Sensor trust.** Lux sensor unavailable/stale (> `lux_stale` 300 s) ⇒
room falls back to open-loop tables (§4.6) at the same role tier; recovery is
seamless because both paths share the §8 funnel. **Closed-loop control also
requires a trustworthy gain model**: a room enters closed-loop only when it is
`calibrated` (§4.4) *or* has completed the first-night bootstrap; otherwise it
runs open-loop (safe by construction — an unknown gain cannot drive it dark or
into hunting). **Closed-loop also requires sufficient capacity** — see the
capacity gate (§4.5): a trustworthy but low-capacity room (`C <
min_closed_loop_capacity`) falls back to the SAME daylight-aware open-loop path
(§4.7) an uncalibrated room uses. **First-night bootstrap:** while an uncalibrated lux-sensor room
runs open-loop, the estimator observes each own commanded step in *shadow* —
armed on the **observed** `ΔL ≥ deadband_abs` (not the model-predicted delta,
which is structurally tiny when the gain is under-modelled) — and records the
room-level ratio `ΔL / Δflux`. After `bootstrap_min_obs` (default 3) such
observations it commits a room-scalar gain `= median(ratios) × bootstrap_margin`
(default 1.5) over the default `b²` curves and flips to closed-loop. The margin
deliberately **over**-models the gain: an over-modelled gain gives loop gain
< 1 (a stable undershoot that converges), whereas under-modelling gives loop
gain > 1 (the hunting regime) — so "conservative" means erring high. The
bootstrap gain is per-run (not persisted); a restart re-learns it.
**Dispersion sanity (arm guard).** Before committing, the collected ratios must
*agree*: with `m = median(ratios)`, require `max(ratios) ≤
bootstrap_dispersion_max × m` **and** `min(ratios) ≥ m / bootstrap_dispersion_max`
(default 3.0). Genuine own-light observations cluster tightly; an
ambient-contaminated set — morning clouds swinging daylight while the room's
lights happen to toggle — scatters wildly (the kjøkken false-promotion incident,
live ratios `[8.73, 95.31, 14.21]`, promoted a deliberately open-loop room into
closed loop chasing an unreachable target). A failing set is **dropped** (the
collected ratios are cleared, not accumulated) so a later quiet period can
bootstrap cleanly. **Wedge notice.** A configured lux sensor whose entity stays
AVAILABLE but produces no state update for `lux_wedge_warn` (default 1800 s)
raises an HA repairs issue (one per sensor) suggesting its ESP reboot button (a
known LTR390 quirk); it clears automatically when reports resume. When a restart
button (`ButtonDeviceClass.RESTART`) exists on the sensor's own device the issue
is **fixable** — its Fix button presses that reboot button and a short grace
window suppresses an immediate re-raise while the sensor boots; otherwise the
issue stays non-fixable with the manual instruction. Ordinary unavailability
(§8.5) is not a wedge and never raises it.

3.6 **Anti-hunting invariant.** The closed loop may not oscillate: control
error uses a **deadband** — no action while `|T' − (N̂ + Â)| < deadband`, where

```
deadband = max(min(deadband_abs, deadband_capacity_frac × C), deadband_floor, deadband_rel × T')
```

(defaults `deadband_abs` 5 lx, `deadband_capacity_frac` 0.2, `deadband_floor`
0.5 lx, `deadband_rel` 0.15; `C` is the room capacity of §4.5). The absolute
component is itself **capacity-scaled**: capped at a fraction of the room's own
capacity so a low-capacity room (e.g. sofakrok, `C ≈ 8.8 lx` ⇒ effective abs
`= min(5, 1.76) = 1.76 lx`) can reach targets that sit at or below the fixed
5-lx floor — its auto day/evening tiers (`0.6·C ≈ 5.3` / `0.2·C ≈ 1.8`) are
otherwise unreachable and the ACTIVE role never lights the room (the live
incident). It is floored at `deadband_floor` (sensor-noise floor) so it never
collapses to zero, and a high-capacity room (`C ≥ 25`) is unchanged (the `min`
picks `deadband_abs`, and `deadband_rel × T'` still dominates for large
targets). This scaling affects **control** only — the bootstrap/gain-EMA arming
thresholds (§3.4/§3.5) still gate on the fixed `deadband_abs`, so a sub-delta
room deliberately never arms bootstrap. Plus **sustain**: the error must
persist for `error_sustain` (default 20 s;
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
`calibration_levels` (default 10/25/50/75/100 %), recording lux. **Early
advance:** a level completes as soon as `CAL_MIN_SAMPLES_PER_LEVEL` (1)
post-blank samples have settled (a fast sensor sweeps quickly), otherwise at
`calibration_dwell` (4 s) — a slow, delta-filtered sensor still gets the full
window. A level whose window elapses with no sample is simply skipped.
**Partial coverage:** a channel commits its measured `g_i`/`f_i` from the
sampled points once at least `CAL_MIN_POINTS` (3) of its levels were captured;
below the lowest sampled level the curve is extrapolated with a square-law
shape anchored at (0, 0) (the committed relative flux scaled as `b²` to meet
the lowest sampled point), so a low-gain channel whose dim levels never clear
the sensor's on-device delta filter still calibrates from its bright levels.
Coverage is reported per channel (fraction of levels sampled). The room
commits transactionally (all-or-nothing, like presence-conductor 3.3) only if
**every** channel reached `CAL_MIN_POINTS`; otherwise it rejects with
`missing_samples` and the per-channel coverage map. A successful commit fires
a result event and marks the room `calibrated`. Uncalibrated rooms run
open-loop until the first-night bootstrap (§3.5) has learned a conservative
room-scalar gain over the default curve, then enter closed-loop with bounded
influence; a full sweep later replaces that estimate with per-channel `g_i`/`f_i`.
The sweep commands each channel at its exact prescribed dwell level directly
(single writer for the room), **bypassing** both the §8 slew governor and the
§4.5 response mapping — it must measure the fixture's *true* lux response, so a
response-mapped channel (e.g. a benke curve of slope 0.8/offset −0.5, which
would map every level ≤ 0.625 to 0) is still swept at the raw prescribed levels
and never falsely rejected as `dark_channel`.

4.5 **Allocation bands.** A room's channels are ordered into bands:
`accent` (fills first, e.g. kjøkken downlights), `primary` (main body, e.g.
taklys), `boost` (only at high demand, e.g. benkebelysning). Demand `D` (lux
to produce) fills bands in order; each band saturates before the next engages,
with `band_overlap` (default 0.15) cross-fade so engagement is never a pop —
this replaces the legacy `base > 62` benkebelysning step and the
`0.8x − 50` mapping. Within a band, channels share according to their
configured `weight` (default equal) — **never** their calibrated sensor
gains: gain is an observation model (§3.1), a function of where the sensor
happens to sit, not of how much a channel lights the room (e.g. the kjøkken
sensor sits next to benkebelysning; its gain dwarfs the taklys gain without
the light being aesthetically dominant). A `boost` band additionally requires `E < boost_evening_max`
(benkebelysning stays off in the evening, matching legacy kitchen-off
behavior where only the accent band survives sunset).

**Per-channel response mapping.** After weight sharing and the boost evening
lockout, each channel applies an affine RESPONSE MAPPING to its post-weight
band output `out`: the emitted command is `clamp(response_slope · out +
response_offset, 0, 1)`. This aligns fixtures whose physical dimming curves
differ — a steep LED strip (kjøkken `benkebelysning`) versus flat spots — so a
lone-channel band, whose weight normalizes away, no longer blasts over its
neighbours at full demand. A **zero band output always stays 0**: the mapping
runs only when `out > 0`, so a positive offset can never light a channel whose
band is off, and it can never resurrect the evening-locked boost band. The
defaults (`response_slope` 1.0, `response_offset` 0.0) are an exact
byte-identical no-op for every existing config. The mapping is the **LAST step**
of open-loop channel mapping — after tier selection, circadian interpolation,
the daylight factor `D` (§4.7), the evening cap (§2.4), master gain (§7), weight
share, and the boost evening lockout — matching the legacy semantics, where the
kitchen's `0.8·base − 50` applied to the *final* base %. Master gain multiplies
the band outputs (`gain.scale`) *before* `allocate()` in the §8 funnel, so the
mapping sees the already-gained output. Because both open-loop consumers of
`allocate()` — the mode-resolution table path (night/TV/outdoor/off) and the
tier/daylight open-loop path — flow every band output through this one function
before the §8 governor, the mapping automatically covers the ACTIVE/ADJACENT/
BACKGROUND tiers and the night-path and TV mode tables alike. The **closed-loop**
path (§3.6/§4.5) is deliberately untouched: there the calibrated lux curves own
the physical response, so no response mapping applies (ADR D16). The estimator
stays consistent for free — `Â`/bootstrap/`record_step` read the channel's
actual `commanded_b` (the mapped value the governor wrote to the ledger) and
`f_i` maps `commanded_b` → flux, so the mapping changes *what* is commanded, not
how observations are interpreted; nothing recomputes an expected level from the
band outputs while bypassing the mapping, so the echo ledger and the gain
observation see the mapped value as the target end to end.

**Room capacity `C`** is `Σ_i g_i · f_i(1) · m` — the sum of the channels'
calibrated lux gains at full output scaled by the online gain multiplier `m`
(§3.4). It is the most light the room can put on its own sensor, and it drives
the auto lux tiers (§2.1), the control deadband (§3.6), and the closed-loop
**capacity gate**: closed-loop control additionally requires `C ≥
min_closed_loop_capacity` (default 4 lx). A calibrated-but-tiny-capacity room
(e.g. kjøkken, whose sensor reads only ~2 lx with its lights at 100 % ⇒ `C ≈
2`) would otherwise servo ~1.2 lx targets against ~1 lx sensor quantization and
never visibly light — a regression versus its working open-loop mode. Below the
gate the room falls back to the daylight-aware open-loop path (§4.7), the exact
same code path an uncalibrated room takes. `C` is computed once per room per
cycle and shared by the gate and the closed loop (no duplicate formula).

4.6 **Open-loop tables.** Without a usable lux sensor, role tiers map to
normalized outputs per band: `out_active_day`, `out_active_evening`,
`out_background` (profile), interpolated by E, scaled by master gain, floored
by `dim_floor`.

4.7 **Daylight-aware open-loop.** A room that *has* a lux sensor but is not
closed-loop **eligible** — either not *trusted* (§3.5 — neither `calibrated`
nor bootstrap-confident) *or* trusted but below the capacity gate (`C <
min_closed_loop_capacity`, §4.5) — runs the open-loop tables (4.6) scaled by a
**daylight factor** `D = clamp(1 − N̂ / daylight_full, daylight_min_factor,
1.0)`, applied
multiplicatively to the ACTIVE/ADJACENT/BACKGROUND outputs *after* circadian
interpolation and *before* the §8 funnel. `N̂` is the estimator's
natural-light estimate, which for an untrusted room ≈ the filtered lux (its own
lamps barely move the sensor — that is precisely what "untrusted" means here,
§3.1). This replicates the legacy `100 − 0.5·lux` daytime damping for rooms
whose sensors are good daylight meters but nearly blind to their own lamps:
bright daylight pulls the tables down, darkness leaves them at full.
NIGHT_PATH, TV (playing) and outdoor outputs are **not** daylight-scaled — they
are mode tables (§6), not tier outputs. The TV **ON** cap (§6.3) is the one
mode input that composes with `D`: it clamps the already-damped tier output,
so daylight and the cap can only agree to make the room dimmer. Lux staleness falls back to unscaled open-loop
exactly as today (`D → 1` when `N̂` is unavailable). `daylight_full`
(default 200 lx) and `daylight_min_factor` (default 0.0) are §12 tunables.

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

6.3 **TV mode (tri-state).** The configured `tv_entities` resolve to exactly
one TV state, highest first:

| state | when |
|---|---|
| `PLAYING` | any entity is `playing` or `buffering` |
| `ON` | any entity is `on`, `paused` or `idle` — powered on, not playing |
| `OFF` | everything else (`off`, `standby`, `unavailable`, `unknown`, none configured) |

The three states drive three different levels of light in rooms flagged
`tv_mode`:

- **PLAYING** ⇒ the room switches to role TV and takes its profile's
  `tv_output` table when ACTIVE, `tv_output_empty` otherwise (sofakrok keeps a
  low glow when occupied, 0 when not; spisebord 15 %→occupied / 5 %→empty;
  gang dims). These are *commanded* values — the room is pinned to them, and
  the §4.7 daylight factor does not apply (they are mode tables, not tiers).
- **ON** ⇒ the room keeps its normal role and its normal tier path (open- or
  closed-loop), but every channel is **capped** at its band's
  `tv_output_paused` (room ACTIVE) or `tv_output_paused_empty` (otherwise):
  `b_i ← min(b_i, cap_band(i))`. A cap only ever takes light away, never adds
  it, so a room the tier path already leaves dark stays dark and a TV merely
  switched on at 09:00 in daylight changes nothing (the §4.7 damping has
  already put the room below the cap). The cap is the LAST step before the §8
  governor and applies identically to the open-loop (§4.6/4.7) and closed-loop
  (§3.6/4.5) per-channel outputs.
- **OFF** ⇒ TV mode contributes nothing; roles are re-evaluated and rooms
  *restore* (the legacy gang light that stayed dimmed forever now recovers).

The published role stays the room's underlying role while the ON cap governs —
the cap modifies outputs, not the FSM. Role `TV` means PLAYING. Precedence is
unchanged: sleep (6.1), night path (6.2) and away (6.4) all resolve before TV,
an outdoor room (6.5) ignores TV entirely, and a latched manual override (§9)
outranks both TV levels.

6.3a **Pause grace.** Leaving PLAYING for ON holds the PLAYING resolution for
`tv_pause_grace` (default 120 s) before the ON cap takes over, so a rewind or a
short pause does not walk the room lights up and straight back down. Resuming
inside the grace is a complete no-op — the room never left its playing level.
Any transition to PLAYING or to OFF clears the hold at once: resuming dims down
immediately, and switching the TV off restores normal lighting without waiting
out the grace. The engine self-schedules a review at the hold expiry (rule 0),
so the step up happens on time in an otherwise quiet house. `tv_pause_grace`
= 0 disables the hold.

6.4 **Away.** `anyone_home is False` ⇒ every indoor room OFF. Outdoor rooms
keep their dusk background (6.5) as presence simulation while the
`away_lighting` switch (§10, restorable, default on) is on; switch off ⇒
outdoor rooms go dark on away too. `None`/unavailable fails safe as home
(consistent with sonos-conductor 1.8). Arrival re-evaluates immediately;
no flash: normal fades apply.

6.5 **Balkong (outdoor room).** A room may be flagged `outdoor`. It ignores
presence and runs: ON at `E ≥ outdoor_on_threshold` (dusk) at
`out_background` warm CT; OFF when sleep turns on, and on away per 6.4
(background only while away — the occupational switch is ignored until
someone is home). Its
`occupational` switch (exposed entity, §10) raises it to `out_active_evening`
at a slightly cooler CT while on — "sitting outside" vs "ambient backdrop".

6.5a **Measured dusk** (outdoor rooms with a lux sensor). An outdoor room may
be given a lux sensor — including one an indoor room already uses, e.g. a window
sensor that sees the same sky as the balcony. It then computes a *dusk factor*

    D_out = 1                                                          if E = 1
          = clamp((outdoor_on_lux − N̂) / (outdoor_on_lux − outdoor_full_lux), 0, 1)

and 6.5's tier (`out_background` or, with the occupational switch on,
`out_active_evening`) is scaled by `D_out`; `D_out = 0` is OFF. The balcony
therefore eases in as the light actually goes — an overcast evening lights it
early — instead of snapping on at a sun-elevation threshold, and releases in
the morning when the light returns rather than when the sun ramp says so.

Two fallbacks bound the trust placed in the sensor. A missing or stale (§3.5)
sensor leaves 6.5's `E >= outdoor_on_threshold` gate exactly as it was. And at
the circadian plateau (`E = 1`: the sun below `sun_low_deg`, or past the
evening clock ramp) the room is lit as 6.5 always lit it, so a sensor reading
falsely bright — an indoor lamp, a wedge stuck at a daylight value — can never
leave the balcony dark through the night.

An outdoor room never enters the closed loop (§4.5)
or the bootstrap (§4.4) — its sensor is a dusk measurement, not a control
feedback path — so it exposes no target-lux and no calibration button, and its
own contribution to that sensor (a balcony lamp seen through a window) stays in
N̂'s own-light term rather than driving a loop.

6.5b **Manual light action declares presence** (outdoor rooms). An outdoor
room has no presence sensing — its occupational switch IS its presence, a
declaration (§1.10). A manual on/off on the room's lights (wall-dimmer button,
app, voice — any §9.1 foreign change) is the same declaration made physically:

- **ON edge** (room dark → lit): occupational turns ON.
- **OFF edge** (room lit → dark) while occupational is ON: occupational turns
  OFF; the room returns to mode resolution (dusk backdrop per 6.5/6.5a, or
  dark by day).
- **OFF edge** while occupational is already OFF is *not* a declaration — the
  user is suppressing the ambient backdrop; the plain §9.1 latch stands.
- A level change with no edge (dialing while lit) adjusts brightness, not
  presence: latch stands, occupational untouched.

The OFF edge requires the whole room dark (every channel off), and a `None`
level (channel momentarily unavailable) is no declaration. No declaration is
minted while a sleep/away hard-off governs the room — the mirrored flag would
outlive the episode (under a hard-off every press is an ON edge) and light the
interior around a phantom occupant at the next dusk.

An occupational edge (6.5b edge or the outdoor room's switch entity) also
arbitrates the room's override latch: a falling edge releases it always (a
declared absence must not leave a latched dial level burning for
`override_timeout`); a rising edge releases it only when
`D_out >= outdoor_presence_factor` — the same "deep enough to matter"
threshold as §1.10. Any weaker gate steps the user DOWN: below `D_out` ≈ 0.25
the sitting tier quantizes to the dim floor, so releasing on a shallow-dusk
press would rewrite a bright press to ~2 % within one cycle. Below the
threshold the latch keeps the user's level (occupational is still set, so
adjacency follows when the ramp deepens). To make that hold, the outdoor
daylight-OFF resolution *respects* a latched override while occupational is
on — only `override_timeout` releases it (never `should_release`'s off-worthy
path, even on a presence-capable outdoor room); sleep/away hard-offs still
release and win, and with occupational off the morning descent still
hard-offs a stray dialed level. Re-submitting an unchanged state is not an
edge, and the arbitration is inert inside the startup grace (the switch's
restore re-submit must never clear a latch at boot).

A foreign ZERO within `outdoor_stale_zero_window` of the engine's own last
write to the room is a SUSPECT stale report, not a declaration (the Plejd
gateway re-delivers superseded off-states ~15-30 s after our write replaces
them — observed live, context-free). A suspect zero carries NO information:
the §9.1 latch stands (the engine emits nothing while latched), but it is
not adopted — the engine's lit belief and the room's OFF edge survive — and
the NEXT foreign report resolves the suspense. A LIT report proves the zero
stale ONLY when it corroborates the preserved pre-adopt belief (within the
echo tolerance — i.e. the poll re-reading the level the engine commanded):
then the latch the zero caused is undone (same down-step gate as the rising
edge) and tier control resumes. A materially different lit level is a USER
action (hold-to-dim up from the suppressed off): the suspicion is consumed
but the §9.1 latch keeps their level. Another ZERO — the poll confirming a
lost write, or a real press that had been held in suspense — fires the
falling declaration normally: session ends, backdrop returns. The suspicion
cannot outlive its context: it is cleared on any occupational edge (session
boundary), under a sleep/away hard-off, and expires unresolved after two
poll intervals; a suspect zero also never re-stamps `override_since` (it is
not new user intent). The window is validated below 175 s (dataclass
invariant) so the poll's correction can never itself be swallowed.
Pressing OFF while
sitting returns the backdrop within a second by design, and because wall
buttons are hardware toggles, arriving at a lit backdrop takes two presses to
reach the sitting tier.

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
step `slew_step_empty` (0.25). The engine does **not** emit intermediate
steps: it emits a *single* command per changed channel carrying the goal and a
**mandatory** `ramp_seconds` sized so the move's flux-relative rate does not
exceed the slew bound. Executing that ramp is the adapter's job — via the
Plejd fork's native transition support, or software stepping below the engine
where the actuator has none. `ramp_seconds` is never optional; a 0 duration
means "as fast as the actuator allows".

8.3 **Write economy.** A channel is commanded only when the quantized goal
differs from the ledger's last commanded value by ≥ `min_delta` (flux-relative
0.03) or crosses on/off. Quantization is two-stage: the engine quantizes on
the `min_delta` flux grid (~33 perceptual levels), and the adapter owns the
final device-resolution quantization (e.g. Plejd's 255-step brightness) when
it renders the command. Rate limit ≥ `min_write_interval` (1.0 s) per
channel, latest-value-wins coalescing. Site-wide concurrent command cap
`max_inflight` (default 3) respects the single-gateway BLE bottleneck.

8.4 **Echo ledger.** Every command is recorded (channel, value, timestamp).
Incoming state reports matching a recent command (± `echo_tolerance`, within
`echo_window` 10 s) are consumed as echoes; everything else is a *foreign
change* (§9). Mirrors sonos-conductor's controller ledger. In addition to the
time-boxed echoes, the adapter keeps a **standing setpoint** per channel (the
last commanded normalized level) so an integration's periodic true-state poll
(Plejd re-reports every ~3 min, as a `uint16/256` float) that re-confirms our
own value long after the echo TTL is not misread as a foreign change.

**Ledger seeding across startup/reload.** On controller start — a fresh setup
*and* an options reload — the standing setpoint of every configured channel is
seeded from its current live state (its normalized brightness if on, `0.0` if
off) *before* subscriptions arm. A reload rebuilds the controller and would
otherwise wipe the standing setpoints; the next poll re-report of the
unchanged pre-reload level then has no ledger match and latches a **false**
manual override within seconds (a live incident). Seeding makes that first
re-report tolerance-match and be consumed. Accepted trade-off: a genuine manual
change made in the snapshot→first-report gap is absorbed once (the same
grossly-different report will re-latch on any subsequent change).

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
(hold expiry at OFF tier) — **presence-capable rooms only**, sleep on, away,
master gain off/on cycle, or `override_timeout` (default 4 h). Release
re-enters normal control with slew ramps (no jumps). A room is
presence-capable when a presence or occupancy-fallback sensor is configured;
in a blind room (door/corridor triggers only) OFF-decay merely means the
trigger hold expired while the occupant may still be present, so the latch
holds until one of the other release conditions — otherwise a wall-dial
adjustment is countered by the vacancy tier within one review (the soverom
incident, 2026-07-29..31).

9.3 **Manual-on respect.** A room turned on manually while the FSM wanted OFF
is an override (9.1) — the legacy "only auto-on if the light is currently
off" behavior falls out of this rule.

9.4 **Plejd wall-event awareness.** Configured `wall_event` entities
(WRT-01/WPH-01 `event.*`) count as foreign changes for their room even if the
resulting state lands inside echo tolerance. **Recovery republishes never
latch:** an ESPHome event entity re-emits its previous event timestamp when the
device reconnects (`unavailable`→old timestamp), which is not a human press — a
genuine press always transitions from one valid event timestamp to a strictly
newer one. A wall event latches the override only when the old state is present
and valid (not `unavailable`/`unknown`/absent) *and* the new timestamp differs
from it; the recovery edge and an identical-timestamp republish are ignored
(the gang + sofakrok false-latch during a 2026-08-01 availability blip).

## 10. Entities (adapter contract)

- `light.light_conductor_master` — master gain dimmer (§7), HomeKit-ready.
- `switch.light_conductor_enabled` — master enable; off = observe only
  (no commands; ledger and estimator keep running).
- `switch.light_conductor_<room>_occupational` — only for outdoor rooms (6.5).
- `switch.light_conductor_<room>_door_lighting` — only for rooms with trigger
  entities configured (1.9), restorable, default on.
- `switch.light_conductor_away_lighting` — outdoor presence simulation while
  away (6.4), restorable, default on.
- Per room diagnostics: `sensor.<room>_role` (enum), `sensor.<room>_natural_lux`
  (measurement, publish-gated: 5-point buckets + ≥ 10 s interval — recorder
  discipline per presence-conductor lesson), `sensor.<room>_target_lux`,
  `binary_sensor.<room>_overridden`.
- Per-room debug sensor `sensor.<room>_channels` (`DIAGNOSTIC`, **disabled by
  default** — registry opt-in while debugging): state is the room's peak
  commanded output as a whole percent; attributes carry one `{output_pct, ct,
  on}` entry per channel from the engine's commanded state. ALL attributes are
  `MATCH_ALL`-unrecorded and pushes are gated to a changed commanded value + a
  ≥ 10 s interval (piggybacking the publish signal — no new timers), so even
  when enabled it never lands in the recorder.
- `button.<room>_record_light_response` (4.4) + calibration result event
  entity.
- All volatile values live in engine state / diagnostics platform, never in
  recorded attributes. A recorder-discipline sweep test is mandatory.

## 11. Seeding & startup

11.1 On start the engine seeds from current entity states: existing light
levels are adopted as ledger baselines (no startup flash, mirroring
grow-conductor's restore-before-enforce), roles evaluate from live presence,
and rooms whose lights differ grossly from the computed goal converge with
`slew_step_empty` ramps only after `startup_grace` (30 s). The adapter's
standing-setpoint consume (a report matching the last commanded level is a
poll re-confirmation, not a foreign change) applies only to NO-OP re-reports:
if the previous state differed materially from the setpoint, the light
actually moved there — a wall dial restoring the previous level lands exactly
on the last command — and it latches (§9.1).

11.2 Restorable entities: master gain, enabled, occupational switches,
door-lighting switches, override latches (not restored — cleared on restart).
The seed snapshot carries the per-room `occupational` and `door_lighting` maps
(door lighting absent ⇒ on, the same default a fresh install and a missed
restore get), so an engine rebuilt from a snapshot gates triggers exactly like
the running one did.

## 12. Tunables (defaults)

| name | default | rule |
|---|---|---|
| hold_seconds (per room) | 120 s (kontor 90) | 1.3 |
| hold_passing_scale / hold_settled_scale | 0.3 / 4.0 | 1.3 |
| adjacent_fraction / adjacent_cap | 0.5 / 30 lx | 1.5 |
| background_fraction / background_cap | 0.25 / 15 lx | 2.1 |
| lux_day_frac / lux_evening_frac / lux_background_frac | 0.6 / 0.2 / 0.05 | 2.1 |
| living_memory | 900 s | 1.6 |
| trigger_hold / door_close_hold | 300 s / 15 s | 1.7, 1.9 |
| presence_blind_hold | 120 s | 1.1, 1.8 |
| sun_high_deg / sun_low_deg | +10° / −4° | 2.3 |
| evening_start / evening_full | 20:00 / 22:30 | 2.3 |
| morning_start / morning_full | 06:00 / 07:30 | 2.3 |
| circadian_tick | 300 s | 2.3 |
| evening_output_cap | profile (0.3 living) | 2.4 |
| evening_cap_threshold | 0.5 | 2.4 |
| write_blank | 5 s | 3.2 |
| tau_lux_up / tau_lux_down | 30 s / 60 s | 3.2 |
| night_prior_deg / tau_night_prior | −6° / 600 s | 3.3 |
| gain_learn_rate | 0.1 | 3.4 |
| bootstrap_min_obs / bootstrap_margin | 3 / 1.5 | 3.5, 4.4 |
| bootstrap_dispersion_max | 3.0 | 3.5 |
| lux_stale | 300 s | 3.5 |
| lux_wedge_warn | 1800 s | 3.5 |
| deadband_abs / deadband_rel | 5 lx / 0.15 | 3.6 |
| deadband_capacity_frac / deadband_floor | 0.2 / 0.5 lx | 3.6 |
| error_sustain / error_sustain_fast | 20 s / 2 s | 3.6 |
| min_closed_loop_capacity | 4 lx | 4.5, 4.7 |
| calibration_levels / calibration_dwell | 10,25,50,75,100 % / 4 s | 4.4 |
| daylight_full / daylight_min_factor | 200 lx / 0.0 | 4.7 |
| band_overlap / boost_evening_max | 0.15 / 0.5 | 4.5 |
| ct_day / ct_evening / ct_min_evening | 3300 / 2400 / 2200 K | 5.1, 5.3 |
| blend_threshold / blend_delta | 0.1 / 300 K | 5.2 |
| warm_dim_output | 0.3 | 5.3 |
| ct_min_delta | 100 K | 5.4 |
| sleep_fade / night_hold / night_fade | 4 s / 600 s / 10 s | 6.1, 6.2 |
| tv_pause_grace | 120 s | 6.3a |
| outdoor_on_threshold | 0.7 | 6.5 |
| outdoor_on_lux / outdoor_full_lux | 15 lx / 2 lx | 6.5a |
| outdoor_presence_factor | 0.5 | 1.10, 6.5a |
| outdoor_stale_zero_window | 45 s | 6.5b |
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
