"""§5: colour-temperature policy."""

from __future__ import annotations

from custom_components.light_conductor.core import ct_policy
from custom_components.light_conductor.core.model import ChannelConfig
from custom_components.light_conductor.core.tunables import Tunables

TUN = Tunables()
CT = ChannelConfig("dw", fixed_ct=None, ct_range=(2200, 4000))
FIXED = ChannelConfig("tak", fixed_ct=2700)


def test_base_target_tracks_circadian() -> None:
    """§5.1: ct_target = ct_day - E*(ct_day - ct_evening)."""
    assert ct_policy.base_target(0.0, TUN) == 3300
    assert ct_policy.base_target(1.0, TUN) == 2400


def test_non_ct_channel_returns_none() -> None:
    assert ct_policy.ct_target(FIXED, 0.0, 0.5, None, TUN) is None


def test_daytime_full_output_is_neutral() -> None:
    """§5.1: high output by day sits near ct_day, clamped to hardware."""
    assert ct_policy.ct_target(CT, 0.0, 0.8, None, TUN) == 3300


def test_low_output_is_warm() -> None:
    """§5.3: below warm_dim_output the cap slides toward ct_min_evening."""
    warm = ct_policy.ct_target(CT, 0.0, 0.0, None, TUN)
    assert warm == 2200  # fully dim -> ct_min_evening
    mid = ct_policy.ct_target(CT, 0.0, 0.15, None, TUN)  # half of warm_dim_output
    assert 2200 < mid < 3300


def test_blend_anchoring_to_fixed_channels() -> None:
    """§5.2: a lit fixed-CT channel clamps CT to within blend_delta."""
    # Day target 3300 pulled to within 300 K of a lit 2700 K fixed channel.
    anchored = ct_policy.ct_target(CT, 0.0, 0.8, 2700, TUN)
    assert anchored == 3000  # 2700 + blend_delta
    # Fixed channels off (evening accent): CT may go fully warm.
    free = ct_policy.ct_target(CT, 1.0, 0.8, None, TUN)
    assert free == 2400


def test_fixed_anchor_detects_lit_fixed_channels() -> None:
    """§5.2: anchor engages only when a fixed channel is >= blend_threshold."""
    assert ct_policy.fixed_anchor({FIXED: 0.05, CT: 0.8}, TUN) is None  # below threshold
    assert ct_policy.fixed_anchor({FIXED: 0.5, CT: 0.8}, TUN) == 2700
