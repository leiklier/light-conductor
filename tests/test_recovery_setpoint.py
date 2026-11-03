"""Adapter e2e for D28: adopted-level setpoints and availability recovery.

Live: Plejd's ~3-min poll flips a lit lamp between integer and float
brightness. The standing-setpoint guard only knew the conductor's OWN commands,
so every flip of a user-set level re-latched (re-stamping override_since), and
after a restart — snapshot taken while the lights were unavailable — the first
poll flip after the one-shot recovery fold latched (17:14:46 recovered ->
sofakrok latched 17:15:26; 22:43:28 -> kontor 22:43:33, sofakrok 22:43:54).
"""

from __future__ import annotations

from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.light_conductor import controller as lc_controller
from custom_components.light_conductor.const import DOMAIN

from .adapter import options, room, set_light, setup_entry


async def _observe_only(hass: HomeAssistant, monkeypatch, *, light_state: str | None):
    clock = [1000.0]
    monkeypatch.setattr(lc_controller, "_monotonic", lambda: clock[0])
    if light_state == "unavailable":
        hass.states.async_set("light.a", STATE_UNAVAILABLE)
    elif light_state is not None:
        set_light(hass, "light.a", light_state, brightness=128, transition=True)
    hass.states.async_set("binary_sensor.pa", "on")  # ACTIVE: never off-worthy
    entry = await setup_entry(
        hass,
        options([room("a", ["light.a"], presence="binary_sensor.pa")]),
        enabled=False,  # observe-only: no own command touches the ledger
    )
    return hass.data[DOMAIN][entry.entry_id], clock


async def test_poll_flips_of_a_user_set_level_are_consumed(hass, monkeypatch) -> None:
    """§8.4/§9.1 (D28): the level adopted from a foreign change becomes the
    standing setpoint, so the poll's integer/float flips of it never reach the
    engine — override_since stays the moment of the dial."""
    controller, clock = await _observe_only(hass, monkeypatch, light_state="on")
    set_light(hass, "light.a", "on", brightness=184, transition=True)  # the dial
    await hass.async_block_till_done()
    rs = controller.engine.room_state("a")
    assert rs.overridden
    since = rs.override_since
    assert abs(controller._last_commanded["light.a"] - 184 / 255) < 1e-9

    submitted: list = []
    original = controller.submit
    monkeypatch.setattr(controller, "submit", lambda ev: (submitted.append(ev), original(ev)))
    for k in range(8):  # 24 minutes of polls
        clock[0] += 180.0
        set_light(hass, "light.a", "on", brightness=184.355 if k % 2 == 0 else 184, transition=True)
        await hass.async_block_till_done()
    assert submitted == []  # consumed at the adapter: no ForeignChange at all
    assert rs.override_since == since

    # A real dial move away from the adopted level still latches/re-stamps.
    set_light(hass, "light.a", "on", brightness=60, transition=True)
    await hass.async_block_till_done()
    assert rs.override_since != since
    assert abs(rs.channels["light.a"].commanded_b - 60 / 255) < 1e-9


async def test_restart_recovery_then_poll_flip_does_not_latch(hass, monkeypatch) -> None:
    """§8.4/§8.5 (D28): the live restart signature. The lights are unavailable
    at seed time, re-registered (unavailable -> unavailable), then come back
    off -> on(true level). The fold adopts the true level as setpoint and engine
    belief, so the poll flip 40 s later is consumed instead of latching."""
    controller, clock = await _observe_only(hass, monkeypatch, light_state="unavailable")
    rs = controller.engine.room_state("a")
    assert "light.a" in controller._unseeded
    assert not rs.channels["light.a"].on  # snapshot: believed off

    clock[0] += 5.0  # 17:14:23 — entity re-registration, still unavailable
    hass.states.async_set("light.a", STATE_UNAVAILABLE, {"friendly_name": "A"})
    await hass.async_block_till_done()
    clock[0] += 23.0  # 17:14:46 — the real recovery edge, then the true level
    set_light(hass, "light.a", "off", transition=True)
    await hass.async_block_till_done()
    set_light(hass, "light.a", "on", brightness=184, transition=True)
    await hass.async_block_till_done()
    assert not rs.overridden
    cs = rs.channels["light.a"]
    assert cs.on and abs(cs.commanded_b - 184 / 255) < 1e-9  # belief corrected
    assert abs(controller._last_commanded["light.a"] - 184 / 255) < 1e-9

    clock[0] += 40.0  # 17:15:26 — the poll's float re-report, grace long gone
    set_light(hass, "light.a", "on", brightness=184.355, transition=True)
    await hass.async_block_till_done()
    assert not rs.overridden

    # A genuine dial after the recovery still latches.
    clock[0] += 60.0
    set_light(hass, "light.a", "on", brightness=90, transition=True)
    await hass.async_block_till_done()
    assert rs.overridden


async def test_first_report_of_unseeded_channel_sets_setpoint_and_belief(hass, monkeypatch) -> None:
    """D28: a channel with no state at seed time adopts its first report as
    setpoint + belief, so its next poll flip is consumed."""
    controller, clock = await _observe_only(hass, monkeypatch, light_state=None)
    assert "light.a" in controller._unseeded
    set_light(hass, "light.a", "on", brightness=200, transition=True)
    await hass.async_block_till_done()
    rs = controller.engine.room_state("a")
    assert rs.channels["light.a"].on and not rs.overridden
    clock[0] += 300.0
    set_light(hass, "light.a", "on", brightness=200.3, transition=True)
    await hass.async_block_till_done()
    assert not rs.overridden


async def test_lamp_recovered_lit_in_a_dark_room_is_turned_off_quietly(hass) -> None:
    """§8.5 (D28): an open-loop room whose goal is 0 never turned off a lamp
    that came back lit after a restart — the engine believed it already off.
    The observed level fixes the belief and the reconcile turns it off, with
    no latch."""
    hass.states.async_set("light.a", STATE_UNAVAILABLE)
    hass.states.async_set("binary_sensor.pa", "off")
    entry = await setup_entry(
        hass,
        options([room("a", ["light.a"], presence="binary_sensor.pa", vacancy="off")]),
    )
    controller = hass.data[DOMAIN][entry.entry_id]
    turn_off = async_mock_service(hass, "light", "turn_off")
    async_mock_service(hass, "light", "turn_on")

    set_light(hass, "light.a", "on", brightness=200, transition=True)  # came back lit
    await hass.async_block_till_done()
    assert [c.data[ATTR_ENTITY_ID] for c in turn_off] == ["light.a"]
    assert not controller.engine.room_state("a").overridden


async def test_outdoor_stale_zero_resolution_still_reaches_the_engine(hass, monkeypatch) -> None:
    """§6.5b + D28: adopting a foreign ZERO as the setpoint must not swallow the
    report that resolves a suspect stale zero — the poll re-reading the true lit
    level is an off -> on transition, which the no-op guard never consumes. Only
    the poll flips AFTER the resolution are consumed."""
    from custom_components.light_conductor.core.events import ForeignChange

    set_light(hass, "light.b", "on", brightness=128, transition=True)
    entry = await setup_entry(
        hass, options([room("balkong", ["light.b"], shape="outdoor")]), enabled=False
    )
    controller = hass.data[DOMAIN][entry.entry_id]
    submitted: list = []
    original = controller.submit
    monkeypatch.setattr(controller, "submit", lambda ev: (submitted.append(ev), original(ev)))

    set_light(hass, "light.b", "off", transition=True)  # the (suspect) zero
    await hass.async_block_till_done()
    set_light(hass, "light.b", "on", brightness=128, transition=True)  # poll: true level
    await hass.async_block_till_done()
    set_light(hass, "light.b", "on", brightness=128.3, transition=True)  # next poll flip
    await hass.async_block_till_done()
    foreign = [ev for ev in submitted if isinstance(ev, ForeignChange)]
    assert [round(ev.level * 255) for ev in foreign] == [0, 128]
