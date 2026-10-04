"""Smoke tests for the M0 repository foundation."""


def test_package_importable() -> None:
    import transport_connectivity

    assert transport_connectivity.__version__ == "0.1.0"


def test_src_layout_visible() -> None:
    import pathlib

    package_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "transport_connectivity"
    assert (package_dir / "__init__.py").exists()
