"""§6: modes — sleep, night path, TV, away, outdoor, vacation."""

from __future__ import annotations

from custom_components.light_conductor.core import modes
from custom_components.light_conductor.core.model import Band, EngineState, Role, RoomState
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import apartment

TUN = Tunables()
APT = apartment()


def _room(room_id: str):
    return APT.room(room_id)


def test_away_turns_indoor_off_keeps_outdoor_background() -> None:
    """§6.4: away => indoor OFF; outdoor keeps its dusk background as presence
    simulation while away_lighting is on (occupational ignored, rule 6.5)."""
    state = EngineState(anyone_home=False, away_lighting=True)
    assert modes.resolve(_room("sofakrok"), RoomState(), state, 1.0, TUN).off
    # Outdoor at dusk: ambient background, and the occupational switch is ignored.
    outdoor = modes.resolve(_room("balkong"), RoomState(occupational=True), state, 0.8, TUN)
    assert outdoor.band_outputs == {Band.PRIMARY: 0.2}  # out_background, not out_active_evening


def test_away_lighting_off_darkens_outdoor() -> None:
    """§6.4: away_lighting off => outdoor rooms go dark on away too."""
    state = EngineState(anyone_home=False, away_lighting=False)
    assert modes.resolve(_room("balkong"), RoomState(), state, 0.8, TUN).off


def test_none_home_fails_safe_as_home() -> None:
    """§6.4: None/unavailable home is not away."""
    state = EngineState(anyone_home=None)
    assert not modes.is_away(state)


def test_sleep_off_except_night_path() -> None:
    """§6.1/6.2: sleep => OFF, but a night-path room lights on a night trigger."""
    sleeping = EngineState(sleep=True)
    res = modes.resolve(_room("sofakrok"), RoomState(), sleeping, 1.0, TUN)
    assert res is not None and res.off and res.fade == TUN.sleep_fade

    night = EngineState(sleep=True, night_active=True)
    res = modes.resolve(_room("sofakrok"), RoomState(), night, 1.0, TUN)
    assert res is not None and res.role is Role.NIGHT_PATH
    assert res.band_outputs == {Band.PRIMARY: 0.04}
    assert res.ct_override == TUN.ct_min_evening  # forced warm (rule 6.2)
    assert res.gain_exempt and res.suppress_override


def test_tv_ladder_occupied_vs_empty() -> None:
    """§6.3: TV output when the room is occupied, tv_output_empty otherwise."""
    playing = EngineState(tv_playing=True)
    occupied = RoomState(self_active=True)
    empty = RoomState(self_active=False)
    res_occ = modes.resolve(_room("spisebord"), occupied, playing, 0.5, TUN)
    res_empty = modes.resolve(_room("spisebord"), empty, playing, 0.5, TUN)
    assert res_occ.band_outputs == {Band.PRIMARY: 0.15}
    assert res_empty.band_outputs == {Band.PRIMARY: 0.05}
    assert res_occ.role is Role.TV


def test_tv_absent_hands_back_to_role_path() -> None:
    """No TV playing => modes yield to the normal role path."""
    assert modes.resolve(_room("spisebord"), RoomState(), EngineState(), 0.0, TUN) is None


def test_outdoor_dusk_on_and_occupational() -> None:
    """§6.5: balkong on at dusk (E >= threshold); occupational raises + cools it."""
    below = modes.resolve(_room("balkong"), RoomState(), EngineState(), 0.6, TUN)
    assert below.off  # before dusk
    ambient = modes.resolve(_room("balkong"), RoomState(), EngineState(), 0.8, TUN)
    assert ambient.band_outputs == {Band.PRIMARY: 0.2}  # out_background
    assert ambient.ct_override == TUN.ct_min_evening and ambient.gain_exempt
    sitting = modes.resolve(_room("balkong"), RoomState(occupational=True), EngineState(), 0.8, TUN)
    assert sitting.band_outputs == {Band.PRIMARY: 0.5}  # out_active_evening
    assert sitting.ct_override == TUN.ct_evening  # slightly cooler
