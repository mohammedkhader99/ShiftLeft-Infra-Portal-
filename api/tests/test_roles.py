"""RBAC role resolution + permission logic (E1.1, F-IAM-01)."""

import pytest

import api.roles as roles


@pytest.fixture(autouse=True)
def _isolate_db_role_map(monkeypatch):
    """These tests exercise env/mock role resolution, so isolate them from any rows
    in the shared DB group->role map (that DB path is covered in test_role_map).
    Otherwise mappings a real deployment has stored would leak in and skew results."""
    monkeypatch.setattr(roles, "_load_db_role_map", lambda: None)


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


def test_unmapped_user_gets_full_access_in_mock(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "mock")
    monkeypatch.setenv("ROLE_MAP", "")
    assert roles.resolve_roles("anyone@example.com") == roles.ALL_ROLES


def test_mapped_user_gets_exactly_their_roles(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "mock")
    monkeypatch.setenv("ROLE_MAP", '{"a@example.com": ["requester", "auditor"]}')
    assert roles.resolve_roles("a@example.com") == {"requester", "auditor"}


def test_unknown_role_names_are_dropped(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "mock")
    monkeypatch.setenv("ROLE_MAP", '{"a@example.com": ["requester", "wizard"]}')
    assert roles.resolve_roles("a@example.com") == {"requester"}


def test_empty_email_is_read_only():
    assert roles.resolve_roles("") == {roles.READ_ONLY}


def test_permission_logic():
    assert roles.can({roles.PLATFORM_ADMIN}, "execute")
    assert not roles.can({roles.REQUESTER}, "execute")
    assert roles.can({roles.REQUESTER}, "create_request")
    assert roles.can({roles.AUDITOR}, "view_audit")
    assert not roles.can({roles.READ_ONLY}, "create_request")


def test_jira_source_maps_groups_to_roles(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "jira")
    monkeypatch.setenv("JIRA_ROLE_MAP", '{"IMD-Admins": "platform_admin"}')
    roles.clear_cache()

    def fake_get(url, params=None, **kwargs):
        if "/user/search" in url:
            return _Resp([{"key": "jdoe", "name": "jdoe"}])
        if url.endswith("/user"):
            return _Resp({"groups": {"items": [{"name": "IMD-Admins"}, {"name": "other"}]}})
        return _Resp({})

    monkeypatch.setattr(roles.httpx, "get", fake_get)
    assert roles.resolve_roles("jdoe@example.com") == {roles.PLATFORM_ADMIN}


def test_jira_source_fails_safe_to_read_only(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "jira")
    roles.clear_cache()

    def boom(*args, **kwargs):
        raise RuntimeError("jira unreachable")

    monkeypatch.setattr(roles.httpx, "get", boom)
    assert roles.resolve_roles("x@example.com") == {roles.READ_ONLY}


def test_jira_source_unmapped_groups_are_read_only(monkeypatch):
    monkeypatch.setenv("ROLE_SOURCE", "jira")
    monkeypatch.setenv("JIRA_ROLE_MAP", '{"IMD-Admins": "platform_admin"}')
    roles.clear_cache()

    def fake_get(url, params=None, **kwargs):
        if "/user/search" in url:
            return _Resp([{"key": "nobody"}])
        if url.endswith("/user"):
            return _Resp({"groups": {"items": [{"name": "some-other-group"}]}})
        return _Resp({})

    monkeypatch.setattr(roles.httpx, "get", fake_get)
    assert roles.resolve_roles("nobody@example.com") == {roles.READ_ONLY}
