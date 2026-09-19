# notifications/tests/test_network_isolation.py
"""
Proves the root conftest.py's `_block_semaphore_network` fixture actually
fails a test when an unmocked call reaches Semaphore, rather than merely
being callable. send_sms swallows exceptions broadly (by design — a
provider outage must never break the caller), so the stub's own `raise`
would otherwise go unnoticed; the teardown check is what turns that into a
real test failure. Demonstrating that requires observing a SEPARATE pytest
run fail from the outside — asserting "this test fails" from inside the
test itself is not expressible any other way.

Runs as a real subprocess, invoked with cwd set to the real backend root,
so the project's own pytest.ini and (unmodified) conftest.py apply exactly
as they do for every other test — no copying, no faked settings module,
no risk of the subprocess's import resolution picking up an unrelated
same-named package from somewhere else on the machine.

The probe file is dropped into this same directory (notifications/tests/)
just long enough to run, then removed. It never touches the database (no
`db` fixture; it calls requests.post directly rather than going through
send_sms/SmsLog), so nothing about it can affect the real test database
the outer suite is using.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]

_PROBE_SOURCE = textwrap.dedent(
    """
    def test_reaches_real_network(settings):
        settings.SEMAPHORE_API_KEY = "leaked-real-key"
        settings.SMS_ENABLED = True
        import notifications.sms as sms
        sms.requests.post(
            "https://api.semaphore.co/api/v4/messages",
            data={"apikey": "leaked-real-key", "number": "09171234567"},
        )
    """
)


def test_unmocked_semaphore_call_fails_the_test():
    probe_path = Path(__file__).resolve().parent / "_semaphore_network_probe.py"
    probe_path.write_text(_PROBE_SOURCE)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(probe_path), "-p", "no:cacheprovider", "-q"],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        probe_path.unlink(missing_ok=True)

    assert result.returncode != 0, (
        "Expected the probe test to fail — the network safety net should "
        f"have caught the unmocked call.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "1 failed" in result.stdout, result.stdout
    assert (
        "unmocked call(s) reached notifications.sms.requests.post" in result.stdout
    ), result.stdout
