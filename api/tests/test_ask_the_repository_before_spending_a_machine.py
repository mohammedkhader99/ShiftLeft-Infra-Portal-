"""C10: the repository is asked before a machine is spent.

Two failures on 2026-08-25 cost five machines between them and provisioned
nothing. Both were knowable from the API in under a second:

    dotnet8.0   Four machines, four requests, each told "Unable to find a
                match" for a name the wildcard search had just shown it. The
                reason took until today to see: `dotnet8.0` is a SOURCE package
                in OL9's AppStream. `repoquery` lists source packages; `dnf
                install` cannot install one.

    mongodb     One machine. The recipe's repository URL was a 404 — MongoDB
                publishes no `.repo` file at that path. Their repository is
                healthy; our URL had rotted with nothing watching it.

THE PROPERTY THAT MATTERS MORE THAN EITHER: this refuses and never approves.
Every uncertainty — a slow repository, a proxy, a listing that would not parse,
a budget spent — is "no objection", and no objection means the ladder behaves
exactly as it did before this check existed. A gate that becomes a new way for
provisioning to fail has cost more than it saved.

Nothing here reaches the network. Every repository answer is injected.
"""

from __future__ import annotations

import gzip
import urllib.error

import pytest

from api import repo_facts as rf

APPSTREAM = "https://yum.oracle.com/repo/OracleLinux/OL9/appstream/x86_64/"
BASEOS = "https://yum.oracle.com/repo/OracleLinux/OL9/baseos/latest/x86_64/"
VENDOR = "https://repo.mongodb.org/yum/redhat/9/mongodb-org/8.0/x86_64/"


def repomd(href="repodata/primary.xml.gz", size=1000) -> bytes:
    return (f'<repomd><data type="primary"><location href="{href}"/>'
            f"<size>{size}</size></data></repomd>").encode()


def primary(*packages: tuple[str, str]) -> bytes:
    """`packages` are (name, arch) pairs, exactly as primary.xml carries them."""
    body = "".join(
        f'<package type="rpm"><name>{n}</name><arch>{a}</arch>'
        f'<version epoch="0" ver="1" rel="1"/></package>' for n, a in packages)
    return gzip.compress(f"<metadata>{body}</metadata>".encode())


def repo(**pages):
    """A fake internet. Anything not named 404s."""
    def fetch(url):
        if url in pages:
            return pages[url]
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
    return fetch


@pytest.fixture(autouse=True)
def _no_cache():
    rf._cache.clear()
    yield
    rf._cache.clear()


# --- what a URL verdict may conclude ------------------------------------------

def test_a_404_is_conclusive():
    assert rf.url_verdict("https://x/gone.repo", fetch=repo()) == rf.DEAD


def test_a_timeout_is_NOT_a_dead_url():
    """A slow afternoon is not a wrong URL. Treating it as one would refuse
    perfectly good recipes whenever the network sulked."""
    def fetch(url):
        raise TimeoutError("took too long")
    assert rf.url_verdict("https://x/a.repo", fetch=fetch) == rf.UNKNOWN


def test_a_server_error_is_NOT_a_dead_url():
    def fetch(url):
        raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)
    assert rf.url_verdict("https://x/a.repo", fetch=fetch) == rf.UNKNOWN


def test_something_that_is_not_a_url_at_all_is_dead():
    for junk in ("", "file:///etc/passwd", "repo.mongodb.org", "ftp://x/y"):
        assert rf.url_verdict(junk, fetch=repo()) == rf.DEAD


# --- a source package is not an installable package ---------------------------

def test_a_SOURCE_package_is_not_offered():
    """THE test in this file. `dotnet8.0` is in OL9's AppStream metadata with
    arch=src. repoquery lists it, dnf cannot install it, and four machines were
    spent learning that one distinction."""
    fetch = repo(**{
        APPSTREAM + "repodata/repomd.xml": repomd(),
        APPSTREAM + "repodata/primary.xml.gz": primary(
            ("dotnet8.0", "src"), ("dotnet-sdk-8.0", "x86_64")),
    })
    names = rf.package_names(APPSTREAM, fetch=fetch)

    assert "dotnet-sdk-8.0" in names
    assert "dotnet8.0" not in names, (
        "a source package was offered as installable — this is the .NET failure")


def test_the_real_recipe_is_refused_and_says_what_to_use_instead():
    fetch = repo(**{
        APPSTREAM + "repodata/repomd.xml": repomd(),
        APPSTREAM + "repodata/primary.xml.gz": primary(
            ("dotnet8.0", "src"), ("dotnet-sdk-8.0", "x86_64"),
            ("aspnetcore-runtime-8.0", "x86_64")),
        BASEOS + "repodata/repomd.xml": repomd(),
        BASEOS + "repodata/primary.xml.gz": primary(("bash", "x86_64")),
    })
    refusal = rf.check_recipe({"rhel": {"packages": ["dotnet8.0"]}}, fetch=fetch)

    assert refusal is not None, "the recipe that cost four machines was allowed"
    assert "dotnet-sdk-8.0" in str(refusal), (
        "the refusal does not tell anyone what to use instead")


def test_the_corrected_recipe_has_no_objection():
    fetch = repo(**{
        APPSTREAM + "repodata/repomd.xml": repomd(),
        APPSTREAM + "repodata/primary.xml.gz": primary(
            ("dotnet-sdk-8.0", "x86_64"), ("aspnetcore-runtime-8.0", "x86_64")),
        BASEOS + "repodata/repomd.xml": repomd(),
        BASEOS + "repodata/primary.xml.gz": primary(("bash", "x86_64")),
    })
    assert rf.check_recipe(
        {"rhel": {"packages": ["dotnet-sdk-8.0", "aspnetcore-runtime-8.0"]}},
        fetch=fetch) is None


# --- a dead repository URL ----------------------------------------------------

def test_a_recipe_whose_repository_is_a_404_is_refused():
    refusal = rf.check_recipe(
        {"repo": {"url": "https://repo.mongodb.org/yum/redhat/mongodb-org-7.0.repo"},
         "rhel": {"packages": ["mongodb-org"]}}, fetch=repo())

    assert refusal is not None
    assert "404" in str(refusal)
    assert "Nothing was built" in str(refusal)


def test_a_dead_SIGNING_KEY_is_also_refused():
    fetch = repo(**{VENDOR: b"ok"})
    refusal = rf.check_recipe(
        {"repo": {"url": VENDOR, "gpg_key": "https://pgp.example/gone.asc"},
         "rhel": {"packages": []}}, fetch=fetch)
    assert refusal is not None and "signing key" in str(refusal)


def test_a_dot_repo_file_is_not_asked_for_its_package_list():
    """A `.repo` file points AT a repository; it is not one. Asking it for
    `repodata/repomd.xml` would 404 and read as 'this vendor offers nothing'."""
    bases = rf.repos_for({"repo": {"url": "https://x/thing.repo"}}, "rhel")
    assert "https://x/thing.repo" not in bases
    bases = rf.repos_for({"repo": {"url": VENDOR}}, "rhel")
    assert VENDOR in bases


# --- refuses, never approves --------------------------------------------------

def test_an_unreadable_repository_refuses_NOTHING():
    """A repository we could not read might be the one carrying the package.
    Refusing here would make this check the thing that breaks provisioning."""
    fetch = repo(**{
        APPSTREAM + "repodata/repomd.xml": repomd(),
        APPSTREAM + "repodata/primary.xml.gz": primary(("bash", "x86_64")),
        # BASEOS answers nothing at all.
    })
    assert rf.check_recipe({"rhel": {"packages": ["totally-made-up"]}},
                           fetch=fetch) is None


def test_a_spent_budget_refuses_nothing():
    fetch = repo(**{
        APPSTREAM + "repodata/repomd.xml": repomd(),
        APPSTREAM + "repodata/primary.xml.gz": primary(("bash", "x86_64")),
        BASEOS + "repodata/repomd.xml": repomd(),
        BASEOS + "repodata/primary.xml.gz": primary(("coreutils", "x86_64")),
    })
    found = rf.offers([APPSTREAM, BASEOS], ["nope"], fetch=fetch, budget=-1)

    assert not found.conclusive, "a partial read was treated as a full answer"
    assert found.unchecked, "the repositories it skipped were not recorded"


def test_a_recipe_naming_no_packages_is_not_refused():
    assert rf.check_recipe({"rhel": {"packages": []}}, fetch=repo()) is None


def test_metadata_too_large_falls_back_to_the_directory_index():
    """OL9's BaseOS primary.xml is 130 MB and its directory index is 3.7 MB.
    Downloading the larger to learn the smaller answer is not a trade worth
    making on the path of a request."""
    fetch = repo(**{
        BASEOS + "repodata/repomd.xml": repomd(size=rf.PRIMARY_SIZE_CAP + 1),
        BASEOS + "index.html":
            b'<td><a href="getPackage/sqlite-libs-3.34.1-11.el9.x86_64.rpm">x</a></td>'
            b'<td><a href="getPackage/bash-5.1.8-9.el9.src.rpm">y</a></td>',
    })
    names = rf.package_names(BASEOS, fetch=fetch)

    assert names == frozenset({"sqlite-libs"}), (
        f"the directory listing was misread: {names}")


def test_the_answer_is_cached_so_a_request_never_pays_twice():
    calls = []

    def fetch(url):
        calls.append(url)
        return {APPSTREAM + "repodata/repomd.xml": repomd(),
                APPSTREAM + "repodata/primary.xml.gz": primary(("bash", "x86_64"))}[url]

    rf.package_names(APPSTREAM, fetch=fetch)
    rf.package_names(APPSTREAM, fetch=fetch)
    assert len(calls) == 2, f"the repository was read twice: {calls}"


# --- the objection is about a RUNG, not about a request -----------------------

def test_a_repository_objection_skips_the_rung_and_does_not_end_the_ladder():
    """REQ-2026-0206, 2026-08-26, four hours after C10 shipped.

    The guess rung asks for `mongodb`; the repositories say no such package,
    and they are right. C10 refused it in seconds with no machine spent — an
    improvement on the three machines it cost the day before — and then ENDED
    THE LADDER, so the vendor-repo rung asking for `mongodb-org` from MongoDB's
    own repository was never tried. A different question, never put.

    `autobuild.py` already carried the warning, written for the `remembered`
    stage: "treating the skip as a verdict would strand vault on the package
    guess for ever." The new stage simply was not in the list.

    Asserted structurally: the loop must treat a `repository` attempt the same
    way it treats a `remembered` one.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "autobuild.py").read_text(
        encoding="utf-8")

    assert 'stage == "repository"' in src, (
        "a repository objection ends the whole ladder, so a technology is "
        "stranded on whichever rung the gate happened to refuse first")
    assert src.index('stage == "repository"') < src.index('stage == "remembered"'), (
        "the repository skip must be reachable before the remembered branch")
