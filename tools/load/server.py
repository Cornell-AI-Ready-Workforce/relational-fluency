"""Start the unchanged server with harness-only logging and configuration."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sys


class RedactKey(logging.Filter):
    def filter(self, record):
        def redact(value):
            return re.sub(r"([?&]key=)[^&\s\"]*", r"\1[REDACTED]", value) if isinstance(value, str) else value
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        return True


def main():
    checkout = os.environ.get("RF_LOAD_SERVER_CHECKOUT")
    if checkout:
        # Select server code before importing any server module. The harness
        # tools package remains the already loaded package from this checkout.
        root = Path(checkout).resolve()
        if not (root / "server/app.py").is_file():
            raise ValueError("selected server checkout has no server/app.py")
        sys.path.insert(0, str(root))
        os.chdir(root)
    import dotenv
    # Production env was resolved first by the parent. Neither .env reader may
    # undo the fake endpoint overrides or import the operator's credentials.
    dotenv.load_dotenv = lambda *args, **kwargs: False
    from server import llm, upstream_telemetry
    llm._FILE.clear()
    data = Path(os.environ["DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    if os.environ.get("LOAD_REQUIRE_ISOLATION") == "1":
        from tools.load.isolation import egress_probe
        proof = egress_probe()
        (data / "isolation.json").write_text(json.dumps(proof), encoding="utf-8")
        if not proof["passed"]:
            raise RuntimeError("fake-only server can reach the public network")
    attempts = (data / "upstream-attempts.jsonl").open("a", encoding="utf-8", buffering=1)
    upstream_telemetry.add_listener(lambda event: attempts.write(json.dumps(event) + "\n"))
    import uvicorn
    from copy import deepcopy
    from uvicorn.config import LOGGING_CONFIG
    config = deepcopy(LOGGING_CONFIG)
    config["filters"] = {"redact_key": {"()": RedactKey}}
    for handler in config["handlers"].values():
        handler["filters"] = ["redact_key"]
    uvicorn.run("server.app:app", host=os.getenv("HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8080")), access_log=True, log_level="info", log_config=config)


if __name__ == "__main__":
    main()
