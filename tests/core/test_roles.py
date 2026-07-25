"""§1: room activity FSM — holds, scaling, demotion, blind, corridors."""

from __future__ import annotations

from custom_components.light_conductor.core import roles
from custom_components.light_conductor.core.model import (
    Activity,
    Role,
    RoomShape,
    RoomState,
    Vacancy,
)
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import at

TUN = Tunables()


def _step(rs: RoomState, now, shape=RoomShape.PRESENCE, hold=None) -> None:
    roles.step(rs, now, TUN, shape, hold)


def test_occupancy_hold_keeps_active() -> None:
    """§1.3: occupancy loss holds ACTIVE for hold_seconds, then leaves."""
    rs = RoomState()
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    _step(rs, at(1, 12, 0))
    assert rs.self_active
    roles.ingest_presence(rs, False, None, at(1, 12, 1))
    _step(rs, at(1, 12, 1))
    assert rs.self_active  # within the 120 s hold
    _step(rs, at(1, 12, 2, 59))
    assert rs.self_active  # hold runs 120 s from the 12:01 loss -> 12:03:01
    _step(rs, at(1, 12, 3, 5))
    assert not rs.self_active  # hold expired


def test_reoccupancy_during_hold_is_seamless() -> None:
    """§1.3: re-occupancy during the hold returns to ACTIVE with no change."""
    rs = RoomState()
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    _step(rs, at(1, 12, 0))
    roles.ingest_presence(rs, False, None, at(1, 12, 1))
    _step(rs, at(1, 12, 1))
    roles.ingest_presence(rs, True, None, at(1, 12, 1, 30))
    _step(rs, at(1, 12, 1, 30))
    assert rs.self_active and rs.vacancy_hold_until is None


def test_settled_scales_hold_four_times() -> None:
    """§1.3: a settled episode holds x4 (hold_settled_scale)."""
    rs = RoomState()
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    _step(rs, at(1, 12, 0))
    roles.ingest_activity(rs, Activity.SETTLED)
    roles.ingest_presence(rs, False, None, at(1, 12, 1))
    _step(rs, at(1, 12, 1))
    # 120 s * 4 = 480 s hold: still ACTIVE well past the plain hold.
    _step(rs, at(1, 12, 5))
    assert rs.self_active
    _step(rs, at(1, 12, 9, 5))
    assert not rs.self_active


def test_passing_scales_hold_down() -> None:
    """§1.3: a passing episode barely holds (hold_passing_scale 0.3)."""
    rs = RoomState()
    roles.ingest_activity(rs, Activity.PASSING)
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    _step(rs, at(1, 12, 0))
    roles.ingest_presence(rs, False, None, at(1, 12, 1))
    _step(rs, at(1, 12, 1))
    _step(rs, at(1, 12, 1, 40))  # 120 * 0.3 = 36 s hold, expired
    assert not rs.self_active


def test_fallback_used_only_when_primary_blind() -> None:
    """§1.1: fallback is OR-ed, and stands alone once the primary is blind."""
    rs = RoomState()
    roles.ingest_presence(rs, False, True, at(1, 12, 0))
    assert roles.occupancy(rs, at(1, 12, 0), TUN) is True  # OR-ed


def test_blind_holds_last_definitive_then_falls_back() -> None:
    """§1.1: an unavailable primary holds its last value for presence_blind_hold."""
    rs = RoomState()
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    roles.ingest_presence(rs, None, False, at(1, 12, 1))  # primary blind, fallback says empty
    assert roles.occupancy(rs, at(1, 12, 1), TUN) is True  # holds last definitive
    assert roles.occupancy(rs, at(1, 12, 4), TUN) is False  # past blind hold -> fallback


def _blind_role(rs) -> Role:
    return roles.base_role(rs, RoomShape.PRESENCE, Vacancy.DIM, False, False, False)


def test_fully_blind_active_room_demotes_gradually() -> None:
    """§1.8: fully blind ACTIVE room freezes, then steps down, never to OFF at once.

    Two phases stack: the primary first holds its last definitive value for
    presence_blind_hold (§1.1), then — still blind, no fallback — §1.8 freezes
    the ACTIVE role for another presence_blind_hold before stepping down.
    """
    rs = RoomState()
    roles.ingest_presence(rs, True, None, at(1, 12, 0))
    _step(rs, at(1, 12, 0))
    roles.ingest_presence(rs, None, None, at(1, 12, 1))  # fully blind, no fallback
    _step(rs, at(1, 12, 2))  # within the §1.1 last-value hold: still ACTIVE
    assert _blind_role(rs) is Role.ACTIVE
    _step(rs, at(1, 12, 4))  # §1.1 hold gone; §1.8 freeze begins here
    assert _blind_role(rs) is Role.ACTIVE
    _step(rs, at(1, 12, 6, 5))  # one freeze later -> one tier down
    assert _blind_role(rs) is Role.ADJACENT
    _step(rs, at(1, 12, 8, 10))  # another tier
    assert _blind_role(rs) is Role.BACKGROUND


def test_vacancy_off_room_goes_straight_off() -> None:
    """§1.4: a vacancy:off room (kontor) demotes to OFF, ignoring adjacency."""
    role = roles.demoted_role(RoomShape.PRESENCE, Vacancy.OFF, True, True, True)
    assert role is Role.OFF


def test_vacancy_dim_floors_at_background() -> None:
    """§1.4/1.6: a vacancy:dim room never drops below BACKGROUND while living active."""
    role = roles.demoted_role(RoomShape.PRESENCE, Vacancy.DIM, False, True, False)
    assert role is Role.BACKGROUND


def test_corridor_role_derivation() -> None:
    """§1.7: corridor = ADJACENT w/ active neighbour, BACKGROUND in evening, else OFF."""
    assert roles.demoted_role(RoomShape.CORRIDOR, Vacancy.DIM, True, False, False) is Role.ADJACENT
    assert roles.demoted_role(RoomShape.CORRIDOR, Vacancy.DIM, False, True, True) is Role.BACKGROUND
    assert roles.demoted_role(RoomShape.CORRIDOR, Vacancy.DIM, False, True, False) is Role.OFF


def test_corridor_trigger_pulses_active() -> None:
    """§1.7: a trigger pulses a corridor ACTIVE for trigger_hold."""
    rs = RoomState()
    roles.ingest_trigger(rs, False, at(1, 12, 0), TUN)
    _step(rs, at(1, 12, 0), shape=RoomShape.CORRIDOR)
    assert rs.self_active
    _step(rs, at(1, 12, 4, 0), shape=RoomShape.CORRIDOR)
    assert rs.self_active  # within 300 s
    _step(rs, at(1, 12, 5, 5), shape=RoomShape.CORRIDOR)
    assert not rs.self_active


def test_door_close_edge_shortens_hold() -> None:
    """§1.9: a closing edge shortens the hold to door_close_hold."""
    rs = RoomState()
    roles.ingest_trigger(rs, True, at(1, 23, 0), TUN)  # closing
    _step(rs, at(1, 23, 0), shape=RoomShape.DOOR)
    assert rs.self_active
    _step(rs, at(1, 23, 0, 20), shape=RoomShape.DOOR)  # 15 s hold expired
    assert not rs.self_active


def test_presence_room_trigger_keeps_active() -> None:
    """§1.9: a trigger holds a presence room ACTIVE even without occupancy."""
    rs = RoomState()
    roles.ingest_trigger(rs, False, at(1, 12, 0), TUN)
    roles.ingest_presence(rs, False, None, at(1, 12, 0))
    _step(rs, at(1, 12, 1))  # occupancy False, but trigger still holds
    assert rs.self_active


def test_priority_ordering() -> None:
    """§1.2: NIGHT_PATH > TV > ACTIVE > ADJACENT > BACKGROUND > OFF."""
    assert roles.took_priority(Role.NIGHT_PATH, Role.TV) is Role.NIGHT_PATH
    assert roles.took_priority(Role.TV, Role.ACTIVE) is Role.TV
    assert roles.took_priority(Role.ADJACENT, Role.BACKGROUND) is Role.ADJACENT
    assert roles.lower(Role.ACTIVE, 3) is Role.OFF
    assert roles.lower(Role.TV, 1) is Role.TV  # not on the ladder
