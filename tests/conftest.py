from __future__ import annotations

import pytest

OPT_IN = {
    "windows": "--run-windows",
    "gpu": "--run-gpu",
    "ollama": "--run-ollama",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--run-windows", action="store_true", help="run real Windows desktop tests")
    parser.addoption("--run-gpu", action="store_true", help="run CUDA + Whisper model tests")
    parser.addoption("--run-ollama", action="store_true", help="run tests against local Ollama")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        for marker, option in OPT_IN.items():
            if marker in item.keywords and not config.getoption(option):
                item.add_marker(pytest.mark.skip(reason=f"needs {option}"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated SHOUTABOUT_HOME for a test."""
    monkeypatch.setenv("SHOUTABOUT_HOME", str(tmp_path / "home"))
    return tmp_path / "home"
