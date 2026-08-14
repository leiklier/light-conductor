"""§9: manual override latch and release."""

from __future__ import annotations

from custom_components.light_conductor.core import override
from custom_components.light_conductor.core.model import ChannelState, RoomState
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import at

TUN = Tunables()


def test_latch_and_adopt() -> None:
    """§9.1: a foreign change latches the room and adopts the observed level."""
    rs = RoomState()
    cs = ChannelState(commanded_b=0.5, on=True)
    override.latch(rs, at(1, 20, 0))
    override.adopt(cs, 0.8, 2500)
    assert rs.overridden and rs.override_since == at(1, 20, 0)
    assert cs.commanded_b == 0.8 and cs.commanded_ct == 2500


def test_adopt_manual_off() -> None:
    """§9.3: a manual off is adopted as the goal (level None/0)."""
    cs = ChannelState(commanded_b=0.5, on=True)
    override.adopt(cs, None, None)
    assert not cs.on and cs.commanded_b == 0.0


def test_release_timeout_and_offworthy_only() -> None:
    """§9.2: release on override_timeout or OFF-worthy vacancy — STANDING
    modes never release here. Sleep/away/vacation win once, at their ONSET
    edge (the engine fold releases every latch); a latch present while the
    mode stands was minted during it and is respected (rules 6.1/6.4)."""
    base = RoomState(overridden=True, override_since=at(1, 20, 0))

    # OFF-worthy vacancy (hold expiry at OFF tier) releases a presence-capable room.
    assert override.should_release(base, True, True, at(1, 20, 1), TUN)
    # 4 h timeout.
    assert override.should_release(base, False, True, at(2, 0, 0), TUN)
    # Neither: hold the override.
    assert not override.should_release(base, False, True, at(1, 20, 1), TUN)


def test_blind_room_holds_latch_through_off_decay() -> None:
    """§9.2: OFF-worthy decay must NOT release a blind room's latch — the
    trigger hold expiring says nothing about vacancy (soverom incident: the
    wall dial was countered to 0 within one review)."""
    base = RoomState(overridden=True, override_since=at(1, 20, 0))

    assert not override.should_release(base, True, False, at(1, 20, 1), TUN)
    # The timeout still applies to blind rooms.
    assert override.should_release(base, True, False, at(2, 0, 0), TUN)


def test_release_noop_when_not_overridden() -> None:
    assert not override.should_release(RoomState(), True, True, at(1, 20, 0), TUN)


def test_override_review_time() -> None:
    """§9.2: the timeout instant is scheduled for review."""
    rs = RoomState(overridden=True, override_since=at(1, 20, 0))
    # 20:00 + 4 h = 00:00 the next day.
    assert override.override_review(rs, at(1, 20, 0), TUN) == at(2, 0, 0)
    assert override.override_review(RoomState(), at(1, 20, 0), TUN) is None
