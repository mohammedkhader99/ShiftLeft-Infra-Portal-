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

AND NO REAL TIME PASSES WHILE THEY DO. The script's wait is bounded by the wall
clock (`date +%s` against a deadline), and the first harness let it be: real
sleeps, and stub programs for `ss` that answered from a counter in a file. It
was deterministic on paper and not in practice. Under load — the full suite
once took 2h16m on a throttled laptop — starting a process costs seconds, the
deadline expired before the third look, and a correct script was reported as
never having seen its broker. A test that cries wolf teaches people to ignore
it.

So the clock is now a shell variable: `sleep` advances it instead of waiting,
the deadline check reads it, and the ports are answered from a scripted
per-pass sequence — the same harness `test_a_service_is_not_reported_on_its_first_port.py`
uses for the container loop. Every stub is a shell FUNCTION in the one `sh`
that runs the script, not a program, because on the machine that produced the
false failure one process start was measured at eight seconds. Nothing about
when the script stops looking is touched: the deadline arithmetic, the
two-port condition, the break, and the log capture are the shipped lines.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
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

SH = shutil.which("sh")


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


@dataclass
class Run:
    """What one run of the report script did."""

    report: str      # the report file the script wrote
    ready: bool      # did the wait loop see both ports
    passes: int      # how many times it looked
    waited: int      # seconds it would have waited on a real machine


def _swap(text: str, old: str, new: str, times: int) -> str:
    """Replace `old`, insisting it occurs exactly `times` times.

    A harness that substitutes into a script it did not write must notice when
    that script changes shape. A silent zero-match would leave the real `date`
    and `ss` in place, the test would wait on the wall clock again, and nobody
    would know until it flaked.
    """
    found = text.count(old)
    assert found == times, (
        f"expected {old!r} {times}x in the report script, found {found}: the "
        f"template changed shape, so this harness needs updating")
    return text.replace(old, new)


def _swap_re(text: str, pattern: str, repl: str, times: int) -> str:
    out, found = re.subn(pattern, repl, text)
    assert found == times, (
        f"expected /{pattern}/ {times}x in the report script, found {found}: the "
        f"template changed shape, so this harness needs updating")
    return out


def run_report(tmp_path, *, passes: list, journal: str = "",
               deadline_seconds: int = 6) -> Run:
    """Run the real report script under `sh`, with time and the ports scripted.

    `passes` is what `ss` sees on each look, one entry per pass: a
    comma-separated port list, or None for nothing listening yet. The last
    entry repeats for as long as the script keeps looking, so a broker that
    never binds is `[None]` and one that binds on its third look is
    `[None, None, "9092,9093"]`.

    THE STUBS SET VARIABLES; NOTHING IS CALLED IN `$(...)`. Command substitution
    runs in a subshell, where a counter incremented is a counter lost. The
    script's two `$(date +%s)` are therefore replaced by `$NOW`, and `sleep`
    becomes a function that adds to NOW — so the deadline arithmetic runs
    exactly as shipped, against a clock only the script can move.

    The 180-second deadline is shortened, as before, so a broker that never
    binds is not watched for thirty-six passes to prove that a loop loops.
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

    script = report_script()
    # The clock: read by the deadline, moved only by `sleep`.
    script = _swap(script, "$(date +%s)", "$NOW", 2)
    script = _swap(script, "+ 180 ))", f"+ {deadline_seconds} ))", 1)
    # The ports: one scripted answer per pass, consumed at the top of each look
    # and read by both halves of the two-port condition.
    script = _swap(script, "  if ss -lnt", "  next_ports\n  if ss -lnt", 1)
    script = _swap_re(script, r"ss -lnt 2>/dev/null \| grep -q ':(\d+) '",
                      r"listening \1", 2)
    # The report's own port lines print what `ss` shows; here they print the
    # same LISTEN line a real `ss` would, for the port the last look saw.
    script = _swap_re(script, r"ss -lnt 2>/dev/null \| grep ':(\d+) '",
                      r'{ listening \1 && echo "LISTEN 0 50 0.0.0.0:\1 0.0.0.0:*"; }', 2)
    # POSIX sh refuses a function named with a hyphen, so this one tool is
    # renamed to be stubbed; every other command is shadowed by name.
    script = _swap(script, "firewall-cmd ", "firewall_cmd ", 1)
    # Files: the report, the first-boot log it quotes, the journal capture.
    script = _swap(script, "/var/log/infra-portal-report.txt", f"{here}/report.txt", 1)
    script = _swap(script, "/var/log/infra-portal.log", f"{here}/boot.log", 1)
    script = _swap(script, "/tmp/kafka-journal", f"{here}/kafka-journal", 3)
    (tmp_path / "boot.log").write_text("PORTAL: kafka install finished\n",
                                       encoding="utf-8")

    arms = "\n".join(
        f"    {i + 1}) PORTS='{'' if p is None else p}';;"
        for i, p in enumerate(passes))
    last = "" if passes[-1] is None else passes[-1]
    quoted_journal = journal.replace("'", "'\\''")
    prelude = textwrap.dedent(f"""\
        NOW=0
        PASS=0
        PORTS=
        JOURNAL='{quoted_journal}'
        next_ports() {{
          PASS=$((PASS+1))
          case $PASS in
        """)
    prelude += arms + "\n"
    prelude += textwrap.dedent(f"""\
            *) PORTS='{last}';;
          esac
        }}
        listening() {{ case ",$PORTS," in *,"$1",*) return 0;; *) return 1;; esac; }}
        sleep() {{ NOW=$((NOW + ${{1:-0}})); }}
        systemctl() {{ echo active; }}
        rpm() {{ echo java-21-openjdk-headless-21.0.12; }}
        journalctl() {{ printf '%s' "$JOURNAL"; }}
        readlink() {{ echo /opt/kafka_2.13-4.3.1; }}
        firewall_cmd() {{ return 0; }}
        curl() {{ return 0; }}
        """)
    epilogue = '\necho "FINAL_READY=${READY:-0} FINAL_PASSES=$PASS FINAL_NOW=$NOW"\n'

    written = tmp_path / "script.sh"
    written.write_text(prelude + script + epilogue, encoding="utf-8", newline="\n")
    done = subprocess.run([SH, str(written).replace("\\", "/")],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    final = re.search(r"FINAL_READY=(\S+) FINAL_PASSES=(\d+) FINAL_NOW=(\d+)",
                      done.stdout)
    assert final, f"the script ended without reporting its state: {done.stdout!r}"
    return Run(report=(tmp_path / "report.txt").read_text(encoding="utf-8"),
               ready=final.group(1) == "1", passes=int(final.group(2)),
               waited=int(final.group(3)))


@pytest.mark.skipif(not SH, reason="needs a POSIX shell")
def test_a_broker_that_is_up_is_reported_as_up(tmp_path):
    """The common case must not get slower: both ports up on the first look
    means the script leaves on the first look."""
    out = run_report(tmp_path, passes=["9092,9093"])

    assert out.ready
    assert out.passes == 1 and out.waited == 0, "a broker that was up was waited for"
    assert "nothing listening on 9092" not in out.report
    assert "0.0.0.0:9092" in out.report


@pytest.mark.skipif(not SH, reason="needs a POSIX shell")
def test_a_healthy_report_does_not_carry_the_service_log(tmp_path):
    """Captured only on failure. Forty lines of journal in every successful
    report is noise that trains a reader to stop looking."""
    out = run_report(tmp_path, passes=["9092,9093"], journal="chatter\n")

    assert "kafka log" not in out.report
    assert "chatter" not in out.report


@pytest.mark.skipif(not SH, reason="needs a POSIX shell")
def test_a_broker_that_never_binds_says_why_in_its_own_words(tmp_path):
    """THE POINT. "nothing listening" is a symptom; the journal is the cause,
    and without it the next person guesses — which is how three wrong guesses
    were made about service failures in a single afternoon."""
    out = run_report(
        tmp_path, passes=[None], deadline_seconds=6,
        journal="ERROR Exiting Kafka due to fatal exception during startup\n")

    assert not out.ready
    assert out.waited >= 6, "it judged the broker before the deadline had passed"
    assert out.passes == 2, f"{out.passes} looks in a six-second deadline at five a pass"
    assert "nothing listening on 9092" in out.report
    assert "kafka log (never bound its ports)" in out.report
    assert "kafka: ERROR Exiting Kafka due to fatal exception" in out.report


@pytest.mark.skipif(not SH, reason="needs a POSIX shell")
def test_a_broker_that_comes_up_late_is_still_seen(tmp_path):
    """THE WAIT, tested by what it achieves rather than by a stopwatch.

    The ports are answered empty for the first two looks and bound on the
    third — which is what a real broker does: nothing, nothing, nothing, then
    bound. A script that checks once records "nothing listening" about a
    machine that is perfectly healthy, which is exactly what happened to
    REQ-2026-0243.

    THREE GENERATIONS OF THIS TEST. The first asserted the run took six
    seconds; starting a shell and seven stubs costs about ten, so a planted
    script with the wait deleted still passed. The second compared a waiting
    run against a non-waiting one — better, and load-sensitive: on a busy
    machine it read 22.5s against 17.1s and failed a correct implementation.
    The third answered the ports from a counter in a file and believed it
    depended on no clock at all — but the script's DEADLINE is the wall clock,
    and on a throttled laptop the third look came after the deadline had
    passed. A correct script was reported as never having seen its broker.

    Now the clock is a variable only `sleep` moves. Ten seconds of waiting is
    ten seconds whatever the machine running the test is doing.
    """
    out = run_report(tmp_path, passes=[None, None, "9092,9093"], deadline_seconds=40)

    assert out.ready, (
        "the report judged the broker before it had bound — it checks once "
        "instead of waiting")
    assert out.passes == 3, (
        f"{out.passes} looks: it did not stop the moment both ports were bound")
    assert out.waited == 10, (
        f"waited {out.waited}s for a broker that bound at 10s")
    assert "nothing listening on 9092" not in out.report
    assert "0.0.0.0:9092" in out.report
    assert "kafka log (never bound its ports)" not in out.report, (
        "a broker that did come up was reported as though it never had")


@pytest.mark.skipif(not SH, reason="needs a POSIX shell")
def test_a_missing_journal_is_said_plainly(tmp_path):
    """An empty capture must not read as an empty journal — "no journal" and
    "the journal said nothing" are different facts."""
    out = run_report(tmp_path, passes=[None], journal="")

    assert "(no journal for kafka)" in out.report


def test_the_report_still_runs_after_the_install():
    """Ordering matters: a report scheduled before the install would describe a
    machine that has not been configured at all."""
    steps = rendered()["runcmd"]
    install = next(i for i, c in enumerate(steps) if "install.sh" in c)
    report = next(i for i, c in enumerate(steps) if "infra-portal-report.sh" in c)

    assert install < report
