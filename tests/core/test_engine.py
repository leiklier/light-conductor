"""Engine scenarios — the legacy behaviours from docs/DISCOVERY.md, end to end.

The engine is seeded, booted past the startup grace (rule 11.1), then driven
with stamped events. Each test cites the rule(s) and the legacy behaviour it
reproduces or deliberately fixes.
"""

from __future__ import annotations

from math import isclose

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ActivityChanged,
    ForeignChange,
    HomeChanged,
    LuxReport,
    MasterGainChanged,
    MasterPowerChanged,
    NightTriggerFired,
    OccupationalChanged,
    PresenceChanged,
    ReviewTick,
    SetAwayLighting,
    SetEnabled,
    SleepChanged,
    SunElevationChanged,
    TriggerFired,
    TvChanged,
    VacationChanged,
)
from custom_components.light_conductor.core.model import (
    Activity,
    Band,
    ChannelConfig,
    EngineConfig,
    InitialSnapshot,
    Profile,
    Role,
    RoomConfig,
    Vacancy,
)
from custom_components.light_conductor.core.plan import SetChannel, TurnOffChannel

from .helpers import apartment, at, diag, offs, review, sets

DAY_SUN = 20.0
NIGHT_SUN = -8.0


def _engine(snapshot: InitialSnapshot | None = None) -> Engine:
    """A booted engine: the first event arms startup grace; callers act later."""
    eng = Engine(apartment(), snapshot)
    eng.handle(SunElevationChanged(DAY_SUN), at(1, 18, 0))
    return eng


# --- §2/§4: kitchen evening accent survival -----------------------------


def test_kitchen_evening_accent_survives_boost_off() -> None:
    """§2.4/§4.5: at dusk the boost band (benkebelysning) locks out and only the
    accent downlights survive — the legacy kitchen-off-after-sunset behaviour."""
    eng = _engine()
    # Daytime occupancy: taklys + downlights + benke all lit.
    day = sets(eng.handle(PresenceChanged("kjokken", True), at(1, 18, 1)))
    assert {"kjokken_downlights", "kjokken_taklys", "kjokken_benke"} <= set(day)

    # Full evening (sun down): downlights survive, taklys + benke go off.
    cmds = eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    assert "kjokken_downlights" in sets(cmds)
    assert {"kjokken_taklys", "kjokken_benke"} <= offs(cmds)
    assert not eng.state.rooms["kjokken"].channels["kjokken_benke"].on
    # The surviving accent is warm (evening CT + dim-to-warm, rules 5.1/5.3).
    assert sets(cmds)["kjokken_downlights"].ct <= 2500


# --- §6.3: spisebord TV ladder ------------------------------------------


def test_spisebord_tv_ladder_occupied_then_empty() -> None:
    """§6.3: TV output is 15 % occupied / 5 % empty (spisebord ladder)."""
    eng = _engine()
    eng.handle(PresenceChanged("spisebord", True), at(1, 20, 0))
    occ = eng.handle(TvChanged(True), at(1, 20, 1))
    assert eng.state.rooms["spisebord"].role is Role.TV
    assert "spisebord_taklys" in sets(occ)  # TV glow commanded
    occ_target = diag(occ, "spisebord").target_output  # ~0.15

    # Room empties (hold expires): drop to the empty TV glow, still TV role.
    eng.handle(PresenceChanged("spisebord", False), at(1, 20, 3))
    empty = eng.handle(ReviewTick(), at(1, 20, 6))
    assert eng.state.rooms["spisebord"].role is Role.TV
    assert diag(empty, "spisebord").target_output < occ_target  # 15 % -> 5 % ladder


# --- §6.3: gang TV dim + restore (the deliberate fix) -------------------


def test_gang_tv_dim_then_restores_after_tv_off() -> None:
    """§6.3: gang dims to 5 % during TV and *restores* when TV ends — the legacy
    gang light that stayed dimmed forever now recovers."""
    eng = _engine()
    eng.handle(PresenceChanged("sofakrok", True), at(1, 21, 0))  # living area active
    dim = sets(eng.handle(TvChanged(True), at(1, 21, 1)))
    assert eng.state.rooms["gang"].role is Role.TV
    assert "gang_taklys" in dim
    dimmed_level = eng.state.rooms["gang"].channels["gang_taklys"].commanded_b

    # TV ends: gang re-evaluates to its corridor role (sofakrok still active =>
    # ADJACENT) and brightens away from the 5 % TV dim.
    restore = sets(eng.handle(TvChanged(False), at(1, 21, 30)))
    assert eng.state.rooms["gang"].role is Role.ADJACENT
    assert eng.state.rooms["gang"].channels["gang_taklys"].commanded_b > dimmed_level
    assert "gang_taklys" in restore


# --- §6.2: night path episode -------------------------------------------


def test_night_path_episode_and_expiry() -> None:
    """§6.2: sleep + night trigger lights the night-path set warm and dim, holds
    night_hold, then fades out (everything else stays off)."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 23, 0))
    eng.handle(SleepChanged(True), at(1, 23, 1))  # all off
    lit = eng.handle(NightTriggerFired(), at(1, 23, 2))
    s = sets(lit)
    assert eng.state.rooms["sofakrok"].role is Role.NIGHT_PATH
    assert "sofakrok_taklys" in s and "gang_taklys" in s
    assert s["kjokken_downlights"].ct == 2200  # forced warm (rule 6.2)
    # Non-night room (kontor) stays off.
    assert eng.state.rooms["kontor"].role is Role.OFF

    # Episode holds, then expires after night_hold (600 s) -> everything off.
    held = eng.handle(ReviewTick(), at(1, 23, 5))
    assert "sofakrok_taklys" not in offs(held)  # still lit within the hold
    expired = eng.handle(ReviewTick(), at(1, 23, 13))  # > 10 min after the trigger
    assert not eng.state.night_active
    assert "sofakrok_taklys" in offs(expired)


# --- §6.4: away turns the house off --------------------------------------


def test_away_turns_house_off() -> None:
    """§6.4: anyone_home False => every managed room OFF."""
    eng = _engine()
    eng.handle(PresenceChanged("kjokken", True), at(1, 20, 0))
    eng.handle(PresenceChanged("sofakrok", True), at(1, 20, 0))
    eng.handle(ReviewTick(), at(1, 20, 1))
    gone = eng.handle(HomeChanged(False), at(1, 20, 2))
    for room in ("kjokken", "sofakrok", "gang", "spisebord"):
        assert eng.state.rooms[room].role is Role.OFF
    assert "kjokken_taklys" in offs(gone)


# --- §7: master gain scaling + morning neutral drift --------------------


def test_master_gain_scales_and_drifts_neutral_at_morning() -> None:
    """§7.1/§7.3: 100 % gain raises output (by day, below the evening cap); the
    gain then drifts back to neutral by the next morning."""
    eng = _engine()  # daytime: no evening cap to swallow the boost
    neutral = sets(eng.handle(PresenceChanged("kontor", True), at(1, 18, 1)))["kontor_taklys"].level
    boosted = sets(eng.handle(MasterGainChanged(100.0), at(1, 18, 2)))["kontor_taklys"].level
    assert boosted > neutral  # gain > 1 raised the un-capped daytime output
    assert eng.state.master_pct == 100.0

    # Into the evening (resets the drift latch), then next morning full day.
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    eng.handle(ReviewTick(), at(1, 22, 1))
    eng.handle(SunElevationChanged(DAY_SUN), at(2, 8, 0))
    eng.handle(ReviewTick(), at(2, 12, 0))  # full day E == 0 -> neutral drift
    assert eng.state.master_pct == 50.0


def test_master_off_kills_indoor_keeps_outdoor() -> None:
    """§7.2: master light off fades indoor rooms off; balkong (outdoor) is exempt."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))  # balkong dusk-on
    eng.handle(PresenceChanged("sofakrok", True), at(1, 22, 1))
    eng.handle(ReviewTick(), at(1, 22, 2))
    off = eng.handle(MasterPowerChanged(False), at(1, 22, 3))
    assert "sofakrok_taklys" in offs(off)
    assert eng.state.rooms["balkong"].channels["balkong_taklys"].on  # outdoor survives


def test_away_keeps_outdoor_until_away_lighting_off() -> None:
    """§6.4: away keeps balkong's dusk backdrop; the away-lighting switch off
    darkens it too."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))  # balkong dusk-on
    eng.handle(ReviewTick(), at(1, 22, 1))
    eng.handle(HomeChanged(False), at(1, 22, 2))
    assert eng.state.rooms["balkong"].channels["balkong_taklys"].on  # presence sim
    dark = eng.handle(SetAwayLighting(False), at(1, 22, 3))
    assert "balkong_taklys" in offs(dark)


# --- §9: override latch + release ---------------------------------------


def test_override_latches_then_releases_on_away() -> None:
    """§9.1/§9.2: a foreign change latches the room; away releases the latch."""
    eng = _engine()
    eng.handle(PresenceChanged("kontor", True), at(1, 20, 0))
    eng.handle(ReviewTick(), at(1, 20, 1))

    # Someone spins the wall rotary to 90 %: latch + adopt, engine stops adjusting.
    eng.handle(ForeignChange("kontor_taklys", 0.9, wall_event=True), at(1, 20, 2))
    assert eng.state.rooms["kontor"].overridden
    quiet = eng.handle(ReviewTick(), at(1, 20, 3))
    assert "kontor_taklys" not in sets(quiet)  # not fighting the manual level
    assert eng.state.rooms["kontor"].channels["kontor_taklys"].commanded_b == 0.9

    # Leaving home releases the override and the hard-off wins.
    gone = eng.handle(HomeChanged(False), at(1, 20, 4))
    assert not eng.state.rooms["kontor"].overridden
    assert "kontor_taklys" in offs(gone)


def test_blind_room_dial_survives_hold_expiry() -> None:
    """§9.2: soverom incident regression — a manual dial in a blind door room
    after its trigger hold expired must latch and HOLD; the OFF-decayed role
    must not release it and counter the light to 0 at the next review. (Test
    reviews run minutes apart; live the counter came 6-16 s after the dial.)"""
    eng = _engine()
    # Door opens; the room lights, then the trigger hold (300 s) expires → off.
    eng.handle(TriggerFired("soverom"), at(1, 20, 0))
    eng.handle(ReviewTick(), at(1, 20, 6))
    assert eng.state.rooms["soverom"].role is Role.OFF

    # The occupant dials the wall to 60 % — the room is already OFF-worthy.
    eng.handle(ForeignChange("soverom_taklys", 0.6), at(1, 20, 7))
    assert eng.state.rooms["soverom"].overridden

    # Reviews keep coming; the latch must hold and the engine must not write.
    for minute in (8, 10, 20):
        plan = eng.handle(ReviewTick(), at(1, 20, minute))
        assert eng.state.rooms["soverom"].overridden
        assert "soverom_taklys" not in sets(plan)
        assert "soverom_taklys" not in offs(plan)
    assert eng.state.rooms["soverom"].channels["soverom_taklys"].commanded_b == 0.6

    # override_timeout (4 h) still ends it: released and driven to vacancy OFF.
    late = eng.handle(ReviewTick(), at(2, 0, 8))
    assert not eng.state.rooms["soverom"].overridden
    assert "soverom_taklys" in offs(late)


def test_override_suspended_by_night_path() -> None:
    """§9.1: night path suspends an override (safety path wins)."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 23, 0))
    eng.handle(PresenceChanged("sofakrok", True), at(1, 23, 0, 30))  # occupied, not OFF-worthy
    eng.handle(ForeignChange("sofakrok_taklys", 0.9), at(1, 23, 1))
    assert eng.state.rooms["sofakrok"].overridden
    eng.handle(SleepChanged(True), at(1, 23, 2))
    lit = eng.handle(NightTriggerFired(), at(1, 23, 3))
    assert eng.state.rooms["sofakrok"].role is Role.NIGHT_PATH
    assert "sofakrok_taklys" in sets(lit)  # night path drives it, override suspended


# --- §1.7: corridor via triggers ----------------------------------------


def test_corridor_trigger_pulses_and_expires() -> None:
    """§1.7: a trigger pulses the gang corridor ACTIVE for trigger_hold, then off."""
    eng = _engine()
    lit = eng.handle(TriggerFired("gang"), at(1, 23, 0))
    assert eng.state.rooms["gang"].role is Role.ACTIVE
    assert "gang_taklys" in sets(lit)
    off = eng.handle(ReviewTick(), at(1, 23, 6))  # > 300 s trigger_hold
    assert eng.state.rooms["gang"].role is Role.OFF
    assert "gang_taklys" in offs(off)


# --- §8.2: evening-cap smoothness ---------------------------------------


def test_evening_drift_never_exceeds_slew() -> None:
    """§8.2: pure circadian drift produces no output step above the slew bound.

    An occupied living room is driven through the whole evening descent; every
    emitted move must be sized so its flux-relative rate <= slew_step/slew_interval."""
    eng = _engine()
    eng.handle(PresenceChanged("sofakrok", True), at(1, 19, 30))
    tun = eng.tun
    bound = tun.slew_step / tun.slew_interval  # ACTIVE room => slew_step
    cs = eng.state.rooms["sofakrok"].channels["sofakrok_taklys"]
    seen = False
    for minute in range(0, 210, 5):  # 19:31 -> 23:01
        hh, mm = divmod(31 + minute, 60)
        prev_flux = cs.commanded_b**2  # the engine's ledger before this move
        cmds = eng.handle(ReviewTick(), at(1, 19 + hh, mm))
        for c in cmds:
            if isinstance(c, SetChannel) and c.channel_id == "sofakrok_taklys":
                rate = abs(c.level**2 - prev_flux) / c.ramp_seconds
                assert rate <= bound + 1e-9, f"step rate {rate} exceeds slew bound {bound}"
                seen = True
    assert seen  # the drift actually moved the light


# --- §11: seeding & startup ----------------------------------------------


def test_seed_adopts_levels_without_flash() -> None:
    """§11.1: existing levels are adopted as ledger baselines; startup grace
    suppresses convergence writes so a restart never flashes the lights."""
    snap = InitialSnapshot(
        sun_elevation=DAY_SUN,
        occupancy={"kjokken": True},
        channels={"kjokken_taklys": (0.45, 2700), "kjokken_downlights": (0.45, 3000)},
    )
    eng = Engine(apartment(), snap)
    # First event within the 30 s grace: no channel writes (no flash).
    boot = eng.handle(ReviewTick(), at(1, 18, 0, 10))
    assert not any(isinstance(c, SetChannel) for c in boot)
    assert eng.state.rooms["kjokken"].channels["kjokken_taklys"].commanded_b == 0.45
    # After the grace, the engine converges divergent channels (benke was not
    # in the seed, so it is driven to its ACTIVE goal).
    after = eng.handle(ReviewTick(), at(1, 18, 1))
    assert any(isinstance(c, SetChannel) for c in after)


def test_observe_only_when_disabled() -> None:
    """§10: master enable off => observe only, no channel commands, FSM still runs."""
    eng = _engine(InitialSnapshot(sun_elevation=DAY_SUN, enabled=False))
    cmds = eng.handle(PresenceChanged("kjokken", True), at(1, 20, 0))
    assert eng.state.rooms["kjokken"].self_active  # FSM keeps tracking
    assert not any(isinstance(c, SetChannel) for c in cmds)  # but no writes
    # Re-enabling resumes writing.
    on = eng.handle(SetEnabled(True), at(1, 20, 1))
    assert any(isinstance(c, SetChannel) for c in on)


def test_sleep_off_restores_and_relaxes_gain() -> None:
    """§6.1/§7.3: sleep-off re-enables normal evaluation and drifts gain neutral."""
    eng = _engine()
    eng.handle(MasterGainChanged(90.0), at(1, 23, 0))
    eng.handle(SleepChanged(True), at(1, 23, 1))
    eng.handle(NightTriggerFired(), at(1, 23, 2))
    assert eng.state.night_active
    eng.handle(SleepChanged(False), at(1, 23, 3))
    assert not eng.state.night_active  # night episode cleared
    assert eng.state.master_pct == 50.0  # neutral drift on the sleep-off edge


def test_activity_sets_episode_peak_and_scales_hold() -> None:
    """§1.3: an ACTIVE room's activity feeds the episode peak used for holds."""
    eng = _engine()
    eng.handle(PresenceChanged("kjokken", True), at(1, 20, 0))
    eng.handle(ActivityChanged("kjokken", Activity.SETTLED), at(1, 20, 1))
    assert eng.state.rooms["kjokken"].episode_peak is Activity.SETTLED


def test_vacation_acts_as_away() -> None:
    """§6.6: vacation on applies the away rules regardless of presence."""
    eng = _engine()
    eng.handle(PresenceChanged("kjokken", True), at(1, 20, 0))
    eng.handle(ReviewTick(), at(1, 20, 1))
    gone = eng.handle(VacationChanged(True), at(1, 20, 2))
    assert eng.state.rooms["kjokken"].role is Role.OFF
    assert "kjokken_taklys" in offs(gone)


def test_occupational_switch_raises_balkong() -> None:
    """§6.5: the occupational switch lifts the outdoor room to the sitting level."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))  # dusk-on
    ambient = diag(eng.handle(ReviewTick(), at(1, 22, 1)), "balkong").target_output
    sitting = diag(
        eng.handle(OccupationalChanged("balkong", True), at(1, 22, 2)), "balkong"
    ).target_output
    assert sitting > ambient


def test_lux_report_and_unknown_channel_are_harmless() -> None:
    """§3 seam / §10.4: a lux report is ignored open-loop; unknown ids are no-ops."""
    eng = _engine()
    eng.handle(LuxReport("kjokken", 42.0), at(1, 20, 0))  # ignored, no crash
    eng.handle(ForeignChange("does_not_exist", 0.5), at(1, 20, 1))  # unknown channel
    assert not eng.state.rooms["kjokken"].overridden


def test_read_surface() -> None:
    eng = _engine()
    assert eng.circadian_factor(at(1, 22, 30)) == 1.0
    assert eng.room_state("gang").role is Role.OFF


# --- review-round-1 fixes ------------------------------------------------


def test_boot_midday_preserves_restored_gain() -> None:
    """§7.3 (F1): booting at midday (E==0) must not clobber a restored gain;
    neutral drift is edge-triggered and still fires on the next morning edge."""
    eng = Engine(apartment(), InitialSnapshot(sun_elevation=DAY_SUN, master_pct=90.0))
    eng.handle(ReviewTick(), at(1, 12, 0))  # E == 0 at boot
    assert eng.state.master_pct == 90.0  # not reset to neutral
    eng.handle(ReviewTick(), at(1, 12, 5))
    assert eng.state.master_pct == 90.0
    # A genuine morning edge (evening E>0 -> next-day E==0) drifts to neutral.
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    eng.handle(ReviewTick(), at(1, 22, 1))
    eng.handle(SunElevationChanged(DAY_SUN), at(2, 12, 0))
    assert eng.state.master_pct == 50.0


def test_overridden_room_schedules_vacancy_release() -> None:
    """§9.2 (F2): a held override still schedules the vacancy-hold expiry, so an
    OFF-worthy room releases at hold end with no other events (not after 4 h)."""
    eng = _engine()
    eng.handle(PresenceChanged("kontor", True), at(1, 20, 0))  # vacancy:off room, ACTIVE
    eng.handle(ForeignChange("kontor_taklys", 0.9), at(1, 20, 1))
    assert eng.state.rooms["kontor"].overridden
    out = eng.handle(PresenceChanged("kontor", False), at(1, 20, 2))  # vacate: 90 s hold
    rev = review(out)
    assert rev == at(1, 20, 3, 30)  # 90 s hold from 20:02, not the 4 h timeout
    eng.handle(ReviewTick(), rev)
    assert not eng.state.rooms["kontor"].overridden  # released at hold expiry


def test_plateau_schedules_next_clock_boundary() -> None:
    """§2.3 (F3): at a circadian plateau the engine schedules the next clock-ramp
    boundary itself, so it can start the 20:00 / 06:00 ramps without sun events."""
    day = Engine(apartment(), InitialSnapshot(sun_elevation=DAY_SUN))
    day.handle(ReviewTick(), at(1, 16, 0))  # boot (grace)
    out = day.handle(ReviewTick(), at(1, 16, 1))  # E == 0 plateau, past grace
    assert review(out) == at(1, 20, 0)  # evening_start

    night = Engine(apartment(), InitialSnapshot(sleep=True))  # sun unknown -> E_clock rules
    night.handle(ReviewTick(), at(1, 2, 0))  # boot (grace)
    out2 = night.handle(ReviewTick(), at(1, 2, 1))  # E == 1 plateau, past grace
    assert review(out2) == at(1, 6, 0)  # morning_start


def test_boundary_instant_wake_enters_ramp() -> None:
    """§2.3 (N1): a review landing in the boundary minute itself (where the
    clock ramp still reads as plateau) must reschedule one minute inside the
    ramp — not skip to the boundary half a day away."""
    day = Engine(apartment(), InitialSnapshot(sun_elevation=DAY_SUN))
    day.handle(ReviewTick(), at(1, 16, 0))  # boot (grace)
    on_boundary = day.handle(ReviewTick(), at(1, 20, 0))  # E still 0.0 here
    assert review(on_boundary) == at(1, 20, 1)  # one minute into the ramp
    in_ramp = day.handle(ReviewTick(), at(1, 20, 1))  # now 0 < E < 1
    assert review(in_ramp) == at(1, 20, 6)  # circadian_tick (300 s) cadence

    night = Engine(apartment(), InitialSnapshot(sleep=True))  # E_clock rules
    night.handle(ReviewTick(), at(1, 2, 0))  # boot (grace)
    on_morning = night.handle(ReviewTick(), at(1, 6, 0, 30))  # mid boundary minute
    assert review(on_morning) == at(1, 6, 1)


def test_night_path_expiry_uses_night_fade() -> None:
    """§6.2 (F6): the night episode fades out over night_fade (10 s), not the
    sleep_fade (4 s) used when sleep first engages."""
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 23, 0))
    eng.handle(SleepChanged(True), at(1, 23, 1))
    eng.handle(NightTriggerFired(), at(1, 23, 2))  # sofakrok lit at night output
    expired = eng.handle(ReviewTick(), at(1, 23, 13))  # > night_hold after the trigger
    fades = [
        c for c in expired if isinstance(c, TurnOffChannel) and c.channel_id == "sofakrok_taklys"
    ]
    assert fades and fades[0].ramp_seconds == 10.0


def test_role_arbitration_through_engine() -> None:
    """§1.2 (F9): the engine resolves competing roles — NIGHT_PATH > TV > ACTIVE."""
    # Night beats a room that is simultaneously occupied and TV-eligible.
    eng = _engine()
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 23, 0))
    eng.handle(PresenceChanged("sofakrok", True), at(1, 23, 0, 30))  # ACTIVE
    eng.handle(TvChanged(True), at(1, 23, 0, 45))  # TV-eligible
    eng.handle(SleepChanged(True), at(1, 23, 1))
    eng.handle(NightTriggerFired(), at(1, 23, 2))
    assert eng.state.rooms["sofakrok"].role is Role.NIGHT_PATH

    # Awake: TV beats plain ACTIVE presence.
    eng2 = _engine()
    eng2.handle(PresenceChanged("spisebord", True), at(1, 20, 0))
    eng2.handle(TvChanged(True), at(1, 20, 1))
    assert eng2.state.rooms["spisebord"].role is Role.TV


# --- §4.5: per-channel response mapping, end to end ---------------------


def _response_room() -> EngineConfig:
    """A kjøkken-like room with a benke boost channel that carries the normalized
    legacy response mapping (slope 0.8, offset -0.5). Dedicated config so the
    shared apartment fixture stays untouched (no perturbation of other tests)."""
    return EngineConfig(
        rooms=(
            RoomConfig(
                room_id="respkj",
                channels=(
                    ChannelConfig("respkj_taklys", band=Band.PRIMARY, fixed_ct=2700),
                    ChannelConfig(
                        "respkj_benke",
                        band=Band.BOOST,
                        fixed_ct=2700,
                        response_slope=0.8,
                        response_offset=-0.5,
                    ),
                ),
                profile=Profile(
                    vacancy=Vacancy.DIM,
                    out_active_day={Band.PRIMARY: 1.0, Band.BOOST: 1.0},
                    out_active_evening={Band.PRIMARY: 0.3, Band.BOOST: 0.6},
                    out_background={Band.PRIMARY: 0.06},
                    evening_output_cap=1.0,
                ),
            ),
        )
    )


def test_response_mapping_end_to_end_benke() -> None:
    """§4.5: at daytime full ACTIVE the benke boost channel is commanded ~0.3 (the
    mapping 0.8*1.0 - 0.5) while the primary sits at full; the evening lockout
    then zeroes benke before the mapping could resurrect it."""
    eng = Engine(_response_room(), None)
    eng.handle(SunElevationChanged(DAY_SUN), at(1, 18, 0))
    day = sets(eng.handle(PresenceChanged("respkj", True), at(1, 18, 1)))
    # Primary at full (flux-space quantization lands it a hair under 1.0).
    assert isclose(day["respkj_taklys"].level, 1.0, abs_tol=0.01)
    # Benke reshaped to 0.3 — no longer blasting equal to the primary.
    assert isclose(day["respkj_benke"].level, 0.3, abs_tol=1e-6)

    # Evening (sun down): the boost band locks out, benke goes off.
    cmds = eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    assert "respkj_benke" in offs(cmds)
    assert not eng.state.rooms["respkj"].channels["respkj_benke"].on


# --- §1.10: occupational presence (balcony-sitting incident) --------------


def _balcony_apartment() -> EngineConfig:
    from custom_components.light_conductor.core.model import RoomShape

    stue = RoomConfig(
        room_id="stue",
        channels=(ChannelConfig("stue_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            vacancy=Vacancy.DIM,
            out_active_day={Band.PRIMARY: 0.7},
            out_active_evening={Band.PRIMARY: 0.3},
            out_background={Band.PRIMARY: 0.06},
            evening_output_cap=0.3,
        ),
        neighbours=("balkong",),
        living_group=True,
    )
    balkong = RoomConfig(
        room_id="balkong",
        channels=(ChannelConfig("balkong_taklys", band=Band.PRIMARY, fixed_ct=None),),
        profile=Profile(
            out_active_evening={Band.PRIMARY: 0.5},
            out_background={Band.PRIMARY: 0.2},
        ),
        shape=RoomShape.OUTDOOR,
        living_group=True,
        presence_capable=False,
    )
    return EngineConfig(rooms=(stue, balkong))


def test_occupational_balcony_keeps_interior_lit() -> None:
    """§1.10 regression (balcony-sitting incident): while the occupational
    switch is on, the balcony is self-active — the neighbouring living room
    holds ADJACENT instead of decaying to OFF 15 minutes after the occupant
    stepped outside; flipping the switch off starts the normal living_memory
    decay to OFF."""
    eng = Engine(_balcony_apartment(), InitialSnapshot(sun_elevation=NIGHT_SUN))
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    lit = eng.handle(OccupationalChanged("balkong", True), at(1, 22, 1))
    assert eng.state.rooms["balkong"].self_active
    assert eng.state.rooms["stue"].role is Role.ADJACENT
    assert "stue_taklys" in sets(lit)

    # Half an hour later (double the old 15-min decay) it STILL glows.
    later = eng.handle(ReviewTick(), at(1, 22, 31))
    assert eng.state.rooms["stue"].role is Role.ADJACENT
    assert "stue_taklys" not in offs(later)

    # Switch off: adjacency drops, living_memory (900 s) holds BACKGROUND,
    # then the room decays to OFF.
    eng.handle(OccupationalChanged("balkong", False), at(1, 22, 40))
    assert eng.state.rooms["stue"].role is Role.BACKGROUND
    gone = eng.handle(ReviewTick(), at(1, 23, 0))
    assert eng.state.rooms["stue"].role is Role.OFF
    assert "stue_taklys" in offs(gone)


def test_occupational_balcony_does_not_defeat_away() -> None:
    """§1.10/§6.4: away hard-off still wins over occupational self-activity."""
    eng = Engine(_balcony_apartment(), InitialSnapshot(sun_elevation=NIGHT_SUN))
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    eng.handle(OccupationalChanged("balkong", True), at(1, 22, 1))
    gone = eng.handle(HomeChanged(False), at(1, 22, 2))
    assert eng.state.rooms["stue"].role is Role.OFF
    assert "stue_taklys" in offs(gone)


def test_occupational_balcony_holds_manual_override_inside() -> None:
    """§1.10/§9.2: while the balcony is in use the living room is BACKGROUND —
    not OFF-worthy — so a manual dial inside STICKS instead of releasing on
    vacancy (the incident's second failure mode)."""
    eng = Engine(_balcony_apartment(), InitialSnapshot(sun_elevation=NIGHT_SUN))
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    eng.handle(OccupationalChanged("balkong", True), at(1, 22, 1))

    eng.handle(ForeignChange("stue_taklys", 0.8), at(1, 22, 5))
    assert eng.state.rooms["stue"].overridden
    plan = eng.handle(ReviewTick(), at(1, 22, 10))
    assert eng.state.rooms["stue"].overridden  # latch holds while balcony in use
    assert "stue_taklys" not in offs(plan)
