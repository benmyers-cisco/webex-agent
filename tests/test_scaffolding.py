"""Proves pytest runs and can import the pulse lib modules."""


def test_pytest_can_import_pulse_lib_namespace():
    import lib  # noqa: F401
