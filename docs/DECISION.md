# Light Conductor — Decision Record

Architecture Decision Record for the light-conductor HACS integration.
Companion to ENGINE_SPEC.md (normative). Discovery data in DISCOVERY.md.

## D1. Scope & shape

One HACS integration (`light_conductor`, repo `leiklier/light-conductor`)
replacing ~10 lighting automations + 3 scripts on the live instance. Mirrors
the conductor family: pure core with no HA imports (CI-enforced), numbered
ENGINE_SPEC, single-writer controller with echo ledger, uv +
pytest-homeassistant-custom-component pinned to the production HA version,
ruff/pytest/hassfest/HACS CI, beta→stable release channels, merges/releases
only with explicit user approval.

Config topology: **one config entry = the whole home** (like sonos-conductor),
with rooms as options-flow sections. Rooms/channels/sensors are opaque ids —
nothing hardcoded to this apartment.

## D2. Regulate lux, not brightness

Legacy automations map lux→brightness% linearly per room with hard trigger
thresholds; the light's own contribution feeds back into the sensor, causing
the reported "cutting in and out". Decision: where a room has a lux sensor,
the control variable is **illuminance at the sensor**, with the room's own
artificial contribution modeled and subtracted (ENGINE_SPEC §3). The natural
light estimate drives a feed-forward correction with deadband + sustain —
never an incremental chase.

Why feed-forward instead of a PI loop: the actuator is lossy (Plejd writes can
drop silently) and the sensor is slow/deduped (~1 Hz, ESPHome-filtered). A
model-predicted setpoint lands in one write and tolerates dropped commands;
an integrator would wind up against them.

## D3. Photometric calibration per room

7-day recorder statistics show the rooms are photometrically incomparable
(spisebord daylight peaks 540 lx; sofakrok midday average is 7.6 lx; the
spisebord sensor reads 68 lx from its own lamp at brightness 20/255 — gain
~50× stronger than sofakrok's). One formula cannot serve them. Decision: a
per-room night calibration sweep (button, like presence-conductor's
RecordBaseline) measures each channel's lux gain and dimming curve at the
sensor; the estimator refines a bounded scalar online. Uncalibrated rooms run
with a square-law default curve so the integration is useful out of the box.

This also replaces the hand-tuned `set_benkebelysning_brightness` mapping
(`0.8x − 50`) and the kitchen `base > 62` step with measured curves + banded
allocation (§4.5) — no more visible pops at band boundaries.

## D4. Plejd: fork with targeted fixes; conductor assumes a lossy actuator

Code review of hass_plejd 0.21.3 / pyplejd 0.21.3 (full findings in
DISCOVERY.md §Plejd) found: no transition support at all (`transition:` args
in the legacy automations were silently stripped by HA), writes silently
dropped while disconnected, staleness-blind `connected` property, state
updated from command echo with true reconciliation only every 3 min,
all-or-nothing availability, and an unconditional double-connect + 5 s sleep
on every reconnect. But the protocol assets (mesh crypto, auth, opcodes,
firmware quirks) are sound and upstream is alive.

Decision: **do not rewrite; do not block on the fork.**
1. v1 of light-conductor treats Plejd as a lossy fire-and-forget actuator:
   coalesced latest-wins writes, ≥1 s/channel spacing, ≤3 in flight,
   software ramps, skip-when-unavailable, quiet re-reconcile on recovery
   (ENGINE_SPEC §8). The closed loop self-corrects dropped writes.
2. A parallel workstream forks `pyplejd`/`hass_plejd` with small
   upstreamable patches: `is_connected` liveness, surfaced write failures +
   retry, BLEDevice refresh + RSSI decay, opt-in double-connect, native
   software transitions, and group-address writes (room-atomic updates —
   the addressing data is already parsed upstream). Patches PR'd upstream;
   fork installed via HACS custom repo meanwhile.

The user's `set_plejd_brightness` script logic was verified correct except:
its `transition` field is a no-op (integration lacks the feature), and a
kelvin-only call (no `brightness_pct`) would turn the light off. Its
CT-before-brightness ordering is real and is adopted as spec rule 5.4.

## D5. Presence: presence-conductor first, template sensors as fallback

Primary per-room inputs are `binary_sensor.presence_conductor_<room>_room_occupancy`
+ `sensor.presence_conductor_<room>_room_activity`; global
`binary_sensor.presence_conductor_anyone_home`. The legacy template sensors
(`binary_sensor.<room>_occupancy` over raw radar zones) become fallback
occupancy entities, used when the primary is blind (blind ≠ absent).
Activity scales vacancy holds (passing 0.3×, settled 4×) — a pass-by no
longer commits a room to full lighting hold. Gang/soverom/bad have no
sensing; gang runs as a corridor (adjacency + evening + night path), soverom
stays door-triggered (`binary_sensor.soverom_dor`). The kontor WMS-01 PIR
(`binary_sensor.kontor_pir_sensor`) joins kontor's fallback list.

## D6. Follow-me smoothing via roles, not per-neighbour ladders

Legacy encoded neighbour-specific brightness ladders (sofakrok active → 50 %,
kjøkken → cap 12, kontor → cap 6…). Decision: collapse to
ACTIVE/ADJACENT/BACKGROUND tiers with per-room profiles: living-group rooms
(`vacancy: dim`) never drop below BACKGROUND while the living area is in use
(15-min memory), kontor (`vacancy: off`) goes dark after its hold. Tier
changes ramp in flux-relative steps sized by whether the room is occupied —
"subtle adjustments in the living room, full off in kontor" becomes
configuration, not code.

## D7. Circadian shaping is continuous

Legacy: `hour ≥ 18` ⇒ cap 30 %. Decision: a continuous circadian factor
E = max(sun-elevation ramp, clock ramp 20:00→22:30, reversed 06:00→07:30)
interpolating lux targets, output caps, and CT (3300 K→2400 K, dim-to-warm
floor 2200 K). At most one circadian-driven adjustment per 5 min, so the
evening descent is a slow drift, never a visible step. Kelvin blending keeps
tunable-white channels within 300 K of fixed-2700 K channels whenever both
are lit (D-spec §5.2) — mixed rooms must read as one scene.

## D8. Master gain as a HomeKit dimmer, neutral at 50 %

`light.light_conductor_master`: exponential gain, 50 % = ×1, 100 % = ×2,
0⁺ = ×0.5; off = all managed indoor lighting off (restorable). Gain drifts
back to neutral each morning (a "dimmer tonight" nudge should not permanently
darken the house). Mirrors the Sonos master-volume mental model. **Open
question for review (Q1):** is boost-above-automation wanted, or should
100 % = neutral (dim-only master)? Neutral-at-50 chosen provisionally
because the user asked for "relative to what the automations would do",
which implies both directions.

## D9. Balkong is an outdoor room, not presence-driven

DWN-02 tunable group. Dusk-on at background warm CT, off at sleep-on; away ⇒
off (tunable, default off, per requirement "when nobody is home all lights
stay off"). An `occupational` switch raises to sitting-outside level/CT.
No lux sensor outside — open-loop tables with the circadian factor.

## D10. Night path built in

The `input_boolean.night_movement` + two automations move into the engine:
sleep on + (soverom door edge | living-room presence/pass-by) ⇒ NIGHT_PATH
for the configured set at fixed dim warm outputs (defaults copied from
`script.night_path_lighting_on`: sofakrok 4 %, gang 5 %, spisebord 1 %,
downlights 20 % @ 2200 K), hold 10 min restartable. The input_boolean is
retired after migration (grow-conductor already has its own trigger input).

## D11. Manual control is respected via an override latch

Any non-echo change (wall rotary, HomeKit, voice) latches the room:
conductor adopts the observed state and stops adjusting until vacancy at
OFF-tier, sleep, away, or 4 h. Wall-controller `event.*` entities count as
manual even when the resulting level matches ours. This generalizes the
legacy "only turn on if currently off" guards, and fixes their gap: legacy
respected manual-on but fought manual dimming.

## D12. Recorder discipline from day one

Lessons from presence-conductor v0.5.2/0.5.3 applied as requirements:
volatile values never in recorded attributes; measurement sensors publish
through a quantize+rate-limit gate (5-lx buckets, ≥10 s); a recorder-
discipline sweep test (zero state writes under churn) ships with the first
entity PR; diagnostics platform carries the full engine state on demand.

## D13. Not in scope (v1)

- Soverom garderobeskap (zigbee, IKEA sensor-driven; its automation stays).
- Apollo/UniFi RGB indicator lights.
- Presence simulation on vacation; rotary-delta master control;
  Plejd device-settings writes (dim speed) — roadmap.
- Scene support (`scene.tv_kveld` is half-broken today; TV mode subsumes it).

## D14. Capacity-scaled deadband + closed-loop capacity gate (beta.7)

The fixed 5-lx control deadband (§3.6) and the plain `calibrated OR
bootstrap_confident` closed-loop entry condition (§3.5) both assume a room can
put meaningful light on its *own* sensor. Two live low-capacity rooms break
that assumption:

- **sofakrok** (calibrated, single channel, gain 8.89) has capacity `C ≈ 8.8
  lx` — its lux sensor sits in a dark corner and barely sees its own lamp. Its
  auto ACTIVE tiers (`0.6·C ≈ 5.3` day, `0.2·C ≈ 1.8` evening) sit at/below the
  5-lx deadband, so `should_correct` never fired and the ACTIVE role never lit
  the room; the evening tier was mathematically unreachable.
- **kjøkken**'s sensor reads only ~2 lx with its lights at 100 % (`C ≈ 2`).
  Were it ever calibrated, it would servo ~1.2 lx targets against ~1 lx sensor
  quantization and never visibly light — a regression versus its currently
  working open-loop mode.

Decision: (1) **capacity-scale the deadband** — the absolute component is
capped at `deadband_capacity_frac × C` and floored at `deadband_floor` (sensor
noise). Note the scaling applies to EVERY room with `C < deadband_abs /
deadband_capacity_frac` (25 lx at defaults) — e.g. C = 10 gets a 2-lx deadband
— not only the two motivating rooms; targets scale with the same C, so control
error stays proportional. Only rooms with `C ≥ 25` are strictly unchanged (the
`min` picks the 5-lx `deadband_abs`). Rooms landing in the 5–20 lx band should
be watched for hunting against their sensor's real noise floor after
calibration. (2) add a **capacity gate** — closed loop additionally requires
`C ≥ min_closed_loop_capacity` (4 lx), below which the room uses the existing
daylight-aware open-loop path (§4.7), the same code path an uncalibrated room
takes. The gate has no hysteresis: a calibrated room whose `gain_mult` drifts
across the boundary could oscillate closed↔open loop — accepted because both
live rooms sit far from it (sofakrok `C ∈ [4.45, 17.78]` over the full
`gain_mult` range; kjøkken's sub-deadband deltas freeze its `gain_mult`
entirely); add hysteresis before configuring a room whose capacity sits near
4–6 lx. A calibrated `C < 4` room with no explicit open-loop output tiers
resolves role outputs to 0 via §4.7 and stays dark — configure output tiers
for any such room. The gain-arming thresholds (§3.4/§3.5) deliberately keep
gating on the fixed `deadband_abs` — a sub-delta room not arming bootstrap is
a safety property, not a bug. Rejected alternative: lowering `deadband_abs`
globally — it would make high-capacity rooms hunt on sensor noise.

## D15. Blind rooms hold manual overrides; consume only no-op re-reports (beta.8)

Live incident (soverom, 2026-07-29..31): every manual wall-dial/wall-press
adjustment was countered to 0 within seconds. Root cause: soverom is a *blind*
room — door-triggered, no presence sensing — so its natural role decays to OFF
whenever the trigger hold expires, with nobody having left. Rule 9.2's
"OFF-worthy vacancy releases the latch" therefore fired at the first review
after every latch (the latch lived so briefly it never published), and
`vacancy: off` re-asserted darkness. The release rule conflated "role decayed
to OFF" with "room observed vacant"; those only coincide when the room can
actually observe vacancy.

Decision: (1) `RoomConfig.presence_capable` (set by the adapter when a
presence or occupancy-fallback sensor is configured) gates the OFF-worthy
release — blind rooms (soverom, gang, balkong today) hold a manual latch until
`override_timeout` (4 h), sleep, away, or a master power cycle. Accepted
consequence: a forgotten manual light in a blind room burns until the timeout
or sleep. (2) The controller's standing-setpoint consume is transition-aware:
a report matching `_last_commanded` is consumed only when the OLD level also
matched (a true no-op re-report, e.g. Plejd's 3-min poll float). A genuine
transition landing on the setpoint — a Plejd dial turn-on restores the
previous level, which is typically exactly our last command — now latches.
Sleep/away hard-offs still win over overrides (unchanged, by design).

## D16. Per-channel affine response mapping for the open-loop allocator (beta.9)

Motivating calibration (live kjøkken). The kitchen has three channels:
`downlights` (accent), `taklys` (primary), `benkebelysning` (boost, an LED
strip). The bench strip has a far steeper perceived-brightness curve than the
spots; the user's legacy script commanded it `benke% = clamp(0.8·base − 50, 0,
100)` — off below base 62.5 %, only 30 % when the others sit at 100 %. The
open-loop allocator gives a *lone-channel* band the full band output (the
within-band weight normalizes away for a single channel), so at daytime full
demand `benke` would be commanded EQUAL to the spots and visually blast over
them. No existing knob covers this: `weight` is relative *within* a band (a
lone channel is always its own peak ⇒ share 1), and `dim_floor` is a floor, not
a ceiling or a slope.

Decision. Add two frozen per-channel fields, `response_slope` (default 1.0) and
`response_offset` (default 0.0), forming an affine RESPONSE MAPPING applied as
the LAST step of open-loop channel mapping: `command = clamp(response_slope ·
out + response_offset, 0, 1)` on the post-weight, post-evening-lockout band
output `out`. A zero band output always stays 0 (the mapping runs only when
`out > 0`) so a positive offset never lights an off channel and never resurrects
the evening-locked boost band. The defaults are an exact byte-identical no-op,
so every existing config and all existing tests are unaffected. Normalized to
the engine's [0, 1] output space, the legacy benke formula becomes **slope 0.8,
offset −0.5** (`0.8·base% − 50%`). The kjøkken values are *user config*, applied
live through the options flow after deployment — never hardcoded in the engine.

Closed-loop boundary. The mapping applies ONLY on the open-loop path. The
closed-loop path (calibrated room above the §4.5 capacity gate) servos the
calibrated lux curves, which already own each fixture's true physical response;
layering a perceptual affine remap on top would double-compensate, so lux
servoing deliberately supersedes it. If a response-mapped channel's room later
calibrates and crosses the capacity gate, the mapping simply stops applying —
no conflict, no double-compensation, a clean handover. Kjøkken today is
open-loop (its sensor reads only ~2 lx at full output, `C ≈ 2 < gate 4`), so
the mapping governs it; that is the intended state. Calibration sweeps
(`RecordLightResponse`) also bypass the mapping — they command raw prescribed
dwell levels to measure the true response, so a benke-like channel is not
falsely rejected as a `dark_channel`. Estimator consistency is automatic:
`Â`/bootstrap/`record_step` read the channel's post-mapping `commanded_b` and
`f_i` maps that to flux, so the mapping changes what is commanded, not how the
observation is interpreted.

## D17. Bootstrap arming dispersion sanity + robustness fixes (beta.10)

Live incident (kjøkken, 2026-08-01). Kjøkken has three channels with a true
own-light gain of only ~2 lx at its sensor, and is deliberately kept OPEN-loop
by the §4.5 capacity gate (`C ≈ 2 < min_closed_loop_capacity 4`). A corrupted
first-night bootstrap promoted it into closed loop anyway: morning clouds swung
daylight 10–17 lx while the room's lights happened to toggle, so the §3.5
shadow-bootstrap — which arms on observed `ΔL ≥ deadband_abs` during own
transitions — recorded ambient swings as own-light response. The live ratios
`[8.73, 95.31, 14.21]` gave `median × bootstrap_margin 1.5 → gain_mult 21.3`, so
capacity `3 × 21.3 = 64 ≫ 4` cleared the gate. The room then chased an
unreachable ~40 lx auto target: banded fill saturated accent+primary and never
reached boost (benkebelysning stayed dark — a user-visible failure). A real
lamp's own-step ratios agree closely; an ambient-contaminated set is wildly
dispersed.

Decision (Fix 1): before the bootstrap commits, require the collected ratios to
**agree** — with `m = median(ratios)`, `max(ratios) ≤ bootstrap_dispersion_max ×
m` and `min(ratios) ≥ m / bootstrap_dispersion_max` (new tunable, default 3.0).
A failing set is **dropped** (ratios cleared) rather than accumulated, so a
later quiet period bootstraps cleanly instead of the contamination latching
forever. Everything else about arming is unchanged (the `deadband_abs` observed
threshold, per-run best-effort, the 1.5 over-model margin).

Rejected alternative: exclude the bootstrap `gain_mult` from the §4.5 capacity
gate (gate on the calibrated base capacity only). Rejected — an *uncalibrated*
room's base capacity is just its channel count (default gains 1.0), so the gate
would reduce to "≥ 4 channels" and be meaningless for the 1–3-channel rooms that
bootstrap is designed to serve; it would kill the feature. Dispersion is the
correct guard: it distinguishes genuine own-light steps (which agree) from
ambient contamination (which scatters), independent of the room's capacity.

Accepted residual: *systematically consistent* contamination (e.g. three cloud
swings of similar magnitude coinciding with three own transitions) passes the
scatter test and still arms. The remaining backstops are the 1.5 over-model
margin (stable undershoot, not hunting) and the fact that such coincidence
across ≥3 independent transitions is far rarer than the single-outlier case
this incident showed. Likewise, the wedge notice's piggyback on the publish
cadence means a wedge starting during a fully-quiet deep-night plateau is
flagged at the next clock-boundary review rather than within `lux_wedge_warn`
— acceptable latency for a hardware-quirk notice.

Two more robustness fixes from the same morning ship together:

- **Fix 2 — wall-event recovery guard (§9.4).** A brief Plejd/ESPHome
  availability blip re-emitted each event entity's *previous* timestamp on
  reconnect (`unavailable`→old timestamp), which `_on_wall_event` accepted and
  latched as a whole-room manual override — gang + sofakrok "pressed their
  dials" in the same second (08:08:23, no human present), and again during a
  later proxy restart. A genuine press always moves from one valid event
  timestamp to a strictly newer one, so the handler now returns early when
  `old_state` is absent/`unavailable`/`unknown` or when the new timestamp equals
  the old (identical-timestamp republish).

- **Fix 3 — lux-wedge repair notice (§3.5).** Both Apollo MSR-2 LTR390 sensors
  "wedged" (entity stays available but stops reporting; fix = press its ESP
  reboot button) with no operator visibility. A sensor available but silent past
  `lux_wedge_warn` (new tunable, default 1800 s — much longer than `lux_stale`'s
  open-loop fallback) now raises a non-fixable WARNING repairs issue (one per
  sensor, en+nb strings), cleared automatically when reports resume. The check
  piggybacks the existing publish cadence (no new timer) and drives off the
  estimator's existing `last_report_at`; repairs issues never touch the
  recorder, so no new recorded entity is added. Ordinary unavailability (§8.5)
  is not a wedge and never raises it.

  **beta.11 — the notice becomes FIXABLE.** Fix 3's non-fixable notice still made
  the operator walk to the sensor. Each Apollo MSR-2 exposes a `button` entity
  with `device_class: restart` on the *same device* that unwedges the LTR390, so
  the notice now carries a one-press Fix. When raising the issue the controller
  resolves that button via the entity registry (sensor entity → `device_id` →
  the device's button entities → the one whose registry `original_device_class`
  is `ButtonDeviceClass.RESTART`; trust the registry, not the live state, which
  is unavailable while wedged; first by `entity_id` if several). If found, the
  issue is raised `is_fixable=True` with `translation_key="lux_wedged_fixable"`
  and `data` carrying the button, sensor, room, and owning `entry_id`; if not,
  the beta.10 non-fixable notice is kept verbatim (the fallback path is
  load-bearing — no restart button, no Fix button). `repairs.py`'s
  `async_create_fix_flow` returns a `RepairsFlow` confirm dialog whose confirm
  handler presses the button and finishes; HA then deletes the issue. Because
  the rebooted sensor takes ~a minute to resume, the confirm handler stamps a
  per-controller grace (`_wedge_fix_pressed[entity_id]`, `WEDGE_FIX_GRACE` 120 s
  — a module constant, not a new tunable) and drops the sensor from `_wedged` so
  registry and controller agree; `_check_lux_wedge` suppresses re-raising within
  the window and re-raises only if the sensor is still silent afterward (the
  reboot did not help). Grace is per-controller and cleared on reload alongside
  `_wedged`; `async_stop`'s withdrawal is unchanged. The `repairs` platform is a
  discovered platform (not in `PLATFORMS`); `manifest.after_dependencies` lists
  `repairs` to satisfy hassfest's import-dependency check. hassfest's
  translations schema makes an issue's `description` and `fix_flow` mutually
  exclusive, so the fixable variant carries only `title` + `fix_flow` (the
  confirm step's own description names the sensor, room, and button); the
  non-fixable fallback keeps its `title` + `description` manual instruction.
  Both texts exist in en + nb.

## Open questions — RESOLVED (user, 2026-07-25)

- **Q1 (D8):** master dimmer neutral at 50 % — confirmed (boost possible).
- **Q2 (D9):** balkong keeps dusk background as presence simulation while
  away, controllable via a restorable `away_lighting` switch (default on).
  Spec 6.4/6.5 updated; `balkong_when_away` tunable replaced by the switch.
- **Q3 (D10):** night-path levels stay as copied from the legacy script.
- **Q4 (D6):** delegated — decision: **keep the evening lockout.** The
  boost band exists for task light (food prep), which is orthogonal to the
  cozy-evening goal; calibrated curves fix the pop, not the aesthetics.
  Revisitable via the `boost_evening_max` tunable without code changes.
- **Q5 (D4):** Plejd fork deployed via HACS custom repo — approved.

**Process (user, 2026-07-25):** all PRs before the first deployment may be
reviewed and merged at Claude's discretion; releases/deployment still get
explicit sign-off.
