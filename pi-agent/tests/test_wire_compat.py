"""
Wire-compatibility test: the Python agent's Uplink talking to the REAL
TypeScript ingest handler (run under Deno with an in-memory store).
Skipped if Deno is not installed.
"""
import os
import pathlib
import shutil
import subprocess
import time

import pytest

from detect_agent.uplink import Uplink

REPO = pathlib.Path(__file__).resolve().parents[2]
DENO = shutil.which("deno") or str(pathlib.Path.home() / ".deno" / "bin" / "deno")
SECRET = "s" * 40


@pytest.fixture(scope="module")
def server():
    if not os.path.exists(DENO):
        pytest.skip("deno not installed")
    port = "8799"
    proc = subprocess.Popen(
        [DENO, "run", "--allow-net", "--allow-env", str(REPO / "supabase/tests/wire_server.ts")],
        env={**os.environ, "PORT": port}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    assert "READY" in line, line
    yield f"http://127.0.0.1:{port}/functions/v1/ingest"
    proc.terminate()
    proc.wait(timeout=10)


def _events(*seqs):
    return [{"seq": s, "type": "TAILGATE", "state": "ASSERT", "input": "optex_tailgating_1",
             "location": "Main foyer", "occurredAt": time.time() - 1} for s in seqs]


def test_python_agent_is_accepted_by_typescript_ingest(server):
    up = Uplink(server, "skygarden-foyer-01", SECRET)
    res = up.send(_events(1, 2), {"uptimeS": 3})
    assert res.ok, res.error
    assert res.accepted == [1, 2]
    again = up.send(_events(2, 3), None)          # overlap re-send: still all confirmed
    assert again.ok and again.accepted == [2, 3]


def test_typescript_ingest_rejects_wrong_secret(server):
    res = Uplink(server, "skygarden-foyer-01", "x" * 40).send(_events(9), None)
    assert not res.ok and res.status == 401
