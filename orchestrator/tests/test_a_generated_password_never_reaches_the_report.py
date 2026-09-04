"""The one credential nobody was supposed to store, stored in a report.

A container that will not start without a password may be told to GENERATE one
-- the option chosen on 2026-09-04, because a generated password is not a
secret this portal holds: the machine makes it, prints it once, and its
administrator reads it there.

Prints it WHERE. In the container's log. And when a service never comes up, the
machine captures `podman logs --tail 15` into its boot report, which is uploaded
to object storage and read back by the portal. So the failure that generates a
password is exactly the failure that would publish it -- into a bucket, an API
response, and an audit trail.

WHAT MUST SURVIVE THE REDACTION is the reason it exists at all: the lines where
the container NAMES the variables it needs are what the drafter reads to answer
it. Redacting those too would trade a leak for a technology that can never be
built.

    keep     - MYSQL_ROOT_PASSWORD                    (a name: evidence)
    redact   GENERATED ROOT PASSWORD: aB3xY9zQ        (a value: a credential)

THIS RUNS THE SHIPPED COMMAND. The redaction is a `sed` expression inside a
generated shell script, and an expression that is subtly wrong -- an unescaped
group, a greedy match eating the whole line -- fails in the direction that
either leaks or blinds. Asserting the text contains "redacted" would catch
neither, so the pipeline is extracted from the rendered script and run.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(SH is None, reason="no POSIX shell on this host")

#: What `docker.io/library/mysql` prints when it is told to generate one, and
#: when it is told nothing at all. Both go through the same capture.
LOG = """\
2026-09-04 01:12:44+00:00 [Note] [Entrypoint]: Entrypoint script for MySQL Server started.
2026-09-04 01:12:45+00:00 [ERROR] [Entrypoint]: Database is uninitialized and password option is not specified
    You need to specify one of the following as an environment variable:
    - MYSQL_ROOT_PASSWORD
    - MYSQL_ALLOW_EMPTY_PASSWORD
    - MYSQL_RANDOM_ROOT_PASSWORD
2026-09-04 01:14:02+00:00 [Note] [Entrypoint]: GENERATED ROOT PASSWORD: aB3xY9zQwErTyU1
2026-09-04 01:14:03+00:00 [Note] MYSQL_ROOT_PASSWORD=hunter2 was supplied
2026-09-04 01:14:04+00:00 [Note] password = correcthorsebattery
"""

SECRETS = ("aB3xY9zQwErTyU1", "hunter2", "correcthorsebattery")


def report_script(tmp_path, monkeypatch, code="somesoftware", ports=(3306,)):
    profile = {
        "code": code, "builds_on": "oci/service-vm", "ports": list(ports),
        "container": {
            "image": f"docker.io/library/{code}", "tag": "1",
            "digest": "sha256:" + "b" * 64,
            "data_dir": f"/var/lib/{code}", "data_mount": f"/var/lib/{code}",
        },
        "rhel": {"packages": [], "services": [code]},
    }
    (tmp_path / f"{code}.json").write_text(json.dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)

    import yaml
    doc = yaml.safe_load(configure.render(
        [{"technology_code": code}], "rhel",
        configure.boot_report_url("REQ-2026-0266", "oci-service-vm")))
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith("report.sh"))


def capture_block(script: str) -> str:
    """The whole `if [ -z "$READY" ]; then ... fi` capture, as generated.

    Not just the podman line: what the capture DOES when podman has nothing is
    the point of the tests below, and that is a decision the block makes.

    BALANCED, not "up to the first `fi`". The capture contains a nested `if`
    (ask the container, and if it has nothing ask the journal), so stopping at
    the first `fi` hands sh an unterminated block -- a syntax error, which is
    not the same as a failing test.
    """
    lines = script.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if 'if [ -z "$READY" ]' in l)
    depth = 0
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("if "):
            depth += 1
        elif stripped == "fi":
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start:i + 1])
    raise AssertionError("the capture block is not terminated")


def run_block(tmp_path, monkeypatch, *, podman: str, journal: str):
    """The generated capture block, with podman and journalctl stubbed.

    `podman` and `journal` are the shell bodies of each stub, so a test can say
    "podman has nothing, the journal has everything" -- which is the machine
    the fallback below exists for.

    THE WHOLE BLOCK, not one line of it. The capture used to be a single
    `podman logs | sed | sed`, and these tests took that pipeline apart. What it
    does when podman has nothing is a decision the BLOCK makes, so extracting a
    line would test a shape rather than a behaviour.
    """
    block = capture_block(report_script(tmp_path, monkeypatch))
    # The `;` is not optional: POSIX sh needs one before the closing brace, and
    # without it the whole block is a syntax error rather than a failing test.
    stub = (f"podman() {{ {podman}; }}\n"
            f"journalctl() {{ {journal}; }}\n"
            "READY=\n")
    done = subprocess.run([SH, "-c", stub + block],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _quoted(text: str) -> str:
    """A shell stub that prints `text` verbatim."""
    body = text.replace("'", "'\\''")
    return f"printf '%s' '{body}'"


def run(tmp_path, monkeypatch, text=LOG):
    """The capture, with the container still present and saying `text`."""
    return run_block(tmp_path, monkeypatch, podman=_quoted(text),
                     journal="return 1")


# --- nothing that is a password gets out ------------------------------------------

def test_no_password_value_survives_the_capture(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch)

    for secret in SECRETS:
        assert secret not in out, (
            f"{secret!r} reached the boot report, which is uploaded to object "
            f"storage and read back by the portal")


def test_the_redaction_says_it_happened(tmp_path, monkeypatch):
    """A blank where a value was reads as "the log said nothing". Saying so is
    the same rule as "(no journal for kafka)"."""
    out = run(tmp_path, monkeypatch)

    assert "redacted by the portal" in out


# --- and the evidence the drafter needs does ---------------------------------------

def test_the_names_of_the_variables_survive(tmp_path, monkeypatch):
    """These are what `container_env` reads to answer the container. Redacting
    them would trade a leak for a technology that can never be built."""
    out = run(tmp_path, monkeypatch)

    for name in ("MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD",
                 "MYSQL_RANDOM_ROOT_PASSWORD"):
        assert name in out, f"{name} was redacted; the drafter can no longer answer it"


def test_the_line_that_explains_the_failure_survives(tmp_path, monkeypatch):
    """"Why is it not listening" is the whole reason the log is captured."""
    out = run(tmp_path, monkeypatch)

    assert "Database is uninitialized" in out
    assert "You need to specify one of the following" in out


def test_ordinary_lines_are_untouched(tmp_path, monkeypatch):
    out = run(tmp_path, monkeypatch)

    assert "Entrypoint script for MySQL Server started." in out
    assert len(out.splitlines()) == len(LOG.splitlines()), (
        "the capture dropped or added lines")


def test_a_log_with_no_password_passes_through_unchanged(tmp_path, monkeypatch):
    """The common case must cost nothing: a container that failed for any other
    reason reports exactly what it said, under the prefix that says whose log
    it is."""
    plain = ("2026-09-04 [ERROR] bind: address already in use\n"
             "2026-09-04 [ERROR] exiting\n")
    out = run(tmp_path, monkeypatch, text=plain)

    assert out == "".join(f"  somesoftware log: {line}\n"
                          for line in plain.splitlines())


# --- and it must still be there to be read ----------------------------------------
#
# PROOF-MYSQL-20260904T195337-B5DB54, a real machine, 2026-09-05 00:27. The
# container rung was reached for the first time and the report came back:
#
#     image_mysql=match (sha256:66aec17cd21a...)
#     declares_mysql=3306,33060
#     listening_inside_mysql=none
#       mysql log: Error: no container with name or ID "mysql" found: no such container
#     mysql=failed
#
# Quadlet runs the container with `--rm`, so when MySQL exited for want of a
# password podman DELETED it -- and the log went with it. `podman logs` is asked
# after the wait loop gives up, which is precisely when the container is most
# likely to be gone. The evidence channel the whole password rule depends on
# was empty at exactly the moment it mattered.
#
# The Kafka path solved this before anyone wrote the container one: it reads
# `journalctl -u kafka`, because a unit's output goes to the journal and stays
# there whatever podman does with the container afterwards. Restart=always means
# the journal holds every attempt.

def run_block(tmp_path, monkeypatch, *, podman: str, journal: str):
    """The generated capture block, with podman and journalctl stubbed.

    `podman` and `journal` are the shell bodies of each stub, so a test can say
    "podman has nothing, the journal has everything" -- which is the machine
    this fix exists for.
    """
    block = capture_block(report_script(tmp_path, monkeypatch))
    # The `;` is not optional: POSIX sh needs one before the closing brace, and
    # without it the whole block is a syntax error rather than a failing test.
    stub = (f"podman() {{ {podman}; }}\n"
            f"journalctl() {{ {journal}; }}\n"
            "READY=\n")
    done = subprocess.run([SH, "-c", stub + block],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


GONE = 'echo \'Error: no container with name or ID "mysql" found: no such container\' >&2; return 125'
JOURNAL = ("printf '%s\\n' "
           "'mysql[1]: Database is uninitialized and password option is not specified' "
           "'mysql[1]:     You need to specify one of the following as an environment variable:' "
           "'mysql[1]:     - MYSQL_ROOT_PASSWORD' "
           "'mysql[1]:     - MYSQL_RANDOM_ROOT_PASSWORD'")


def test_the_reason_survives_the_container_being_removed(tmp_path, monkeypatch):
    """THE DEFECT, measured. Quadlet removes the container when it exits, so the
    capture asked a container that no longer existed and reported podman's
    complaint instead of the software's."""
    out = run_block(tmp_path, monkeypatch, podman=GONE, journal=JOURNAL)

    assert "no such container" not in out, (
        "the report carries podman's error instead of the reason the software "
        "would not start")
    assert "MYSQL_RANDOM_ROOT_PASSWORD" in out, (
        "the variables the container named are not in the report, so nothing "
        "can answer it")
    assert "Database is uninitialized" in out


def test_the_journal_is_prefixed_like_any_other_capture(tmp_path, monkeypatch):
    """`container_env` and `discovery` find a technology's lines by that prefix.
    A fallback that wrote unprefixed lines would be invisible to both."""
    out = run_block(tmp_path, monkeypatch, podman=GONE, journal=JOURNAL)

    assert all(line.startswith("  somesoftware log: ")
               for line in out.splitlines() if line.strip()), out


def test_the_journal_is_redacted_too(tmp_path, monkeypatch):
    """The fallback is a second way out of the machine, and every way out has to
    be closed: the generated password is printed by the container, and the
    journal is where the container's output goes."""
    out = run_block(
        tmp_path, monkeypatch, podman=GONE,
        journal="printf '%s\\n' 'mysql[1]: GENERATED ROOT PASSWORD: aB3xY9zQwErTyU1'")

    assert "aB3xY9zQwErTyU1" not in out
    assert "redacted by the portal" in out


def test_a_container_that_is_still_there_is_still_asked_first(tmp_path, monkeypatch):
    """The container's own log is the better source when it exists: it is what
    THIS container printed, while the journal holds every restart. The fallback
    must not displace it."""
    out = run_block(
        tmp_path, monkeypatch,
        podman="printf '%s\\n' 'the container was still here'",
        journal="printf '%s\\n' 'the journal should not have been read'")

    assert "the container was still here" in out
    assert "should not have been read" not in out


def test_nothing_is_captured_when_the_service_came_up(tmp_path, monkeypatch):
    """Unchanged: a healthy machine's report carries no log at all."""
    block = capture_block(report_script(tmp_path, monkeypatch))
    stub = ("podman() { echo chatter; }\njournalctl() { echo chatter; }\nREADY=1\n")
    done = subprocess.run([SH, "-c", stub + block], capture_output=True,
                          text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


# --- the capture is still what it was ----------------------------------------------

def test_the_report_still_prefixes_every_line_with_the_technology(tmp_path, monkeypatch):
    """The prefix is how `container_env` and `discovery` tell one technology's
    log from another's on a multi-component machine. A filter inserted before
    it must not displace it."""
    block = capture_block(report_script(tmp_path, monkeypatch))

    assert "log: |'" in block, block
    out = run(tmp_path, monkeypatch, text="hello\n")
    assert out.strip() == "somesoftware log: hello", (
        "a filter inserted into the capture displaced the prefix that says which "
        "technology a log line belongs to")
