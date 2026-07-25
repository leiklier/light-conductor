"""Output commands the engine emits from one ``handle()`` call (§8).

The engine owns slew sizing, quantization, min-delta, dim floor, and
off-is-off (rules 8.2/8.3/8.6): every :class:`SetChannel` / :class:`TurnOffChannel`
carries the goal it computed plus the ``ramp_seconds`` the adapter must
stretch the move over. The *adapter* owns rate-limit, coalescing, the
concurrency cap (``max_inflight``), and the echo ledger (§8.3/8.4) — it
executes ramps as timed step writes.

CT is emitted before brightness by the adapter (rule 5.4); ``SetChannel.ct``
of ``None`` means "leave CT unchanged" (the move was < ``ct_min_delta``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .model import RoomDiagnostics


@dataclass(frozen=True, slots=True)
class Command:
    """Base class for all engine output commands."""


@dataclass(frozen=True, slots=True)
class SetChannel(Command):
    """Move a channel toward ``level`` (normalized) over ``ramp_seconds``.

    ``ct`` is the target kelvin, or ``None`` to leave colour unchanged.
    """

    channel_id: str
    level: float
    ct: int | None
    ramp_seconds: float


@dataclass(frozen=True, slots=True)
class TurnOffChannel(Command):
    """Ramp a channel down to its dim floor then off (rule 8.6)."""

    channel_id: str
    ramp_seconds: float


@dataclass(frozen=True, slots=True)
class PublishState(Command):
    """Publish per-room diagnostics and master state (rule 10)."""

    rooms: tuple[RoomDiagnostics, ...]
    master_pct: float
    master_on: bool
    enabled: bool


@dataclass(frozen=True, slots=True)
class ScheduleReview(Command):
    """Ask the adapter to re-invoke the engine at ``at`` (rule 0)."""

    at: datetime


@dataclass(slots=True)
class Plan:
    """Accumulates the commands of one ``handle()`` call.

    Channel writes are collected first; :meth:`finalize` appends the single
    :class:`ScheduleReview` (earliest requested review) and the
    :class:`PublishState`.
    """

    commands: list[Command] = field(default_factory=list)
    _reviews: list[datetime] = field(default_factory=list)

    def set_channel(self, channel_id: str, level: float, ct: int | None, ramp: float) -> None:
        self.commands.append(SetChannel(channel_id, level, ct, ramp))

    def turn_off(self, channel_id: str, ramp: float) -> None:
        self.commands.append(TurnOffChannel(channel_id, ramp))

    def review_at(self, at: datetime | None) -> None:
        if at is not None:
            self._reviews.append(at)

    def finalize(
        self,
        rooms: tuple[RoomDiagnostics, ...],
        master_pct: float,
        master_on: bool,
        enabled: bool,
    ) -> list[Command]:
        if self._reviews:
            self.commands.append(ScheduleReview(min(self._reviews)))
        self.commands.append(PublishState(rooms, master_pct, master_on, enabled))
        return self.commands
