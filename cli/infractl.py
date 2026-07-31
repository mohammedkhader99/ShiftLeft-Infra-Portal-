"""infractl — command-line client for the portal API (F-INT-09).

Drives the same API the portal uses, authenticating with an API key (F-INT-01),
so it is subject to the same RBAC — a read-only user's key can only read. The key
acts as the user who issued it.

Config (flags override env):
  --url / INFRA_API_URL   API base URL (default http://localhost:8081)
  --key / INFRA_API_KEY   API key (issue one from the portal Admin console)

Run: python -m cli.infractl <command> [options]
"""

import argparse
import os
import sys

import httpx

DEFAULT_URL = "http://localhost:8081"


def _base_url(args) -> str:
    return (args.url or os.getenv("INFRA_API_URL") or DEFAULT_URL).rstrip("/")


def _api_key(args) -> str | None:
    return args.key or os.getenv("INFRA_API_KEY")


def api_request(method: str, url: str, key: str, path: str, params=None):
    """One API call with the key header. Raises httpx.HTTPError on transport errors."""
    return httpx.request(method, f"{url}{path}", headers={"X-API-Key": key},
                         params=params, timeout=30.0)


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def _err(resp) -> str:
    try:
        body = resp.json()
        return body.get("detail") or body.get("error") or f"HTTP {resp.status_code}"
    except Exception:  # noqa: BLE001
        return f"HTTP {resp.status_code}"


def cmd_whoami(args, url, key) -> int:
    r = api_request("GET", url, key, "/api/me")
    if r.status_code != 200:
        return _fail(_err(r))
    d = r.json()
    print(f"{d['email']}   roles: {', '.join(d['roles'])}")
    return 0


def cmd_requests(args, url, key) -> int:
    params: dict = {}
    if args.status:
        params["status"] = args.status
    if args.type:
        params["request_type"] = args.type
    if args.mine:
        me = api_request("GET", url, key, "/api/me")
        if me.status_code == 200:
            params["requester"] = me.json()["email"]
    r = api_request("GET", url, key, "/api/requests", params=params)
    if r.status_code != 200:
        return _fail(_err(r))
    rows = r.json()
    if not rows:
        print("(no requests)")
        return 0
    print(f"{'REFERENCE':<18} {'STATUS':<14} {'ENVIRONMENT':<22} {'MONTHLY':>10}")
    for x in rows:
        env = (x.get("environment_name") or x.get("target_environment") or "-")[:22]
        monthly = (x.get("estimate") or {}).get("monthly")
        m = f"{monthly:,.0f}" if monthly is not None else "-"
        print(f"{x['reference']:<18} {x['status']:<14} {env:<22} {m:>10}")
    return 0


def cmd_request(args, url, key) -> int:
    r = api_request("GET", url, key, f"/api/requests/{args.reference}")
    if r.status_code == 404:
        return _fail(f"{args.reference} not found")
    if r.status_code != 200:
        return _fail(_err(r))
    x = r.json()
    print(f"Reference:   {x['reference']}")
    print(f"Status:      {x['status']}")
    print(f"Type:        {x.get('request_type') or '-'}")
    print(f"Environment: {x.get('environment_name') or x.get('target_environment') or '-'}")
    print(f"Owner:       {x.get('owner') or '-'}")
    est = x.get("estimate") or {}
    if est:
        print(f"Cost:        {est.get('monthly', 0):,.0f} {est.get('currency', 'AED')}/mo")
    ttl = x.get("ttl")
    if ttl:
        print(f"TTL:         {ttl['status']} (expires {ttl['expiry'][:10]}, {ttl['days_left']}d)")
    h = x.get("health")
    if h:
        print(f"Health:      {h['grade']} ({h['score']}/100)")
    d = x.get("drift")
    if d:
        print(f"Drift:       {'DETECTED' if d['detected'] else 'none'} (checked {d['checked_at'][:10]})")
    if x.get("approval"):
        print(f"Jira:        {x['approval']['jira_key']}")
    return 0


def cmd_showback(args, url, key) -> int:
    r = api_request("GET", url, key, "/api/showback",
                    params={"group_by": args.by, "scope": args.scope})
    if r.status_code == 403:
        return _fail("not permitted (needs an oversight role)")
    if r.status_code != 200:
        return _fail(_err(r))
    d = r.json()
    cur = d["currency"]
    print(f"Showback by {d['group_by']} ({d['scope']}):")
    for row in d["rows"]:
        print(f"  {row['key']:<24} {row['monthly']:>12,.0f} {cur}/mo  ({row['count']} req)")
    print(f"  {'TOTAL':<24} {d['total']['monthly']:>12,.0f} {cur}/mo")
    return 0


def cmd_health(args, url, key) -> int:
    r = api_request("GET", url, key, "/api/health-scores")
    if r.status_code == 403:
        return _fail("not permitted (needs an oversight role)")
    if r.status_code != 200:
        return _fail(_err(r))
    d = r.json()
    if not d["scores"]:
        print("(no provisioned environments)")
        return 0
    print(f"Estate health (average {d['average']}):")
    for s in d["scores"]:
        print(f"  {s['grade']}  {s['score']:>3}/100  {s['reference']:<18} {s.get('environment') or ''}")
    return 0


def cmd_renew(args, url, key) -> int:
    params = {"days": args.days} if args.days else None
    r = api_request("POST", url, key, f"/api/requests/{args.reference}/renew", params=params)
    if r.status_code != 200:
        return _fail(_err(r))
    d = r.json()
    print(f"renewed {d['reference']} -> new expiry {d['new_expiry'][:10]} ({d['days']} days)")
    return 0


def cmd_drift(args, url, key) -> int:
    r = api_request("POST", url, key, f"/api/requests/{args.reference}/drift-check")
    if r.status_code != 200:
        return _fail(_err(r))
    d = r.json()
    if d["drift"]:
        print(f"DRIFT: {args.reference} — {d.get('summary')}")
        for c in d.get("changes", []):
            print(f"  {c['address']}: {c['actions']}")
    else:
        print(f"no drift: {args.reference} ({d.get('summary')})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="infractl", description="Infrastructure Provisioning Portal CLI (F-INT-09).")
    p.add_argument("--url", help="API base URL (default INFRA_API_URL or http://localhost:8081)")
    p.add_argument("--key", help="API key (default INFRA_API_KEY)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="show your identity and roles").set_defaults(func=cmd_whoami)

    pr = sub.add_parser("requests", help="list requests")
    pr.add_argument("--status", help="filter by status (comma-separated)")
    pr.add_argument("--type", help="filter by request type")
    pr.add_argument("--mine", action="store_true", help="only your own requests")
    pr.set_defaults(func=cmd_requests)

    pg = sub.add_parser("request", help="show one request in detail")
    pg.add_argument("reference")
    pg.set_defaults(func=cmd_request)

    ps = sub.add_parser("showback", help="cost breakdown")
    ps.add_argument("--by", default="cost_centre",
                    choices=["cost_centre", "project", "environment", "owner"])
    ps.add_argument("--scope", default="active", choices=["active", "committed"])
    ps.set_defaults(func=cmd_showback)

    sub.add_parser("health", help="estate health scores").set_defaults(func=cmd_health)

    pn = sub.add_parser("renew", help="extend a non-prod environment's TTL")
    pn.add_argument("reference")
    pn.add_argument("--days", type=int)
    pn.set_defaults(func=cmd_renew)

    pd = sub.add_parser("drift-check", help="check an environment for drift")
    pd.add_argument("reference")
    pd.set_defaults(func=cmd_drift)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    url = _base_url(args)
    key = _api_key(args)
    if not key:
        return _fail("no API key. Set INFRA_API_KEY or pass --key "
                     "(issue one from the portal Admin console).")
    try:
        return args.func(args, url, key)
    except httpx.HTTPError as exc:
        return _fail(f"cannot reach {url}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
