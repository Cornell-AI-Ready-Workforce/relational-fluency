"""Runtime egress and Docker-topology proof for the fake-only mode."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket


def egress_probe(connect=socket.create_connection):
    checks = []
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with connect((host, 443), timeout=2):
                reachable = True
        except OSError as exc:
            reachable = False
            error = type(exc).__name__
        checks.append({"host": host, "port": 443, "reachable": reachable,
                       "error": None if reachable else error})
    return {"passed": all(not check["reachable"] for check in checks), "checks": checks}


def validate_topology(network, server, expected_members):
    actual = set((network.get("Containers") or {}).keys())
    networks = (server.get("NetworkSettings") or {}).get("Networks") or {}
    return bool(network.get("Internal") and actual == set(expected_members) and len(networks) == 1
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
