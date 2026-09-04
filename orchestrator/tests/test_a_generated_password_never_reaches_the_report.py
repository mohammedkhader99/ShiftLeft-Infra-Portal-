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


def capture_pipeline(script: str) -> str:
    """The shipped `podman logs | sed ... | sed ...` line, with podman removed.

    Only the command that FETCHES the log is dropped; every filter it is piped
    through is the script's own text, unmodified.
    """
    line = next(ln for ln in script.splitlines() if "podman logs" in ln)
    assert "sed" in line, f"the capture no longer filters anything: {line!r}"
    return line.split("|", 1)[1].strip()


def run(tmp_path, monkeypatch, text=LOG):
    pipeline = capture_pipeline(report_script(tmp_path, monkeypatch))
    done = subprocess.run([SH, "-c", pipeline], input=text,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


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


# --- the capture is still what it was ----------------------------------------------

def test_the_report_still_prefixes_every_line_with_the_technology(tmp_path, monkeypatch):
    """The prefix is how `container_env` and `discovery` tell one technology's
    log from another's on a multi-component machine. A filter inserted before
    it must not displace it."""
    script = report_script(tmp_path, monkeypatch)
    line = next(ln for ln in script.splitlines() if "podman logs" in ln)

    assert "log: |'" in line, line
    out = run(tmp_path, monkeypatch, text="hello\n")
    assert out.strip() == "somesoftware log: hello", (
        "a filter inserted into the capture displaced the prefix that says which "
        "technology a log line belongs to")
