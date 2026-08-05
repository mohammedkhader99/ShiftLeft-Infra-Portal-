"""Live OCI cloud-state adapter checks (describe + actuate).

The `oci` SDK client builders (`_object_storage_client` / `_compute_client`) and
the one write seam (`_instance_action`) are monkeypatched, so these run fully
offline without the SDK installed. They exercise the real mapping, gating, and
routing logic — not the SDK's own HTTP calls.
"""

import pytest

from orchestrator import cloud_state


# --- Fakes -------------------------------------------------------------------

class FakeServiceError(Exception):
    """Mimics oci.exceptions.ServiceError (carries a .status)."""
    def __init__(self, status):
        super().__init__(f"service error {status}")
        self.status = status


class _Data:
    def __init__(self, data):
        self.data = data


class FakeObjectStorage:
    def __init__(self, exists=True, error_status=None):
        self._exists = exists
        self._error_status = error_status

    def get_namespace(self):
        return _Data("ns1")

    def get_bucket(self, namespace, name):
        if self._error_status is not None:
            raise FakeServiceError(self._error_status)
        return _Data({"name": name})


class FakeInstance:
    def __init__(self, id="ocid1.instance.oc1..abc", lifecycle_state="RUNNING",
                 display_name="web-1"):
        self.id = id
        self.lifecycle_state = lifecycle_state
        self.display_name = display_name


class FakeCompute:
    def __init__(self, instances=None):
        self._instances = instances if instances is not None else [FakeInstance()]
        self.actions = []

    def get_instance(self, ocid):
        return _Data(FakeInstance(id=ocid))

    def list_instances(self, compartment_id, display_name=None):
        # display_name is optional in the real SDK: omitting it lists the
        # compartment, which is how an indexed name (<name>-01) is found.
        if display_name is None:
            return _Data(list(self._instances))
        return _Data([i for i in self._instances if i.display_name == display_name])

    def instance_action(self, instance_id, action):
        self.actions.append((instance_id, action))


@pytest.fixture()
def live_env(monkeypatch, tmp_path):
    """CLOUD_STATE_MODE=live with valid-looking creds + an existing key file."""
    key = tmp_path / "oci_api_key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..t")
    monkeypatch.setenv("OCI_USER_OCID", "ocid1.user.oc1..u")
    monkeypatch.setenv("OCI_FINGERPRINT", "aa:bb:cc")
    monkeypatch.setenv("OCI_REGION", "me-dubai-1")
    monkeypatch.setenv("OCI_COMPARTMENT_OCID", "ocid1.compartment.oc1..c")
    monkeypatch.setenv("OCI_PRIVATE_KEY_PATH", str(key))


# --- Credential gate ---------------------------------------------------------

def test_describe_requires_creds(monkeypatch):
    monkeypatch.setenv("CLOUD_STATE_MODE", "live")
    for k in cloud_state._REQUIRED_OCI:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(cloud_state.CloudStateUnavailable):
        cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])


# --- describe: object storage ------------------------------------------------

def test_describe_live_bucket_exists(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_object_storage_client", lambda: FakeObjectStorage(exists=True))
    out = cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])
    assert out[0]["status"] == "active" and out[0]["exists"] is True and out[0]["source"] == "oci"


def test_describe_live_bucket_missing(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_object_storage_client",
                        lambda: FakeObjectStorage(error_status=404))
    out = cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])
    assert out[0]["status"] == "missing" and out[0]["exists"] is False


def test_describe_live_bucket_reraises_non_404(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_object_storage_client",
                        lambda: FakeObjectStorage(error_status=500))
    with pytest.raises(FakeServiceError):
        cloud_state.describe("REQ-1", [{"kind": "oci-bucket", "name": "b1"}])


# --- describe: compute -------------------------------------------------------

def test_describe_live_compute_running(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_compute_client",
                        lambda: FakeCompute([FakeInstance(lifecycle_state="RUNNING", display_name="web-1")]))
    out = cloud_state.describe("REQ-1", [{"kind": "oci-instance", "name": "web-1"}])
    assert out[0]["status"] == "active" and out[0]["exists"] is True and out[0]["power_state"] == "running"


def test_describe_live_compute_stopped(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_compute_client",
                        lambda: FakeCompute([FakeInstance(lifecycle_state="STOPPED", display_name="web-1")]))
    out = cloud_state.describe("REQ-1", [{"kind": "oci-instance", "name": "web-1"}])
    assert out[0]["power_state"] == "stopped" and out[0]["exists"] is True


def test_describe_live_compute_missing(live_env, monkeypatch):
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: FakeCompute([]))
    out = cloud_state.describe("REQ-1", [{"kind": "oci-instance", "name": "web-1"}])
    assert out[0]["status"] == "missing" and out[0]["exists"] is False


# --- actuate: gating ---------------------------------------------------------

def test_actuate_requires_explicit_enable(live_env, monkeypatch):
    monkeypatch.delenv("OCI_ACTUATE_ENABLED", raising=False)  # live read on, write off
    with pytest.raises(cloud_state.CloudStateUnavailable, match="not enabled"):
        cloud_state.actuate("REQ-1", [{"kind": "oci-instance", "name": "web-1"}], "stop")


def test_actuate_rejects_non_compute(live_env, monkeypatch):
    monkeypatch.setenv("OCI_ACTUATE_ENABLED", "true")
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: FakeCompute())
    with pytest.raises(cloud_state.CloudStateUnavailable, match="only compute"):
        cloud_state.actuate("REQ-1", [{"kind": "oci-bucket", "name": "b1"}], "stop")


def test_actuate_instance_not_found(live_env, monkeypatch):
    monkeypatch.setenv("OCI_ACTUATE_ENABLED", "true")
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: FakeCompute([]))
    with pytest.raises(cloud_state.CloudStateUnavailable, match="no matching OCI instance"):
        cloud_state.actuate("REQ-1", [{"kind": "oci-instance", "name": "web-1"}], "stop")


# --- actuate: the real stop/start --------------------------------------------

def test_actuate_stops_compute_with_softstop(live_env, monkeypatch):
    fake = FakeCompute([FakeInstance(id="ocid1.instance.oc1..w1", display_name="web-1")])
    monkeypatch.setenv("OCI_ACTUATE_ENABLED", "true")
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: fake)
    out = cloud_state.actuate("REQ-1", [{"kind": "oci-instance", "name": "web-1"}], "stop")
    assert out[0]["power_state"] == "stopped" and out[0]["source"] == "oci"
    assert fake.actions == [("ocid1.instance.oc1..w1", "SOFTSTOP")]


def test_actuate_starts_compute(live_env, monkeypatch):
    fake = FakeCompute([FakeInstance(id="ocid1.instance.oc1..w1",
                                     lifecycle_state="STOPPED", display_name="web-1")])
    monkeypatch.setenv("OCI_ACTUATE_ENABLED", "true")
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: fake)
    out = cloud_state.actuate("REQ-1", [{"kind": "oci-instance", "name": "web-1"}], "start")
    assert out[0]["power_state"] == "running"
    assert fake.actions == [("ocid1.instance.oc1..w1", "START")]


def test_find_instance_by_ocid(live_env, monkeypatch):
    fake = FakeCompute()
    monkeypatch.setenv("OCI_ACTUATE_ENABLED", "true")
    monkeypatch.setattr(cloud_state, "_compute_client", lambda: fake)
    ocid = "ocid1.instance.oc1..byid"
    out = cloud_state.actuate("REQ-1", [{"kind": "oci-instance", "name": ocid}], "stop")
    assert out[0]["power_state"] == "stopped"
    assert fake.actions == [(ocid, "SOFTSTOP")]
