from pathlib import Path

import pytest

from workjig import Run, RunBusyError, busy, hold


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
