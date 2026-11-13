import asyncio
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess

import pytest

from tools.load import isolation, runtime
from tools.load.run import parse_args


@pytest.mark.parametrize("args", [
    ["--ramp", "2"], ["--ramp", "1,5,5"], ["--ramp", "1,0"],
    ["--repeat", "0"], ["--arrival", "stagger:-1"], ["--arrival", "stagger:nan"],
    ["--duration", "inf"], ["--scenarios", "S5A"], ["--steps", "say:a1"],
    ["--gateway", "live"], ["--gateway", "live", "--allow-live"],
    ["--isolation-only", "--backend", "local"],
])
def test_bad_arguments_fail_before_any_process_or_credential_access(args):
    with pytest.raises(SystemExit) as exc:
        parse_args(args)
    assert exc.value.code == 2


def test_defaults_and_authorized_live_cap():
    args = parse_args([])
    assert args.ramp == [1, 5, 10, 20, 30] and args.backend == "docker"
    assert args.scenarios == ["S1A", "S2A", "S3A", "S4A"] and args.arrival == 0
    assert parse_args(["--arrival", "stagger:2.5"] ).arrival == 2.5
    args = parse_args(["--gateway", "live", "--allow-live", "--ramp", "1,20"])
    assert args.max_concurrency == 20


def test_environment_overrides_production_last_and_never_uses_real_tts_or_aws():
    production = {"REALTIME_MODEL": "cascade-argon-elevenlabs", "LLM_BASE_URL": "https://production",
                  "ELEVENLABS_API_KEY": "real-tts", "STORAGE_PREFLIGHT": "1", "ARCHIVE_SESSIONS_TO_S3": "1"}
    env = runtime.environment(gateway="fake", fake_url="https://upstream:8443", data_dir="/data",
                              key="door", production=production, isolated=True)
    assert env["LLM_BASE_URL"] == "https://upstream:8443"
    assert env["ELEVENLABS_WS_URL"].startswith("wss://upstream:8443/")
    assert env["ELEVENLABS_API_KEY"] == "load-only-fake-tts-key"
    assert env["STORAGE_PREFLIGHT"] == env["ARCHIVE_SESSIONS_TO_S3"] == "0"
    assert env["LOOP_LAG_REPORT_S"] == "5" and env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["LOAD_REQUIRE_ISOLATION"] == "1"
    live = runtime.environment(gateway="live", fake_url="https://upstream:8443", data_dir="/data", key="door",
                               live_key="mock-gateway", live_url="https://gateway", production=production)
    assert live["LLM_BASE_URL"] == "https://gateway" and live["LITELLM_API_KEY"] == "mock-gateway"
    assert live["ELEVENLABS_API_KEY"] == env["ELEVENLABS_API_KEY"]


def test_egress_proof_requires_every_connect_to_fail_and_exactly_three_members():
    def blocked(*args, **kwargs):
        raise OSError("network unreachable")
    assert isolation.egress_probe(blocked, lambda *a, **kw: [], "gateway.example")["passed"]
    class Reachable:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    assert not isolation.egress_probe(lambda *a, **kw: Reachable(), lambda *a, **kw: [], "gateway.example")["passed"]
    network = {"Internal": True, "Id": "n", "Containers": {"server": {}, "fake": {}, "relay": {}}}
    server = {"NetworkSettings": {"Networks": {"n": {"NetworkID": "n"}}}}
    assert isolation.validate_topology(network, server, {"server", "fake", "relay"})
    network["Containers"]["extra"] = {}
    assert not isolation.validate_topology(network, server, {"server", "fake", "relay"})


def test_docker_plan_uses_repo_image_internal_network_caps_and_synthetic_env(monkeypatch, tmp_path):
    calls, captured_env = [], {}
    instance = runtime.Runtime(tmp_path)
    instance.bundle = tmp_path / "ca.pem"
    instance.fake_ssl = object()
    monkeypatch.setattr(runtime.shutil, "which", lambda command: "/bin/" + command)

    async def command(*args, **kwargs):
        calls.append(tuple(map(str, args)))
        if args[:2] == ("docker", "info"):
            return 0, json.dumps({"Architecture": "aarch64", "NCPU": 4}), ""
        if "tools.load.calibrate" in args:
            return 0, json.dumps({"version": "test", "frames_per_second": 100}), ""
        if "tools.load.inspect" in args:
            return 0, '{"errors": []}', ""
        if args[:2] == ("docker", "run"):
            if "--env-file" in args:
                path = Path(args[args.index("--env-file") + 1])
                captured_env.update(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())
            return 0, "server-id" if instance.server_name in args else "fake-id", ""
        if args[:2] == ("docker", "port"):
            assert args[2:] == (instance.relay_name, "8080/tcp")
            return 0, "127.0.0.1:12345", ""
        if args[:3] == ("git", "rev-parse", "--short"):
            return 0, "f1fb333", ""
        if args[:3] == ("docker", "image", "inspect"):
            return 1, "", "missing"
        return 0, "", ""

    async def ready(*args, **kwargs): return {}
    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(instance, "_wait", ready)
    monkeypatch.setattr(instance, "_spawn", ready)
    asyncio.run(instance._docker_start())
    assert any(call[:3] == ("docker", "network", "create") and "--internal" in call for call in calls)
    assert any(call[:2] == ("docker", "build") and str(runtime.ROOT / "Dockerfile") in call for call in calls)
    servers = [call for call in calls if call[:2] == ("docker", "run") and instance.server_name in call]
    assert len(servers) == 1
    server = servers[0]
    assert server[server.index("--cpus") + 1] == "0.5" and server[server.index("--memory") + 1] == "1g"
    assert server.count("--network") == 1
    assert "-p" not in server and "SYS_PTRACE" not in server and "seccomp=unconfined" not in server
    fake = next(c for c in calls if c[:2] == ("docker", "run") and instance.fake_name in c)
    assert "-p" not in fake
    relay = next(c for c in calls if c[:2] == ("docker", "run") and instance.relay_name in c)
    assert relay[relay.index("--network") + 1] == "bridge"
    assert relay[relay.index("-p") + 1] == "127.0.0.1::8080"
    assert any(c[:3] == ("docker", "network", "connect") and instance.relay_name in c for c in calls)
    assert all(c[2] == instance.relay_name for c in calls if c[:2] == ("docker", "port"))
    assert captured_env["LLM_BASE_URL"] == "https://upstream:8443"
    assert captured_env["LOAD_REQUIRE_ISOLATION"] == "1"
    assert not (tmp_path / "server.env").exists()


def test_rss_is_process_rss_not_docker_working_set_and_profile_is_sampled():
    assert runtime.rss_from_status("Name:\tpython\nVmRSS:\t1234 kB\n") == 1234 * 1024
    profile = runtime.folded_top20("main;render 10\nmain;decode 3\nmain;render 5\n")
    assert profile["samples"] == 18 and profile["top20"][0] == {"frame": "render", "self_samples": 15}


def test_fake_tls_bundle_keeps_public_roots(tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is absent")
    cert, key, bundle = asyncio.run(runtime.certificates(tmp_path))
    context = ssl.create_default_context(cafile=str(bundle))
    assert context.get_ca_certs()
    text = bundle.read_text(encoding="ascii")
    assert cert.read_text(encoding="ascii") in text and text.count("BEGIN CERTIFICATE") > 1
    assert key.stat().st_mode & 0o777 == 0o600


def test_docker_isolation_blocks_public_egress(tmp_path):
    """Real Docker test; no skipped proof is reported as a successful run."""
    if os.getenv("RF_LOAD_DOCKER_TESTS") != "1":
        pytest.skip("real Docker test requires RF_LOAD_DOCKER_TESTS=1")
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is absent; run the Track B isolation smoke command")
    info = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                          text=True, encoding="utf-8", timeout=10)
    if info.returncode:
        pytest.skip("Docker daemon unavailable: " + info.stderr.strip())
    async def run():
        instance = runtime.Runtime(tmp_path)
        try:
            await instance.start()
            assert instance.isolation["passed"]
            assert (await instance.prove_isolation())["passed"]
        finally:
            await instance.stop()
    asyncio.run(run())
