"""A synthetic room ("plant") that drives the real Engine closed-loop.

The plant owns the *ground truth* the engine cannot see: a natural-light
trajectory ``N(t)`` and, per channel, a true lux gain and true flux curve at
the sensor. At each tick it computes the true illuminance from the engine's
currently commanded outputs, quantizes it like a real sensor, and feeds it
back as a :class:`~.events.LuxReport` — so the engine regulates against a
world with its own gains and curves, exactly as in the field.

This is the harness behind the §3/§4.5 proofs: convergence, anti-hunting,
write-blanking, the night prior, online-gain learning, stale fallback, and
calibration recovery all run the production :class:`Engine` against it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from custom_components.light_conductor.core.engine import Engine
from custom_components.light_conductor.core.events import LuxReport
from custom_components.light_conductor.core.model import (
    Band,
    ChannelConfig,
    EngineConfig,
    InitialSnapshot,
    Profile,
    RoomCalibration,
    RoomConfig,
    Vacancy,
)
from custom_components.light_conductor.core.plan import SetChannel

CT_RANGE = (2200, 4000)


def square_law(b: float) -> float:
    return b * b


@dataclass
class Channel:
    """Ground-truth photometry for one channel.

    ``gain`` is the true lux at full output; ``model_gain`` is what the engine
    is told (its calibrated gain). Equal ⇒ calibrated; ``model_gain=1`` (the
    default config gain) with a large ``gain`` ⇒ an uncalibrated room.
    """

    channel_id: str
    gain: float  # true lux at the sensor at full output
    band: Band = Band.PRIMARY
    curve: Callable[[float], float] = square_law
    weight: float = 1.0
    model_gain: float | None = None  # None ⇒ calibrated to the true gain

    @property
    def engine_gain(self) -> float:
        return self.gain if self.model_gain is None else self.model_gain

    def lux(self, b: float) -> float:
        return self.gain * self.curve(max(0.0, min(1.0, b)))


@dataclass
class Plant:
    """Drives one closed-loop room of an :class:`Engine`."""

    engine: Engine
    room_id: str
    channels: list[Channel]
    n_of_t: Callable[[datetime], float]
    quant: float = 1.0  # sensor quantization (lux)
    noise: Callable[[datetime], float] = lambda _now: 0.0
    #: Delta-filtered field-sensor regime (Apollo LTR390, §4.4 calibration): with
    #: ``delta`` > 0 the plant emits a LuxReport only when the value moved more
    #: than ``delta`` since the last publish AND at least ``min_cadence`` elapsed
    #: (on-device dedup at a ~60 s cadence) — used via :meth:`tick_field`.
    delta: float = 0.0
    min_cadence: float = 0.0
    #: Recorded (time, {cid: commanded_b}) after every tick, for assertions.
    history: list[tuple[datetime, dict[str, float]]] = field(default_factory=list)
    commands: list[object] = field(default_factory=list)
    _last_pub_value: float | None = field(default=None, init=False, repr=False)
    _last_pub_at: datetime | None = field(default=None, init=False, repr=False)

    def true_lux(self, now: datetime) -> float:
        rs = self.engine.state.rooms[self.room_id]
        art = sum(ch.lux(rs.channels[ch.channel_id].commanded_b) for ch in self.channels)
        return max(0.0, self.n_of_t(now) + art)

    def sample(self, now: datetime) -> float:
        raw = self.true_lux(now) + self.noise(now)
        return max(0.0, round(raw / self.quant) * self.quant)

    def tick(self, now: datetime, lux: float | None = None) -> list[object]:
        """Feed one lux sample (defaults to the quantized true lux) + recompute."""
        value = self.sample(now) if lux is None else lux
        cmds = self.engine.handle(LuxReport(self.room_id, value), now)
        self.commands.extend(cmds)
        rs = self.engine.state.rooms[self.room_id]
        snap = {c.channel_id: rs.channels[c.channel_id].commanded_b for c in self.channels}
        self.history.append((now, snap))
        return cmds

    def tick_field(self, now: datetime) -> list[object]:
        """Delta-filtered field-sensor tick (Apollo LTR390 regime, §4.4).

        Emits a LuxReport only when the quantized value moved more than ``delta``
        since the last publish AND at least ``min_cadence`` has elapsed — so a
        dim level whose contribution stays within ``delta`` of the last published
        value produces no sample at all (the sub-delta night regime). Ticks that
        do not publish still record history for command analysis.
        """
        value = self.sample(now)
        publish = self._last_pub_value is None
        if not publish:
            moved = abs(value - self._last_pub_value) > self.delta
            elapsed = (now - self._last_pub_at).total_seconds() >= self.min_cadence
            publish = moved and elapsed
        rs = self.engine.state.rooms[self.room_id]
        snap = {c.channel_id: rs.channels[c.channel_id].commanded_b for c in self.channels}
        if not publish:
            self.history.append((now, snap))
            return []
        self._last_pub_value = value
        self._last_pub_at = now
        cmds = self.engine.handle(LuxReport(self.room_id, value), now)
        self.commands.extend(cmds)
        self.history.append((now, snap))
        return cmds

    def run(self, start: datetime, seconds: float, dt: float = 2.0) -> None:
        """Tick every ``dt`` seconds for ``seconds`` of simulated time."""
        t = start
        end = start + timedelta(seconds=seconds)
        while t <= end:
            self.tick(t)
            t = t + timedelta(seconds=dt)

    # -- command analysis --------------------------------------------------

    def sets_for(self, cmds: list[object], cid: str) -> list[SetChannel]:
        return [c for c in cmds if isinstance(c, SetChannel) and c.channel_id == cid]

    def reversals(self, cid: str) -> int:
        """Command-direction reversals for a channel across the whole run (§3.6b).

        Counts sign changes in successive *commanded* level deltas — the
        anti-hunting metric.
        """
        levels = [snap[cid] for _t, snap in self.history]
        directions: list[int] = []
        for a, b in pairwise(levels):
            if abs(b - a) > 1e-6:
                directions.append(1 if b > a else -1)
        return sum(1 for x, y in pairwise(directions) if x != y)


def closed_config(
    channels: list[Channel],
    *,
    lux_active_day: float = 120.0,
    lux_active_evening: float = 60.0,
    lux_background: float = 10.0,
    lux_max: float = 1000.0,
    evening_output_cap: float = 1.0,
    out_active_day: dict[Band, float] | None = None,
    room_id: str = "lab",
) -> EngineConfig:
    """A single presence-driven room with a lux sensor (closed-loop, §2.1).

    ``out_active_day`` provides open-loop tables (§4.6) used as the seamless
    stale-sensor fallback (§3.5); omitted ⇒ empty (falls back to off).
    """
    return EngineConfig(
        rooms=(
            RoomConfig(
                room_id=room_id,
                channels=tuple(
                    ChannelConfig(
                        c.channel_id,
                        band=c.band,
                        fixed_ct=None if c.band is Band.ACCENT else 2700,
                        ct_range=CT_RANGE if c.band is Band.ACCENT else None,
                        weight=c.weight,
                        gain=c.engine_gain,
                    )
                    for c in channels
                ),
                profile=Profile(
                    vacancy=Vacancy.DIM,
                    lux_active_day=lux_active_day,
                    lux_active_evening=lux_active_evening,
                    lux_background=lux_background,
                    lux_max=lux_max,
                    evening_output_cap=evening_output_cap,
                    out_active_day=out_active_day or {},
                    out_active_evening=out_active_day or {},
                ),
                has_lux_sensor=True,
            ),
        )
    )


def calibration_for(config: EngineConfig, room_id: str = "lab") -> RoomCalibration:
    """A RoomCalibration marking the room calibrated with its config gains.

    Square-law curves (the closed-loop tests all use square-law true curves), so
    a room built this way models its plant exactly — the "calibrated" case.
    """
    room = next(r for r in config.rooms if r.room_id == room_id)
    grid = (0.0, 0.25, 0.5, 0.75, 1.0)
    return RoomCalibration(
        room_id=room_id,
        gains={c.channel_id: c.gain for c in room.channels},
        curves={c.channel_id: tuple((b, b * b) for b in grid) for c in room.channels},
    )


def booted_engine(
    config: EngineConfig, *, sun: float, room_id: str = "lab", calibrated: bool = True
) -> Engine:
    """An engine booted past the startup grace with the room occupied (ACTIVE).

    ``calibrated`` (default) loads a matching calibration so the room enters
    closed-loop immediately; ``False`` leaves it uncalibrated, so it runs
    open-loop and learns its first-night bootstrap gain in shadow (§3.5/4.4).
    """
    from custom_components.light_conductor.core.events import PresenceChanged, SunElevationChanged

    cals = {room_id: calibration_for(config, room_id)} if calibrated else None
    eng = Engine(
        config, InitialSnapshot(sun_elevation=sun, occupancy={room_id: True}), calibrations=cals
    )
    # First event arms the startup grace; step past it before measuring.
    base = datetime(2026, 7, 1, 12, 0, 0)
    eng.handle(SunElevationChanged(sun), base)
    eng.handle(PresenceChanged(room_id, True), base + timedelta(seconds=40))
    return eng
