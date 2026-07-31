"""Shared helpers for core tests: a fixed clock and a realistic apartment.

The apartment mirrors docs/DISCOVERY.md so scenario tests can reproduce the
legacy behaviours the engine replaces. Times are aware datetimes; the
circadian clock term reads the local wall time of ``now`` (rule 2.3), so the
hour/minute passed here is what the engine sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    Profile,
    RoomConfig,
    RoomDiagnostics,
    RoomShape,
    Vacancy,
)
from custom_components.light_conductor.core.plan import (
    Command,
    PublishState,
    ScheduleReview,
    SetChannel,
    TurnOffChannel,
)

__all__ = ["at", "timedelta"]

CT_RANGE = (2200, 4000)


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    """An aware instant in July 2026 (the discovery month)."""
    return datetime(2026, 7, day, hour, minute, second, tzinfo=UTC)


# --- command inspection --------------------------------------------------


def sets(commands: list[Command]) -> dict[str, SetChannel]:
    """Latest SetChannel per channel in a plan."""
    out: dict[str, SetChannel] = {}
    for c in commands:
        if isinstance(c, SetChannel):
            out[c.channel_id] = c
    return out


def offs(commands: list[Command]) -> set[str]:
    """Channel ids turned off in a plan."""
    return {c.channel_id for c in commands if isinstance(c, TurnOffChannel)}


def review(commands: list[Command]) -> datetime | None:
    for c in commands:
        if isinstance(c, ScheduleReview):
            return c.at
    return None


def diag(commands: list[Command], room_id: str) -> RoomDiagnostics:
    """The published diagnostics for one room (rule 10)."""
    for c in commands:
        if isinstance(c, PublishState):
            return next(d for d in c.rooms if d.room_id == room_id)
    raise AssertionError("no PublishState in plan")


# --- apartment factory ---------------------------------------------------


def _kjokken() -> RoomConfig:
    return RoomConfig(
        room_id="kjokken",
        channels=(
            ChannelConfig("kjokken_downlights", band=Band.ACCENT, fixed_ct=None, ct_range=CT_RANGE),
            ChannelConfig("kjokken_taklys", band=Band.PRIMARY, fixed_ct=2700),
            ChannelConfig("kjokken_benke", band=Band.BOOST, fixed_ct=2700),
        ),
        profile=Profile(
            vacancy=Vacancy.DIM,
            out_active_day={Band.ACCENT: 0.45, Band.PRIMARY: 0.45, Band.BOOST: 0.6},
            out_active_evening={Band.ACCENT: 0.15},  # legacy evening accent survivor
            out_background={Band.ACCENT: 0.1},
            evening_output_cap=0.3,
            night_output={Band.ACCENT: 0.2},
        ),
        neighbours=("sofakrok", "spisebord", "gang"),
        night_path=True,
    )


def _kontor() -> RoomConfig:
    return RoomConfig(
        room_id="kontor",
        channels=(ChannelConfig("kontor_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            vacancy=Vacancy.OFF,  # kontor goes dark after its hold (rule 1.4)
            out_active_day={Band.PRIMARY: 0.6},
            out_active_evening={Band.PRIMARY: 0.18},
            out_background={Band.PRIMARY: 0.05},
            evening_output_cap=0.3,
        ),
        neighbours=("gang",),
        hold_seconds=90.0,  # discovery: kontor 90 s
    )


def _living_room(room_id: str) -> RoomConfig:
    return RoomConfig(
        room_id=room_id,
        channels=(ChannelConfig(f"{room_id}_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            vacancy=Vacancy.DIM,
            out_active_day={Band.PRIMARY: 0.7},
            out_active_evening={Band.PRIMARY: 0.3},
            out_background={Band.PRIMARY: 0.06},
            evening_output_cap=0.3,
            tv_output={Band.PRIMARY: 0.15},
            tv_output_empty={Band.PRIMARY: 0.05 if room_id == "spisebord" else 0.0},
            night_output={Band.PRIMARY: 0.04 if room_id == "sofakrok" else 0.01},
        ),
        neighbours=("gang", "kjokken"),
        living_group=True,
        tv_mode=True,
        night_path=True,
    )


def _gang() -> RoomConfig:
    return RoomConfig(
        room_id="gang",
        channels=(ChannelConfig("gang_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            vacancy=Vacancy.DIM,
            out_active_day={Band.PRIMARY: 0.7},
            out_active_evening={Band.PRIMARY: 0.3},
            out_background={Band.PRIMARY: 0.06},
            evening_output_cap=0.3,
            tv_output={Band.PRIMARY: 0.05},  # legacy gang-TV dim
            tv_output_empty={Band.PRIMARY: 0.05},
            night_output={Band.PRIMARY: 0.05},
        ),
        shape=RoomShape.CORRIDOR,
        neighbours=("sofakrok", "spisebord", "kjokken", "kontor"),
        presence_capable=False,  # live gang has no occupancy sensing
        tv_mode=True,
        night_path=True,
    )


def _soverom() -> RoomConfig:
    return RoomConfig(
        room_id="soverom",
        channels=(ChannelConfig("soverom_taklys", band=Band.PRIMARY, fixed_ct=2700),),
        profile=Profile(
            vacancy=Vacancy.OFF,
            out_active_day={Band.PRIMARY: 0.7},
            out_active_evening={Band.PRIMARY: 0.4},
            out_background={Band.PRIMARY: 0.05},
            evening_output_cap=0.4,
        ),
        shape=RoomShape.DOOR,
        presence_capable=False,  # live soverom: door trigger only, blind
    )


def _balkong() -> RoomConfig:
    return RoomConfig(
        room_id="balkong",
        channels=(
            ChannelConfig("balkong_taklys", band=Band.PRIMARY, fixed_ct=None, ct_range=CT_RANGE),
        ),
        profile=Profile(
            out_active_evening={Band.PRIMARY: 0.5},
            out_background={Band.PRIMARY: 0.2},
        ),
        shape=RoomShape.OUTDOOR,
        presence_capable=False,  # live balkong: occupational switch only
    )


def apartment() -> EngineConfig:
    """The full live apartment (docs/DISCOVERY.md)."""
    return EngineConfig(
        rooms=(
            _kjokken(),
            _kontor(),
            _living_room("sofakrok"),
            _living_room("spisebord"),
            _gang(),
            _soverom(),
            _balkong(),
        )
    )
