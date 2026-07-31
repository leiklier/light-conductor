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
