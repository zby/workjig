"""Require an explicit execution boundary for every collected test."""

from pathlib import Path

import pytest

LIVE = Path(__file__).parent / "live"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-smoke",
        action="store_true",
        help="Allow real-harness smoke tests (uses credentials and model usage).",
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    if collection_path == LIVE and not config.getoption("--live-smoke"):
        return True
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if LIVE in item.path.parents:
            if not item.config.getoption("--live-smoke"):
                raise pytest.UsageError("Real-harness tests require explicit --live-smoke approval")
            if item.get_closest_marker("live") is None:
                raise pytest.UsageError(
                    f"{item.nodeid}: real-harness tests require the live marker"
                )
        elif item.get_closest_marker("live") is not None:
            raise pytest.UsageError(f"{item.nodeid}: live tests must stay under tests/live")
        groups = [mark.name for mark in item.iter_markers() if mark.name in {"unit", "subprocess"}]
        if len(groups) != 1:
            raise pytest.UsageError(
                f"{item.nodeid}: mark each test with exactly one of 'unit' or 'subprocess'"
            )
