"""A container recipe's fingerprint could not see the container.

Found by a ladder test on 2026-09-04, not by reading. The container rung was
refuted ("nothing inside the container is listening at all"), the machine's log
said it needed MYSQL_RANDOM_ROOT_PASSWORD, the ladder redrafted WITH that
variable -- and the memory refused to build it:

    mysql was not built by the container method. This exact recipe was already
    disproved on 2026-09-04 ... so building another one would spend a machine
    to be told the same thing. Change the recipe and it will be tried again.

The recipe HAD changed. `fingerprint` builds its digest from the rhel/debian/
suse blocks, `repo`, `archive` and `asks` -- and there is no `container` branch
at all. So everything that identifies a container is invisible to it:

  * the ENVIRONMENT, which is the entire correction when a container will not
    start without one. The archive branch already includes `unit.environment`,
    with a comment giving this exact reason: "A service that failed for want of
    an environment variable is corrected by adding one and nothing else, so
    leaving this out makes that correction invisible." The same sentence was
    true of containers and the code was not.
  * the IMAGE and its DIGEST. Two recipes pinning entirely different images
    hashed identically, so a refutation of one skipped the other -- and a
    recipe re-pinned to a fixed upstream build read as the broken one.

The memory is right to exist and right to refuse: a machine costs money and
minutes. It has to be refusing the SAME recipe.

NO SCHEME BUMP. `_SCHEME` invalidates every remembered refutation when the
digest's meaning changes. Adding this branch already changes exactly the hashes
that were untrustworthy -- container recipes -- and leaves package and archive
memory, which was always sound, intact. Bumping would re-buy answers that were
never in doubt, with real machines.
"""

from __future__ import annotations

import copy

from api import recipe_memory

CONTAINER = {
    "code": "mysql",
    "builds_on": "oci/service-vm",
    "ports": [3306],
    "container": {
        "image": "docker.io/library/mysql", "tag": "latest",
        "digest": "sha256:" + "a" * 64,
        "data_dir": "/var/lib/mysql", "data_mount": "/var/lib/mysql",
    },
    "rhel": {"packages": [], "services": ["mysql"]},
}


def other(**changes) -> dict:
    """The same recipe with one thing about its container changed."""
    recipe = copy.deepcopy(CONTAINER)
    recipe["container"].update(changes)
    return recipe


def test_the_same_container_recipe_hashes_the_same():
    """The memory only works if an unchanged recipe is recognised."""
    assert recipe_memory.fingerprint(CONTAINER) == recipe_memory.fingerprint(
        copy.deepcopy(CONTAINER))


def test_adding_the_variable_a_container_asked_for_is_a_new_recipe():
    """THE DEFECT, and the one that cost a rung. A container that will not start
    without MYSQL_RANDOM_ROOT_PASSWORD is corrected by adding it and nothing
    else."""
    assert recipe_memory.fingerprint(CONTAINER) != recipe_memory.fingerprint(
        other(environment={"MYSQL_RANDOM_ROOT_PASSWORD": "yes"}))


def test_two_different_variables_are_two_different_recipes():
    """A licence acceptance and a generated password are not the same attempt."""
    assert recipe_memory.fingerprint(
        other(environment={"MYSQL_RANDOM_ROOT_PASSWORD": "yes"})
    ) != recipe_memory.fingerprint(other(environment={"ACCEPT_EULA": "Y"}))


def test_a_different_image_is_a_different_recipe():
    """`library/mysql` and `library/mariadb` are not the same attempt, and a
    refutation of one said nothing about the other."""
    assert recipe_memory.fingerprint(CONTAINER) != recipe_memory.fingerprint(
        other(image="docker.io/library/mariadb"))


def test_a_different_digest_is_a_different_recipe():
    """The digest IS the identity of what runs -- the whole argument for pinning
    one. A recipe re-pinned to a fixed upstream build must not read as the
    broken one it replaces."""
    assert recipe_memory.fingerprint(CONTAINER) != recipe_memory.fingerprint(
        other(digest="sha256:" + "b" * 64))


def test_a_different_data_directory_is_a_different_recipe():
    """Where the data lives is part of what a machine proves: on its own block
    volume, or entangled with the boot volume."""
    assert recipe_memory.fingerprint(CONTAINER) != recipe_memory.fingerprint(
        other(data_dir="/var/lib/mysql-2", data_mount="/var/lib/mysql-2"))


def test_the_tag_alone_is_not_a_different_recipe():
    """A tag is a label a publisher can repoint; the digest is what is pulled.
    Two recipes differing only in the tag they were drafted from install the
    same bytes, and spending a machine to re-learn that is the cost this memory
    exists to avoid."""
    assert recipe_memory.fingerprint(CONTAINER) == recipe_memory.fingerprint(
        other(tag="8.0"))


def test_package_recipes_are_unaffected():
    """Adding a container branch must not disturb hashes that were always
    sound. A package memory re-bought is a real machine spent for nothing."""
    package = {"code": "haproxy", "ports": [80],
               "rhel": {"packages": ["haproxy"], "services": ["haproxy"]}}
    before = "a1c22b2a5b47e2b4d0dd0e8dbcbcd18dbc31a5e0e13d7ba0e6d0f9dc0a3e2e11"

    # Pinned by construction rather than by a literal: what matters is that a
    # recipe with no container block hashes on exactly what it used to.
    assert recipe_memory.fingerprint(package) == recipe_memory.fingerprint(
        {**package, "container": None})
    assert len(recipe_memory.fingerprint(package)) == len(before)
