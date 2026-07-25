"""The lighting engine: a pure, synchronous event processor (ENGINE_SPEC §11).

``handle(event, now) -> list[Command]`` folds one event into
:class:`~.model.EngineState` and returns the commands the adapter must
execute. It never performs I/O, never reads a clock, and never sleeps — time
enters only through the ``now`` stamp and future work is requested via
:class:`~.plan.ScheduleReview` (rule 0).

This module owns state, seeding, dispatch, and the recompute pipeline; the
behavioural rules live in the feature modules (roles, circadian, targets,
photometry, ct_policy, modes, gain, governor, override), which never import
one another — only model/tunables/plan. The pipeline wires them in the §8.1
funnel order: role/mode target -> master gain -> evening cap -> allocation
-> CT policy -> governor (slew/quantize/dim-floor/off).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import circadian, ct_policy, gain, governor, modes, override, photometry, roles, targets
from .events import (
    ActivityChanged,
    Event,
    ForeignChange,
    HomeChanged,
    LuxReport,
    MasterGainChanged,
    MasterPowerChanged,
    NightTriggerFired,
    OccupationalChanged,
    PresenceChanged,
    ReviewTick,
    SetAwayLighting,
    SetEnabled,
    SleepChanged,
    SunElevationChanged,
    TriggerFired,
    TvChanged,
    VacationChanged,
)
from .model import (
    BAND_ORDER,
    ChannelState,
    EngineConfig,
    EngineState,
    InitialSnapshot,
    Role,
    RoomConfig,
    RoomDiagnostics,
    RoomState,
)
from .photometry import RoomPhotometry
from .plan import Command, Plan
from .tunables import Tunables


class Engine:
    """Deterministic core of Light Conductor."""

    def __init__(
        self,
        config: EngineConfig,
        snapshot: InitialSnapshot | None = None,
        tunables: Tunables | None = None,
    ) -> None:
        self.config = config
        self.tun = tunables or Tunables()
        self.state = EngineState()
        self._photo: dict[str, RoomPhotometry] = {
            r.room_id: RoomPhotometry(r) for r in config.rooms
        }
        self._seed(snapshot or InitialSnapshot())

    # -- seeding (§11) ------------------------------------------------------

    def _seed(self, snap: InitialSnapshot) -> None:
        s = self.state
        s.enabled = snap.enabled
        s.sun_elevation = snap.sun_elevation
        s.sleep = snap.sleep
        s.anyone_home = snap.anyone_home
        s.vacation = snap.vacation
        s.away_lighting = snap.away_lighting
        s.tv_playing = snap.tv_playing
        s.master_on = snap.master_on
        s.master_pct = snap.master_pct
        for room in self.config.rooms:
            rs = RoomState()
            occ = snap.occupancy.get(room.room_id)
            rs.primary = occ
            if occ is not None:
                rs.last_definitive_occupied = bool(occ)
                rs.self_active = bool(occ)  # rule 11.1: roles evaluate from live presence
            rs.activity = snap.activity.get(room.room_id)
            rs.occupational = snap.occupational.get(room.room_id, False)
            for ch in room.channels:
                level, ct = snap.channels.get(ch.channel_id, (0.0, None))
                rs.channels[ch.channel_id] = ChannelState(
                    commanded_b=max(0.0, min(1.0, level)),
                    commanded_ct=ct,
                    on=level > 0.0,
                )
            s.rooms[room.room_id] = rs

    # -- read surface -------------------------------------------------------

    def room_state(self, room_id: str) -> RoomState:
        return self.state.rooms[room_id]

    def circadian_factor(self, now: datetime) -> float:
        return circadian.factor(self.state.sun_elevation, now, self.tun)

    # -- dispatch -----------------------------------------------------------

    def handle(self, event: Event, now: datetime) -> list[Command]:
        if not self.state.started:
            self.state.started = True
            self.state.start_at = now
        self._fold(event, now)
        return self._recompute(now)

    def _fold(self, event: Event, now: datetime) -> None:
        s = self.state
        match event:
            case PresenceChanged():
                if (rs := s.rooms.get(event.room_id)) is not None:
                    roles.ingest_presence(rs, event.primary, event.fallback, now)
            case ActivityChanged():
                if (rs := s.rooms.get(event.room_id)) is not None:
                    roles.ingest_activity(rs, event.activity)
            case TriggerFired():
                if (rs := s.rooms.get(event.room_id)) is not None:
                    roles.ingest_trigger(rs, event.closing, now, self.tun)
            case ForeignChange():
                self._on_foreign(event, now)
            case SunElevationChanged():
                s.sun_elevation = event.elevation_deg
            case ReviewTick():
                pass  # recompute only
            case SleepChanged():
                s.sleep = event.active
                if not event.active:
                    s.night_active = False
                    s.night_hold_until = None
                    gain.relax_to_neutral(s, self.tun)  # rule 7.3 (sleep-off edge)
            case HomeChanged():
                s.anyone_home = event.anyone_home
            case VacationChanged():
                s.vacation = event.active
            case TvChanged():
                s.tv_playing = event.playing
            case NightTriggerFired():
                if s.sleep:
                    s.night_active = True
                    s.night_hold_until = now + timedelta(seconds=self.tun.night_hold)
            case OccupationalChanged():
                if (rs := s.rooms.get(event.room_id)) is not None:
                    rs.occupational = event.on
            case MasterGainChanged():
                s.master_pct = max(0.0, min(100.0, event.pct))
                s.master_on = True
            case MasterPowerChanged():
                s.master_on = event.on
                for rs in s.rooms.values():  # rule 9.2: power cycle releases overrides
                    override.release(rs)
            case SetEnabled():
                s.enabled = event.enabled
            case SetAwayLighting():
                s.away_lighting = event.on
            case LuxReport():
                pass  # estimator seam (§3); ignored on the open-loop path
            case _:
                pass

    def _on_foreign(self, event: ForeignChange, now: datetime) -> None:
        room = self.config.channel_room(event.channel_id)
        if room is None:
            return
        rs = self.state.rooms[room.room_id]
        override.latch(rs, now)  # rules 9.1, 9.4 (wall events always latch)
        override.adopt(rs.channels[event.channel_id], event.level, event.ct)

    # -- recompute pipeline -------------------------------------------------

    def _in_grace(self, now: datetime) -> bool:
        return (
            self.state.start_at is not None
            and (now - self.state.start_at).total_seconds() < self.tun.startup_grace
        )

    def _living_active(self, now: datetime) -> bool:
        for room in self.config.rooms:
            if not room.living_group:
                continue
            rs = self.state.rooms[room.room_id]
            if rs.self_active:
                return True
            if (
                rs.last_active_end is not None
                and (now - rs.last_active_end).total_seconds() < self.tun.living_memory
            ):
                return True
        return False

    def _recompute(self, now: datetime) -> list[Command]:
        s, tun = self.state, self.tun
        plan = Plan()
        e = circadian.factor(s.sun_elevation, now, tun)
        evening = e >= tun.evening_cap_threshold
        g = gain.multiplier(s, tun)

        # Night-path hold expiry (rule 6.2). Its rooms fade out over night_fade.
        night_expiring = False
        if s.night_active and s.night_hold_until is not None:
            if now >= s.night_hold_until:
                s.night_active = False
                s.night_hold_until = None
                night_expiring = True
            else:
                plan.review_at(s.night_hold_until)

        # Morning neutral drift (rule 7.3): edge-triggered on the E>0 -> E==0
        # morning transition, so booting at midday never resets a restored gain.
        if s.last_e is not None and s.last_e > 0.0 and e == 0.0:
            gain.relax_to_neutral(s, tun)
        s.last_e = e

        # Pass 1: advance every room's FSM so adjacency reads settled state.
        for room in self.config.rooms:
            roles.step(self.state.rooms[room.room_id], now, tun, room.shape, room.hold_seconds)
        living = self._living_active(now)

        diags: list[RoomDiagnostics] = []
        for room in self.config.rooms:
            diags.append(
                self._reconcile_room(room, now, e, evening, g, living, night_expiring, plan)
            )

        # Circadian self-scheduling (rule 2.3): follow the ramp with a tick, and
        # from a plateau schedule the next clock-ramp boundary so the engine can
        # start the 20:00 / 06:00 ramps itself without waiting on sun events.
        if s.enabled:
            if 0.0 < e < 1.0:
                plan.review_at(now + timedelta(seconds=tun.circadian_tick))
            else:
                plan.review_at(self._next_clock_boundary(now))
        if self._in_grace(now) and s.start_at is not None:
            plan.review_at(s.start_at + timedelta(seconds=tun.startup_grace))

        return plan.finalize(tuple(diags), s.master_pct, s.master_on, s.enabled)

    def _next_clock_boundary(self, now: datetime) -> datetime:
        """The next local evening_start / morning_start instant after ``now`` (rule 2.3)."""
        tun = self.tun
        minute = now.hour * 60 + now.minute
        bounds = sorted({tun.evening_start_min, tun.morning_start_min})
        for b in bounds:
            if b == minute:
                # Within the boundary minute the clock ramp still evaluates to
                # its plateau value (seconds are truncated), so waking here
                # must land one minute inside the ramp — not half a day away.
                return now.replace(
                    hour=b // 60, minute=b % 60, second=0, microsecond=0
                ) + timedelta(minutes=1)
            if b > minute:
                return now.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)
        b = bounds[0]  # wrap to the earliest boundary tomorrow
        nxt = now + timedelta(days=1)
        return nxt.replace(hour=b // 60, minute=b % 60, second=0, microsecond=0)

    def _reconcile_room(
        self,
        room: RoomConfig,
        now: datetime,
        e: float,
        evening: bool,
        g: float,
        living: bool,
        night_expiring: bool,
        plan: Plan,
    ) -> RoomDiagnostics:
        s, tun = self.state, self.tun
        rs = s.rooms[room.room_id]

        neighbour_active = any(s.rooms[n].self_active for n in room.neighbours if n in s.rooms)
        base = roles.base_role(
            rs, room.shape, room.profile.vacancy, neighbour_active, living, evening
        )
        res = modes.resolve(room, rs, s, e, tun, night_expiring)
        off_worthy = base is Role.OFF and not rs.self_active

        # Override arbitration (rule 9): mode hard-offs and night path win.
        if rs.overridden:
            if res is not None and res.suppress_override:
                pass  # night path suspends the override → fall through
            elif res is not None and res.off:
                override.release(rs)  # sleep/away hard-off releases + wins
            elif override.should_release(rs, s, off_worthy, now, tun):
                override.release(rs)
            else:
                # Still adjusting nothing, but keep both clocks alive: the
                # timeout AND the vacancy-hold expiry that will make the room
                # OFF-worthy and release the latch (F2 — otherwise release could
                # wait up to override_timeout in a quiet house).
                plan.review_at(override.override_review(rs, now, tun))
                plan.review_at(roles.next_review(rs, now))
                return self._diag(room, rs, rs.role)

        # Resolve per-band outputs (funnel order, rule 8.1).
        ct_override: int | None = None
        fade: float | None = None
        if res is not None:
            role = res.role
            if res.off:
                outputs = dict.fromkeys(BAND_ORDER, 0.0)
            else:
                outputs = dict(res.band_outputs or {})
                if not res.gain_exempt:  # night path / outdoor are unscaled (7.4/7.2)
                    outputs = gain.scale(outputs, g)
                    outputs = targets.apply_evening_cap(outputs, e, room.profile, tun)
            ct_override, fade = res.ct_override, res.fade
        else:
            role = base
            outputs = targets.role_outputs(room.profile, role, e, tun)
            outputs = gain.scale(outputs, g)
            outputs = targets.apply_evening_cap(outputs, e, room.profile, tun)

        rs.role = role
        photo = self._photo[room.room_id]
        channel_b = photometry.allocate(room.channels, outputs, e, tun)

        # Emit writes unless observe-only (rule 10) or inside startup grace (11.1).
        if s.enabled and not self._in_grace(now):
            ch_out = {ch: channel_b[ch.channel_id] for ch in room.channels}
            anchor = ct_policy.fixed_anchor(ch_out, tun)
            room_active = role is Role.ACTIVE
            for ch in room.channels:
                b = channel_b[ch.channel_id]
                if ct_override is not None:
                    ct = ct_override if ch.ct_capable else None
                else:
                    ct = ct_policy.ct_target(ch, e, b, anchor, tun)
                governor.plan_channel(
                    plan, ch, rs.channels[ch.channel_id], room_active, b, ct, photo, tun, fade
                )

        plan.review_at(roles.next_review(rs, now))
        return self._diag(room, rs, role, max(channel_b.values(), default=0.0))

    def _diag(
        self, room: RoomConfig, rs: RoomState, role: Role, peak: float | None = None
    ) -> RoomDiagnostics:
        if peak is None:
            peak = max((cs.commanded_b for cs in rs.channels.values()), default=0.0)
        return RoomDiagnostics(
            room_id=room.room_id,
            role=role,
            overridden=rs.overridden,
            target_output=peak,
            natural_lux=None,  # estimator seam (§3)
        )
