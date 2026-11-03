"""§1.7/§1.9 (D34): only a DOOR-shaped room shortens its hold on a closing edge.

The plan is to add the front-door contact as a gang (corridor) trigger;
closing the front door behind you must not darken the hallway 15 s later.
"""

from __future__ import annotations

from datetime import timedelta

from custom_components.light_conductor.core import roles
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ReviewTick,
    SunElevationChanged,
    TriggerFired,
)
from custom_components.light_conductor.core.model import Role, RoomShape, RoomState
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import apartment, at

TUN = Tunables()


def _engine() -> Engine:
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(-8.0), at(1, 21, 0))
    return eng


def test_a_corridor_ignores_the_closing_edge() -> None:
    """D34: the front door opens (gang pulses ACTIVE for trigger_hold) and closes
    5 s later — the hallway stays lit for the full hold (beta.19: 15 s)."""
    eng = _engine()
    opened = at(1, 21, 1)
    eng.handle(TriggerFired("gang"), opened)
    eng.handle(TriggerFired("gang", closing=True), opened + timedelta(seconds=5))
    rs = eng.state.rooms["gang"]
    assert rs.trigger_hold_until == opened + timedelta(seconds=TUN.trigger_hold)
    eng.handle(ReviewTick(), opened + timedelta(seconds=60))
    assert rs.role is Role.ACTIVE
    eng.handle(ReviewTick(), opened + timedelta(seconds=TUN.trigger_hold))
    assert rs.role is not Role.ACTIVE


def test_a_corridor_closing_edge_alone_lights_nothing() -> None:
    rs = RoomState()
    roles.ingest_trigger(rs, True, at(1, 12, 0), TUN, RoomShape.CORRIDOR)
    assert rs.trigger_hold_until is None


def test_a_door_room_still_shortens_on_close() -> None:
    """§1.9: soverom's door closing shortens the hold to door_close_hold."""
    eng = _engine()
    opened = at(1, 21, 1)
    eng.handle(TriggerFired("soverom"), opened)
    closed = opened + timedelta(seconds=5)
    eng.handle(TriggerFired("soverom", closing=True), closed)
    rs = eng.state.rooms["soverom"]
    assert rs.trigger_hold_until == closed + timedelta(seconds=TUN.door_close_hold)


def test_a_presence_room_with_triggers_keeps_the_door_behaviour() -> None:
    """D34 (specified): a presence-shaped room's trigger closing edge still
    shortens the trigger hold — its presence sensing carries the occupant."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_trigger(rs, False, t, TUN, RoomShape.PRESENCE)
    roles.ingest_trigger(rs, True, t + timedelta(seconds=5), TUN, RoomShape.PRESENCE)
    assert rs.trigger_hold_until == t + timedelta(seconds=5 + TUN.door_close_hold)
