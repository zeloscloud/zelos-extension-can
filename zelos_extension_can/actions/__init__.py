"""Protocol-specific actions with dynamic dropdowns.

Actions are free-standing functions that use dynamic ``choices`` callbacks
to populate bus selectors, message lists, etc. at runtime after codecs
have been created and DBC files parsed.

Usage in app.py::

    from .actions import registry as action_registry

    # After creating codecs
    action_registry.register("powertrain", codec)

    # Conditionally import protocol actions (registers @action decorators)
    if any(c.config.get("j1939_enabled") for c, _ in codec_pairs):
        import zelos_extension_can.actions.j1939  # noqa: F401
"""
