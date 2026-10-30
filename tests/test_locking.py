import os
from pathlib import Path

import pytest

from workjig import RecordError, Run, RunBusyError, busy, hold


@pytest.mark.unit
def test_hold_and_busy_share_run_lock(tmp_path: Path) -> None:
    records = tmp_path / "records"
    assert not busy(records)
    with hold(records):
        assert busy(records)
        with pytest.raises(RunBusyError):
            with hold(records):
                pass
        with pytest.raises(RunBusyError):
            with Run(records, tmp_path):
                pass
    assert not busy(records)


@pytest.mark.unit
def test_run_excludes_hold(tmp_path: Path) -> None:
    records = tmp_path / "records"
    with Run(records, tmp_path):
        with pytest.raises(RunBusyError):
            with hold(records):
                pass


@pytest.mark.unit
def test_run_accepts_lock_only_directory(tmp_path: Path) -> None:
    records = tmp_path / "records"
    with hold(records):
        pass
    with Run(records, tmp_path):
        pass
    assert (records / "run.json").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("contents", [b"", b"interrupted"])
def test_hold_preserves_interrupted_first_open(tmp_path: Path, contents: bytes) -> None:
    records = tmp_path / "records"
    records.mkdir()
    lock = records / ".lock"
    lock.write_bytes(contents)
    for _ in range(2):
        with pytest.raises(RecordError, match="run record is missing"):
            with Run(records, tmp_path):
                pass
        with hold(records):
            assert busy(records)
        assert lock.read_bytes() == contents
        assert not busy(records)
    with pytest.raises(RecordError, match="run record is missing"):
        with Run(records, tmp_path):
            pass
    assert not (records / "run.json").exists()


@pytest.mark.unit
def test_repeated_hold_preserves_lock_only_directory(tmp_path: Path) -> None:
    records = tmp_path / "records"
    for _ in range(2):
        with hold(records):
            pass
    with Run(records, tmp_path):
        pass


@pytest.mark.unit
def test_hold_does_not_mark_lock_created_by_competing_first_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"
    lock = records / ".lock"
    original_open = os.open
    injected = False

    def competing_open(path, flags, *args, **kwargs):
        nonlocal injected
        if Path(path) == lock and not injected:
            injected = True
            # Another controller creates the lock and stops before writing run.json.
            lock.touch()
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", competing_open)
    with hold(records):
        pass
    assert injected
    assert lock.read_bytes() == b""
    with pytest.raises(RecordError, match="run record is missing"):
        with Run(records, tmp_path):
            pass
