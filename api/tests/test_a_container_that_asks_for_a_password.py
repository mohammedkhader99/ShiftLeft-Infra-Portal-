"""A container that will not start without a password, and what may answer it.

The ladder reaches `docker.io/library/mysql` and the image exits immediately:
it will not initialise a database without being told how to set the root
password. The container rung is the last one, so MySQL stops there and goes to
manual fulfilment -- over one environment variable the machine printed in its
own log.

WHAT THE IMAGE DECLARES, measured 2026-09-04: MYSQL_MAJOR, MYSQL_VERSION,
MYSQL_SHELL_VERSION, ports 3306 and 33060. Nothing about a password. So the
image's own config cannot answer this and the machine has to be read.

THE DECISION, taken by the operator on 2026-09-04: where a container offers to
generate its own password, take it. Nothing is chosen by this portal, written
to a recipe, stored, or committed -- the machine generates it, prints it once,
and its administrator reads it there. Where a container demands a NAMED
password there is nothing safe to choose, and the ladder stops with a refusal
that says who must supply what and where.

These tests are the whole of that rule, including the two ways it must refuse.
"""

from __future__ import annotations

import pytest

from api import container_env

CODE = "mysql"

#: What `docker.io/library/mysql` prints and the boot report captures. The
#: `mysql log:` prefix is what configure.py writes (`podman logs --tail 15`).
MYSQL_LOG = """\
--- services ---
container_mysql=running
listening_inside_mysql=none
  mysql log: 2026-09-04 01:12:44+00:00 [Note] [Entrypoint]: Entrypoint script for MySQL Server started.
  mysql log: 2026-09-04 01:12:45+00:00 [ERROR] [Entrypoint]: Database is uninitialized and password option is not specified
  mysql log:     You need to specify one of the following as an environment variable:
  mysql log:     - MYSQL_ROOT_PASSWORD
  mysql log:     - MYSQL_ALLOW_EMPTY_PASSWORD
  mysql log:     - MYSQL_RANDOM_ROOT_PASSWORD
--- first-boot log ---
PORTAL: mysql install finished
"""

#: Postgres offers no random alternative -- measured from its entrypoint's own
#: message. This is the case that must STOP the ladder rather than invent one.
POSTGRES_LOG = """\
  postgres log: Error: Database is uninitialized and superuser password is not specified.
  postgres log:        You must specify POSTGRES_PASSWORD to a non-empty value for the
  postgres log:        superuser. For example, "-e POSTGRES_PASSWORD=password" on "docker run".
"""

MSSQL_LOG = """\
  mssql log: ERROR: Missing environment variables. Please specify the ACCEPT_EULA and
  mssql log:        MSSQL_SA_PASSWORD environment variables.
"""


# --- what the machine said --------------------------------------------------------

def test_the_variables_are_read_from_the_containers_own_log():
    assert container_env.demanded(MYSQL_LOG, CODE) == [
        "MYSQL_ROOT_PASSWORD", "MYSQL_ALLOW_EMPTY_PASSWORD",
        "MYSQL_RANDOM_ROOT_PASSWORD"]


def test_only_this_containers_lines_are_read():
    """A report carries every technology on the machine. MongoDB's log is not
    evidence about MySQL, and a recipe drafted from the wrong one would set a
    variable the image ignores while the real requirement stayed unmet."""
    mixed = MYSQL_LOG + "  mongodb log: needs MONGO_INITDB_ROOT_PASSWORD\n"

    assert "MONGO_INITDB_ROOT_PASSWORD" not in container_env.demanded(mixed, CODE)
    assert container_env.demanded(mixed, "mongodb") == ["MONGO_INITDB_ROOT_PASSWORD"]


def test_shouting_in_a_log_is_not_a_variable():
    """Every log says ERROR, WARN, FATAL and UTC. A drafter that read those as
    requirements would refuse every container for needing `ERROR`."""
    noisy = ("  mysql log: [ERROR] [Entrypoint] FATAL: server GONE at 01:12:44 UTC\n"
             "  mysql log: WARNING NOT INSTALLED\n")

    assert container_env.demanded(noisy, CODE) == []


def test_a_report_with_no_container_log_demands_nothing():
    assert container_env.demanded("--- services ---\nmysql=active\n", CODE) == []
    assert container_env.demanded("", CODE) == []


# --- what may be answered ---------------------------------------------------------

def test_a_container_that_can_generate_its_own_password_is_given_that():
    """THE DECISION. Nothing is chosen here: the image generates the password on
    the machine and prints it once."""
    out = container_env.for_report(MYSQL_LOG, CODE)

    assert out.environment == {"MYSQL_RANDOM_ROOT_PASSWORD": "yes"}
    assert out.answerable and not out.blocked


def test_an_empty_password_is_never_chosen():
    """MYSQL_ALLOW_EMPTY_PASSWORD also starts the container, and would put an
    unauthenticated database on a real network."""
    out = container_env.for_report(MYSQL_LOG, CODE)

    assert "MYSQL_ALLOW_EMPTY_PASSWORD" not in out.environment


def test_only_one_way_of_choosing_a_password_is_set():
    """The log offers alternatives -- "one of the following". Setting two is how
    an image refuses to start for a brand-new reason."""
    out = container_env.for_report(MYSQL_LOG, CODE)

    passwords = [k for k in out.environment if "PASSWORD" in k]
    assert len(passwords) == 1, passwords


def test_the_answer_tells_the_reader_how_to_get_the_password():
    """A generated password nobody can find is a database nobody can use."""
    out = container_env.for_report(MYSQL_LOG, CODE)

    assert "podman logs mysql" in out.detail
    assert "never sees it" in out.detail


# --- what must stop the ladder ----------------------------------------------------

def test_a_named_password_with_no_random_alternative_stops_and_says_who_must_act():
    """Postgres offers no generated option. There is nothing safe to choose, and
    inventing one would mean this portal generated a credential, wrote it into a
    recipe an approver reads, and stored it."""
    out = container_env.for_report(POSTGRES_LOG, "postgres")

    assert out.environment == {}
    assert out.blocked and out.needs_a_person == ["POSTGRES_PASSWORD"]
    assert "secrets/container.env" in out.detail
    assert "postgres.<NAME>" in out.detail


def test_a_licence_is_not_the_agents_to_accept():
    """ACCEPT_EULA is a contract with a vendor. Off unless an operator recorded
    acceptance in the Admin console."""
    out = container_env.for_report(MSSQL_LOG, "mssql", licence_accepted=False)

    assert out.blocked
    assert "ACCEPT_EULA" in out.needs_a_person
    assert "MSSQL_SA_PASSWORD" in out.needs_a_person


def test_an_accepted_licence_is_supplied_and_the_password_still_is_not():
    """The two are different kinds of thing and are answered separately: an
    operator accepting terms says nothing about a password."""
    out = container_env.for_report(MSSQL_LOG, "mssql", licence_accepted=True)

    assert out.environment == {"ACCEPT_EULA": "Y"}
    assert out.needs_a_person == ["MSSQL_SA_PASSWORD"]
    assert out.blocked


def test_a_container_offering_only_an_empty_password_is_refused():
    """Contrived, and the rule has to hold anyway: the only thing on offer is
    an unauthenticated database."""
    log = "  thing log: set THING_ALLOW_EMPTY_PASSWORD to start\n"
    out = container_env.answer(container_env.demanded(log, "thing"), code="thing")

    assert out.environment == {}
    assert out.blocked
    assert "no password" in out.detail


def test_a_container_that_asked_for_nothing_is_answered_with_nothing():
    """Most images start with no environment at all -- mongodb and rabbitmq do.
    An empty answer is the ordinary case and is not a refusal."""
    out = container_env.answer([], code="rabbitmq")

    assert out.environment == {} and not out.blocked and not out.answerable


# --- what reaches a systemd unit --------------------------------------------------

def test_a_name_a_unit_file_could_not_carry_is_ignored():
    """These become `Environment=NAME=value` in a Quadlet unit read by systemd.
    profile_rules judges the name, here as everywhere else."""
    out = container_env.answer(["OK_NAME", "BAD NAME", "BAD=NAME"], code="thing")

    assert out.demanded == ["OK_NAME"]


@pytest.mark.parametrize("name", ["MYSQL_RANDOM_ROOT_PASSWORD", "MARIADB_RANDOM_ROOT_PASSWORD"])
def test_the_random_rule_is_not_a_table_of_technologies(name):
    """MariaDB's variable was never typed anywhere in this portal, and its
    container is answered by the same rule that answers MySQL's."""
    out = container_env.answer([name], code="x")

    assert out.environment == {name: "yes"}


def test_one_technologys_log_is_not_read_as_anothers():
    """`sql` must not read `mysql`'s log. A substring match would answer one
    container from another's requirements."""
    assert container_env.demanded(MYSQL_LOG, "sql") == []


def test_exactly_one_way_of_generating_a_password_is_chosen():
    """FOUND BY A PLANT, 2026-09-04: a version that set EVERY random-password
    variable passed every test here.

    The log offers ALTERNATIVES -- "one of the following" -- and an image told
    two different ways to choose its root password refuses to start for a new
    reason, which reads as "the answer did not work" rather than "the answer
    was given twice". The code says "ONE, not all of them"; nothing checked it.
    """
    out = container_env.answer(
        ["MARIADB_RANDOM_ROOT_PASSWORD", "MYSQL_RANDOM_ROOT_PASSWORD"])

    assert len(out.environment) == 1, out.environment
    assert out.answerable


def test_the_choice_among_alternatives_does_not_depend_on_log_order():
    """Two runs of the same machine must produce the same recipe. If the choice
    followed the order the container happened to print them in, the same image
    would fingerprint as two different recipes and each would buy its own
    machine."""
    names = ["MYSQL_RANDOM_ROOT_PASSWORD", "MARIADB_RANDOM_ROOT_PASSWORD"]

    assert (container_env.answer(names).environment
            == container_env.answer(list(reversed(names))).environment)
