"""The production target must keep idle HTTP connections longer than the ALB."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "server/app.py"
APP = ast.parse(APP_PATH.read_text(encoding="utf-8"))
CONSTANT = "HTTP_KEEP_ALIVE_TIMEOUT_S"


def production_launch():
    """Execute the real constant and Uvicorn call, without starting a server."""
    definition = next(node for node in APP.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == CONSTANT
                              for target in node.targets))
    main = next(node for node in APP.body if isinstance(node, ast.If)
                and ast.unparse(node.test) == "__name__ == '__main__'")
    calls = [node for node in ast.walk(main) if isinstance(node, ast.Expr)
             and isinstance(node.value, ast.Call)
             and ast.unparse(node.value.func) == "uvicorn.run"]
    assert len(calls) == 1
    timeout = next(keyword.value for keyword in calls[0].value.keywords
                   if keyword.arg == "timeout_keep_alive")
    assert isinstance(timeout, ast.Name) and timeout.id == CONSTANT
    captured = []
    namespace = {"uvicorn": SimpleNamespace(run=lambda *args, **kwargs: captured.append((args, kwargs))),
                 "host": "0.0.0.0", "port": 8080}
    launch = ast.Module(body=[definition, calls[0]], type_ignores=[])
    exec(compile(launch, str(APP_PATH), "exec"), namespace)
    assert len(captured) == 1
    return captured[0]


def test_production_keepalive_is_strictly_above_alb_idle_timeout():
    hcl = (ROOT / "infra/terraform/alb.tf").read_text(encoding="utf-8")
    load_balancer = re.search(r'resource\s+"aws_lb"\s+"main"\s*\{(.*?)^\}', hcl, re.M | re.S)
    assert load_balancer, "the production ALB resource is missing"
    timeout = re.search(r"(?m)^\s*idle_timeout\s*=\s*(\d+)\b", load_balancer[1])
    assert timeout, "read the ALB idle_timeout before changing the launch timeout"
    args, kwargs = production_launch()
    assert args == ("server.app:app",)
    assert kwargs["host"] == "0.0.0.0" and kwargs["port"] == 8080
    assert kwargs["timeout_keep_alive"] > int(timeout[1]), (
        "the server must keep idle connections open longer than the ALB; "
        "raise HTTP_KEEP_ALIVE_TIMEOUT_S when raising alb.tf idle_timeout"
    )


def test_container_and_ecs_use_the_configured_production_launch():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    commands = re.findall(r"(?m)^CMD\s+(\[.*\])\s*$", dockerfile)
    assert len(commands) == 1
    assert json.loads(commands[0]) == ["python", "-m", "server.app"]
    assert not re.search(r"(?m)^ENTRYPOINT\s", dockerfile)
    ecs = (ROOT / "infra/terraform/ecs.tf").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*(?:command|entryPoint)\s*=", ecs), (
        "an ECS launch override must preserve the configured HTTP keep-alive timeout"
    )
