"""§8.3 (D27): the command-space grid, end to end through the engine.

beta.19 quantized on an absolute 0.03 flux grid: on the default b² curve every
goal below b ≈ 0.12 rendered as the 2 % dim floor. These scenarios pin the
live dim tiers to their intended levels and the CT policy to the level that is
actually emitted.
"""

from __future__ import annotations

from custom_components.light_conductor.core import ct_policy
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    PresenceChanged,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    Profile,
    RoomConfig,
)
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import CT_RANGE, apartment, at, sets

NIGHT_SUN = -8.0


def _bound(goal: float) -> float:
    return max(0.01, 0.08 * goal)


def test_kitchen_adjacent_renders_its_tier_not_the_floor() -> None:
    """§1.5/§8.3: kjøkken ADJACENT at full evening is 0.5 x the 0.15 accent =
    0.075. beta.19 rendered it as the 2 % floor (the live ~12 % -> 2 % report)."""
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    cmds = eng.handle(PresenceChanged("spisebord", True), at(1, 22, 1))
    assert eng.state.rooms["kjokken"].role.value == "adjacent"
    level = sets(cmds)["kjokken_downlights"].level
    assert abs(level - 0.075) <= _bound(0.075), level


def test_background_renders_its_table_not_the_floor() -> None:
    """§4.6/§8.3: a living room's 6 % BACKGROUND is emitted as ~6 %, not 2 %."""
    eng = Engine(apartment())
    eng.handle(SunElevationChanged(NIGHT_SUN), at(1, 22, 0))
    eng.handle(PresenceChanged("sofakrok", True), at(1, 22, 1))
    # sofakrok leaves; after its hold it decays to BACKGROUND (living memory).
    eng.handle(PresenceChanged("sofakrok", False), at(1, 22, 2))
    cmds = eng.handle(ReviewTick(), at(1, 22, 10))
    assert eng.state.rooms["sofakrok"].role.value == "background"
    level = sets(cmds)["sofakrok_taklys"].level
    assert abs(level - 0.06) <= _bound(0.06), level


def _ct_room(day_output: float) -> EngineConfig:
    return EngineConfig(
        rooms=(
            RoomConfig(
                room_id="lab",
                channels=(ChannelConfig("lab_spot", fixed_ct=None, ct_range=CT_RANGE),),
                profile=Profile(out_active_day={Band.PRIMARY: day_output}),
            ),
        )
    )


def test_dim_to_warm_ct_reads_the_quantized_level() -> None:
    """§5.3/D27: CT is computed on the level the governor actually emits. A 0.25
    goal renders as grid level 0.259375; its dim-to-warm cap (3151 K) differs
    from the pre-rounding goal's (3117 K) — the lamp at 25.9 % gets 25.9 %'s CT."""
    tun = Tunables()
    eng = Engine(_ct_room(0.25))
    eng.handle(SunElevationChanged(20.0), at(1, 12, 0))
    cmd = sets(eng.handle(PresenceChanged("lab", True), at(1, 12, 1)))["lab_spot"]
    assert cmd.level != 0.25
    ch = eng.config.rooms[0].channels[0]
    assert cmd.ct == ct_policy.ct_target(ch, 0.0, cmd.level, None, tun)
    assert cmd.ct != ct_policy.ct_target(ch, 0.0, 0.25, None, tun)
