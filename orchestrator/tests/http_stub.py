"""A stand-in for the orchestrator's shared HTTP client.

The orchestrator trusts nothing it is handed: it asks the API whether the
approval is real, asks OPA whether policy still allows it, and re-prices the
request itself. Any test that exercises something downstream of those checks
has to answer them, and answers by URL — `/api/cost` gets a price, anything
else gets a policy verdict.

While each call built its own client, tests stubbed `httpx.get` and
`httpx.post`. Those are two separate attributes, so a shared helper could set
one and an individual test could set the other on top of it. This keeps that
property: one stub per test, installed once, with `get` and `post` settable
independently.
"""


class HttpStub:
    """Answers nothing until a test says what the answer is.

    Unstubbed verbs raise rather than return an empty result: a test that
    reaches the network without meaning to should say so loudly, not quietly
    behave as though the approval check returned nothing.
    """

    def get(self, *a, **k):
        raise AssertionError("this test made an outbound GET it did not stub")

    def post(self, *a, **k):
        raise AssertionError("this test made an outbound POST it did not stub")


class _Getter:
    """Stands in for `http_client`, which the code under test calls to get the
    process-wide client. Recognisable by type, so `stub_http` can tell an
    already-installed stub from the real thing without calling it."""

    def __init__(self, stub: HttpStub):
        self.stub = stub

    def __call__(self) -> HttpStub:
        return self.stub


def stub_http(monkeypatch, module) -> HttpStub:
    """Install a stub over `module.http_client`, or return the one already there.

    Idempotent on purpose, so a helper function and the test that calls it share
    one stub and can each set a different verb on it. monkeypatch removes it at
    the end of the test, so nothing leaks into the next one.
    """
    getter = getattr(module, "http_client", None)
    if not isinstance(getter, _Getter):
        getter = _Getter(HttpStub())
        monkeypatch.setattr(module, "http_client", getter)
    return getter.stub
