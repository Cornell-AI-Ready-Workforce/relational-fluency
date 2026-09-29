"""A local server from this checkout, configured the way production is.

    python -m tools.sim.serve --port 8797 --data-dir /tmp/rf-sim --build 4798e64

tools/sim/check.py starts this itself; it is a separate entry point so it can
also be run by hand beside a manual sim.

WHY not `python -m server.app`. The check compares against a baseline recorded
on production's configuration, and a researcher's .env is not that: it may name
another REALTIME_MODEL, a different director, or an experimental knob, and
server/llm.py deliberately lets .env win over the environment. So this process
takes exactly two values from .env, the gateway key and SESSION_KEY (the /test
door and the socket need the same key the sim sends), and builds the rest of
its environment from the task definition in infra/terraform/ecs.tf, with each
`var.X` resolved from terraform.tfvars or the variable's default. What the task
definition gets from AWS (the S3 bucket, the host names) is left out: nothing
the sim does uploads, and archiving is switched off.

Safety, because this server records real sessions to disk: AWS credentials are
stripped from the environment and ARCHIVE_SESSIONS_TO_S3 is 0, so nothing it
records can reach the study bucket; sessions go to --data-dir. Access logging
is off because uvicorn's access log prints request URLs, and the /test door's
URL carries the researcher key.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "infra" / "terraform"

# From .env, and only these.
CREDENTIALS = ("ANTHROPIC_API_KEY", "SESSION_KEY")

# Task-definition entries that describe the AWS deployment rather than the
# code's behaviour, and are replaced or dropped locally.
NOT_LOCAL = {"APP_HOST", "API_HOST", "S3_BUCKET", "AWS_REGION", "DATA_DIR", "HOST", "PORT"}

_AWS_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_PROFILE", "AWS_DEFAULT_PROFILE")


def terraform_variables(tf_dir: Path = TF_DIR) -> Dict[str, str]:
    """Every string variable's value: terraform.tfvars, else its default."""
    values: Dict[str, str] = {}
    for tf in sorted(tf_dir.glob("*.tf")):
        text = tf.read_text(encoding="utf-8")
        for m in re.finditer(r'(?ms)^variable\s+"(\w+)"\s*\{(.*?)^\}', text):
            d = re.search(r'(?m)^\s*default\s*=\s*"([^"]*)"', m.group(2))
            if d:
                values[m.group(1)] = d.group(1)
    tfvars = tf_dir / "terraform.tfvars"
    if tfvars.is_file():
        for m in re.finditer(r'(?m)^\s*(\w+)\s*=\s*"([^"]*)"', tfvars.read_text(encoding="utf-8")):
            values[m.group(1)] = m.group(2)
    return values


def production_environment(tf_dir: Path = TF_DIR) -> Dict[str, str]:
    """The platform container's `environment` list from ecs.tf, resolved."""
    text = (tf_dir / "ecs.tf").read_text(encoding="utf-8")
    block = text.split("environment = [", 1)
    if len(block) != 2:
        raise RuntimeError("infra/terraform/ecs.tf has no container `environment = [` list")
    body = block[1].split("\n    ]", 1)[0]
    variables = terraform_variables(tf_dir)
    env: Dict[str, str] = {}
    for m in re.finditer(r'\{\s*name\s*=\s*"(\w+)"\s*,\s*value\s*=\s*([^}]+?)\s*\}', body):
        name, value = m.group(1), m.group(2).strip()
        if name in NOT_LOCAL:
            continue
        if value.startswith('"') and value.endswith('"'):
            env[name] = value[1:-1]
        elif value.startswith("var."):
            env[name] = variables.get(value[4:], "")
    if "REALTIME_MODEL" not in env:
        raise RuntimeError("could not read REALTIME_MODEL from infra/terraform/ecs.tf")
    return env


def configure(port: int, data_dir: Path, build: str = "") -> Dict[str, str]:
    """Set this process up as the local production-like server; return the
    environment applied (credential values excluded)."""
    from dotenv import dotenv_values

    creds = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if k in CREDENTIALS and v}
    for k in _AWS_ENV:
        os.environ.pop(k, None)
    applied = dict(production_environment())
    applied.update(ARCHIVE_SESSIONS_TO_S3="0", DATA_DIR=str(data_dir), HOST="127.0.0.1",
                   PORT=str(port), AWS_SHARED_CREDENTIALS_FILE=os.devnull,
                   AWS_CONFIG_FILE=os.devnull, AWS_EC2_METADATA_DISABLED="true")
    if build:
        applied["BUILD_SHA"] = build
    else:
        os.environ.pop("BUILD_SHA", None)
    os.environ.update(applied)
    os.environ.update(creds)
    # The two ways the server reads .env, both closed to everything but the
    # credentials: server.app calls load_dotenv() at import (it would fill any
    # unset variable from .env), so that becomes a no-op before the import;
    # server.llm parses .env into _FILE and lets it win, so that parse is
    # replaced by the credentials alone.
    import dotenv

    dotenv.load_dotenv = lambda *a, **k: False
    sys.path.insert(0, str(REPO_ROOT))
    from server import llm

    llm._FILE.clear()
    llm._FILE.update(creds)
    return applied


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--build", default="", help="BUILD_SHA to report on /health")
    args = ap.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    applied = configure(args.port, args.data_dir.resolve(), args.build)
    print("  sim server environment (credentials not shown):", flush=True)
    for k in sorted(applied):
        print(f"    {k}={applied[k]}", flush=True)
    os.chdir(REPO_ROOT)
    import uvicorn

    uvicorn.run("server.app:app", host="127.0.0.1", port=args.port,
                log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
