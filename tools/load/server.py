"""Start the unchanged server with harness-only logging and configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path


def main():
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
    uvicorn.run("server.app:app", host=os.getenv("HOST", "0.0.0.0"),
                port=int(os.getenv("PORT", "8080")), access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
