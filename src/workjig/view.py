"""Read-only, lock-free inspection of a run's committed records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .models import (
    UNSETTLED_STATUSES,
    CommandWorker,
    LaunchFiles,
    LaunchRecord,
    LaunchStatus,
    launch_files_under,
)
from .native_session import NativeSession, locate_session
from .payloads import PayloadStore
from .records import WORK_FIELDS, CallEntry, load_state


@dataclass(frozen=True)
class RecordedCall:
    """One call identity's recorded history under one work-material version.

    The work material is exposed as fields, not as a ``Request``: execution
    controls are not stored per call; each launch records the ones it used.
    ``status`` follows the same rule as ``CallResult.status``; ``outputs`` is
    empty unless a result is committed.
    """

    call_id: str
    request_hash: str
    task: str
    inputs: Mapping[str, str]
    required_outputs: tuple[str, ...]
    optional_outputs: tuple[str, ...]
    worker: CommandWorker
    status: LaunchStatus
    outputs: Mapping[str, str]
    launches: tuple[LaunchRecord, ...]

    @classmethod
    def _from_entry(cls, entry: CallEntry) -> RecordedCall:
        delivery = entry.delivery
        return cls(
            call_id=entry.request.call_id,
            request_hash=entry.request_hash,
            **{name: getattr(entry.request, name) for name in WORK_FIELDS},
            status=entry.status,
            outputs=MappingProxyType(dict(delivery.outputs) if delivery is not None else {}),
            launches=tuple(entry.launches),
        )


@dataclass(frozen=True)
class RunView:
    """A validated snapshot of one committed ``run.json``.

    ``calls`` holds one entry per call ID and work-material version; their
    order, in particular across versions of one call ID, is not meaningful.
    Select a version by ``request_hash``. The snapshot may be stale by the
    time it is used, and files of an unsettled launch may still be changing.
    """

    record_dir: Path
    workdir: str
    calls: tuple[RecordedCall, ...]

    def unresolved_launches(self) -> tuple[LaunchRecord, ...]:
        """Reserved or running launches in this snapshot.

        While a controller has the run open these include its own active
        launches, not only stale reservations. The view does not consult the
        lock; ``busy(record_dir)`` asks separately, and its answer can change
        between the two reads.
        """
        return tuple(
            launch
            for call in self.calls
            for launch in call.launches
            if launch.status in UNSETTLED_STATUSES
        )

    def native_session(self, launch: LaunchRecord, *, resolve: bool = False) -> NativeSession:
        """Locate this attempt's native transcript, without reading it.

        By default this starts no process. When the receipt holds an ID whose
        transcript only the harness can report, it raises
        ``UnresolvedSessionError``; pass ``resolve=True`` to ask the harness.
        """
        call, recorded, directory = self._launch_location(launch)
        return locate_session(call.worker, recorded, directory, resolve=resolve)

    def launch_files(self, launch: LaunchRecord) -> LaunchFiles:
        """Locations of a launch's files under this view's record directory.

        Paths are derived from the directory given to ``read_run``, not the
        recorded ``launch_dir``, so they follow a copied or moved run. They
        are locations only: a file may not exist.
        """
        _, _, directory = self._launch_location(launch)
        return launch_files_under(directory)

    def _launch_location(self, launch: LaunchRecord) -> tuple[RecordedCall, LaunchRecord, Path]:
        name = launch.launch_id
        if name in {".", ".."} or Path(name).name != name or "\\" in name:
            raise ValueError(f"launch ID is not a directory name: {name!r}")
        for call in self.calls:
            for recorded in call.launches:
                if recorded.launch_id == name:
                    return call, recorded, self.record_dir / "launches" / name
        raise ValueError(f"launch is not in this run: {name!r}")


def read_run(record_dir: str | Path) -> RunView | None:
    """Return a validated snapshot of a run's records, or None without ``run.json``.

    Takes no lock and writes nothing. A corrupt, inconsistent or
    unsupported-version record raises ``RecordError``. The recorded working
    directory is reported, not checked.
    """
    root = Path(record_dir).resolve()
    state = load_state(root / "run.json", PayloadStore(root))
    if state is None:
        return None
    return RunView(
        record_dir=root,
        workdir=state.workdir,
        calls=tuple(
            RecordedCall._from_entry(entry)
            for versions in state.calls.values()
            for entry in versions.values()
        ),
    )
