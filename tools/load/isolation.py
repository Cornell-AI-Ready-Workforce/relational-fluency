"""Runtime egress and Docker-topology proof for the fake-only mode."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket


def egress_probe(connect=socket.create_connection, resolve=socket.getaddrinfo, gateway_host=None):
    checks = []
    dns = []
    gateway_host = gateway_host or os.getenv("LOAD_PRODUCTION_GATEWAY_HOST", "")
    hosts = list(dict.fromkeys(["api.elevenlabs.io", gateway_host, "s3.amazonaws.com"]))
    targets = [(host, host) for host in ("1.1.1.1", "8.8.8.8")]
    for host in filter(None, hosts):
        try:
            addresses = list(dict.fromkeys(row[4][0] for row in resolve(host, 443, type=socket.SOCK_STREAM)))
            dns.append({"host": host, "resolved": True, "addresses": addresses})
            targets.extend((host, address) for address in addresses[:2])
        except OSError as exc:
            dns.append({"host": host, "resolved": False, "error": type(exc).__name__})
    for host, address in targets:
        try:
            with connect((address, 443), timeout=1):
                reachable = True
        except OSError as exc:
            reachable = False
            error = type(exc).__name__
        checks.append({"host": host, "address": address, "port": 443, "reachable": reachable,
                       "error": None if reachable else error})
    return {"passed": bool(gateway_host) and all(not check["reachable"] for check in checks),
            "checks": checks, "dns": dns, "gateway_host": gateway_host,
            "dns_policy": "DNS may resolve; all resolved TCP destinations must be blocked"}


def validate_topology(network, server, expected_members):
    actual = set((network.get("Containers") or {}).keys())
    networks = (server.get("NetworkSettings") or {}).get("Networks") or {}
    return bool(len(expected_members) == 3 and network.get("Internal") and actual == set(expected_members) and len(networks) == 1
                and next(iter(networks.values())).get("NetworkID") == network["Id"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = egress_probe()
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
