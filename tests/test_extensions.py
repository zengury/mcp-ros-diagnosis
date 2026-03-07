import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from manastone_diag.extensions import ExtensionRegistry


def test_extension_registry_discovery(monkeypatch):
    monkeypatch.setenv(
        "MANASTONE_EXTENSIONS",
        "manastone_diag.extensions.demo_extension",
    )
    registry = ExtensionRegistry()
    assert registry.discover_modules() == ["manastone_diag.extensions.demo_extension"]


def test_extension_registry_load():
    registry = ExtensionRegistry()
    loaded = registry.load_extensions(["manastone_diag.extensions.demo_extension"])
    assert len(loaded) == 1
    assert loaded[0].module_name == "manastone_diag.extensions.demo_extension"
    assert callable(loaded[0].register_fn)
