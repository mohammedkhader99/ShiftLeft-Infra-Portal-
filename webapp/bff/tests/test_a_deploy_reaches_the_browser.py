"""A front end nobody can see is a front end nobody deployed.

On 2026-09-05 two changes to the request form were built, deployed, and
verified present in the container's bundle -- and the reviewer's browser kept
showing the old page. Twice. The second time the screenshot came back
pixel-identical, down to the same truncated OS image name.

WHY. `index.html` was served by `FileResponse` with no `Cache-Control` header
at all -- only `etag` and `last-modified`. With no explicit directive a browser
applies HEURISTIC caching (commonly a tenth of the age since last-modified) and
reuses the file without asking. And index.html is the one file that must never
be reused: Vite content-hashes the bundle, so the fresh JS ships under a NEW
name -- `index-FEl78cUl.js` -- and only index.html knows that name. A stale
index.html therefore pins the browser to a bundle that no longer exists in the
deployment, and every future front-end change is invisible until somebody
happens to press Ctrl+F5.

The hashing that makes the assets safe to cache forever is exactly what makes
the pointer to them unsafe to cache at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import webapp.bff.main as bff

client = TestClient(bff.app)


def _index(tmp_path, monkeypatch):
    """Serve a stand-in build, since the real one exists only in the image."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><script src="/assets/index-ABC123.js"></script>',
        encoding="utf-8")
    (dist / "assets" / "index-ABC123.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(bff, "SPA_DIST", dist)
    return dist


def test_index_html_is_never_cached(tmp_path, monkeypatch):
    """THE DEFECT. Without this the browser keeps a deployed-over page for as
    long as its own heuristic decides, and every deploy needs a hard refresh."""
    _index(tmp_path, monkeypatch)

    response = client.get("/some/client/route")

    directive = response.headers.get("cache-control", "")
    assert directive, "index.html is served with no Cache-Control at all"
    assert "no-cache" in directive or "no-store" in directive, directive


def test_the_page_still_carries_a_validator_so_revalidation_is_cheap(
        tmp_path, monkeypatch):
    """`no-cache` means "ask before reusing", not "never cache". With an etag
    the answer is a 304 and a few bytes, so the fix costs nothing per load."""
    _index(tmp_path, monkeypatch)

    response = client.get("/")

    assert response.headers.get("etag") or response.headers.get("last-modified")


def test_a_client_route_still_serves_the_app(tmp_path, monkeypatch):
    """The reason this route exists at all: client-side routing means every
    unknown path is the app, not a 404."""
    _index(tmp_path, monkeypatch)

    response = client.get("/requests/new")

    assert response.status_code == 200
    assert "index-ABC123.js" in response.text


def test_a_missing_build_still_says_so(tmp_path, monkeypatch):
    """Unchanged, and worth holding: during unit tests there is no build, and a
    404 that explains itself beats an empty 200."""
    monkeypatch.setattr(bff, "SPA_DIST", tmp_path / "nothing-here")

    response = client.get("/")

    assert response.status_code == 404
    assert "SPA build not found" in response.text
