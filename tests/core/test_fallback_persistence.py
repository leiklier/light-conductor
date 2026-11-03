"""§1.1 (D31): fallback occupancy must persist before it counts.

The raw mmWave fallbacks are OR-ed with presence-conductor's room occupancy
and dominate it; their isolated blips each minted a full ACTIVE episode (and,
in the kitchen, the bench strip). While the primary is available, a fallback
now counts only once it has been on for ``fallback_min_on`` (dropouts up to
``FALLBACK_BRIDGE`` bridged); a blind primary keeps today's raw fallback.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from custom_components.light_conductor.core import roles
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    PresenceChanged,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import EngineConfig, RoomState
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import apartment, at, review

TUN = Tunables()
MIN_ON = TUN.fallback_min_on


def _engine(config: EngineConfig | None = None) -> tuple[Engine, object]:
    eng = Engine(config or apartment())
    t = at(1, 21, 0)
    eng.handle(SunElevationChanged(-8.0), t)
    t += timedelta(seconds=31)
    eng.handle(PresenceChanged("kontor", False, False), t)  # primary live and empty
    return eng, t


def test_a_blip_shorter_than_min_on_never_activates_the_room() -> None:
    """D31: a 9 s raw-only blip (the live median) mints no ACTIVE episode."""
    eng, t = _engine()
    eng.handle(PresenceChanged("kontor", False, True), t + timedelta(seconds=1))
    eng.handle(PresenceChanged("kontor", False, False), t + timedelta(seconds=10))
    eng.handle(ReviewTick(), t + timedelta(seconds=30))
    rs = eng.state.rooms["kontor"]
    assert not rs.self_active and rs.vacancy_hold_until is None
    assert not rs.channels["kontor_taklys"].on


def test_a_persisting_spell_counts_at_min_on_via_a_self_scheduled_review() -> None:
    """D31: the engine schedules the persistence boundary itself — no other
    event arrives, yet the room goes ACTIVE exactly at on + fallback_min_on."""
    eng, t = _engine()
    on = t + timedelta(seconds=1)
    cmds = eng.handle(PresenceChanged("kontor", False, True), on)
    assert not eng.state.rooms["kontor"].self_active
    assert review(cmds) == on + timedelta(seconds=MIN_ON)
    eng.handle(ReviewTick(), on + timedelta(seconds=MIN_ON - 0.5))
    assert not eng.state.rooms["kontor"].self_active
    eng.handle(ReviewTick(), on + timedelta(seconds=MIN_ON))
    assert eng.state.rooms["kontor"].self_active
    assert eng.state.rooms["kontor"].channels["kontor_taklys"].on


def test_sub_bridge_dropouts_do_not_restart_the_clock() -> None:
    """D31: the raw sensors re-trigger through near-instant off->on toggles
    (half of all kjøkken/sofakrok gaps are < 50 ms); those continue the spell."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)
    for k in range(1, 4):  # off for 0.2 s every 4 s
        roles.ingest_presence(rs, False, False, t + timedelta(seconds=4 * k), TUN)
        roles.ingest_presence(rs, False, True, t + timedelta(seconds=4 * k + 0.2), TUN)
    assert roles.occupancy(rs, t + timedelta(seconds=MIN_ON), TUN) is True


def test_a_real_gap_restarts_the_clock() -> None:
    """D31: a dropout longer than FALLBACK_BRIDGE starts a new spell."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)
    roles.ingest_presence(rs, False, False, t + timedelta(seconds=5), TUN)
    back = t + timedelta(seconds=5 + roles.FALLBACK_BRIDGE + 1)
    roles.ingest_presence(rs, False, True, back, TUN)
    assert roles.occupancy(rs, t + timedelta(seconds=MIN_ON), TUN) is False
    assert roles.occupancy(rs, back + timedelta(seconds=MIN_ON), TUN) is True


def test_a_long_stationary_sit_still_counts() -> None:
    """D31: the genuine stationary presence presence-conductor misses (a
    dining-table sit) is kept — after fallback_min_on it holds the room for as
    long as the raw sensor does, chatter included."""
    eng, t = _engine()
    on = t + timedelta(seconds=1)
    eng.handle(PresenceChanged("kontor", False, True), on)
    for k in range(1, 120):  # 20 minutes, chattering every 10 s
        s = on + timedelta(seconds=10 * k)
        eng.handle(PresenceChanged("kontor", False, False), s)
        eng.handle(PresenceChanged("kontor", False, True), s + timedelta(seconds=0.05))
        if k * 10 >= MIN_ON + 1:
            assert eng.state.rooms["kontor"].self_active, k


def test_a_pir_pulse_is_not_starved() -> None:
    """D31: the kontor PIR's on-pulses are >= 75 s (its hold time; p5 75 s over
    247 pulses) — persistence delays its evidence by fallback_min_on but never
    filters it out."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)
    assert roles.occupancy(rs, t + timedelta(seconds=MIN_ON), TUN) is True
    assert roles.occupancy(rs, t + timedelta(seconds=74), TUN) is True


def test_primary_presence_is_never_delayed() -> None:
    """D31 filters the fallback only — presence-conductor's own True is instant."""
    eng, t = _engine()
    eng.handle(PresenceChanged("kontor", True, False), t + timedelta(seconds=1))
    assert eng.state.rooms["kontor"].self_active


def test_a_blind_primary_keeps_the_raw_fallback() -> None:
    """§1.1: with the primary unavailable (past presence_blind_hold) the raw
    fallback stands alone, unfiltered — there is nothing better to read."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, False, t, TUN)
    roles.ingest_presence(rs, None, False, t + timedelta(seconds=1), TUN)
    blind = t + timedelta(seconds=1 + TUN.presence_blind_hold + 1)
    roles.ingest_presence(rs, None, True, blind, TUN)
    assert roles.occupancy(rs, blind, TUN) is True


def test_a_room_override_of_zero_is_the_raw_or() -> None:
    """D31: a per-room fallback_min_on of 0 keeps beta.19's raw OR for that
    room only (kontor and spisebord never fired while the house was empty)."""
    apt = apartment()
    rooms = tuple(
        replace(r, fallback_min_on=0.0) if r.room_id == "kontor" else r for r in apt.rooms
    )
    eng, t = _engine(EngineConfig(rooms=rooms))
    eng.handle(PresenceChanged("kontor", False, True), t + timedelta(seconds=1))
    assert eng.state.rooms["kontor"].self_active
    eng.handle(PresenceChanged("kjokken", False, True), t + timedelta(seconds=2))
    assert not eng.state.rooms["kjokken"].self_active  # the tunable still applies here


def test_zero_tunable_disables_the_filter() -> None:
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)
    assert roles.occupancy(rs, t, replace(TUN, fallback_min_on=0.0)) is True


def test_going_blind_holds_a_persisted_fallback_as_occupied() -> None:
    """§1.1/§1.8: a fallback spell that persisted since the last report is part
    of the last definitive value the blind hold freezes."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)  # not yet persisted here
    blind = t + timedelta(seconds=MIN_ON + 5)
    roles.ingest_presence(rs, None, True, blind, TUN)
    assert roles.occupancy(rs, blind + timedelta(seconds=1), TUN) is True
