"""Channels, photometry & allocation (ENGINE_SPEC §4, minus §4.4 calibration).

:class:`RoomPhotometry` is the closed-loop seam: it owns each channel's
relative-flux curve ``f(b)`` (default square-law ``b**2`` until calibrated,
rule 4.2) and its calibrated lux gain (unused open-loop). ``calibrated`` is
always ``False`` this PR — the §4.4 sweep that flips it lands later.

Allocation (§4.5/§4.6): on the open-loop path a room's per-band normalized
outputs come straight from the profile tables (rule 4.6). This module maps
those band outputs onto channels, applies the boost-band evening gate (rule
4.5), and exposes flux <-> command conversions used by the governor for
flux-relative slew and min-delta (rules 4.3, 8.2).

Feature-module discipline: imports only :mod:`model` and :mod:`tunables`.
"""

from __future__ import annotations

from math import sqrt

from .model import Band, ChannelConfig, RoomCalibration, RoomConfig
from .tunables import Tunables


class Curve:
    """A channel's relative-flux curve ``f(b)`` (rule 4.2).

    Default is square-law ``b**2``; a config curve is a monotone
    piecewise-linear list of ``(b, flux)`` points that must span b=0 and
    b=1. Both directions are provided (allocation and slew work in flux
    space, rule 4.3).
    """

    __slots__ = ("_points",)

    def __init__(self, points: tuple[tuple[float, float], ...] | None) -> None:
        self._points = tuple(sorted(points)) if points else None

    def flux(self, b: float) -> float:
        b = max(0.0, min(1.0, b))
        if self._points is None:
            return b * b
        return _interp(self._points, b)

    def command(self, f: float) -> float:
        f = max(0.0, min(1.0, f))
        if self._points is None:
            return sqrt(f)
        # Invert: interpolate b as a function of flux.
        inverse = tuple((y, x) for x, y in self._points)
        return _interp(tuple(sorted(inverse)), f)

    def points(self) -> tuple[tuple[float, float], ...]:
        """Explicit ``(b, flux)`` points (square-law samples for the default)."""
        if self._points is not None:
            return self._points
        grid = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)
        return tuple((b, b * b) for b in grid)


def _interp(points: tuple[tuple[float, float], ...], x: float) -> float:
    lo_x, lo_y = points[0]
    if x <= lo_x:
        return lo_y
    for hi_x, hi_y in points[1:]:
        if x <= hi_x:
            if hi_x == lo_x:
                return hi_y
            t = (x - lo_x) / (hi_x - lo_x)
            return lo_y + t * (hi_y - lo_y)
        lo_x, lo_y = hi_x, hi_y
    return points[-1][1]


class RoomPhotometry:
    """Per-room photometric model (rules 4.1-4.3, 4.4).

    Satisfies :class:`~.model.PhotometricModel` structurally, so the governor,
    estimator and calibration modules consume it without importing this
    module. ``calibrated`` flips True once a §4.4 sweep commits, or when a
    persisted :class:`~.model.RoomCalibration` is loaded that matches the
    room's channel set.
    """

    __slots__ = ("_channel_ids", "_curves", "_gains", "calibrated")

    def __init__(self, room: RoomConfig, calibration: RoomCalibration | None = None) -> None:
        self._channel_ids: tuple[str, ...] = tuple(c.channel_id for c in room.channels)
        self._curves: dict[str, Curve] = {c.channel_id: Curve(c.curve) for c in room.channels}
        self._gains: dict[str, float] = {c.channel_id: c.gain for c in room.channels}
        #: Flipped True by the §4.4 sweep or a matching persisted calibration.
        self.calibrated: bool = False
        if (
            calibration is not None
            and calibration.matches(self._channel_ids)
            and calibration.is_valid()
        ):
            # Persisted calibration bound to this exact channel set AND
            # well-formed (rule 5): load it. A mismatch or a malformed payload
            # leaves the defaults + uncalibrated (safe by construction).
            self.apply_calibration(calibration)

    def flux(self, channel_id: str, b: float) -> float:
        return self._curves[channel_id].flux(b)

    def command_for_flux(self, channel_id: str, f: float) -> float:
        return self._curves[channel_id].command(f)

    def gain(self, channel_id: str) -> float:
        return self._gains[channel_id]

    # -- calibration transaction (rule 4.4) --------------------------------

    def export_calibration(self, room_id: str) -> RoomCalibration:
        """Snapshot the current gains + curves as plain data (rule 4.4/5)."""
        return RoomCalibration(
            room_id=room_id,
            gains=dict(self._gains),
            curves={cid: c.points() for cid, c in self._curves.items()},
        )

    def apply_calibration(self, cal: RoomCalibration) -> None:
        """Commit measured gains + curves (rule 4.4). Marks the room calibrated."""
        for cid in self._channel_ids:
            if cid in cal.gains:
                self._gains[cid] = cal.gains[cid]
            if cid in cal.curves:
                self._curves[cid] = Curve(cal.curves[cid])
        self.calibrated = True


def allocate(
    channels: tuple[ChannelConfig, ...],
    band_outputs: dict[Band, float],
    evening_factor: float,
    tun: Tunables,
) -> dict[str, float]:
    """Map per-band normalized outputs onto channels (rules 4.5/4.6).

    Open-loop path: a band's tier output is shared across its channels by
    their configured ``weight`` (rule 4.5) — relative to the band's heaviest
    channel, so the default equal weights leave every channel at the band
    output (matching §4.6's "normalized output per band") while a lighter
    weight scales a channel down. Sharing uses ``weight``, **never** the
    calibrated sensor gain (§3.1). The boost band is gated off once
    ``E >= boost_evening_max`` (benkebelysning evening lockout, rule 4.5).
    The lux ``band_overlap`` crossfade is a closed-loop mechanism (estimator).
    """
    max_weight: dict[Band, float] = {}
    for ch in channels:
        max_weight[ch.band] = max(max_weight.get(ch.band, 0.0), ch.weight)
    result: dict[str, float] = {}
    for ch in channels:
        out = band_outputs.get(ch.band, 0.0)
        if ch.band is Band.BOOST and evening_factor >= tun.boost_evening_max:
            out = 0.0  # rule 4.5 evening lockout
        peak = max_weight.get(ch.band, 0.0)
        share = ch.weight / peak if peak > 0.0 else 1.0
        result[ch.channel_id] = max(0.0, min(1.0, out * share))
    return result
