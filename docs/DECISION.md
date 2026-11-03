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

## D18. Occupational switch counts as presence (beta.12)

Live incident (2026-08-01 evening): the user sat on the glassed balcony with
its occupational switch on; 15 minutes after they left the interior,
`living_recently_active` expired and every interior light went dark around
them. The balcony's ACTIVE role is mode-resolved from the occupational switch
and never set `self_active` — the only flag adjacency (`neighbour_active`) and
`_living_active` read — so configuring balkong as a neighbour/living-group
member was structurally inert, and a manual dial inside would not stick either
(vacant presence rooms release the override latch as OFF-worthy).

Decision: `roles.step` for OUTDOOR rooms sets `self_active = occupational`
(rule 1.10). Everything else falls out of existing machinery: adjacency, the
living-group memory, and override retention (a living room is BACKGROUND, not
OFF-worthy, while the balcony is in use). Away/sleep mode hard-offs still
resolve before the role path and win unchanged.

## D19. Outdoor rooms measure dusk (beta.13)

Reported 2026-08-07: the user was sitting on the balcony with "sitting outside"
on and the balcony light was off. Not a bug — outdoor rooms consulted *no light
measurement at all*; they were gated purely on `E >= outdoor_on_threshold`, and
E was 0.691 at 21:40 local, crossing 0.7 (and lighting the balcony) three
minutes before sunset. The user's proposal: the spisebord lux sensor sits at the
window next to the balcony, so let the balcony read it and dim off it.

History confirmed the sensor is a good outdoor proxy despite being behind a
curtain (≈510 lx midday, 36 lx at 19:00, 15 lx at 20:40, 3 lx at sunset) and
that its cross-talk from the balcony lamp is negligible: when the balcony lit at
21:44 the previous evening the sensor went 2.0 → 2.2 → 2.1 → 2.0 lx.

Decision (§6.5a): an outdoor room may take a lux sensor — *including one an
indoor room already uses* — and scales its 6.5 tier by a dusk factor that ramps
0 → 1 as N̂ falls from `outdoor_on_lux` (15 lx, ≈20:40 local in August) to
`outdoor_full_lux` (2 lx). Three properties make this safe:

- **Two bounded fallbacks, not blind trust.** A missing or stale sensor leaves
  the old E gate untouched (a sensorless outdoor room is bit-for-bit pre-6.5a),
  and at the circadian plateau (E = 1 — sun below `sun_low_deg` or past the
  evening clock ramp) the balcony is lit exactly as before, so a falsely-bright
  sensor cannot leave it dark all night. Between those bounds the measurement
  governs in BOTH directions: the alternative — a pure union with the E gate —
  would have kept this morning's balcony lit until 08:28 local at 155 lx,
  because the sun ramp releases hours after the light actually returns.
- **The closed loop stays out.** `modes.resolve` returns a resolution for every
  outdoor room, so the engine's closed-loop and bootstrap paths are unreachable
  there by construction. The sensor is a measurement, not a feedback path — so
  outdoor rooms expose no target-lux sensor and no calibration button.
- **Presence mirroring is gated deeper than lighting** (`outdoor_presence_factor`
  0.5): D18 mirrors the occupational switch into `self_active`, and the interior
  must not follow the balcony into ADJACENT while it is still bright indoors.
  With no sensor the factor is binary, so this reduces exactly to D18's gate.

Consequence for the adapter: the lux index becomes entity → *rooms*. It was
entity → room, so sharing a sensor would have silently starved whichever room
lost the collision of its lux feed.

## D20. The balcony wall button toggles "sitting outside" (beta.14)

User request 2026-08-07 (same evening as D19): pressing the physical dimmer
button for the balcony should count as toggling the occupational switch.

The insight that makes this clean: an outdoor room has NO presence sensing —
the occupational switch is not a preference, it IS the room's presence, as a
declaration (§1.10). A manual light action on that room is the same declaration
made physically, so it belongs in the §9.1 foreign-change funnel (covers wall
button, Plejd app, HomeKit, voice — not just one event entity), mapped by edge:
room-dark→lit ⇒ occupational on; lit→dark while occupational on ⇒ occupational
off; lit→dark while already off ⇒ NOT a declaration (backdrop suppression — the
plain latch stands, room stays dark); dial-while-lit ⇒ brightness only.

Override arbitration on the edge (any source, button or switch): falling edge
always releases (declared absence returning to mode resolution — backdrop at
dusk, off by day; without this, HomeKit-off after a dial would leave the dialed
level burning for override_timeout). Rising edge releases only at
D_out >= outdoor_presence_factor. The first cut gated on D_out > 0 with the
argument "only D_out = 0 resolves OFF and counters the press"; the adversarial
review (F1) measured that the argument condemns the whole shallow half of the
ramp equally — below D_out ≈ 0.25 the sitting tier quantizes to the dim floor,
so a 90 % press was rewritten to 2 % within one cycle, the beta.8 incident
reproduced by the very mechanism built to prevent it. The §1.10 threshold is
the honest gate and makes 6.5b and 1.10 consistent. Review also found (F2) that
presses under a sleep/away hard-off minted un-clearable declarations (every
press is an ON edge once the hard-off zeroes the channels) that would light the
interior around a phantom occupant at the next dusk — no declaration is minted
under a governing hard-off; (F4) that should_release's off_worthy path defeated
respect_override the moment anyone gives the balcony an occupancy fallback —
a respected latch now releases only on timeout; (F5) that the restore-resubmit
safety was queue ordering, not the documented guard — the arbitration is now
inert inside the startup grace; (F8) that a None level (wall event during a
Plejd blip) read as an off-press. F3 (stale/lost zero cancelling a
session) was first accepted as a residual — then materialized on the FIRST
live field test (2026-08-07 21:02:48 UTC): a context-free device report of
the superseded off-state arrived 14 s after the conductor's sitting-tier
write and cancelled the session the two-press arrival had just started. The
two-press sequence is precisely what leaves a stale zero in flight, so the
flagship interaction was unreliable, not an edge case. beta.15 adds the
stale-zero guard. Round 2 of review rejected the first cut (suppress but
adopt): adopting the suspect zero consumed the room's only OFF edge, so the
documented poll-zero recovery was unreachable — the session could never be
cancelled, tier control froze under the latch, and a real press inside the
window stranded 4 h of phantom adjacency ending in a nocturnal relight. The
shipped design treats a suspect zero as carrying NO information: latched but
NOT adopted (belief and OFF edge survive, suspect_zero_at stamped), resolved
by the next report — corroborating lit (matches the pre-adopt belief within
echo tolerance) ⇒ latch undone, tier control resumes; non-matching lit = a
user dial ⇒ suspicion consumed, latch keeps their level (round 3 found the
uncorroborated release rewrote a 95 % dial to the 49 % tier — beta.8 again;
the first corroboration attempt then compared post-adopt and matched
everything); zero ⇒ falling declaration fires (session ends, backdrop
returns; covers both the genuinely lost write and a real press held in
suspense, at ≤ poll-interval latency). Round 3 also pinned the stamp
lifecycle: cleared at session boundaries (OccupationalChanged), under
hard-offs, and by a 2-poll-interval TTL, so a leaked suspicion can never
discard a later session's first dial; and suspect zeros never re-stamp
override_since.
outdoor_stale_zero_window is dataclass-validated < 175 s so the poll's
correction can never be swallowed. Deliberately asymmetric — ON edges are not
windowed, so the arrival sequence is never suppressed. Re-submitting an unchanged
state never releases.

Building the daylight case surfaced a pre-existing behaviour: the outdoor
daylight-OFF resolution was shaped exactly like a sleep/away hard-off, so ANY
daylight press on the balcony was released-and-countered within a cycle (nobody
had noticed — who lights a balcony at noon?). Now that a press is a
declaration, `RoomResolution.respect_override` marks the dusk-OFF as
override-respecting while occupational is on; sleep/away are untouched and an
undeclared room keeps the morning hard-off cleanup.

Known consequences, accepted: pressing OFF while sitting returns the backdrop
(~15 %) within a second — that is what "sitting outside off at dusk" means
(§6.5); and because Plejd buttons are hardware toggles, arriving at a lit
backdrop takes two presses to reach the sitting tier (off, then on). The
adapter needs no changes: OccupationalSwitch.is_on already reads engine state
and every entity refreshes on each engine cycle, so a button-driven flip
appears in HomeKit automatically.

## D21. TV mode is tri-state, and the middle tier is a CEILING (beta.16)

User request 2026-08-09: TV mode worked, but it was binary — `playing` dimmed
the living rooms, anything else restored them fully — so a pause walked the
lights up and a resume walked them back down. Asked for three levels (TV off /
TV on-or-paused / TV playing) plus a timeout before stepping up after a pause.

**Tri-state, not two booleans.** `TvState` (OFF < ON < PLAYING) is resolved by
the adapter over every configured TV entity, highest wins. The live setup makes
the middle state trivially observable: the LG (`media_player.sofakrok_tv`)
reports `on` for a whole session and drops to `unavailable` when powered off,
while the Apple TV carries `playing`/`paused`/`idle`. `buffering` counts as
playing; `idle` (Apple TV home screen) and plain `on` count as ON;
`standby`/`unavailable`/`unknown`/missing entity are OFF.

**PLAYING commands, ON caps.** The playing tier stays what it was — a mode
resolution that pins the room to `tv_output`/`tv_output_empty`. The new middle
tier deliberately does NOT resolve the room: it keeps its own role and tier
path and is merely ceilinged at `tv_output_paused` /
`tv_output_paused_empty` (defaults 30 %/15 %). Rationale: the ON state spans
whole sessions, including daytime ones (the live history has the TV on
07:05–08:13), and mode tables are exempt from the §4.7 daylight damping — a
commanded middle tier would have pinned rooms to 30 % in full morning
daylight, i.e. TV-on would sometimes ADD light. A ceiling composes with
everything upstream (daylight factor, master gain, evening cap, closed loop)
and can only subtract, which is exactly the user's mental model of "dim a bit
because the TV is on". The room's published role stays its real role; role
`TV` continues to mean PLAYING.

**Closed-loop rooms clamp the TARGET too.** Sofakrok, the one calibrated room,
is also the TV room. Applying the ceiling only to its outputs would leave the
loop with a permanently unclosable error: the sustain would re-arm every cycle
and re-issue the same (coalesced, unwritten) command forever. So the ceiling is
also converted into lux — `T' <- min(T', N̂ + Σ g_i·f_i(cap_i)·m)` — and the loop
parks satisfied at the ceiling, with an honest published `target_lux`. The
per-channel `min()` stays as the hard guarantee for every path; it lands within
one §8.3 min-delta flux quantum of the ceiling, like every other target.

**Pause grace (§6.3a, `tv_pause_grace` 120 s).** Only the PLAYING → ON edge
arms it; the engine self-schedules the expiry review. Resume clears it (no-op,
the room never moved), and TV-off clears it (the user ended the session
themselves — waiting out a grace would just be lag). 120 s comes from the live
Apple TV history: this week's rewind-length pauses were 1–41 s and the
resumed-after pauses ran to ~167 s, while the pause that preceded a real TV-off
was 118 s — so 120 s suppresses the flicker cases without stranding a genuine
break, and it is a §12 tunable.

Alternatives rejected: a global multiplier on the playing tiers (no independent
control, and 0 % empty rooms stay 0 %); paused ⇒ full normal lighting unless
configured (that is today's behaviour, i.e. no feature); daylight-scaling the
middle tier (the cap already subsumes it — `D` has damped the tier before the
ceiling sees it).

## D22. Door lighting is a per-room toggle, gated in the engine (beta.17)

User request 2026-08-13: §1.9 door-triggered lighting works, but there was no
way to turn it off — opening the bedroom door lights the room whether or not
that is wanted right now (reading in bed, a guest sleeping, airing the room).

**Engine-side gate, not adapter-side.** The obvious cheap version is to stop
submitting `TriggerFired` from the controller when the switch is off. Rejected:
the engine is the authority on room state (§0), and events are the only way
time and input enter it (rule 0). A gate that lives in the adapter would make a
seeded or replayed engine behave differently from the live one. So
`door_lighting` is per-room engine state (default on) and
`roles.ingest_trigger` returns early while it is off. In production the knob
reaches the engine the way every runtime knob does (§11.2): the switch is a
RestoreEntity that re-submits its restored state into the controller queue
during platform setup, ahead of the first drain — the production snapshot's
`door_lighting` map stays empty. The `InitialSnapshot` mapping exists for
engine-level replay/tests, where a seeded engine must gate triggers exactly
like the running one did. Ingestion is the right seam: `_step_trigger` and
`next_review` then need no changes at all, because a room with the toggle off
simply has no hold.

**Default on, and a restore miss stays on.** The enabled switch fails safe to
*off* because a conductor going live unbidden after a restart is the dangerous
direction (§10). This toggle is the opposite: it is a convenience knob, and the
failure a user would actually notice is walking into a dark bedroom because a
restore miss silently disabled the feature they never turned off. So the engine
default is `True` and a restore miss keeps it — as does a restored
`unavailable`/`unknown` state, which carries no user intent and must not be
read as OFF (the `_RestoreSwitch` warning still fires either way, naming the
default it kept — a missed restore stays visible in the log).

**The falling edge ends trigger-borne activity, shape-independently.** Leaving
an in-flight `trigger_hold` to expire on its own would mean "off" takes up to
`trigger_hold` (300 s) to do anything visible — the user flips the switch and
the light stays on for five minutes, which reads as a broken switch. And in a
presence-shaped room with triggers, clearing the trigger hold alone is not
enough: the trigger's self-activity would mint a fresh vacancy hold in that
very recompute and burn for up to `hold_seconds` — same broken switch. So the
fold clears `trigger_hold_until` and, iff the room's occupancy is not
currently True (i.e. the activity was trigger-borne, not presence-borne), the
vacancy hold and self-activity too; a genuinely occupied presence room stays
ACTIVE. The room re-resolves in that same recompute and leaves down the normal
demotion/fade path, i.e. exactly the transition hold expiry would have produced
— not a hard-off, so a neighbour-driven ADJACENT/BACKGROUND role still stands.
The rising edge is deliberately NOT retroactive: re-arming a hold from a door
edge that happened while the feature was off would light the room for a door
event the occupant had opted out of.

**Nothing else moves.** The toggle gates trigger ingestion only: sleep, away,
night path and master already win over the ACTIVE role and are untouched, and
it is explicitly *not* an override release condition — the §9.2 blind-room
latch protection (the soverom incident) must keep holding a wall dial, whether
or not the toggle is flipped afterwards.

The entity is keyed off configured trigger entities rather than
`shape == DOOR`, so a corridor room given triggers (§1.7) gets the same knob
for free; outdoor rooms are excluded — their lighting is mode-resolved (§6.5),
never trigger-driven, so the switch would be inert. Today only soverom has any.

## D23. Modes win at their onset edge; standing modes respect the dial (beta.18)

Live incident 2026-08-14: "the light level cannot be adjusted in soverom and
gang" — from the wall dials. Diagnosis: while `household_sleep_mode` stands,
every §9.1 latch was released and countered within one review — the sleep
hard-off re-released on *every* recompute (~1 Hz, driven by the lux sensors),
so a 03:00/06:30 dial-up faded straight back off over `sleep_fade`; and while
the night path was active (re-armed by the very person moving around), gang
was pinned to its 5 % night level over any latch (`suppress_override`). The
two rooms named in the report are exactly the rooms whose dials are used
during sleep hours; the legacy automations never fought a wall dial.

**Decision: a mode hard-off wins once, at its onset edge.** Engaging
sleep/away/vacation releases every latch in the event fold (the house goes
dark whatever was dialed before bed — §6.1/§6.4 keep their teeth), and a
latch present while the mode *stands* was therefore minted during it:
deliberate manual control, respected until `override_timeout`. Mechanically,
the sleep/away hard-off resolutions are now `respect_override` (the shape
D20 introduced for the outdoor daylight-OFF), the standing-mode checks left
`should_release`, and `suppress_override` is **removed** — the night path
governs only unlatched rooms; its resolution is not `off`, so the ordinary
hold branch keeps a latched room untouched. The onset-edge test is explicit
about non-edges: `anyone_home = False` re-submitted while already away (the
adapter re-resolves on every fallback entity change) must not release a
during-away latch.

Alternatives rejected: gating the release on "who latched, when" timestamps
(fragile against re-armed episodes; the onset edge encodes the same intent
with no bookkeeping); releasing night-path-room latches at episode end (the
timeout already bounds them, and an end-of-episode counter is the same
fight one step later); keeping suppression for "safety" (the path lights a
route for someone who is demonstrably at a wall plate — there is no safety
case for overriding them).

Accepted residual: a during-sleep dial left on burns until the 4 h timeout
(then the standing hard-off takes it back). That is the price of respecting
manual control, same as any latch; the wall dial that lit it turns it off.

**Companion adapter fix (§8.4a).** The §9.1 latch silences the engine but
nothing silenced the *writer*: on the no-transition Plejd lights every move
is a 1 Hz software stepping ramp, and a dial mid-ramp — soverom's door-open
blast, 0 → day target ≈ 10 s, commanded at the exact moment a person stands
at the wall plate — was overrun by the remaining queued steps and erased.
Foreign changes (and §9.4 wall events, room-wide) now abandon the channel's
stepping ramp, pending slot and rate timer before the event is submitted.

## D24. Engine time is the local wall clock (beta.18)

Found while diagnosing D23's incident: the adapter stamped `handle()` with
`dt_util.utcnow()`, while the §2.3 clock ramps (and `_next_clock_boundary`)
read minutes past **local** midnight — documented as local since day one
(§12: "20:00", "06:00"; the core test helpers even say so). On the live
Europe/Oslo install every clock term ran two hours late in summer: E held
1.0 (full evening) until 08:00 local each morning, so the sensor-less rooms
— soverom's door lighting and gang, whose level is a pure function of E —
came on at the capped 30 % evening level through the whole morning window,
and the evening-cap clamp (applied after master gain) meant no dimmer could
raise them. The lux rooms masked the same phase error behind daylight
damping, which is why it read as "just soverom and gang". Invisible in
high summer (bright mornings need no light); exposed by August. Fix:
`dt_util.now()`. Aware-vs-aware arithmetic everywhere else makes the zone
otherwise irrelevant; no engine change.

## D25. Echo corridor is a value band, not a moving front; availability recovery never latches (beta.19)

Twelve days of live logs (2026-08-24 → 2026-09-03) turned up one cause behind
every false override latch: the adapter was reasoning about **when** an echo
should arrive, on a transport that cannot promise it.

The evidence. (a) *kjøkken latched 2 s after its own turn-on*, with every
channel sitting at exactly the commanded value — benkebelysning's first-step
echo (0.016) arrived ~2 s late against a fade-front corridor of [0.033, 0.093]
and was classified foreign. (b) *kontor + gang latched 4 s into the sleep
hard-off* (twice), which kept gang dark on the night-path walk the hard-off
had just fired. (c) *spisebord and gang latched within seconds of their own
turn-ons*, repeatedly. The mechanism is the fork's transport: ~130 ms per GATT
write, the whole mesh serialized at ~7 writes/s, a `transition` rendered as a
**deadline-paced** ramp that skips levels when writes are slow, and a gateway
that coalesces LASTDATA echoes under load. A room transition fades several
channels at once (a sleep hard-off fades 7 channels in 4 s), so intermediate
echoes routinely land seconds away from where a moving front expects them —
while their *values* are always on the segment between start and goal.

Decision: the corridor becomes a **value band with monotone progress** (§8.4).
While it lives, a level in `[lo − tol, hi + tol]` that does not move *away*
from the goal is an echo, whatever the timestamp; `_Envelope.front` and the
temporal slack are gone. The overshoot margin floor goes 2 → 5 s (a congested
mesh tail is normal, not exceptional).

The band alone would be too generous, and the first draft of this ADR was wrong
about why that is survivable: it claimed the ~3 min true-state poll corrects
anything the band absorbs. It does not. An absorbed report has already become
the entity's HA state, so the poll re-reporting that same value fires
`state_reported`, not `state_changed`, and never reaches the foreign-change
path — an absorbed dial would have stood until the next command. Hence the
**high-water mark**: a fade only ever approaches its goal, so a report
materially further from the goal than the best progress so far is a hand on the
dial and latches. That covers both the mid-fade back-dial and the dial-down in
the margin seconds after our fade completed. Level skipping and late echoes
still pass, since progress only ever advances. Re-targeting mid-fade takes the
**union** of the live band and the new segment, because `frm` comes from HA's
lagging state while the lamp is really somewhere mid-fade; the mark resets to
measure progress toward the new goal.

Accepted residual: a dial *toward* our goal during our own fade (dialling to
40 % while we fade 0 → 60 %) is absorbed until the fade ends. It is
indistinguishable from an intermediate echo by construction, it is a few
seconds of the conductor's ramp, and the next command re-asserts the setpoint —
versus 4 hours of a false latch. That trade is the whole point.

Alternatives rejected: widening the temporal slack (guesses at a distribution
with no upper bound — the same bug with a bigger constant); requiring N
consecutive foreign reports before latching (delays the *genuine* dial by a
poll interval, the one thing §9.1 must never do); trusting the fork to report
truthfully (upstream work, tracked in §13, and the conductor must be correct
against the transport it has).

**Availability recovery never latches.** Separately, every HA restart latched
every Plejd room: the entities go unavailable → off → on(true level) ~29 s in,
and that off→on report has no standing setpoint to match (the seed ran while
the entity was unavailable, so nothing was seeded). The adapter now stamps each
entity's recovery edge — its unavailable → available transition, or its first
report when it was unseeded — and folds the **first** would-be foreign report
within `RECOVERY_GRACE` (20 s) as a `ReviewTick`: no latch, no writer
interrupt, §8.5 reconciles quietly. The grace is deliberately one-shot and
debounced (an edge within 60 s of the previous one does not re-arm it): the
restart signature is exactly one fold, while "fold everything for 20 s, re-armed
on every flap" is a hole a flapping Plejd link could hold open indefinitely —
the room would stop responding to its wall dial entirely, which is the very
failure this ADR exists to end. Accepted trade-off: a genuine dial press that
is the first divergence within 20 s of a reconnect is absorbed once.

**CT-first pops an off lamp on (§5.4).** The third latch source: writing CT as
its own `turn_on` before brightness. On the fork `turn_on(colortemp=…,
dim=None)` becomes the plain ON command, so an *off* lamp jumps to its
remembered level (96 %) and reports it — a level nobody commanded, with no CT,
hence no echo. An off lamp now takes CT and brightness (and the transition) in
one call; a lit lamp keeps the §5.4 split ordering, where the OUTPUT_SET
clobber it exists to avoid is real. The combined write records **no** CT echo:
a CT-only echo carries no level test, and a CT-capable lamp repeats its kelvin
on every state change, so it would have swallowed any dial for the corridor's
whole TTL — trading one false-latch bug for a silent-deafness bug. The level
corridor and the final-value echo already cover the write.

**Observability.** Brightness attributes are not recorded on the live instance,
so a false latch left no trail at all — every diagnosis above came from
correlating service calls with state history by hand. Every foreign
classification is now one INFO line (entity, old → new level, CT, live
corridor + age, standing setpoint) and every consumed echo a DEBUG line.

## D26. Daylight reference rooms, per-room daylight_full, evening boost output, sleep_keeps_override, on_ramp_max (beta.19)

The same twelve days produced a second class of complaint — not "the
conductor stopped listening" but "the level is wrong" — and every instance
traces to a global knob a room could not opt out of.

The evidence. (a) **kjøkken runs 70-96 % all day**: its sensor reads only
40-60 lx at noon against a *global* `daylight_full` of 200 lx, so `D` never
falls far. (b) **spisebord's sensor sits behind a curtain** (10-60 lx at
shoulder hours, 300-450 lx at noon) — the same 200 lx reference is wrong for it
in the other direction. (c) **gang has no sensor at all** and glowed ADJACENT
at 49 % all day whenever a neighbour was active; the user dialled it off
repeatedly. (d) **sofakrok is the only closed-loop room** and its sensor sits
in a dark corner under the lamp (N̂ ≈ 0.07 lx at noon), so the loop faithfully
servoed the lamp to ~24 % in broad daylight. (e) The user turned the kitchen
bench strip on at **22:04** and the boost band's evening lockout is
unconditional. (f) Sleep onset released the latch on a bedroom light the user
had *just* turned on and hard-offed it; they re-lit it 9 s later. (g) Turning a
room on fades over ~9.6 s in an occupied room (`|Δflux| / slew_step`), which
reads as sluggish rather than gentle.

User decisions (given, not derived): kitchen much dimmer by day; kitchen
evening cap 25 % with the bench strip allowed in the evening; gang off by day
and ~15 % in the evening while ADJACENT; sofakrok off in daylight by following
spisebord's sensor. This ADR is about making those expressible in config, not
about the values.

Decisions:

1. **Daylight reference rooms + per-room `daylight_full` (§4.7).** A room may
   name another room whose fresh N̂ supplies its daylight factor, and may
   override the global `daylight_full`. A sensorless corridor (gang) and a room
   whose own sensor is untrustworthy (spisebord's curtain) can now be damped by
   a sensor that actually sees the sky, and kjøkken's 40-60 lx noon reading can
   be scaled against a reference that matches it. A stale or missing source
   still means `D = 1` (unchanged fallback, §3.5).
   On the **closed-loop** path the factor scales the lux target `T'` **only**
   when a reference is set: a closed-loop room's own N̂ is already subtracted by
   the estimator, so applying its own sensor twice would be a double count —
   but sofakrok's own sensor is *blind to daylight*, which is exactly the case a
   reference fixes. When the factor reaches its floor on a lit room the deadband
   is bypassed, because `|error| = Â` can sit *inside* the deadband on a
   low-capacity room and would otherwise strand the lamp lit all day — the
   sofakrok failure mode, one level down. (Keyed on the floor rather than on
   `T' == 0`, so a non-zero `daylight_min_factor` cannot silently disable it.)
   The factor is adopted with **hysteresis** (`DAYLIGHT_MIN_STEP` 0.05, with
   both endpoints always reachable): `N̂` drifts continuously under cloud and
   `D` feeds the output directly, so the naive version re-commanded the room on
   every wobble — ~159 writes/h per channel over a cloudy hour, on a mesh that
   manages ~7 writes/s for the entire house. Cheap damping beats a clever
   controller here.
   Rejected: a global "corridors follow the brightest sensor" rule (implicit,
   unpredictable when a sensor wedges); teaching gang a virtual sensor (a
   second estimator to keep honest, for a room with no measurement).
2. **`boost_evening_output` (§4.5).** The evening lockout stays the default
   (D6/Q4 — the boost band is task light, orthogonal to a cozy evening), but a
   profile may name an explicit normalized output the BOOST band takes in an
   **ACTIVE** room during the lockout window. It is applied after the evening
   cap and is deliberately *not* subject to it — an explicit "this is what the
   bench strip does in the evening" value, not a tier the cap should trim — but
   it **is** scaled by master gain: `gain.multiplier` returns 0 when the master
   is off (§7.2), which is the master switch's only mechanism, and a band that
   ignored it would be the one light in the house the switch could not
   extinguish. ADJACENT/BACKGROUND, the mode tables and the closed-loop path
   stay locked out. Rejected: raising `boost_evening_max` (a global that
   unlocks every boost band, in every room, at every tier).
3. **`sleep_keeps_override` (§6.1/§9.2).** Per room, sleep's onset edge does
   *not* release the latch. The standing sleep OFF already respects a latch
   (D23), so a bedroom light latched a minute before bed survives sleep onset.
   The latch then stands for the full `override_timeout` (4 h) — the same
   residual D23 already accepted for a latch minted *during* standing sleep —
   or until a master cycle or, in a presence-capable room, observed vacancy.
   Turning the lamp off by hand does **not** release it (that is simply another
   foreign change; the room stays latched *and* dark, which is what was asked
   for). Off by default — the house going dark at bedtime is the rule, and this
   is the one room where the rule is wrong.
4. **`on_ramp_max` (§8.2, default 3 s).** A turn-on from off is capped at
   3 s; dimming and turn-off ramps keep the full slew-derived ramp. The slew
   bound exists so a *change* in a lit room is not a jolt; a room that is dark
   has no such continuity to protect, and 9.6 s of fade before the room is lit
   reads as a slow integration, not as care. The cap is engine-side, so it
   bounds both adapter paths — the native transition and the software stepping
   ramp, whose step count derives from the same `ramp_seconds`.

## D27. Quantize in command space on a grid dense at the dim end (beta.20)

The evidence. Every dim tier in the house rendered as the 2 % dim floor:
background 8 %, the night path's 2–5 %, TV 5/10 %, kjøkken ADJACENT ~12 %. The
governor quantized on an absolute 0.03 **flux** grid and gated writes on a
0.03 flux delta. On the default `b²` curve flux 0.03 is b ≈ 0.173, so every
goal below b ≈ 0.122 rounded to flux 0 and clamped to the floor — and from a
lamp at the floor the first level up (0.173) was a 0.0296 flux move, *under*
the gate: a channel at 2 % could never leave it for any goal below ~25 %. A
flux grid is coarsest exactly where the eye resolves the most (§4.3 is about
*ramps* looking even; a *rendering* grid must look even, which flux spacing
does not).

Decision (§8.3):

1. **Grid in command space**, per channel: `g_0 = dim_floor`, `g_{k+1} = g_k
   + max(quant_step, quant_rel·g_k)`, last level 1.0. Absolute 0.01 steps
   through the dim end (a relative step would drop below Plejd's 1/255 device
   resolution there), 10 % relative steps — Weber-like — above 10 % output.
   33 levels at the live `dim_floor` 0.02, ≤ 50 for any floor: write volume
   stays bounded (a full sweep is ≤ 32 level changes). A last interior level
   within half a step of 1.0 is absorbed into it (no 0.985 → 1.0 sliver for a
   ramp to skip). Command space, not flux,
   because the rendering bound is stated on the command (`|b_emitted − b_goal| ≤
   max(0.01, 0.08·b_goal)`) and must hold on every curve: a relative flux grid
   would halve the error on `b²` and double it on sofakrok's near-linear
   calibrated dim end.
2. **Sticky quantizer** (`QUANT_HOLD` 0.7): a lit channel at `c` is held while
   the goal is within 0.7 of a local step of `c`; otherwise the goal renders to
   its nearest level. The gate is then "emit iff the rendering is not the
   current level" — by construction it can never strand a channel (outside the
   band the nearest level is provably a different level) and never re-emits a
   settled goal (the nearest level is provably inside the new band). Error ≤
   `max(0.007, 0.07·b)`, and jitter below 0.4 step peak-to-peak — a drifting
   own-sensor daylight factor — emits nothing. Holding on the *actual* ledger
   value (not its grid snap) matters: seeded and adopted levels sit off the
   grid, and a snapped hold would have let them sit up to 1.25 steps away.
3. **Slew and ramps stay in flux space** (§8.2/§4.3) — only the rendering grid
   moved. **CT reads the quantized level**: dim-to-warm (§5.3) and blend
   anchoring (§5.2) are computed on the level the lamp will actually show.
4. `min_delta` (absolute flux) is retired; `quant_step` / `quant_rel` replace
   it in §12 and the options form. A stored `min_delta` override is ignored
   (unknown tunable keys are skipped), which is the behaviour we want.

Echo interplay. `ECHO_LEVEL_TOL` (0.03) now spans up to four adjacent dim
levels. That is benign: every engine command records its own echo, so no match
ever has to tell two dim levels apart, and a late or poll-repeated report of
the *previous* dim level — the only report the overlap can confuse — is a
stale own echo, which is correctly not latched. Residual (pre-existing): a dial
moving a lamp by less than the tolerance is indistinguishable from an echo.

Rejected: keeping the flux grid and only lowering `min_delta` (the floor
cliff remains — at 0.01 flux the first level above 0.02 is still b = 0.1 — and
the top end gets three times the writes); a log-flux grid (curve-dependent
error, and the dim end collapses below device resolution); pure nearest
rounding with no hold band (goal jitter on a level boundary re-commands every
cycle, the D26 159-writes/h failure in a new place).

## D28. The adopted level is the standing setpoint; recovery is observed, not just folded (beta.20)

The evidence. (a) **The 4 h timeout never fired on a lit lamp.** Plejd's ~3 min
true-state poll flips a lit lamp's brightness between integer and float (184
→ 184.355, back to integer 5–80 s later). The adapter's no-op guard consumed a
re-report only if it matched `_last_commanded` — set on the conductor's *own*
writes — so after a user dial every flip was a fresh ForeignChange and
`override.latch()` re-stamped `override_since` twice per poll. Diagnostics
showed `override_since` equal to the lamp's `last_updated` in several rooms.
(b) **Every restart latched someone anyway.** The snapshot is taken while the
Plejd entities are unavailable, so the engine seeds every channel off and the
ledger has no setpoint. D25's recovery fold absorbed the off → on(true level)
report as a bare `ReviewTick` — no setpoint, no belief — and was one-shot, so
the next poll flip latched (17:14:46 recovered → sofakrok latched 17:15:26;
22:43:28 → kontor 22:43:33, sofakrok 22:43:54). The stale "off" belief also
had the sofakrok estimator count its own lamp as daylight, and an open-loop
room whose goal was 0 never turned off a lamp that came back lit. (c) A
re-registration rewrite (`unavailable` → `unavailable`, 5 s into a restart)
armed the recovery grace, so the real edge 23 s later fell inside the 60 s
debounce and the off → on report landed just outside the 20 s window.

Decisions (§8.4/§8.5/§9.1/§11.1):

1. **The setpoint follows the adopted level.** A report classified foreign also
   records its level as the channel's standing setpoint, so later no-op poll
   flips are consumed. The transition-aware rule is unchanged: a dial *away*
   from the adopted level (old = adopted, new ≠) still latches, and a dial
   restoring a previous level (old ≠ setpoint) still latches. Wall events do
   not move the setpoint (their adopted level may be a mid-fade HA state); the
   engine guard below covers them.
2. **Recovery is observed.** A recovery edge (first available report after
   unavailable/unknown, or the first report of an unseeded channel) and the
   one report the grace folds both set the setpoint and submit a new engine
   event, `ObservedLevel`, which corrects the channel's belief without
   latching. `unavailable` → `unavailable` is no longer an edge. The engine
   re-bases `Â_filt`/`N̂` on the corrected belief at once and marks a lit
   observation **reconcile**, so the next move converges with
   `slew_step_empty` — the quiet reconcile §8.5/§11.1 already promised but the
   governor never implemented (it used the room-role slew). Lit snapshot
   seeds get the same flag.
3. **Engine re-stamp guard.** On an already-latched room, a non-wall foreign
   report that matches the adopted `commanded_b` (0.03) and CT (60 K) neither
   re-stamps nor re-adopts. It is redundant with (1) on the poll path and
   exists for the paths (1) cannot see: a wall event adopting a level the
   setpoint does not know, a reload, a future integration. Disabled while a
   §6.5b suspect zero is pending — the report that resolves a suspicion *is* a
   re-statement of the preserved belief.

Tolerance (review, 2026-11-03): the no-op consume used the absolute
`ECHO_LEVEL_TOL` 0.03, which at the dim end D27 now renders is a third of the
level — a hand nudging a 5 % TV level to 8 % was consumed and overwritten. It
uses `min(0.03, max(2/255, 0.1·setpoint))` now; the 2/255 floor covers every
live poll flip (max 0.754/255) and the 8-bit re-report of every grid level.
The echo ledger and fade corridor keep 0.03 (in-flight reports, not re-reads).

What did not change: the only `override.latch` call sites remain `_on_foreign`;
a §6.5b suspect zero still never re-stamps; CT-only reports keep their beta.19
handling (a CT change at the standing level is consumed by the adapter's
level-only no-op guard, as before; at the engine a different CT is not a
re-statement). The outdoor stale-zero resolution is unaffected: the poll
re-reading the true lit level after a suspect zero is an off → on transition,
which the no-op guard never consumes.

Risk accepted: the observed transient "off" a Plejd link reports first on a
mid-day reconnect is adopted as belief, so the engine may re-send a lit
room's level a second before the true level arrives; the true report then
matches that command's echo. Before, the transient was ignored and a lamp that
genuinely came back off stayed off until something else moved.

## D29. The away onset is debounced (beta.20)

The evidence. presence-conductor's `anyone_home` reads off for 31–35 s after
every HA restart — longer than the 30 s startup grace — and flaps off for
2–7 min while the untracked second occupant is home alone (10-25
19:58:44 → 20:01:46; 10-26 16:34:35 → 16:36:53 and 16:40:34 → 16:47:24). Each
flap engaged away: the whole house went dark and every latch was released.
D28 makes it worse on restarts: the engine now *knows* the lamps are lit after
recovery, so a 35 s phantom departure would hard-off every one of them.

Decision (§6.4): away mode, and with it the onset edge (hard-off + latch
release) and the outdoor away lighting, engages only after the house has read
empty for `away_debounce` (default 600 s, §12, options form): `anyone_home`
False *and* no room's primary presence True. Primary presence during the
window cancels it and the window restarts when the room empties — someone
demonstrably in a room is someone home, whatever the phone-based aggregate
says. The engine self-schedules the expiry; a boot with `anyone_home` False
counts from the first event. Arrival stays instant; vacation is not debounced
(it is a deliberate declaration, not a sensor reading). 600 s is above every
observed flap (max 6 min 50 s) with margin, and well below any real absence
worth lighting for.

Why primary presence and not the raw fallbacks: the raw kitchen sensor was on
~116 times while nobody was home (D31 data); letting it cancel the window
would keep an empty house "home" indefinitely. presence-conductor's room
occupancy is the calibrated estimator, and is what its own `anyone_home`
should have agreed with in the first place.

Rejected: a longer startup grace (fixes restarts, not the daytime flaps);
debouncing in the adapter (the window must be cancelled by room presence and
self-scheduled, both engine facts); ignoring `anyone_home` entirely (away is
the only thing that turns the balcony to presence simulation and darkens a
house left with a light on).

## D30. The override timeout never hands back an occupied room (beta.20)

User decision (2026-11-02): manual overrides must not hand back while
someone is still in the room. Until D28 the question barely arose, because the
4 h timeout effectively never fired on a lit lamp (every poll flip re-stamped
it); now that it counts from the last genuine touch, a 4 h evening on the sofa
would end with the conductor dimming the user's chosen level to its 22:00 tier
mid-film.

Decision (§9.2): an elapsed `override_timeout` is deferred while the room is
self-active (occupancy True, vacancy hold, trigger hold — the same
`self_active` the FSM uses for ACTIVE) and releases at the first recompute in
which it is not, re-entering normal control with slew ramps. No new timer:
leaving is an occupancy event and the hold expiries are already scheduled
(§1.3); `override_review` stops returning the elapsed deadline, which would
otherwise have asked for an immediate review on every recompute. Blind rooms
(no presence, no live triggers) keep the plain timeout, and every other release
path is unchanged — the off-worthy vacancy release, the sleep/away/vacation
onset edges (incl. `sleep_keeps_override`), the master cycle.

The deferral also applies under a respected standing hard-off (a latch minted
during sleep, still occupied at its timeout): the same rule — someone is in
the room — and the hard-off follows the moment they leave. Rejected: a longer
timeout (it would still fire on an occupant, just later) and resetting the
timer on occupancy (a forgotten lamp in a busy room would never be handed
back; deferral hands it back the first time the room empties).

## D31. Raw fallback evidence lights at once; unconfirmed episodes get a provisional hold (beta.20)

The evidence (recorder 10-25 → 11-03, replayed through the real `roles` code,
80.5 h home+awake): the raw fallbacks are OR-ed with presence-conductor's room
occupancy and dominate it — **every** replayed ACTIVE episode in kjøkken,
spisebord, sofakrok and kontor *started* on a fallback edge; presence-conductor
confirms 8–27 s later (median, from the last raw rise) or never (kjøkken: 85
primary spells in nine days). The raw sensors also fire with the house empty —
kjøkken 0.73/h, sofakrok 0.89/h (46 h of away time); kontor and spisebord never
did — and each such blip held a room ACTIVE for the full 120 s (an EMPTY
activity peak scales by 1.0), the kitchen bench strip included.

**Correction.** The first beta.20 draft *gated* the fallback: it counted only
after 15 s of persistence. That delayed every one of the 443 replayed
fallback-led turn-ons by 15 s — all 83 in kontor, 53 of them into a dark room,
where the raw sensors never blipped at all — to remove blips. The draft
rejected the alternative below on a replay that scaled *every* EMPTY-peak hold
by 0.3, primary-confirmed episodes included; that is not the alternative, and
the comparison was wrong (independent review, 2026-11-03).

Decision (§1.1): the fallback OR counts **at once** (beta.19). An ACTIVE
episode that has seen no confirmed evidence gets a **provisional hold**
`min(normal hold, fallback_provisional_hold)`, default 60 s. It is
**confirmed** — normal holds for the rest of the episode — when the primary is
seen True in it, or a fallback spell persists `fallback_min_on` (15 s; per-room
override, 0 = always confirmed; dropouts ≤ `FALLBACK_BRIDGE` 2 s bridged; the
spell is judged at its falling edge, before that edge's hold is set). The blind
path is unchanged. One aggregate spell per room rather than per entity: three
rooms have one fallback, and kontor's two (a mmWave with a ≥ 14 s minimum spell
and a PIR with a ≥ 75 s pulse) agree.

Replay (beta.19 → draft gate → this): turn-ons delayed 0 → 443 → **0**;
ACTIVE dips ≤ 60 s 100 → 80 → **103**; time lit by blip episodes (no primary,
no raw spell ≥ 15 s) 311 → 0 → **212 min**; kitchen dark-time episodes (each
an on/off of the bench strip) 133 → 125 → 136. 60 s rather than 30 s: at 30 s
the sofakrok short dips double (the hold then ends inside the gaps of a real
occupant's chatter). The table is in the beta.20 PR.

Not shipped: "≥ 2 rising edges within W" (rising-edge bursts within 3–20 s are
statistically identical away and home — the onset of a blip cannot be told from
an entry, which is exactly why the turn-on must not wait).

## D32. Primary presence holds an open night-path episode (beta.20)

The evidence: since the bedroom door sensor came back (10-31 22:43) the path
opens on time, but `night_hold` — 600 s after the last trigger — ran out with
people up: 11-02 the path faded 06:57:06 and the kitchen was lit by hand at
06:57:37; 11-01 it went off 08:10:48 and sofakrok was lit by hand 08:19:15.

Decision (§6.2): while an episode is open, primary occupancy True in any
night-path room restarts `night_hold` (re-checked every recompute, and stamped
at the instant such a room empties so the occupant always gets the full hold).
Only presence-conductor's primary counts, never the raw fallbacks (the raw
kitchen sensor was on ~91 min during one night's sleep); presence never opens
an episode; levels stay `night_output`.

Replay (the real `Engine` on the live options, 10-31 22:44 → 11-03 07:18; the
beta.19 run reproduces the live NIGHT_PATH role spells to the second): the
11-02 06:56:54 fade is held to sleep-off 07:05:57 (kitchen primary 06:48–06:49,
sofakrok 06:57); the 11-01 08:10:48 fade to 09:26 (breakfast — kitchen and
sofakrok presence throughout); 11-01 22:07 +12 min, 11-03 06:36 +10 min,
11-02 05:57 +2 min, 10-31 23:12 +2 min. **No nocturnal (00:00–05:00) episode
was extended** (03:46 and 04:07 unchanged). Not fixed, by design: 11-01
07:30:35 → kitchen lit by hand 07:57:19 — nobody was in a path room for the
whole episode, and the return 20 minutes later opened no episode (no door
edge). Holding on a confirmed (≥ 15 s) fallback spell instead (D31) changes the replay by
seconds (+0.3 min on that episode) and is not worth the raw sensors' night
noise.

## D33. Darkness E and wind-down W are separate factors; the evening cap and boost hand-over are continuous (beta.20)

User decision (2026-11-02): split "it is dark" from "it is late". One factor
`E = max(E_sun, E_clock)` did both, and at 59° N they are hours apart: 2 Nov
E ≥ 0.5 from ~16:09, 21 Dec from ~14:51, and E never reaches 0 at midday
~25 Nov–17 Jan. The house went to its bedtime tier mid-afternoon — the
kitchen at 25 % from 16:09 — because the evening cap switched on at E ≥ 0.5
as a cliff (kitchen 0.625 → 0.25, the other rooms ~0.65 → 0.30, every room's
`evening_output_cap` equals its `active_evening`), and the bench strip jumped
6 % → 18 % at the same threshold when its `boost_evening_output` took over.

Decision (§2.3/§2.4/§4.5):

1. **E stays darkness; new `W = max(E_clock, min(E_sun, dusk_ceiling))`.**
   The sun alone carries the wind-down only to `dusk_ceiling` (new tunable,
   default 1.0 ⇒ W ≡ E, i.e. beta.19's *routing*; planned live value 0.4);
   the clock carries it the rest of the way. **1.0 is not beta.19's output**
   — see (3).
2. **Every consumer, assigned** (grep of `circadian.`, `e >=`/`e <`,
   `evening_factor`, `is_evening`, `last_e`, parameters named `e`):
   - *W* — `targets.role_outputs`/`active_outputs` (ACTIVE interpolation, and
     ADJACENT through §1.5; BACKGROUND is a fixed table): how bright a room
     is *for the hour*. `estimator.target_lux` (closed-loop ACTIVE/ADJACENT/
     BACKGROUND lux targets): same tier, measured. `targets.evening_cap` on
     all three output paths: the cap is the wind-down's ceiling.
     `targets.boost_weight` in `photometry.allocate` (open-loop tiers and mode
     tables) and `estimator.channel_outputs_for_demand` (closed loop), plus
     `boost_evening_output` via the same hand-over: the bench strip is task
     light vs a cozy *late* evening, not vs darkness — cooking at 17:00 in
     December is cooking.
   - *E* — `ct_policy.ct_target` (§5): warm light belongs to the dark; at
     17:00 the kitchen is bright *and* warm. `targets.outdoor_dusk_factor` and
     `modes._outdoor` (the `outdoor_on_threshold` gate and the E = 1 plateau
     backstop, §6.5/6.5a), `roles.step`'s occupational-presence gate (§1.10),
     and the §6.5b arbitration through `_outdoor_dusk`: a balcony is lit
     because it is dark outside. The engine's `evening` flag → the corridor
     BACKGROUND gate (§1.7): a dark hallway beside a lit living room needs its
     glow at 17:00, not from 20:00. `last_e` → the §7.3 morning drift: left on
     E as asked; between ~25 Nov and ~17 Jan E never reaches 0 so that edge
     does not fire, and the daily reset rests on the sleep-off edge (verified:
     `SleepChanged(False)` calls `relax_to_neutral` in the fold).
     `circadian.is_evening` (tests only) and `Engine.circadian_factor` stay E;
     `Engine.wind_down_factor` and the `last_w` diagnostic are new.
   - *Neither* — the night-path and TV PLAYING tables (§6.2/6.3) carry no
     factor; they meet W only through the cap and boost hand-over the funnel
     applies to every gain-scaled table.
3. **Continuous cap** `cap(W) = (1 − W) + W·evening_output_cap` on every
   path, the closed-loop *hold* included (a held level above the cap follows
   it down a grid step at a time instead of waiting for a deadband
   correction to land the whole difference). Linear because it is the same
   line the interpolation draws from full output to the cap: a profile whose
   evening tier is at or below its cap is never dimmed by the cap at all, and
   the cap only trims what rides above it (master gain, closed-loop demand).
   `evening_cap_threshold` now only gates the §1.7 corridor (it keeps its name
   — stored options use it).
   **Visible at any `dusk_ceiling`, 1.0 included** (independent review,
   2026-11-03 — an earlier draft of this ADR said 1.0 kept beta.19). Every live
   room has `evening_output_cap == active_evening`, so beta.19's cap step at
   E = 0.5 was the evening tier arriving early, and the continuous cap removes
   exactly that: at E = 0.5 the kitchen is 61 % instead of 25 %, sofakrok
   (open loop) 64–67 % instead of 30 %; on 2 Nov the 25/30 % tier is reached at
   ~17:08–17:11 (E = 1) instead of 16:09. That is the intended change — the
   user asked for no cliffs and a brighter dusk — but it is a change. Second
   consequence: with master gain above neutral the cap trims winter *daytime*
   output (E never reaches 0 at midday ~25 Nov–17 Jan; 21 Dec noon E ≈
   0.17–0.20 ⇒ cap ≈ 0.86–0.88 at `evening_output_cap` 0.25–0.3, where beta.19
   left a ×2 boost at 100 %).
4. **Continuous boost hand-over**: the boost band is `β·tier + (1 − β)·evening`
   with β falling 1 → 0 across `W ∈ [boost_evening_max − 0.2,
   boost_evening_max]`; at and above `boost_evening_max` the band is exactly
   beta.19's (locked out, or `boost_evening_output`). The closed loop scales
   the boost band's capacity by β.
5. **Self-scheduling per term**: the engine ticks while `E_sun` or `E_clock`
   is strictly inside its ramp — with the sun down E sits at 1 while W climbs
   20:00 → 22:30, and the beta.19 test on E alone would have slept until
   06:00. **`circadian_tick` 300 → 60 s**: with the D27 grid's 0.01 dim-end
   steps a 5-min tick moved the bench strip two or three levels per tick on
   the 90-min morning ramp. A property test sweeps W through the live
   configuration (evening and morning ramps, ACTIVE/ADJACENT/BACKGROUND,
   open-loop, plus sofakrok's closed-loop goal at gain 1 and 2) and asserts
   ≤ 1 grid level per tick; at 300 s it fails, at 60 s it holds. Writes are
   bounded by levels crossed (the governor emits only on a level change), not
   by the tick. (Also fixed for the property: D27's grid had a 0.985 → 1.0
   sliver level that a ramp skipped over; a last level within half a step of
   1.0 is now absorbed into it — 33 levels.)

Caveat for the closed loop: the deadband (§3.6) still decides *when* a
correction lands, so a closed-loop room moves in deadband-sized corrections as
before; what D33 guarantees is that the level it lands on (and the capped
hold) is continuous in W.

## D34. Corridor triggers ignore the closing edge (beta.20)

`roles.ingest_trigger` shortened the hold to `door_close_hold` (15 s) on a
closing edge for every room shape. Right for a door-shaped room (§1.9 — the
bedroom door closing means you left, or shut yourself in), wrong for a
corridor: the plan is to add the front-door contact as a gang trigger, and
closing the front door behind you would darken the hallway 15 s later, while
you take your shoes off. Decision (§1.7): a CORRIDOR's triggers pulse
`trigger_hold` on opening only; the closing edge is ignored. DOOR rooms are
unchanged; PRESENCE-shaped rooms with triggers keep the shortening (specified
explicitly now — their presence sensing carries an occupant past the short
hold, and a trigger there is a door into that room). Outdoor rooms never use
triggers (§10).

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
