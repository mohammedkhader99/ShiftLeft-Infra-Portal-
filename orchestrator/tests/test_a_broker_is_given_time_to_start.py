"""A machine judged before its software has started is judged wrongly.

REQ-2026-0243 asked for Apache Kafka. The machine did everything right:

    java-21-openjdk-headless installed
    kafka /opt/kafka_2.13-4.3.1              <- archive fetched and unpacked
    Formatting metadata directory /var/lib/kafka/data with metadata.version 4.3-IV0
    Created symlink .../kafka.service
    PORTAL: kafka install finished 2026-08-30T17:38:32

and the same report said, in the next breath:

    kafka=active
    nothing listening on 9092
    nothing listening on 9093

`infra-portal-report.sh` runs in `runcmd` IMMEDIATELY after install.sh, which
ends with `systemctl enable --now kafka`. Kafka in KRaft mode takes tens of
seconds to form its quorum and bind. The report was sampling the ports a few
seconds after the broker was told to start, and recording "nothing listening"
about a broker that was still coming up. 6m22s of real machine to learn nothing.

AND `systemctl is-active` COULD NOT HAVE CAUGHT IT. The unit is Type=simple with
Restart=on-failure, so systemd reports `active` the instant the process is
spawned: a crash-looping broker and a still-starting one both read `active`.

Two changes, and the second is what makes a real failure diagnosable rather than
merely visible — the same change that made container failures diagnosable, which
twice in one afternoon corrected a confident wrong guess about why a service
would not start.

THESE TESTS RUN THE ACTUAL SCRIPT. Asserting that the right words appear in a
template is not the same as asserting the script behaves, and this project has
already been caught by that difference once.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path("orchestrator/terraform/oci/kafka/templates/cloud-init.yaml.tftpl")

VALUES = {
    "client_port": "9092", "controller_port": "9093", "node_id": "1",
    "java_package": "java-21-openjdk-headless", "quorum_voters": "1@host:9093",
    "advertised_host": "10.0.0.5", "boot_report_url": "https://example/par",
    "kafka_source_url": "https://example/kafka.tgz",
    "kafka_version": "2.13-4.3.1", "heap_opts": "-Xmx1g",
    "data_dir": "/var/lib/kafka/data",
}


def rendered() -> dict:
    """The cloud-init document, with the module's template variables filled in.

    A crude stand-in for `templatefile`, which needs terraform — and terraform is
    not on every machine that runs this suite. Enough to get at the scripts,
    which is what these tests are about.
    """
    text = re.sub(r"%\{[^}]*\}~?", "", TEMPLATE.read_text(encoding="utf-8"))
    text = re.sub(r"\$\{([a-z_0-9]+)\}",
                  lambda m: VALUES.get(m.group(1), f"<{m.group(1)}>"), text)
    return yaml.safe_load(text)


def report_script() -> str:
    for entry in rendered()["write_files"]:
        if entry["path"].endswith("infra-portal-report.sh"):
            return entry["content"]
    raise AssertionError("the template no longer writes a report script")


def run_report(tmp_path, *, listening: bool, journal: str = "",
               deadline_seconds: int = 6) -> str:
    """Run the real report script with stubbed system commands, and return what
    it wrote.

    The 180-second wait is shortened here so the suite does not sit for three
    minutes proving that a loop loops. Everything else is the shipped script.
    """
    # FORWARD SLASHES. A Windows path written into a shell script has its
    # backslashes eaten as escapes -- C:\Users\... arrives as C:Users... and the
    # redirect fails silently. sh accepts C:/Users/... on this platform.
    here = str(tmp_path).replace("\\", "/")
    # FAIL LOUDLY RATHER THAN WRITE SOMEWHERE ELSE. The first version of this
    # harness left a Windows path in the script. sh ate the backslashes, so
    # the redirect target stopped being absolute and became a RELATIVE file
    # literally named "CUsersmohammed.khader...report.txt" -- created in the
    # repository root, and committed before anyone noticed. The same shape as
    # the draft blueprint that ended up outside the repository entirely.
    assert "\\" not in here, f"the report path is not POSIX: {here}"
    script = (report_script()
              .replace("+ 180 ))", f"+ {deadline_seconds} ))")
              .replace("/var/log/infra-portal-report.txt", f"{here}/report.txt")
              .replace("/var/log/infra-portal.log", f"{here}/boot.log"))
    (tmp_path / "boot.log").write_text("PORTAL: kafka install finished\n",
                                       encoding="utf-8")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    ss_out = ("LISTEN 0 50 0.0.0.0:9092 0.0.0.0:*\n"
              "LISTEN 0 50 0.0.0.0:9093 0.0.0.0:*\n") if listening else ""
    stubs = {
        "ss": f"#!/bin/sh\nprintf '%s' '{ss_out}'\n",
        "systemctl": "#!/bin/sh\necho active\n",
        "rpm": "#!/bin/sh\necho java-21-openjdk-headless-21.0.12\n",
        "firewall-cmd": "#!/bin/sh\nexit 0\n",
        "journalctl": f"#!/bin/sh\nprintf '%s' '{journal}'\n",
        "curl": "#!/bin/sh\nexit 0\n",
        "readlink": "#!/bin/sh\necho /opt/kafka_2.13-4.3.1\n",
    }
    for name, body in stubs.items():
        path = binaries / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    env = {**os.environ,
           "PATH": str(binaries).replace("\\", "/") + ":" + os.environ.get("PATH", "")}
    written = tmp_path / "script.sh"
    written.write_text(script, encoding="utf-8", newline="\n")
    done = subprocess.run([shutil.which("sh"), str(written).replace("\\", "/")], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return (tmp_path / "report.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_a_broker_that_is_up_is_reported_as_up(tmp_path):
    out = run_report(tmp_path, listening=True)

    assert "nothing listening on 9092" not in out
    assert "0.0.0.0:9092" in out


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_a_healthy_report_does_not_carry_the_service_log(tmp_path):
    """Captured only on failure. Forty lines of journal in every successful
    report is noise that trains a reader to stop looking."""
    out = run_report(tmp_path, listening=True, journal="chatter\n")

    assert "kafka log" not in out
    assert "chatter" not in out


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_a_broker_that_never_binds_says_why_in_its_own_words(tmp_path):
    """THE POINT. "nothing listening" is a symptom; the journal is the cause,
    and without it the next person guesses — which is how three wrong guesses
    were made about service failures in a single afternoon."""
    out = run_report(
        tmp_path, listening=False,
        journal="ERROR Exiting Kafka due to fatal exception during startup\n")

    assert "nothing listening on 9092" in out
    assert "kafka log (never bound its ports)" in out
    assert "kafka: ERROR Exiting Kafka due to fatal exception" in out


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_it_waits_rather_than_judging_immediately(tmp_path):
    """The wait is the fix; the log capture only explains what the wait proves.

    MEASURED AS A DIFFERENCE, and the first version of this test was not. It
    asserted the run took at least six seconds -- and a planted script with the
    wait deleted still took ten, because starting a shell and seven stub
    programs costs about that much on this machine. It passed with the very
    defect it existed to catch.

    A broker that is already up returns on the first check; one that never binds
    waits out the deadline. Both pay the same start-up cost, so the difference
    between them is the wait and nothing else."""
    import time

    up, down = tmp_path / "up", tmp_path / "down"
    up.mkdir()
    down.mkdir()

    began = time.monotonic()
    run_report(up, listening=True, deadline_seconds=12)
    without = time.monotonic() - began

    began = time.monotonic()
    run_report(down, listening=False, deadline_seconds=12)
    with_wait = time.monotonic() - began

    assert with_wait - without >= 8, (
        f"the report did not wait for the broker: {with_wait:.1f}s when it never "
        f"bound against {without:.1f}s when it was already up")


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX shell")
def test_a_missing_journal_is_said_plainly(tmp_path):
    """An empty capture must not read as an empty journal — "no journal" and
    "the journal said nothing" are different facts."""
    out = run_report(tmp_path, listening=False, journal="")

    assert "(no journal for kafka)" in out


def test_the_report_still_runs_after_the_install():
    """Ordering matters: a report scheduled before the install would describe a
    machine that has not been configured at all."""
    steps = rendered()["runcmd"]
    install = next(i for i, c in enumerate(steps) if "install.sh" in c)
    report = next(i for i, c in enumerate(steps) if "infra-portal-report.sh" in c)

    assert install < report
