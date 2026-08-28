"""D2d: the credential a container needs to start comes from a person.

Measured from `mcr.microsoft.com/mssql/server` on 2026-08-28: the image declares
`MSSQL_PID=developer` and `MSSQL_RPC_PORT=135`, and does NOT declare
`ACCEPT_EULA` or `MSSQL_SA_PASSWORD` — both of which it requires at runtime. So
SQL Server reaches its container rung, starts, and exits immediately.

TWO DIFFERENT KINDS OF MISSING VALUE, and they must not be solved the same way:

    ACCEPT_EULA         a CONTRACT. An operator records the acceptance in the
                        Admin console and the recipe carries it. The agent
                        recommends; it does not sign. (api/settings.py)
    MSSQL_SA_PASSWORD   a CREDENTIAL. It must never appear in a recipe at all —
                        a recipe is drafted by an agent, written to a store,
                        read by two services, quoted in an audit trail and shown
                        to an approver. It lives in a file only a person writes.

This file is the second one. The file is inside `/secrets`, which this service
already mounts read-only, so nothing here needs a new mount or a compose
variable per technology — which would be a table by another name.

Nothing here reaches the network or a machine.
"""

from __future__ import annotations

import json

import pytest

yaml = pytest.importorskip("yaml")

from orchestrator import configure

PAR = ("https://objectstorage.me-dubai-1.oraclecloud.com/p/T/n/ns"
       "/b/shiftleft-boot-reports/o/")

MSSQL = {
    "code": "mssql", "builds_on": "oci/service-vm", "ports": [1433],
    "container": {
        "image": "mcr.microsoft.com/mssql/server", "tag": "latest",
        "digest": "sha256:" + "c" * 64,
        "data_dir": "/var/lib/mssql", "data_mount": "/var/lib/mssql",
        "environment": {"ACCEPT_EULA": "Y"},
    },
    "rhel": {"packages": [], "services": ["mssql"]},
}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("CONFIG_ENABLED", "true")
    monkeypatch.delenv("CONFIG_PACKAGE_MAP", raising=False)
    monkeypatch.setenv("OCI_BOOT_REPORT_PAR_URL", PAR)


def secrets_file(tmp_path, monkeypatch, text):
    path = tmp_path / "container.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(configure, "CONTAINER_SECRETS_FILE", str(path))
    return path


def render(tmp_path, monkeypatch, profile=None, code="mssql"):
    store = tmp_path / "profiles"
    store.mkdir(exist_ok=True)
    (store / f"{code}.json").write_text(json.dumps(profile or MSSQL))
    monkeypatch.setattr(configure, "GENERATED_PROFILE_DIR", store)
    url = configure.boot_report_url("REQ-2026-0225", "oci-service-vm")
    return configure.render([{"technology_code": code}], "rhel", url)


def unit_file(tmp_path, monkeypatch, **kw):
    doc = yaml.safe_load(render(tmp_path, monkeypatch, **kw))
    return next(f for f in doc["write_files"]
                if f["path"].endswith(".container"))


# --- reading what a person wrote ----------------------------------------------

def test_a_scoped_secret_is_found(tmp_path, monkeypatch):
    secrets_file(tmp_path, monkeypatch, "mssql.MSSQL_SA_PASSWORD=Str0ng!Pass\n")

    assert configure.container_secrets("mssql") == {
        "MSSQL_SA_PASSWORD": "Str0ng!Pass"}


def test_a_secret_never_reaches_another_technology(tmp_path, monkeypatch):
    """THE reason for the prefix. One file holds every operator credential, and
    a MongoDB container has no business receiving SQL Server's password."""
    secrets_file(tmp_path, monkeypatch,
                 "mssql.MSSQL_SA_PASSWORD=Str0ng!Pass\n"
                 "mongodb.MONGO_INITDB_ROOT_PASSWORD=other\n")

    assert configure.container_secrets("mongodb") == {
        "MONGO_INITDB_ROOT_PASSWORD": "other"}
    assert "MSSQL_SA_PASSWORD" not in configure.container_secrets("mongodb")


def test_a_line_with_no_technology_is_ignored(tmp_path, monkeypatch):
    """Not "applied to every container". That is how a credential ends up
    somewhere nobody meant it to be.

    TWO MECHANISMS enforce this and the test asserts the PROPERTY. An unprefixed
    line splits into a prefix that is not the technology and an EMPTY key, so
    the prefix check and `ENV_KEY` both refuse it independently. Worth writing
    down: removing either one alone still refuses, so a plant against one of
    them will not fail this test and that is not the test being weak."""
    secrets_file(tmp_path, monkeypatch, "MSSQL_SA_PASSWORD=Str0ng!Pass\n")

    assert configure.container_secrets("mssql") == {}


def test_comments_and_blank_lines_are_ignored(tmp_path, monkeypatch):
    secrets_file(tmp_path, monkeypatch,
                 "# SQL Server will not start without this\n"
                 "\n"
                 "   mssql.MSSQL_SA_PASSWORD=Str0ng!Pass   \n")

    assert configure.container_secrets("mssql") == {
        "MSSQL_SA_PASSWORD": "Str0ng!Pass"}


def test_an_absent_file_is_an_ordinary_empty_answer(tmp_path, monkeypatch):
    """The container then fails on the machine and says so in its own words,
    which is honest. Inventing a password here would be worse."""
    monkeypatch.setattr(configure, "CONTAINER_SECRETS_FILE",
                        str(tmp_path / "nothing-here.env"))

    assert configure.container_secrets("mssql") == {}


# --- and it is still a unit file ----------------------------------------------

def test_a_value_the_unit_file_cannot_carry_is_refused(tmp_path, monkeypatch):
    """These become `Environment=K=V` lines in a systemd unit read by root, so
    every value goes through the same `ENV_VALUE` rule a recipe's own
    environment does: printable ASCII and nothing else.

    NOT A NEWLINE TEST, and the first version of this was. A newline cannot
    reach here — the file is parsed line by line, so a value containing one is
    two lines by construction and the second is discarded for having no
    technology prefix. Asserting the impossible passes forever and guards
    nothing. A control character is the thing that CAN arrive."""
    secrets_file(tmp_path, monkeypatch,
                 "mssql.MSSQL_SA_PASSWORD=ok\n"
                 "mssql.TABBED=a\tb\n"
                 "mssql.BELL=a\x07b\n")

    found = configure.container_secrets("mssql")

    assert found == {"MSSQL_SA_PASSWORD": "ok"}, found


def test_a_name_that_is_not_an_environment_name_is_refused(
        tmp_path, monkeypatch):
    secrets_file(tmp_path, monkeypatch,
                 "mssql.lower_case=x\nmssql.HAS-DASH=y\nmssql.GOOD=z\n")

    assert configure.container_secrets("mssql") == {"GOOD": "z"}


# --- what the machine actually receives ---------------------------------------

def test_the_operators_secret_reaches_the_container(tmp_path, monkeypatch):
    secrets_file(tmp_path, monkeypatch, "mssql.MSSQL_SA_PASSWORD=Str0ng!Pass\n")

    content = unit_file(tmp_path, monkeypatch)["content"]

    assert "Environment=MSSQL_SA_PASSWORD=Str0ng!Pass" in content
    assert "Environment=ACCEPT_EULA=Y" in content, (
        "the recipe's own environment was dropped when secrets were merged")


def test_the_operators_value_wins_over_the_recipes(tmp_path, monkeypatch):
    """A recipe is a draft an agent wrote; the file is what a person decided."""
    secrets_file(tmp_path, monkeypatch, "mssql.ACCEPT_EULA=N\n")

    content = unit_file(tmp_path, monkeypatch)["content"]

    assert "Environment=ACCEPT_EULA=N" in content
    assert "Environment=ACCEPT_EULA=Y" not in content


def test_the_unit_is_not_world_readable_once_it_holds_a_credential(
        tmp_path, monkeypatch):
    """systemd reads it as root. A password in a 0644 file is readable by every
    local account on the machine the requester was given."""
    secrets_file(tmp_path, monkeypatch, "mssql.MSSQL_SA_PASSWORD=Str0ng!Pass\n")

    assert unit_file(tmp_path, monkeypatch)["permissions"] == "0600"


def test_a_unit_with_no_credential_is_left_as_it_was(tmp_path, monkeypatch):
    """Not a blanket tightening: the mode changes because of what the file now
    holds, and a container with no secret has not changed."""
    monkeypatch.setattr(configure, "CONTAINER_SECRETS_FILE",
                        str(tmp_path / "absent.env"))

    assert unit_file(tmp_path, monkeypatch)["permissions"] == "0644"


def test_the_rendered_user_data_is_still_valid_yaml(tmp_path, monkeypatch):
    """A password is arbitrary text going into a YAML document. If it broke the
    document, every technology in the request would fail to configure."""
    secrets_file(tmp_path, monkeypatch,
                 "mssql.MSSQL_SA_PASSWORD=a: b #c |d 'e' \"f\"\n")

    doc = yaml.safe_load(render(tmp_path, monkeypatch))

    assert isinstance(doc, dict) and doc.get("write_files")
