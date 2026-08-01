"""Single-writer controller — the adapter actor (mirrors sonos-conductor).

One :class:`Controller` per config entry owns the pure :class:`~.core.engine.Engine`,
a serial event queue drained by one task, the echo ledger, per-channel write
executors (native transition or software stepping ramp), and the single
``ScheduleReview`` timer. Entities never touch the engine directly: they
``submit`` events and read the last-published snapshot.

Layering: the engine is a pure function of (state, event, now); this module is
the only place HA I/O happens. ``handle`` runs the engine synchronously and the
resulting :class:`~.core.plan.Command` list is executed here (rules §8.3/§8.4/§8.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import ButtonDeviceClass
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    LightEntityFeature,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    EVENT_STATE_REPORTED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACTIVITY_SENSOR,
    CONF_ANYONE_HOME_ENTITY,
    CONF_CALIBRATIONS,
    CONF_LUX_SENSOR,
    CONF_NIGHT_TRIGGERS,
    CONF_OCCUPANCY_FALLBACK,
    CONF_PRESENCE_FALLBACK,
    CONF_PRESENCE_PRIMARY,
    CONF_ROOM_ID,
    CONF_ROOMS,
    CONF_SLEEP_ENTITY,
    CONF_TRIGGERS,
    CONF_TV_ENTITIES,
    CONF_VACATION_ENTITY,
    CONF_WALL_EVENTS,
    DOMAIN,
    EVENT_CALIBRATION,
    build_engine_config,
    build_tunables,
    signal_calibration,
    signal_update,
)
from .core.engine import Engine
from .core.events import (
    ActivityChanged,
    ForeignChange,
    HomeChanged,
    LuxReport,
    NightTriggerFired,
    PresenceChanged,
    ReviewTick,
    SleepChanged,
    SunElevationChanged,
    TriggerFired,
    TvChanged,
    VacationChanged,
)
from .core.events import Event as CoreEvent
from .core.model import Activity, InitialSnapshot, RoomDiagnostics
from .core.plan import (
    CalibrationResult,
    PublishState,
    ScheduleReview,
    SetChannel,
    TurnOffChannel,
)

_LOGGER = logging.getLogger(__name__)

#: Sun push cadence: sun.sun elevation attribute changes drive E_sun (§2.3).
SUN_ENTITY = "sun.sun"

#: Brightness echo tolerance (normalized flux) and CT echo tolerance (kelvin).
ECHO_LEVEL_TOL = 0.03
ECHO_CT_TOL = 60

#: Software stepping ramp cadence (~1 step/s, rule §8.2 adapter fallback).
STEP_INTERVAL = 1.0

#: Floor for the fade-corridor overshoot margin: deadline = start + ramp +
#: max(ENVELOPE_MARGIN, 0.5*ramp) so a congested mesh tail never latches (§8.4, F1).
ENVELOPE_MARGIN = 2.0

#: After a lux-wedge Fix flow presses the sensor's ESP reboot button, suppress
#: re-raising the wedge issue for this grace window (§3.5, D17 beta.11) — the
#: sensor takes a moment to boot and resume reporting, so re-raising immediately
#: would just re-nag the user. If it is still silent past the window the reboot
#: did not help and the issue is re-raised.
WEDGE_FIX_GRACE = 120.0


def _monotonic() -> float:
    """Patchable monotonic clock (echo TTL / rate limiting)."""
    return time.monotonic()


def _level_to_brightness(level: float) -> int:
    """Device-resolution quantization: normalized flux → 0-255 (rule §8.3)."""
    return max(1, min(255, round(level * 255.0)))


def _obs_level(state: State | None) -> float | None:
    """Observed normalized output of a light state (``None`` => unavailable)."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if state.state != STATE_ON:
        return 0.0
    bri = state.attributes.get(ATTR_BRIGHTNESS)
    if bri is None:
        return 1.0
    return float(bri) / 255.0


def _obs_ct(state: State | None) -> int | None:
    if state is None:
        return None
    ct = state.attributes.get(ATTR_COLOR_TEMP_KELVIN)
    return int(ct) if ct is not None else None


def _activity_of(state: State | None) -> Activity | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return Activity(state.state)
    except ValueError:
        return None


def _is_on(state: State | None) -> bool | None:
    """Tri-state on/off; ``None`` when unavailable."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    return state.state == STATE_ON


# ---------------------------------------------------------------------------
# Echo ledger (§8.4)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Echo:
    level: float | None
    ct: int | None
    deadline: float


@dataclass(slots=True)
class _Envelope:
    """A live fade-front corridor for a native-transition command (§8.4, F1).

    The Plejd fork steps brightness linearly during a transition, so the
    expected level at time ``t`` is ``front(t)`` — a linear ramp from ``frm`` to
    ``to`` over ``ramp`` seconds. A report is an echo only while it tracks that
    moving front (within a temporal ``slack`` window absorbing device
    nonlinearity/jitter), so a foreign dial is caught the moment the front
    advances past where it stuck — not blindly absorbed for the whole fade.
    """

    frm: float
    to: float
    start: float
    ramp: float
    deadline: float

    def front(self, t: float) -> float:
        if self.ramp <= 0.0:
            return self.to
        p = (t - self.start) / self.ramp
        p = 0.0 if p < 0.0 else 1.0 if p > 1.0 else p
        return self.frm + (self.to - self.frm) * p


class EchoLedger:
    """Per-entity record of own commands; classifies incoming reports (§8.4).

    A one-shot write records a level echo and/or a CT echo (consume-one). A
    *native-transition* write additionally opens a fade-front **corridor**
    (:class:`_Envelope`): a report is an echo iff it lies within the front's
    position over ``[now-slack, now+slack]`` (± ``ECHO_LEVEL_TOL``), so the
    fork's ~150 ms intermediate fade reports are echoes but a dial that drifts
    off the front — including one that sticks while a full-range fade sweeps
    past it — is a foreign change and latches (§9.1). After the corridor
    deadline a normal final-value echo (recorded at corridor creation) still
    absorbs a late completion near target. Wall-event entities bypass all of
    this and always latch (§9.4).
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._entries: dict[str, deque[_Echo]] = {}
        self._envelopes: dict[str, _Envelope] = {}

    def record(
        self, entity_id: str, level: float | None, ct: int | None, ttl: float | None = None
    ) -> None:
        deadline = _monotonic() + (self._ttl if ttl is None else ttl)
        q = self._entries.setdefault(entity_id, deque())
        if level is not None:
            q.append(_Echo(level=level, ct=None, deadline=deadline))
        if ct is not None:
            q.append(_Echo(level=None, ct=ct, deadline=deadline))

    def record_envelope(
        self, entity_id: str, from_level: float, to_level: float, ramp: float
    ) -> None:
        """Open a fade-front corridor and a final-value echo (F1)."""
        start = _monotonic()
        margin = max(ENVELOPE_MARGIN, 0.5 * ramp)  # a congested mesh tail must not latch
        self._envelopes[entity_id] = _Envelope(
            frm=from_level, to=to_level, start=start, ramp=ramp, deadline=start + ramp + margin
        )
        # The final-value echo must OUTLIVE the corridor: with the plain TTL a
        # 4-10 s fade (sleep_fade, night_fade) expires it before the deadline,
        # and a completion report just past the deadline would latch a
        # spurious override.
        self.record(entity_id, to_level, None, ttl=ramp + margin + self._ttl)

    def consume(self, entity_id: str, level: float | None, ct: int | None) -> bool:
        """True if this observation matches (and consumes) a recorded command."""
        now = _monotonic()
        env = self._envelopes.get(entity_id)
        if env is not None:
            if now > env.deadline:
                del self._envelopes[entity_id]
            elif level is not None:
                slack = max(0.5, 0.3 * env.ramp)
                a, b = env.front(now - slack), env.front(now + slack)
                if min(a, b) - ECHO_LEVEL_TOL <= level <= max(a, b) + ECHO_LEVEL_TOL:
                    return True  # tracking the fade front — echo; corridor stays live
        q = self._entries.get(entity_id)
        if not q:
            return False
        while q and q[0].deadline < now:
            q.popleft()
        for echo in list(q):
            if echo.deadline < now:
                continue
            if (
                echo.level is not None
                and level is not None
                and abs(echo.level - level) <= ECHO_LEVEL_TOL
            ):
                q.remove(echo)
                return True
            if echo.ct is not None and ct is not None and abs(echo.ct - ct) <= ECHO_CT_TOL:
                q.remove(echo)
                return True
        return False


# ---------------------------------------------------------------------------
# Per-channel write executor (§8.2/§8.3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Cmd:
    """A pending one-shot channel command (latest-wins slot)."""

    off: bool
    level: float  # normalized target (0 for off)
    ct: int | None
    ramp: float


class ChannelWriter:
    """Serialized per-channel write executor (§8.2/§8.3).

    Every write for a channel flows through one *pending slot* (latest wins)
    and one in-flight guard, so service calls execute strictly in order — a
    ``turn_off`` can never be buried by a stale ``turn_on`` on a slow device
    (F3). Consecutive writes are spaced by ``min_write_interval`` (F2). A move
    is either a single native-transition write, or — when the light has no
    ``TRANSITION`` feature — a software flux-linear stepping ramp whose steps
    are themselves fed one at a time through the same slot. CT is written
    before brightness (§5.4). Site-wide concurrency is capped by the shared
    ``max_inflight`` semaphore.
    """

    def __init__(self, controller: Controller, entity_id: str) -> None:
        self._c = controller
        self.entity_id = entity_id
        self._min_interval = controller.tun.min_write_interval
        self._pending: _Cmd | None = None
        self._inflight = False
        self._last_write = 0.0
        self._rate_cancel: CALLBACK_TYPE | None = None
        self._step_cancel: CALLBACK_TYPE | None = None
        self._step_state: tuple[float, float, int | None, int, bool] | None = None
        self._stopped = False

    @callback
    def cancel(self) -> None:
        """Stop the writer for good (unload): no more pumps, cancel all timers."""
        self._stopped = True
        self._pending = None
        self._cancel_stepping()
        if self._rate_cancel is not None:
            self._rate_cancel()
            self._rate_cancel = None

    @callback
    def set_channel(self, level: float, ct: int | None, ramp: float) -> None:
        self._cancel_stepping()
        if self._c.supports_transition(self.entity_id) or ramp <= STEP_INTERVAL:
            self._submit(_Cmd(off=False, level=max(0.0, min(1.0, level)), ct=ct, ramp=ramp))
        else:
            self._start_stepping(_level_to_brightness(level) / 255.0, ct, ramp, off_at_end=False)

    @callback
    def turn_off(self, ramp: float) -> None:
        self._cancel_stepping()
        if self._c.supports_transition(self.entity_id) or ramp <= STEP_INTERVAL:
            self._submit(_Cmd(off=True, level=0.0, ct=None, ramp=ramp))
        else:
            self._start_stepping(0.0, None, ramp, off_at_end=True)

    # -- pending slot + rate limit + serialization (F2/F3) -----------------

    @callback
    def _submit(self, cmd: _Cmd) -> None:
        self._pending = cmd  # latest wins: an intervening command is coalesced away
        self._pump()

    @callback
    def _pump(self) -> None:
        if (
            self._stopped
            or self._inflight
            or self._pending is None
            or self._rate_cancel is not None
        ):
            return
        wait = self._min_interval - (_monotonic() - self._last_write)
        if wait > 0:
            self._rate_cancel = async_call_later(self._c.hass, wait, self._on_rate)
        else:
            self._flush()

    @callback
    def _on_rate(self, _now: Any) -> None:
        self._rate_cancel = None
        self._flush()  # the spacing has elapsed — execute without re-checking

    @callback
    def _flush(self) -> None:
        if self._inflight or self._pending is None:
            return
        cmd, self._pending = self._pending, None
        self._inflight = True
        self._last_write = _monotonic()
        coro = self._do_off(cmd.ramp) if cmd.off else self._do_single(cmd.level, cmd.ct, cmd.ramp)
        task = self._c.async_run_write(coro)
        task.add_done_callback(self._on_write_done)

    @callback
    def _on_write_done(self, _task: asyncio.Task) -> None:
        self._inflight = False
        if self._stopped:
            return  # unloaded mid-flight — never re-pump / re-arm a timer
        self._pump()

    # -- one-shot writes ---------------------------------------------------

    async def _do_single(self, level: float, ct: int | None, ramp: float) -> None:
        brightness = _level_to_brightness(level)
        native = self._c.supports_transition(self.entity_id) and ramp > 0
        if ct is not None:
            # CT before brightness (rule §5.4): the OUTPUT_SET can clobber CT.
            await self._c.async_call_light(
                self.entity_id, {ATTR_COLOR_TEMP_KELVIN: ct}, level=None, ct=ct
            )
        data: dict[str, Any] = {ATTR_BRIGHTNESS: brightness}
        target = brightness / 255.0
        if native:
            data[ATTR_TRANSITION] = ramp
            frm = _obs_level(self._c.hass.states.get(self.entity_id)) or 0.0
            await self._c.async_call_light(
                self.entity_id, data, level=target, ct=None, envelope=(frm, target, ramp)
            )
        else:
            await self._c.async_call_light(self.entity_id, data, level=target, ct=None)

    async def _do_off(self, ramp: float) -> None:
        native = self._c.supports_transition(self.entity_id) and ramp > 0
        data: dict[str, Any] = {}
        if native:
            data[ATTR_TRANSITION] = ramp
            frm = _obs_level(self._c.hass.states.get(self.entity_id)) or 0.0
            await self._c.async_call_light(
                self.entity_id, data, level=0.0, ct=None, turn_off=True, envelope=(frm, 0.0, ramp)
            )
        else:
            await self._c.async_call_light(self.entity_id, data, level=0.0, ct=None, turn_off=True)

    # -- software stepping ramp (no native transition) ---------------------

    def _cancel_stepping(self) -> None:
        if self._step_cancel is not None:
            self._step_cancel()
            self._step_cancel = None
        self._step_state = None

    def _start_stepping(self, goal_b: float, ct: int | None, ramp: float, off_at_end: bool) -> None:
        b0 = _obs_level(self._c.hass.states.get(self.entity_id)) or 0.0
        steps = max(1, round(ramp / STEP_INTERVAL))
        self._step_state = (b0, goal_b, ct, steps, off_at_end)
        self._ramp_step(0)

    @callback
    def _ramp_step(self, i: int) -> None:
        if self._step_state is None:
            return
        b0, goal_b, ct, steps, off_at_end = self._step_state
        i += 1
        frac = i / steps
        # Flux-linear interpolation under the default square-law curve — one
        # step "looks" the same size at any level (rule §4.3 intent). Each step
        # goes through the shared pending slot, so it is spaced + serialized and
        # records its own per-step echo (F1: the stepping path stays as-is).
        b = (b0 * b0 + (goal_b * goal_b - b0 * b0) * frac) ** 0.5
        last = i >= steps
        if last and off_at_end:
            self._submit(_Cmd(off=True, level=0.0, ct=None, ramp=0.0))
        else:
            self._submit(_Cmd(off=False, level=max(0.0, b), ct=ct if i == 1 else None, ramp=0.0))
        if not last:
            self._step_cancel = async_call_later(
                self._c.hass, STEP_INTERVAL, lambda _now: self._ramp_step(i)
            )
        else:
            self._step_cancel = None


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class Controller:
    """One serial actor per config entry."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.options: Mapping[str, Any] = entry.options
        self.tun = build_tunables(entry.options)
        self.engine = Engine(
            build_engine_config(hass, entry.options),
            tunables=self.tun,
            calibrations=self._load_calibrations(),
        )
        self._queue: deque[CoreEvent] = deque()
        self._task: asyncio.Task | None = None
        self._draining = False
        self._started = False
        self._unsubs: list[CALLBACK_TYPE] = []
        self._review_cancel: CALLBACK_TYPE | None = None
        self._echo = EchoLedger(self.tun.echo_window)
        #: Standing setpoint per channel — poll re-confirmations are not foreign.
        self._last_commanded: dict[str, float] = {}
        self._sem = asyncio.Semaphore(self.tun.max_inflight)
        self._writers: dict[str, ChannelWriter] = {}
        self._write_tasks: set[asyncio.Task] = set()
        #: Lux sensor entity ids with an active "wedged" repairs issue (§3.5).
        self._wedged: set[str] = set()
        #: Lux sensor entity id -> utcnow() at which its Fix flow last pressed the
        #: ESP reboot button. Consulted by ``_check_lux_wedge`` to grace-suppress
        #: an immediate re-raise (§3.5, D17 beta.11). Per-controller: cleared on
        #: reload with the rest of the wedge state.
        self._wedge_fix_pressed: dict[str, datetime] = {}

        # Published snapshot the entities read (rule §10).
        self.diagnostics: dict[str, RoomDiagnostics] = {}
        self.master_pct: float = self.engine.state.master_pct
        self.master_on: bool = self.engine.state.master_on
        # Fail-safe boundary (§10/§11): a fresh install AND a missed restore
        # both boot observe-only — the conductor must never go live unbidden
        # after a restart. The enabled switch's restore submits
        # SetEnabled(True) when the stored state was on.
        self.enabled: bool = False

        self._build_indexes()

    # -- config indexing ----------------------------------------------------

    def _rooms(self) -> Iterable[Mapping[str, Any]]:
        return self.options.get(CONF_ROOMS, ())

    def _build_indexes(self) -> None:
        self._channel_room: dict[str, str] = {}
        self._lux_room: dict[str, str] = {}
        self._presence_primary: dict[str, str] = {}
        self._activity_room: dict[str, str] = {}
        self._occ_fallback: dict[str, str] = {}
        self._trigger_room: dict[str, str] = {}
        self._wall_room: dict[str, str] = {}
        self._room_channels: dict[str, list[str]] = {}
        for room in self._rooms():
            rid = room[CONF_ROOM_ID]
            chans = [c["entity"] for c in room.get("channels", ())]
            self._room_channels[rid] = chans
            for cid in chans:
                self._channel_room[cid] = rid
            if room.get(CONF_LUX_SENSOR):
                self._lux_room[room[CONF_LUX_SENSOR]] = rid
            if room.get(CONF_PRESENCE_PRIMARY):
                self._presence_primary[room[CONF_PRESENCE_PRIMARY]] = rid
            if room.get(CONF_ACTIVITY_SENSOR):
                self._activity_room[room[CONF_ACTIVITY_SENSOR]] = rid
            for e in room.get(CONF_OCCUPANCY_FALLBACK, ()):
                self._occ_fallback[e] = rid
            for e in room.get(CONF_TRIGGERS, ()):
                self._trigger_room[e] = rid
            for e in room.get(CONF_WALL_EVENTS, ()):
                self._wall_room[e] = rid

    def _load_calibrations(self) -> dict[str, Any]:
        from .core.model import RoomCalibration

        raw = self.options.get(CONF_CALIBRATIONS, {}) or {}
        out: dict[str, Any] = {}
        for room_id, data in raw.items():
            # from_dict raises ValueError on a corrupt payload; is_valid() guards
            # NaN / negative-gain / non-monotone curves. Either way: discard, log,
            # leave the room uncalibrated (rule 5 of the estimator brief).
            try:
                cal = RoomCalibration.from_dict(data)
            except (ValueError, KeyError, TypeError) as err:
                _LOGGER.warning("Discarding unreadable calibration for %s: %s", room_id, err)
                continue
            if not cal.is_valid():
                _LOGGER.warning("Discarding malformed calibration for %s", room_id)
                continue
            out[room_id] = cal
        return out

    # -- lifecycle ----------------------------------------------------------

    def build_snapshot(self) -> InitialSnapshot:
        """Seed the engine from live + restored world state (§11, no flash)."""
        hass = self.hass
        occupancy: dict[str, bool | None] = {}
        activity: dict[str, Activity | None] = {}
        channels: dict[str, tuple[float, int | None]] = {}
        for room in self._rooms():
            rid = room[CONF_ROOM_ID]
            if room.get(CONF_PRESENCE_PRIMARY):
                occupancy[rid] = _is_on(hass.states.get(room[CONF_PRESENCE_PRIMARY]))
            if room.get(CONF_ACTIVITY_SENSOR):
                activity[rid] = _activity_of(hass.states.get(room[CONF_ACTIVITY_SENSOR]))
            for cid in self._room_channels.get(rid, ()):
                st = hass.states.get(cid)
                lvl = _obs_level(st)
                if lvl:
                    channels[cid] = (lvl, _obs_ct(st))
        sun = hass.states.get(SUN_ENTITY)
        sun_elev = None
        if sun is not None:
            sun_elev = sun.attributes.get("elevation")
        # away_lighting / enabled / master_* are restorable entity state, not
        # world state — they are authoritatively restored by their entities'
        # restore events (queued before the first ReviewTick), so the snapshot
        # just carries the engine defaults here.
        return InitialSnapshot(
            enabled=self.enabled,
            sun_elevation=float(sun_elev) if sun_elev is not None else None,
            sleep=self._resolve_bool(self.options.get(CONF_SLEEP_ENTITY)),
            anyone_home=self._resolve_home(),
            vacation=self._resolve_bool(self.options.get(CONF_VACATION_ENTITY)),
            tv_playing=self._resolve_tv(),
            master_on=self.master_on,
            master_pct=self.master_pct,
            occupancy=occupancy,
            activity=activity,
            channels=channels,
        )

    async def async_start(self, snapshot: InitialSnapshot) -> None:
        """Re-seed the engine from the snapshot and arm subscriptions (§11)."""
        # Rebuild engine on the restored snapshot so seeding adopts baselines.
        self.engine = Engine(
            build_engine_config(self.hass, self.options),
            snapshot=snapshot,
            tunables=self.tun,
            calibrations=self._load_calibrations(),
        )
        # Seed the command ledger from the live snapshot BEFORE subscriptions
        # arm (§8, §11.1): a config-entry reload rebuilds this controller and
        # wipes the per-channel _last_commanded tracking. The Plejd 3-min true-
        # state poll then re-reports the PRE-reload standing level as a float and,
        # with no ledger match, latches a FALSE manual override ~seconds later.
        # Seeding each channel from its current on/off state makes that first
        # poll re-report tolerance-match and be consumed. Accepted trade-off: a
        # genuine manual change made in the snapshot→first-report gap is absorbed
        # once (documented in §8).
        self._seed_command_ledger()
        self._subscribe()
        self._started = True
        # Re-read live presence/activity now that subscriptions are armed: a state
        # change that landed in the setup gap — between build_snapshot() and
        # _subscribe() — fires no event and would otherwise be lost, leaving a
        # room stuck in its seeded (e.g. OFF, from an unavailable primary) role
        # until an unrelated change happened to re-evaluate it (post-restart role
        # stickiness, shadow audit). These queue before the first ReviewTick.
        self._reconfirm_live_state()
        # First review kicks self-scheduling and publishes initial state.
        self.submit(ReviewTick())

    def _seed_command_ledger(self) -> None:
        """Seed per-channel ``_last_commanded`` from current light state (§8/§11.1).

        For every configured channel, adopt its current observed normalized level
        as the standing setpoint (0.0 when off, the brightness/255 when on), using
        the SAME normalization + tolerance semantics the poll-reconfirmation path
        in ``_on_light_change`` consumes. An unavailable channel is left unseeded
        (it reconciles on availability recovery). Idempotent on a fresh setup —
        the first reconcile's own command overwrites the seed as usual.
        """
        for cid in self._channel_room:
            level = _obs_level(self.hass.states.get(cid))
            if level is not None:  # 0.0 (off) is a valid setpoint; None (dead) is not
                self._last_commanded[cid] = level

    def _reconfirm_live_state(self) -> None:
        """Re-submit live presence/activity per room after arming subscriptions.

        Closes the startup gap (§11): the snapshot is read before subscriptions
        exist, so any presence/activity change during setup fires no event. This
        replays the current live values so the engine's view matches reality the
        moment the actor starts (idempotent when nothing changed).
        """
        for room in self._rooms():
            rid = room[CONF_ROOM_ID]
            if room.get(CONF_PRESENCE_PRIMARY) or room.get(CONF_OCCUPANCY_FALLBACK):
                self.submit(
                    PresenceChanged(
                        room_id=rid,
                        primary=self._room_primary(rid),
                        fallback=self._room_fallback(rid),
                    )
                )
            if room.get(CONF_ACTIVITY_SENSOR):
                self.submit(
                    ActivityChanged(
                        room_id=rid,
                        activity=_activity_of(self.hass.states.get(room[CONF_ACTIVITY_SENSOR])),
                    )
                )

    async def async_stop(self) -> None:
        self._started = False
        # Withdraw outstanding lux-wedge repairs issues: `_wedged` is
        # per-instance state, so an issue surviving stop would never be
        # deleted by the next controller (stale false notice after a
        # recovery-during-reload; orphaned forever on entry removal). A
        # still-wedged sensor is simply re-flagged by the fresh controller.
        for entity_id in self._wedged:
            ir.async_delete_issue(self.hass, DOMAIN, f"lux_wedged_{entity_id}")
        self._wedged.clear()
        self._wedge_fix_pressed.clear()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._review_cancel is not None:
            self._review_cancel()
            self._review_cancel = None
        # Stop writers FIRST (sets each _stopped so an in-flight write's
        # completion can't re-pump or re-arm a rate timer), then cancel the
        # in-flight write tasks, then the drain task.
        for w in self._writers.values():
            w.cancel()
        for t in list(self._write_tasks):
            t.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # -- serial event loop --------------------------------------------------
    #
    # One tracked drain task processes the queue in submit order and *exits*
    # when the queue empties (a never-ending actor would deadlock
    # ``async_block_till_done``). A submit re-arms the task if none is running,
    # so the single-writer discipline holds: ``_process`` is synchronous and
    # runs to completion before the next event.

    @callback
    def submit(self, event: CoreEvent) -> None:
        self._queue.append(event)
        if not self._draining and self._started:
            self._draining = True
            self._task = self.hass.async_create_task(
                self._drain(), name=f"light_conductor-actor-{self.entry.entry_id}"
            )

    async def _drain(self) -> None:
        try:
            while self._queue:
                event = self._queue.popleft()
                try:
                    self._process(event)
                except Exception:
                    _LOGGER.exception("Error handling %s", type(event).__name__)
                await asyncio.sleep(0)  # let write tasks interleave
        finally:
            self._draining = False
            self._task = None

    def _process(self, event: CoreEvent) -> None:
        commands = self.engine.handle(event, dt_util.utcnow())
        for cmd in commands:
            # Isolate each command (F4): one bad exec must not swallow the
            # trailing ScheduleReview/PublishState and stall the self-schedule.
            try:
                match cmd:
                    case SetChannel():
                        self._exec_set(cmd)
                    case TurnOffChannel():
                        self._exec_off(cmd)
                    case PublishState():
                        self._exec_publish(cmd)
                    case ScheduleReview():
                        self._exec_review(cmd)
                    case CalibrationResult():
                        self._exec_calibration(cmd)
            except Exception:
                _LOGGER.exception("Error executing %s", type(cmd).__name__)

    # -- plan execution -----------------------------------------------------

    def _writer(self, entity_id: str) -> ChannelWriter:
        w = self._writers.get(entity_id)
        if w is None:
            w = ChannelWriter(self, entity_id)
            self._writers[entity_id] = w
        return w

    def _exec_set(self, cmd: SetChannel) -> None:
        self._writer(cmd.channel_id).set_channel(cmd.level, cmd.ct, cmd.ramp_seconds)

    def _exec_off(self, cmd: TurnOffChannel) -> None:
        self._writer(cmd.channel_id).turn_off(cmd.ramp_seconds)

    def _exec_publish(self, cmd: PublishState) -> None:
        self.diagnostics = {d.room_id: d for d in cmd.rooms}
        self.master_pct = cmd.master_pct
        self.master_on = cmd.master_on
        self.enabled = cmd.enabled
        # Piggyback the lux-wedge check on the publish cadence (§3.5) — no new
        # timer; every recompute publishes and re-checks every configured sensor.
        self._check_lux_wedge(dt_util.utcnow())
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    def _check_lux_wedge(self, now: datetime) -> None:
        """Raise/clear a repairs issue for a wedged lux sensor (§3.5).

        A wedged sensor's entity stays AVAILABLE but stops reporting (a known
        Apollo MSR-2 LTR390 hardware quirk, fix = its ESP reboot button). We
        detect it off the estimator's ``last_report_at`` ageing past
        ``lux_wedge_warn`` while the entity is still available, and clear the
        issue the moment reports resume. Ordinary unavailability (§8.5) is NOT a
        wedge and never raises the issue.
        """
        threshold = self.tun.lux_wedge_warn
        for entity_id, room_id in self._lux_room.items():
            st = self.hass.states.get(entity_id)
            available = st is not None and st.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            rs = self.engine.state.rooms.get(room_id)
            last = rs.est.last_report_at if rs is not None else None
            wedged = available and last is not None and (now - last).total_seconds() > threshold

            if not wedged:
                # Reports resumed (or the sensor went unavailable): clear any
                # outstanding issue and the post-Fix grace. Ordinary
                # unavailability (§8.5) reaches here too and never raises.
                self._wedge_fix_pressed.pop(entity_id, None)
                if entity_id in self._wedged:
                    self._wedged.discard(entity_id)
                    ir.async_delete_issue(self.hass, DOMAIN, f"lux_wedged_{entity_id}")
                continue

            if entity_id in self._wedged:
                continue  # already flagged; do not re-warn or re-raise (log once)

            # Fix-flow grace (§3.5, D17 beta.11): the reboot button was pressed
            # for this sensor within WEDGE_FIX_GRACE — give the ESP time to boot
            # before re-nagging. Past the window a still-silent sensor means the
            # reboot did not help, so fall through and re-raise.
            pressed = self._wedge_fix_pressed.get(entity_id)
            if pressed is not None and (now - pressed).total_seconds() < WEDGE_FIX_GRACE:
                continue

            self._wedged.add(entity_id)
            _LOGGER.warning(
                "Lux sensor %s (room %s) appears wedged: available but no report in "
                "%.0f s — press its ESP reboot button",
                entity_id,
                room_id,
                threshold,
            )
            button = self._resolve_restart_button(entity_id)
            if button is not None:
                # A restart button on the same device exists — offer a one-press
                # Fix. Stash what repairs.py's flow needs (HA passes issue.data to
                # async_create_fix_flow): the button to press, the sensor it heals,
                # the room for text, and the owning entry so the flow can reach
                # this controller to mark the grace timestamp.
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"lux_wedged_{entity_id}",
                    is_fixable=True,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="lux_wedged_fixable",
                    translation_placeholders={
                        "entity_id": entity_id,
                        "room": room_id,
                        "button": button,
                    },
                    data={
                        "entry_id": self.entry.entry_id,
                        "sensor_entity_id": entity_id,
                        "button_entity_id": button,
                        "room": room_id,
                    },
                )
            else:
                # No restart button resolvable — keep the beta.10 non-fixable
                # notice with its manual instruction (fallback path).
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"lux_wedged_{entity_id}",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="lux_wedged",
                    translation_placeholders={"entity_id": entity_id, "room": room_id},
                )

    def _resolve_restart_button(self, entity_id: str) -> str | None:
        """Resolve the ESP reboot button on the SAME device as a lux sensor.

        sensor entity -> ``device_id`` -> that device's ENABLED ``button``
        entities -> the one whose effective registry device class
        (``device_class`` override, else ``original_device_class``) is
        :attr:`ButtonDeviceClass.RESTART` (the state may be unavailable while the
        sensor is wedged, so trust the registry, not the live attribute; a
        DISABLED button cannot be pressed — offering it would make Fix a silent
        no-op, so disabled entities are excluded and the issue falls back to the
        manual notice). Returns the first match sorted by entity_id
        (deterministic if a device somehow exposes several restart buttons), or
        ``None`` when the sensor is not in the registry, has no device, or the
        device has no enabled restart button — in which case the wedge issue
        stays non-fixable.
        """
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None
        candidates = [
            e.entity_id
            for e in er.async_entries_for_device(ent_reg, entry.device_id)
            if e.domain == BUTTON_DOMAIN
            and (e.device_class or e.original_device_class) == ButtonDeviceClass.RESTART
        ]
        if not candidates:
            return None
        return sorted(candidates)[0]

    @callback
    def note_wedge_fix_pressed(self, entity_id: str) -> None:
        """Record that a Fix flow just pressed ``entity_id``'s reboot button.

        Called from ``repairs.py`` after the ``button.press``. Stamps the grace
        window and drops the sensor from ``_wedged`` — HA deletes the issue when
        the fix flow finishes, so ``_wedged`` would otherwise disagree with the
        registry and never re-raise (or re-clear). The next ``_check_lux_wedge``
        pass then honours the grace window before re-raising.
        """
        self._wedge_fix_pressed[entity_id] = dt_util.utcnow()
        self._wedged.discard(entity_id)

    def _exec_review(self, cmd: ScheduleReview) -> None:
        if self._review_cancel is not None:
            self._review_cancel()
            self._review_cancel = None
        delay = max(0.0, (cmd.at - dt_util.utcnow()).total_seconds())
        self._review_cancel = async_call_later(self.hass, delay, self._on_review)

    @callback
    def _on_review(self, _now: Any) -> None:
        self._review_cancel = None
        self.submit(ReviewTick())

    def _exec_calibration(self, cmd: CalibrationResult) -> None:
        data = {
            "room_id": cmd.room_id,
            "ok": cmd.ok,
            "reason": cmd.reason,
            "coverage": dict(cmd.coverage),
        }
        self.hass.bus.async_fire(EVENT_CALIBRATION, data)
        async_dispatcher_send(self.hass, signal_calibration(self.entry.entry_id, cmd.room_id), data)
        if cmd.ok:
            self._persist_calibration(cmd.room_id)

    def _persist_calibration(self, room_id: str) -> None:
        cal = self.engine.calibration_of(room_id)
        cals = dict(self.options.get(CONF_CALIBRATIONS, {}) or {})
        cals[room_id] = cal.to_dict()
        new_options = {**self.options, CONF_CALIBRATIONS: cals}
        # Runtime write: RUNTIME_OPTION_KEYS is excluded from the reload guard,
        # so this commit never triggers an entry reload loop.
        self.options = new_options
        self.hass.config_entries.async_update_entry(self.entry, options=new_options)

    # -- write plumbing -----------------------------------------------------

    def supports_transition(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        feats = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        return bool(feats & LightEntityFeature.TRANSITION)

    @callback
    def async_run_write(self, coro: Any) -> asyncio.Task:
        task = self.hass.async_create_task(coro)
        self._write_tasks.add(task)
        task.add_done_callback(self._on_write_done)
        return task

    @callback
    def _on_write_done(self, task: asyncio.Task) -> None:
        self._write_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            _LOGGER.error("Light write failed: %s", exc, exc_info=exc)

    async def async_call_light(
        self,
        entity_id: str,
        data: Mapping[str, Any],
        *,
        level: float | None,
        ct: int | None,
        turn_off: bool = False,
        envelope: tuple[float, float, float] | None = None,
    ) -> None:
        if self.hass.states.is_state(entity_id, STATE_UNAVAILABLE):
            return  # §8.5: never queue against a dead link
        # Record the echo BEFORE the (non-blocking) service call (§8.4). A
        # native-transition write records a fade envelope so intermediate mesh
        # reports during the fade are not mistaken for a foreign change (F1).
        if envelope is not None:
            self._echo.record_envelope(entity_id, *envelope)
        else:
            self._echo.record(entity_id, level, ct)
        # Persist the standing setpoint: integrations that poll true device
        # state (Plejd: every ~3 min, as a float) re-report our own value long
        # after the echo TTL — such re-confirmations must never read as
        # foreign changes (they would latch a false override within minutes
        # of every command).
        target = envelope[1] if envelope is not None else level
        if target is not None:
            self._last_commanded[entity_id] = target
        service = SERVICE_TURN_OFF if turn_off else SERVICE_TURN_ON
        async with self._sem:  # §8.3 max_inflight concurrency cap
            await self.hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {ATTR_ENTITY_ID: entity_id, **data},
                blocking=True,
            )

    # -- subscriptions ------------------------------------------------------

    def _subscribe(self) -> None:
        hass = self.hass
        track = async_track_state_change_event

        lights = list(self._channel_room)
        if lights:
            self._unsubs.append(track(hass, lights, self._on_light_change))
        walls = list(self._wall_room)
        if walls:
            self._unsubs.append(track(hass, walls, self._on_wall_event))

        # Lux sensors: state_reported catches same-value 1 Hz samples (§3);
        # state_changed catches availability transitions.
        lux = list(self._lux_room)
        if lux:
            lux_set = set(lux)

            @callback
            def _lux_filter(event_data: Mapping[str, Any], _s: set[str] = lux_set) -> bool:
                # HA calls the filter with the event *data* mapping, not the Event.
                return event_data.get("entity_id") in _s

            self._unsubs.append(
                hass.bus.async_listen(
                    EVENT_STATE_REPORTED, self._on_lux_reported, event_filter=_lux_filter
                )
            )
            self._unsubs.append(track(hass, lux, self._on_lux_changed))

        presence = list(self._presence_primary) + list(self._occ_fallback)
        if presence:
            self._unsubs.append(track(hass, presence, self._on_presence_change))
        activity = list(self._activity_room)
        if activity:
            self._unsubs.append(track(hass, activity, self._on_activity_change))
        triggers = list(self._trigger_room)
        if triggers:
            self._unsubs.append(track(hass, triggers, self._on_trigger))

        globals_: list[str] = []
        for key in (CONF_SLEEP_ENTITY, CONF_VACATION_ENTITY, CONF_ANYONE_HOME_ENTITY):
            if self.options.get(key):
                globals_.append(self.options[key])
        globals_.extend(self.options.get(CONF_PRESENCE_FALLBACK, ()))
        globals_.extend(self.options.get(CONF_TV_ENTITIES, ()))
        night = list(self.options.get(CONF_NIGHT_TRIGGERS, ()))
        if globals_:
            self._unsubs.append(track(hass, globals_, self._on_global_change))
        if night:
            self._unsubs.append(track(hass, night, self._on_night_trigger))
        self._unsubs.append(track(hass, [SUN_ENTITY], self._on_sun_change))

    # -- subscription handlers ---------------------------------------------

    @callback
    def _on_light_change(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        level = _obs_level(new)
        ct = _obs_ct(new)
        # Availability recovery (§8.5): re-reconcile quietly, never override.
        if old is not None and old.state == STATE_UNAVAILABLE:
            self.submit(ReviewTick())
            return
        if level is None:
            return  # went unavailable — handled on recovery
        if self._echo.consume(entity_id, level, ct):
            return  # our own command echo (incl. an intermediate fade sample)
        # Re-confirmation of the standing setpoint (e.g. Plejd's 3-min true-
        # state poll re-reporting our value as a float): not a foreign change.
        # Only a NO-OP re-report qualifies — if the light actually moved to
        # reach the setpoint value (old level differs materially), that is a
        # genuine change (a wall dial restoring the previous level lands
        # exactly on our last command) and must latch (§9.1/§11.1).
        last = self._last_commanded.get(entity_id)
        if last is not None and abs(level - last) <= ECHO_LEVEL_TOL:
            old_level = _obs_level(old) if old is not None else None
            if old_level is None or abs(old_level - last) <= ECHO_LEVEL_TOL:
                return
        # level is 0.0 (off) or > 0 here — pass it through verbatim; the engine
        # reads 0/None alike as "off" but 0.0 must not become a spurious None.
        self.submit(ForeignChange(channel_id=entity_id, level=level, ct=ct))

    @callback
    def _on_wall_event(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is None or new.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        # §9.4: an availability-recovery republish never latches. ESPHome event
        # entities re-emit their PREVIOUS event timestamp when the device
        # reconnects (unavailable/unknown/None -> old timestamp), which is not a
        # human press — a genuine press always transitions from one valid event
        # timestamp to a strictly NEWER one. Guard the recovery edge (old None or
        # unavailable) and the identical-timestamp republish (new == old). This
        # is what falsely latched gang + sofakrok in the same second (08:08:23)
        # during a Plejd/ESPHome availability blip.
        if old is None or old.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        if new.state == old.state:
            return
        room_id = self._wall_room[entity_id]
        # A wall press latches override for the whole room, adopting each
        # channel's currently observed level/ct (rule §9.4).
        for cid in self._room_channels.get(room_id, ()):
            st = self.hass.states.get(cid)
            self.submit(
                ForeignChange(
                    channel_id=cid,
                    level=_obs_level(st),  # 0.0 (off) stays 0.0, not None
                    ct=_obs_ct(st),
                    wall_event=True,
                )
            )

    @callback
    def _on_lux_reported(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        new: State | None = event.data.get("new_state")
        room_id = self._lux_room.get(entity_id)
        if room_id is None:
            return
        self.submit(LuxReport(room_id=room_id, lux=_float_or_none(new)))

    @callback
    def _on_lux_changed(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        room_id = self._lux_room.get(event.data["entity_id"])
        if room_id is None:
            return
        self.submit(LuxReport(room_id=room_id, lux=_float_or_none(new)))

    @callback
    def _on_presence_change(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        room_id = self._presence_primary.get(entity_id) or self._occ_fallback.get(entity_id)
        if room_id is None:
            return
        self.submit(
            PresenceChanged(
                room_id=room_id,
                primary=self._room_primary(room_id),
                fallback=self._room_fallback(room_id),
            )
        )

    @callback
    def _on_activity_change(self, event: Event) -> None:
        room_id = self._activity_room.get(event.data["entity_id"])
        if room_id is None:
            return
        self.submit(
            ActivityChanged(room_id=room_id, activity=_activity_of(event.data.get("new_state")))
        )

    @callback
    def _on_trigger(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        room_id = self._trigger_room[entity_id]
        if new is None:
            return
        # binary_sensor door: on=open (trigger), off=close (shortened hold).
        if new.domain == "binary_sensor":
            if new.state == STATE_ON:
                self.submit(TriggerFired(room_id=room_id, closing=False))
            elif old is not None and old.state == STATE_ON:
                self.submit(TriggerFired(room_id=room_id, closing=True))
        else:  # event.* pass-by / momentary
            self.submit(TriggerFired(room_id=room_id, closing=False))

    @callback
    def _on_night_trigger(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is None:
            return
        if new.domain == "binary_sensor" and new.state != STATE_ON:
            return
        if old is not None and new.state == old.state and new.domain == "binary_sensor":
            return
        self.submit(NightTriggerFired())

    @callback
    def _on_sun_change(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        if new is None:
            return
        elev = new.attributes.get("elevation")
        if elev is not None:
            self.submit(SunElevationChanged(elevation_deg=float(elev)))

    @callback
    def _on_global_change(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        opts = self.options
        if entity_id == opts.get(CONF_SLEEP_ENTITY):
            self.submit(SleepChanged(active=self._resolve_bool(entity_id)))
        elif entity_id == opts.get(CONF_VACATION_ENTITY):
            self.submit(VacationChanged(active=self._resolve_bool(entity_id)))
        elif entity_id in opts.get(CONF_TV_ENTITIES, ()):
            self.submit(TvChanged(playing=self._resolve_tv()))
        else:  # anyone_home primary or a home fallback presence entity
            self.submit(HomeChanged(anyone_home=self._resolve_home()))

    # -- resolvers ----------------------------------------------------------

    def _resolve_bool(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        return self.hass.states.is_state(entity_id, STATE_ON)

    def _resolve_tv(self) -> bool:
        return any(
            self.hass.states.is_state(e, "playing") for e in self.options.get(CONF_TV_ENTITIES, ())
        )

    def _resolve_home(self) -> bool | None:
        primary = self.options.get(CONF_ANYONE_HOME_ENTITY)
        vals: list[bool | None] = []
        if primary:
            vals.append(_is_on(self.hass.states.get(primary)))
        for e in self.options.get(CONF_PRESENCE_FALLBACK, ()):
            vals.append(_is_on(self.hass.states.get(e)))
        known = [v for v in vals if v is not None]
        if not known:
            return None  # §6.4 fails safe as home downstream
        return any(known)

    def _room_primary(self, room_id: str) -> bool | None:
        for room in self._rooms():
            if room[CONF_ROOM_ID] == room_id and room.get(CONF_PRESENCE_PRIMARY):
                return _is_on(self.hass.states.get(room[CONF_PRESENCE_PRIMARY]))
        return None

    def _room_fallback(self, room_id: str) -> bool | None:
        vals: list[bool | None] = []
        for room in self._rooms():
            if room[CONF_ROOM_ID] != room_id:
                continue
            for e in room.get(CONF_OCCUPANCY_FALLBACK, ()):
                vals.append(_is_on(self.hass.states.get(e)))
        known = [v for v in vals if v is not None]
        if not known:
            return None
        return any(known)


def _float_or_none(state: State | None) -> float | None:
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        return float(state.state)
    except TypeError, ValueError:
        return None
