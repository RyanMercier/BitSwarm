"""Opt-in checks against a real Docker daemon and the built harness image."""

import os
from pathlib import Path

import pytest

from harness.verify import Verifier

pytestmark = pytest.mark.skipif(
    os.environ.get("BITSWARM_DOCKER_TESTS") != "1",
    reason="set BITSWARM_DOCKER_TESTS=1 after building the harness image",
)


def source(tmp_path: Path, test: str) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_probe.py").write_text(test)
    return root


def test_container_restrictions(tmp_path):
    root = source(
        tmp_path,
        """import os
import socket
from pathlib import Path
import pytest

def test_restrictions():
    assert os.getuid() == 65534
    status = Path("/proc/self/status").read_text()
    assert "CapEff:\\t0000000000000000" in status
    assert "NoNewPrivs:\\t1" in status
    with pytest.raises(OSError):
        Path("/source/forbidden").write_text("no")
    with pytest.raises(OSError):
        Path("/forbidden").write_text("no")
    Path("/tmp/allowed").write_text("yes")
    with socket.socket() as connection:
        connection.settimeout(1)
        with pytest.raises(OSError):
            connection.connect(("1.1.1.1", 443))
""",
    )
    gate = Verifier().gate(root)
    assert gate.outcome == "PASS", gate
    assert not (root / "forbidden").exists()


def test_large_junit_report(tmp_path):
    root = source(
        tmp_path,
        """import pytest
@pytest.mark.parametrize("number", range(500), ids=lambda n: "long_test_identity_" + str(n))
def test_many(number):
    assert number >= 0
""",
    )
    gate = Verifier().gate(root)
    assert gate.outcome == "PASS", gate
    assert len(gate.tests) == 500


def test_timeout_is_inconclusive(tmp_path):
    root = source(tmp_path, "import time\ndef test_hang():\n    time.sleep(30)\n")
    gate = Verifier(timeout=2).gate(root)
    assert gate.outcome == "INCONCLUSIVE"
    assert "timed out" in gate.reason


def test_candidate_mutation_is_not_accepted(tmp_path):
    root = source(
        tmp_path,
        """from pathlib import Path
def test_mutate():
    Path("generated.py").write_text("value = 1")
""",
    )
    gate = Verifier().gate(root)
    assert gate.outcome == "INCONCLUSIVE"
    assert "86" in gate.reason
    assert not (root / "generated.py").exists()


def test_collection_error_is_inconclusive(tmp_path):
    root = source(tmp_path, "raise RuntimeError('collection failed')\n")
    gate = Verifier().gate(root)
    assert gate.outcome == "INCONCLUSIVE"


def test_assertion_failure_is_fail(tmp_path):
    root = source(tmp_path, "def test_wrong():\n    assert 1 == 2\n")
    gate = Verifier().gate(root)
    assert gate.outcome == "FAIL"
    assert list(gate.tests.values()) == ["failed"]


def test_thread_exhaustion_is_inconclusive_with_counters(tmp_path):
    root = source(
        tmp_path,
        """import threading
import time

def test_many_threads():
    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait) for _ in range(80)]
    try:
        for thread in threads:
            thread.start()
    finally:
        stop.set()
""",
    )
    gate = Verifier(pids_limit=32).gate(root)
    assert gate.outcome == "INCONCLUSIVE", gate
    assert "process limit refused" in gate.reason
    assert int(gate.resources["pids.events"]["max"]) > 0
