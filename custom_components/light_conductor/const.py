"""Constants and the options contract.

Options contract (house convention, shared with the rest of the
conductor family): ``entry.data`` stays empty. Every user-facing
setting lives in ``entry.options`` so the options flow can edit all of
it without recreating the entry; ``entry.title`` is the display name.
Runtime knobs that HA restores (dimmer levels, mode switches) are entity
state, never options.

The concrete option keys land with the config-flow PR; the normative
list of signals and tunables is ``docs/ENGINE_SPEC.md``.
"""

from __future__ import annotations

DOMAIN = "light_conductor"
