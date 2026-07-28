"""Shadow-audit regressions for 0.1.0-beta.4.

Two live-session defects:
 1. Post-restart role stickiness — a room seeded from an *unavailable* presence
    entity stayed OFF after the entity recovered, because the recovery landed in
    the setup gap (between snapshot and subscription) and fired no event.
 2. Outdoor daytime background — an outdoor room lingered in its dusk background
    after E fell below the threshold, because at an E plateau the engine
    scheduled no near-term re-review.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.light_conductor.const import DOMAIN
from custom_components.light_conductor.controller import Controller
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ReviewTick,
    SleepChanged,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    InitialSnapshot,
    Profile,
    Role,
    RoomConfig,
    RoomShape,
    Vacancy,
)
from custom_components.light_conductor.core.plan import ScheduleReview

from .adapter import options, room, set_light

# --- bug 1: post-restart role stickiness --------------------------------


async def test_presence_recovery_in_setup_gap_reaches_active(hass: HomeAssistant) -> None:
    """A room seeded from an unavailable primary must reach ACTIVE once the
    entity recovers to on — even if that recovery lands in the setup gap."""
    set_light(hass, "light.k", transition=True)
    hass.states.async_set("binary_sensor.pk", "unavailable")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={},
        options=options([room("k", ["light.k"], presence="binary_sensor.pk")]),
    )
    entry.add_to_hass(hass)
    async_mock_service(hass, "light", "turn_on")  # room goes ACTIVE ⇒ it writes
    async_mock_service(hass, "light", "turn_off")
    controller = Controller(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = controller

    # Snapshot is taken while the primary is unavailable (seeds occupancy None)...
    snapshot = controller.build_snapshot()
    # ...then the primary recovers to on *before* subscriptions are armed (the
    # gap): no state-change event will fire for it.
    hass.states.async_set("binary_sensor.pk", "on")
    await controller.async_start(snapshot)
    await hass.async_block_till_done()

    assert controller.engine.room_state("k").role == Role.ACTIVE
    await controller.async_stop()  # cancel the review timer (manual lifecycle)


# --- bug 2: outdoor daytime background lingering ------------------------


def _outdoor_engine() -> Engine:
    prof = Profile(
        vacancy=Vacancy.DIM,
        out_active_evening={Band.PRIMARY: 0.6},
        out_background={Band.PRIMARY: 0.2},
    )
    cfg = EngineConfig(
        rooms=(
            RoomConfig(
                room_id="balk",
                channels=(ChannelConfig("l", fixed_ct=2700),),
                profile=prof,
                shape=RoomShape.OUTDOOR,
            ),
        )
    )
    return Engine(cfg, InitialSnapshot(sun_elevation=30.0))


def test_outdoor_off_at_sleep_off_in_daylight() -> None:
    """§6.5: sleep turning off in daylight (E = 0) leaves an outdoor room OFF."""
    eng = _outdoor_engine()
    base = datetime(2026, 7, 1, 7, 39, 0)
    eng.handle(SunElevationChanged(30.0), base)  # sun high ⇒ E = 0
    eng.handle(SleepChanged(True), base + timedelta(seconds=10))
    eng.handle(SleepChanged(False), base + timedelta(seconds=40))
    assert eng.state.rooms["balk"].role is Role.OFF


def test_outdoor_on_self_schedules_a_near_review() -> None:
    """Shadow audit: while an outdoor room is ON at an E plateau it schedules a
    circadian-cadence re-review, so it does not linger when E later descends."""
    eng = _outdoor_engine()
    base = datetime(2026, 7, 1, 22, 0, 0)
    eng.handle(SunElevationChanged(-8.0), base)  # dusk ⇒ E = 1 ⇒ background
    assert eng.state.rooms["balk"].role is Role.BACKGROUND

    out = eng.handle(ReviewTick(), base + timedelta(seconds=100))  # past startup grace
    now = base + timedelta(seconds=100)
    ahead = [(c.at - now).total_seconds() for c in out if isinstance(c, ScheduleReview)]
    assert ahead and min(ahead) <= eng.tun.circadian_tick + 1e-6


def test_outdoor_turns_off_promptly_when_daylight_returns() -> None:
    """The scheduled re-review turns the room off once E falls below threshold."""
    eng = _outdoor_engine()
    base = datetime(2026, 7, 1, 22, 0, 0)
    eng.handle(SunElevationChanged(-8.0), base)
    assert eng.state.rooms["balk"].role is Role.BACKGROUND
    # Late morning (E → 0: sun high AND past the morning clock ramp). The next
    # self-scheduled review re-evaluates and turns the room off.
    eng.handle(SunElevationChanged(30.0), base + timedelta(hours=10))
    eng.handle(ReviewTick(), base + timedelta(hours=10, seconds=1))
    assert eng.state.rooms["balk"].role is Role.OFF
