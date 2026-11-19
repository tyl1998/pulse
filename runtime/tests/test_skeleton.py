import importlib

import pytest

from pulse_runtime import __version__
from pulse_runtime.__main__ import main

SUBPACKAGES = [
    "api",
    "engine",
    "gate",
    "drivers",
    "drivers.android",
    "drivers.ios",
    "perception",
    "recorder",
    "ai",
    "experience",
    "store",
    "node",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_importable(name: str) -> None:
    importlib.import_module(f"pulse_runtime.{name}")


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out
