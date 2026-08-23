"""A false alarm costs trust exactly as a missed failure does (REQ-2026-0185).

The request asked for HashiCorp Vault. The agent skipped the package guess it had
already had refuted, added HashiCorp's own repository, and the machine came back:

    vault-2.0.4-1.x86_64
    repo_vault=present
    version_vault=WRONG wanted 1 got 2.0.4 [Vault v2.0.4 ...]
    vault=active
    http_8200=400
    LISTEN 0      4096         0.0.0.0:8200       0.0.0.0:*
    firewall_8200=open
    PORTAL: first-boot configuration finished

Read it. The package installed, the repository arrived, the unit is active, the
socket is listening, the firewall is open, first boot finished clean. That is a
working Vault. It was refused, the profile withdrawn and the request sent to
manual fulfilment, on two lines that say nothing about the machine:

  * `wanted 1` — a version the drafting code RECALLED. Vault is on 2.x, and the
    catalogue entry ("HashiCorp Vault") never promised a version at all. The
    recipe invented a promise and then failed the machine for breaking it.
  * `http_8200=400` — the report curls `/`, and Vault's API lives under `/v1/`,
    so a bare GET is answered 400. Only an HTTP server can send a 400. The `ss`
    line directly beneath proves the same thing a second way.

Both are the SAME mistake in two places: a check that tests something narrower
than what it claims to prove, then reports the difference as a broken machine.
The rule these tests hold: say only what the evidence supports.
"""

from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import boot_reports, configure

# The report exactly as the machine wrote it, kept verbatim. A paraphrase would
# let the test drift away from the thing it exists to describe.
REQ_0185 = """os_family=rhel
technologies=vault
os=Oracle Linux Server 9.8
--- packages ---
vault-2.0.4-1.x86_64
repo_vault=present
--- versions ---
version_vault=UNPROMISED (2.0.4)
--- services ---
vault=active
--- ports ---
http_8200=400
LISTEN 0      4096         0.0.0.0:8200       0.0.0.0:*
firewall_8200=open
--- first-boot log ---
PORTAL: first-boot configuration finished
"""


def test_the_machine_from_REQ_2026_0185_is_healthy():
    """The whole point, asserted once and plainly."""
    result = boot_reports.verdict(REQ_0185)
    assert result["ok"] is True, result["problems"]


# --- a status code proves an HTTP server answered, and nothing more -----------

@pytest.mark.parametrize("code", ["200", "204", "301", "302", "400", "401",
                                  "403", "404", "418"])
def test_a_server_that_answers_is_serving(code):
    """4xx included, deliberately. An API root that declines a bare GET is the
    normal shape of an API, not a broken service — Vault answers 400, and others
    answer 401 or 404 to the same request."""
    assert boot_reports.serving_http(code) is True


@pytest.mark.parametrize("code", ["000", "500", "501", "502", "503", "",
                                  "Connection refused", "curl: (7)"])
def test_nothing_answering_is_still_a_failure(code):
    """The check must not become unfailable. `000` and a curl error mean no HTTP
    conversation happened at all; 5xx means the server answered that it could
    not serve, which is a running-but-broken app and worth catching.

    501 IS DELIBERATELY LEFT HERE, against a tempting argument. A sealed Vault
    answers 501 from `/v1/sys/health` and is perfectly installed — but this
    check curls `/`, never a health endpoint, so that 501 cannot arrive by that
    route. Widening a rule to admit a case that does not occur buys nothing and
    sells the 5xx guard: a service answering "not implemented" to every request
    would then read as healthy. If a health endpoint is ever probed, that is a
    separate decision to make on its own evidence.
    """
    assert boot_reports.serving_http(code) is False


def test_a_dead_port_is_still_reported():
    report = "vault=active\nhttp_8200=000\nfirewall_8200=open\n"
    result = boot_reports.verdict(report)
    assert result["ok"] is False
    assert "http_8200 returned 000" in result["problems"][0]


def test_an_application_erroring_on_every_request_is_still_reported():
    """Widening to 4xx must not quietly widen to 5xx. A service returning 500 to
    everything is installed, running and useless."""
    assert boot_reports.verdict("http_8080=500\n")["ok"] is False


def test_nothing_listening_is_unaffected_by_any_of_this():
    """The independent second witness. Even if the status code check were
    wrong in both directions, `ss` still answers the question honestly."""
    report = "http_8200=200\nnothing listening on 8200\n"
    assert boot_reports.verdict(report)["ok"] is False


# --- asked always, compared only against a promise ----------------------------

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def _script(profile, code="vault", family="rhel", tmp_path=None, monkeypatch=None):
    (tmp_path / f"{code}.json").write_text(__import__("json").dumps(profile))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    doc = yaml.safe_load(configure.render([{"technology_code": code}], family, url))
    return next(f["content"] for f in doc["write_files"]
                if f["path"].endswith("report.sh"))


ROLLING = {"code": "vault", "builds_on": "oci/service-vm", "ports": [8200],
           "version_command": "vault version 2>&1",
           "repo": {"url": "https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo",
                    "gpg_key": "https://rpm.releases.hashicorp.com/gpg"},
           "rhel": {"packages": ["vault"], "services": ["vault"]}}


def test_a_recipe_that_promises_nothing_is_still_asked(tmp_path, monkeypatch):
    """THE regression that mattered most. An empty `expects` used to drop the
    technology from the version list entirely, so the machine was never asked at
    all — and a question never asked is the silence that shipped Redis 6.2 under
    an entry called "Redis 7". No promise must never mean no evidence.
    """
    script = _script(ROLLING, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert "vault version 2>&1" in script, "the machine is never asked"
    assert "version_vault=UNPROMISED" in script


def test_nothing_promised_means_nothing_compared(tmp_path, monkeypatch):
    script = _script(ROLLING, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert "version_vault=WRONG" not in script, (
        "it compares against a promise nobody made — the REQ-2026-0185 failure")
    assert "version_vault=OK" not in script, (
        "claiming OK would assert a comparison that never happened")


def test_unpromised_says_which_version_actually_arrived(tmp_path, monkeypatch):
    """Reported, not compared — but REPORTED. A line that only said 'unpromised'
    would be the silence again in a politer font."""
    script = _script(ROLLING, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert "${GOT:-none}" in script


def test_a_promise_that_was_made_is_still_kept(tmp_path, monkeypatch):
    """The lesson must not overshoot. Where a recipe DOES pin a version, the
    comparison is the whole point and both branches must exist."""
    pinned = {**ROLLING, "code": "mongodb", "expects": "7",
              "version_command": "mongod --version 2>&1",
              "rhel": {"packages": ["mongodb-org"], "services": ["mongod"]}}
    script = _script(pinned, code="mongodb", tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert "version_mongodb=OK" in script
    assert "version_mongodb=WRONG" in script


def test_the_report_still_parses_as_cloud_config(tmp_path, monkeypatch):
    """A stray quote here breaks first boot on every machine, silently."""
    (tmp_path / "vault.json").write_text(__import__("json").dumps(ROLLING))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    doc = yaml.safe_load(configure.render([{"technology_code": "vault"}], "rhel", url))
    assert isinstance(doc, dict) and doc.get("write_files")


def test_unpromised_carries_no_raw_output(tmp_path, monkeypatch):
    """`$RAW` is multi-line for several of these tools (java prints three lines).
    On the WRONG branch that is worth the risk — it is what gets a mismatch
    fixed. On a line that will now appear for every rolling-repo install it is
    not: a stray line would be read as another key=value fact by verdict()."""
    script = _script(ROLLING, tmp_path=tmp_path, monkeypatch=monkeypatch)
    unpromised = next(line for line in script.splitlines()
                      if "version_vault=UNPROMISED" in line)
    assert "$RAW" not in unpromised


# --- the verdict side of UNPROMISED -------------------------------------------

def test_unpromised_is_a_fact_on_the_record_not_a_failure():
    assert boot_reports.verdict("version_vault=UNPROMISED (2.0.4)\n")["ok"] is True


def test_a_broken_promise_is_still_a_failure():
    """The Redis 6.2 guard, untouched."""
    report = "version_redis7=WRONG wanted 7 got 6.2.7 [Redis server v=6.2.7]\n"
    assert boot_reports.verdict(report)["ok"] is False


def test_asked_and_unable_to_answer_is_a_failure():
    """THE hole this fix could have left. `UNPROMISED (none)` means the version
    command ran and produced no number at all — usually the binary is not on the
    path under the name the recipe uses. Passing it because "nothing was
    promised" would make the check unfailable: the report would read as though
    the machine answered when it did not."""
    result = boot_reports.verdict("vault=active\nversion_vault=UNPROMISED (none)\n")
    assert result["ok"] is False
    assert "could not say" in result["problems"][0]


def test_an_answer_is_still_an_answer():
    assert boot_reports.verdict("version_vault=UNPROMISED (2.0.4)\n")["ok"] is True


# --- three traps found QA'ing this change -------------------------------------

def test_a_repository_install_waits_for_its_port_like_an_archive_does(
        tmp_path, monkeypatch):
    """The wait loop was gated on archives alone. A service installed from a
    vendor repository binds no faster — reporting the instant `systemctl enable`
    returns would call it broken on the port check, which is the recurring
    failure this whole file is about."""
    (tmp_path / "vault.json").write_text(__import__("json").dumps(ROLLING))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", tmp_path)
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    rendered = configure.render([{"technology_code": "vault"}], "rhel", url)
    assert "seq 1 60" in rendered, "it reports before the service can answer"


def test_the_unsupported_repo_placeholder_does_not_disable_its_own_alarm():
    """`false  # no debian repo support yet:` commented out the
    `|| echo 'PORTAL FAILURE...'` that follows it on the same rendered line, so
    the step failed silently and the machine reported nothing wrong. A
    placeholder that disables the alarm it exists to trigger is worse than none."""
    for family, command in configure._REPO_ADD.items():
        assert "#" not in command, (
            f"{family}'s repo command comments out the rest of its own line: "
            f"{command!r}")


def test_a_package_override_is_asked_what_it_delivered(monkeypatch):
    """`merged` omitted _package_overrides() while profile_for() included it, so
    a technology introduced only by CONFIG_PACKAGE_MAP was installed and never
    asked, and an override's own `expects` was ignored in favour of the shipped
    one."""
    monkeypatch.setenv("CONFIG_PACKAGE_MAP", __import__("json").dumps(
        {"redis7": {"expects": "8", "version_command": "redis-server --version",
                    "rhel": {"packages": ["redis"], "services": ["redis"]}}}))
    url = configure.boot_report_url("REQ-2026-0199", "oci-service-vm")
    rendered = configure.render([{"technology_code": "redis7"}], "rhel", url)
    assert "8|8.*)" in rendered, (
        "the override's expectation was ignored and the shipped one checked")
