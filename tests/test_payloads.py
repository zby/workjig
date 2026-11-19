"""Persistence and isolation at the run-local payload boundary."""

import hashlib
import json
import shutil
import sys
from dataclasses import replace
from threading import Barrier, Event

import pytest

from workjig import CommandWorker, RecordError, Request, Run, read_run
from workjig.payloads import PayloadStore

TEXT = "snapshot: λ\r\nsecond\rthird\nfourth"
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()


def request():
    # Change this launch's own input file after copying it byte-for-byte. Neither
    # a sibling nor replay may observe the mutation as the declared input.
    code = (
        "from pathlib import Path; import sys; a,b=map(Path,sys.argv[1:]); "
        'b.write_bytes(a.read_bytes()); a.write_bytes(b"mutated")'
    )
    return Request(
        "first",
        TEXT,
        {"in.txt": TEXT},
        ("out.txt",),
        CommandWorker((sys.executable, "-c", code, "{input_dir}/in.txt", "{output_dir}/out.txt")),
    )


@pytest.mark.subprocess
def test_call_payloads_shared_but_work_and_launches_independent(tmp_path):
    records = tmp_path / "records"
    req = request()
    with Run(records, tmp_path) as run:
        a = run.call(req)
        b = run.call(replace(req, call_id="second", inputs={"in.txt": a.outputs["out.txt"]}))
        assert a.launches != b.launches and not b.reused
    assert list((records / "payloads").iterdir()) == [records / "payloads" / DIGEST]
    assert (records / "payloads" / DIGEST).read_bytes() == TEXT.encode()
    state = json.loads((records / "run.json").read_text())
    for versions in state["calls"].values():
        [call] = versions.values()
        assert call["request"]["task"] == call["request"]["inputs"]["in.txt"] == DIGEST
        assert call["result"]["outputs"]["out.txt"] == DIGEST
    # Inspection and replay depend only on the manifest and its closure, not
    # on the worker files, old absolute launch paths or original store.
    copied = tmp_path / "copied"
    shutil.copytree(records, copied, ignore=shutil.ignore_patterns("launches"))
    shutil.rmtree(records)
    assert all(
        c.inputs == {"in.txt": TEXT} and c.outputs == {"out.txt": TEXT}
        for c in read_run(copied).calls
    )
    with Run(copied, tmp_path) as run:
        replay = run.call(req)
        assert replay.reused and replay.outputs == a.outputs
        blob = copied / "payloads" / DIGEST
        blob.unlink()
        with pytest.raises(RecordError, match="payload"):
            run.call(req)
        original = (copied / "run.json").read_bytes()
        with pytest.raises(RecordError, match="uncertain"):
            run.call(replace(req, call_id="third", task="disjoint", inputs={"in.txt": "untouched"}))
        assert (copied / "run.json").read_bytes() == original
        assert not blob.exists()
    with pytest.raises(RecordError, match="payload"):
        read_run(copied)
    with pytest.raises(RecordError, match="payload"), Run(copied, tmp_path):
        pytest.fail("opened corrupt record")


@pytest.mark.unit
def test_existing_orphan_is_checked_before_being_referenced(tmp_path):
    store = PayloadStore(tmp_path)
    store.root.mkdir()
    (store.root / DIGEST).write_bytes(b"corrupt")
    with pytest.raises(RecordError, match="mismatch"):
        store.put(TEXT)
    assert (store.root / DIGEST).read_bytes() == b"corrupt"


@pytest.mark.unit
@pytest.mark.parametrize("ref", ["../run.json", "A" * 64, "x", None])
def test_only_canonical_payload_names_resolve(tmp_path, ref):
    with pytest.raises(RecordError, match="reference"):
        PayloadStore(tmp_path).get(ref)
    assert not list(tmp_path.iterdir())


@pytest.mark.unit
def test_interrupted_blob_publication_cannot_commit_a_reservation(tmp_path, monkeypatch):
    from workjig import payloads

    req = request()
    path = tmp_path / "records"
    with Run(path, tmp_path) as run:
        original = (path / "run.json").read_bytes()
        with monkeypatch.context() as patch:
            sync = payloads.sync_directory

            def fail(directory):
                if directory.name == "payloads":
                    raise OSError("failed payload directory sync")
                sync(directory)

            patch.setattr(payloads, "sync_directory", fail)
            for _ in range(3):  # The orphan must not bypass the failed flush on retry.
                with pytest.raises(OSError, match="directory sync"):
                    run.call(req)
        assert (path / "run.json").read_bytes() == original
        assert (path / "payloads" / DIGEST).is_file()  # Orphan, not evidence of a launch.
        assert not read_run(path).calls
        assert not run.unresolved_launches()


@pytest.mark.subprocess
def test_parallel_failed_payload_write_leaves_only_that_launch_unresolved(tmp_path, monkeypatch):
    from workjig import run as runtime

    launch, put = runtime.launch, PayloadStore.put
    started, failing = Barrier(2), Event()

    def finish(request, *args, **kwargs):
        result = launch(request, *args, **kwargs)
        started.wait(timeout=5)
        if request.call_id == "B":
            assert failing.wait(5)
        return result

    def inject(store, text):
        if text == "result-A":
            failing.set()
            raise OSError("output publication failed")
        return put(store, text)

    monkeypatch.setattr(runtime, "launch", finish)
    monkeypatch.setattr(PayloadStore, "put", inject)
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])"
    reqs = [
        Request(
            k,
            k,
            {},
            ("out",),
            CommandWorker((sys.executable, "-c", code, "{output_dir}/out", f"result-{k}")),
        )
        for k in ("A", "B")
    ]
    root = tmp_path / "records"
    with Run(root, tmp_path) as run, pytest.raises(ExceptionGroup):
        run.parallel(reqs, max_workers=2)
    view = read_run(root)
    assert {c.call_id: c.status for c in view.calls} == {"A": "running", "B": "delivered"}
    assert len(view.unresolved_launches()) == 1
