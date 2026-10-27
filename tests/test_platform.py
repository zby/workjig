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
def test_windows_process_creation_uses_a_new_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="nt"))

    assert platform.process_creation_options() == {
        "creationflags": platform._CREATE_NEW_PROCESS_GROUP
    }


@pytest.mark.unit
def test_windows_cleanup_delegates_tree_termination_to_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="nt"))
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(platform.subprocess, "run", run)
    process = SimpleNamespace(pid=42, poll=lambda: None)

    assert platform.cleanup_process_group(process) is True
    assert calls == [["taskkill", "/PID", "42", "/T", "/F"]]


@pytest.mark.unit
def test_windows_cleanup_does_not_target_a_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        platform.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("taskkill must not run for an exited leader"),
    )

    assert platform.cleanup_process_group(SimpleNamespace(pid=42, poll=lambda: 0)) is False
