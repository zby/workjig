"""Platform policy tests that do not require a live Windows runner."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import workjig.platform as platform


@pytest.mark.unit
def test_windows_command_resolution_uses_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(platform.shutil, "which", lambda _: r"C:\Tools\worker.exe")

    assert platform.resolve_command(("worker", "--flag")) == (r"C:\Tools\worker.exe", "--flag")


@pytest.mark.unit
def test_posix_command_resolution_uses_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(platform.shutil, "which", lambda _: "/tmp/bin/worker")

    assert platform.resolve_command(("worker", "--flag")) == ("/tmp/bin/worker", "--flag")


@pytest.mark.unit
def test_posix_worker_starts_in_its_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="posix"))
    options: dict[str, object] = {}

    def popen(argv, **kwargs):
        options.update(kwargs)
        return SimpleNamespace(pid=42, returncode=None)

    monkeypatch.setattr(platform.subprocess, "Popen", popen)
    process = platform.start_process(("worker",), "/work", {"A": "1"}, "out", "err")

    assert process.pid == 42
    assert options["start_new_session"] is True
    assert options["stdin"] is platform.subprocess.DEVNULL
    assert (options["cwd"], options["env"]) == ("/work", {"A": "1"})
