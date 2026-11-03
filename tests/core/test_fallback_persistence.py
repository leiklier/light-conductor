"""§1.1 (D31): raw fallback evidence lights at once; unconfirmed episodes get
a provisional hold.

The raw mmWave fallbacks are the fast detectors — every replayed episode in
kjøkken, spisebord, sofakrok and kontor started on a raw edge, presence-
conductor confirming 8-27 s later or never — but they also blip with the house
empty (kjøkken 0.73/h, sofakrok 0.89/h), and each blip held a room ACTIVE for
the full 120 s. Raw evidence now counts immediately; an episode only ever
supported by UNCONFIRMED raw evidence releases after
``fallback_provisional_hold`` (60 s). It is confirmed — normal hold from then
on — once the primary is seen in it or a raw spell persists
``fallback_min_on`` (15 s, dropouts <= FALLBACK_BRIDGE bridged).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from custom_components.light_conductor.core import roles
from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import (
    ActivityChanged,
    PresenceChanged,
    ReviewTick,
    SunElevationChanged,
)
from custom_components.light_conductor.core.model import (
    Activity,
    EngineConfig,
    RoomShape,
    RoomState,
)
from custom_components.light_conductor.core.tunables import Tunables

from .helpers import apartment, at

TUN = Tunables()
MIN_ON = TUN.fallback_min_on
PROVISIONAL = TUN.fallback_provisional_hold
HOLD = TUN.hold_seconds  # kjokken: no per-room override


def _engine(config: EngineConfig | None = None) -> tuple[Engine, object]:
    eng = Engine(config or apartment())
    t = at(1, 21, 0)
    eng.handle(SunElevationChanged(-8.0), t)
    t += timedelta(seconds=31)
    eng.handle(PresenceChanged("kjokken", False, False), t)  # primary live and empty
    return eng, t


def _spell(eng: Engine, start, seconds: float, room: str = "kjokken", primary=False):
    eng.handle(PresenceChanged(room, primary, True), start)
    end = start + timedelta(seconds=seconds)
    eng.handle(PresenceChanged(room, False, False), end)
    return end


def test_raw_evidence_lights_the_room_at_once() -> None:
    """D31: no turn-on latency — the raw edge alone makes the room ACTIVE
    (beta.20's first draft delayed every entry by 15 s)."""
    eng, t = _engine()
    eng.handle(PresenceChanged("kjokken", False, True), t + timedelta(seconds=1))
    rs = eng.state.rooms["kjokken"]
    assert rs.self_active and rs.channels["kjokken_downlights"].on


def test_an_unconfirmed_blip_gets_the_provisional_hold() -> None:
    """D31: a 9 s raw-only blip (the live median) releases provisional_hold
    after it ends — 60 s, not the full 120 s."""
    eng, t = _engine()
    end = _spell(eng, t + timedelta(seconds=1), 9.0)
    rs = eng.state.rooms["kjokken"]
    assert not rs.episode_confirmed
    assert rs.vacancy_hold_until == end + timedelta(seconds=min(HOLD, PROVISIONAL))
    eng.handle(ReviewTick(), end + timedelta(seconds=PROVISIONAL))
    assert not rs.self_active


def test_a_spell_persisting_min_on_confirms_the_episode() -> None:
    """D31: a raw spell of fallback_min_on confirms — the normal hold follows."""
    eng, t = _engine()
    end = _spell(eng, t + timedelta(seconds=1), MIN_ON)
    rs = eng.state.rooms["kjokken"]
    assert rs.episode_confirmed
    assert rs.vacancy_hold_until == end + timedelta(seconds=HOLD)


def test_primary_presence_confirms_even_a_short_raw_tail() -> None:
    """D31: presence-conductor seen once in the episode confirms it; a later
    short raw spell still ends on the normal hold."""
    eng, t = _engine()
    t1 = t + timedelta(seconds=1)
    eng.handle(PresenceChanged("kjokken", False, True), t1)
    eng.handle(PresenceChanged("kjokken", True, True), t1 + timedelta(seconds=3))
    eng.handle(PresenceChanged("kjokken", False, True), t1 + timedelta(seconds=5))
    end = t1 + timedelta(seconds=8)
    eng.handle(PresenceChanged("kjokken", False, False), end)
    rs = eng.state.rooms["kjokken"]
    assert rs.episode_confirmed
    assert rs.vacancy_hold_until == end + timedelta(seconds=HOLD)


def test_confirmation_lasts_the_episode_and_resets_after_it() -> None:
    """D31: once confirmed, a later blip inside the same episode keeps the
    normal hold; after the episode ends, the next blip is provisional again."""
    eng, t = _engine()
    end = _spell(eng, t + timedelta(seconds=1), 20.0)  # confirmed
    end2 = _spell(eng, end + timedelta(seconds=30), 5.0)  # re-occupied inside the hold
    rs = eng.state.rooms["kjokken"]
    assert rs.episode_confirmed
    assert rs.vacancy_hold_until == end2 + timedelta(seconds=HOLD)
    eng.handle(ReviewTick(), end2 + timedelta(seconds=HOLD))
    assert not rs.self_active and not rs.episode_confirmed
    end3 = _spell(eng, end2 + timedelta(seconds=HOLD + 60), 5.0)
    assert rs.vacancy_hold_until == end3 + timedelta(seconds=PROVISIONAL)


def test_sub_bridge_dropouts_do_not_restart_the_spell() -> None:
    """D31: the raw sensors re-trigger through near-instant off->on toggles
    (half of all kjøkken/sofakrok gaps are < 50 ms); those continue the spell."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, TUN)
    roles.step(rs, t, TUN, RoomShape.PRESENCE, None)
    for k in range(1, 5):  # off for 0.2 s every 4 s
        roles.ingest_presence(rs, False, False, t + timedelta(seconds=4 * k), TUN)
        roles.step(rs, t + timedelta(seconds=4 * k), TUN, RoomShape.PRESENCE, None)
        roles.ingest_presence(rs, False, True, t + timedelta(seconds=4 * k + 0.2), TUN)
        roles.step(rs, t + timedelta(seconds=4 * k + 0.2), TUN, RoomShape.PRESENCE, None)
    assert rs.episode_confirmed  # 16 s of bridged spell


def test_a_real_gap_restarts_the_spell() -> None:
    """D31: two 10 s spells 5 s apart are two spells — neither confirms."""
    eng, t = _engine()
    end = _spell(eng, t + timedelta(seconds=1), 10.0)
    end2 = _spell(eng, end + timedelta(seconds=roles.FALLBACK_BRIDGE + 3), 10.0)
    rs = eng.state.rooms["kjokken"]
    assert not rs.episode_confirmed
    assert rs.vacancy_hold_until == end2 + timedelta(seconds=PROVISIONAL)


def test_a_passing_peak_shorter_than_provisional_keeps_its_own_hold() -> None:
    """D31: the provisional hold only ever SHORTENS — min(normal, provisional)."""
    eng, t = _engine()
    eng.handle(ActivityChanged("kjokken", Activity.PASSING), t)
    end = _spell(eng, t + timedelta(seconds=1), 5.0)
    hold = HOLD * TUN.hold_passing_scale
    assert hold < PROVISIONAL
    assert eng.state.rooms["kjokken"].vacancy_hold_until == end + timedelta(seconds=hold)


def test_a_pir_pulse_confirms() -> None:
    """D31: the kontor PIR's on-pulses are >= 75 s (its hold time; p5 75 s over
    247 pulses) — they light kontor at once and always confirm."""
    eng, t = _engine()
    eng.handle(PresenceChanged("kontor", False, False), t)
    end = _spell(eng, t + timedelta(seconds=1), 75.0, room="kontor")
    rs = eng.state.rooms["kontor"]
    assert rs.episode_confirmed
    assert rs.vacancy_hold_until == end + timedelta(seconds=90.0)  # kontor's own hold


def test_a_blind_primary_keeps_the_full_hold() -> None:
    """§1.1: with the primary unavailable the fallback stands alone as before —
    no provisional hold on the blind path."""
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, False, t, TUN)
    roles.ingest_presence(rs, None, False, t + timedelta(seconds=1), TUN)
    blind = t + timedelta(seconds=1 + TUN.presence_blind_hold + 1)
    roles.step(rs, blind, TUN, RoomShape.PRESENCE, None)
    roles.ingest_presence(rs, None, True, blind, TUN)
    roles.step(rs, blind, TUN, RoomShape.PRESENCE, None)
    assert rs.self_active
    off = blind + timedelta(seconds=5)
    roles.ingest_presence(rs, None, False, off, TUN)
    roles.step(rs, off, TUN, RoomShape.PRESENCE, None)
    assert rs.vacancy_hold_until == off + timedelta(seconds=HOLD)


def test_a_room_override_of_zero_is_beta19() -> None:
    """D31: per-room fallback_min_on 0 confirms every spell — kontor and
    spisebord's sensors never fired with the house empty."""
    apt = apartment()
    rooms = tuple(
        replace(r, fallback_min_on=0.0) if r.room_id == "kjokken" else r for r in apt.rooms
    )
    eng, t = _engine(EngineConfig(rooms=rooms))
    end = _spell(eng, t + timedelta(seconds=1), 3.0)
    assert eng.state.rooms["kjokken"].vacancy_hold_until == end + timedelta(seconds=HOLD)


def test_zero_tunable_disables_the_provisional_hold() -> None:
    tun = replace(TUN, fallback_min_on=0.0)
    rs = RoomState()
    t = at(1, 12, 0)
    roles.ingest_presence(rs, False, True, t, tun)
    roles.step(rs, t, tun, RoomShape.PRESENCE, None)
    roles.ingest_presence(rs, False, False, t + timedelta(seconds=3), tun)
    roles.step(rs, t + timedelta(seconds=3), tun, RoomShape.PRESENCE, None)
    assert rs.vacancy_hold_until == t + timedelta(seconds=3 + HOLD)
