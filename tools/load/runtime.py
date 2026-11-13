"""Process/container lifecycle, TLS, resource sampling, and close completion."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import sys
import time

import httpx

from tools.load.fake_upstream import FakeConfig
from tools.load.isolation import validate_topology
from tools.sim.serve import production_environment


ROOT = Path(__file__).resolve().parents[2]


class RuntimeUnavailable(RuntimeError):
    pass


async def command(*args, timeout=120, check=True, **kwargs):
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if check and process.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace")[-2000:])
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def environment(*, gateway, fake_url, data_dir, key, live_key=None, live_url=None,
                certificate=None, isolated=False, production=None):
    """Apply harness overrides AFTER the checked-in task environment."""
    env = dict(production if production is not None else production_environment())
    if not env.get("REALTIME_MODEL", "").startswith("cascade"):
        raise ValueError("this harness requires the production cascade model")
    if gateway == "live" and not live_key:
        raise ValueError("live gateway needs an explicitly loaded gateway key")
    env.update(
        LLM_BASE_URL=live_url if gateway == "live" else fake_url,
        ELEVENLABS_WS_URL=fake_url.replace("https://", "wss://").replace("http://", "ws://")
                          + "/v1/text-to-dialogue/stream-input",
        ANTHROPIC_API_KEY=live_key if gateway == "live" else "load-only-gateway-key",
        LITELLM_API_KEY=live_key if gateway == "live" else "load-only-gateway-key",
        ELEVENLABS_API_KEY="load-only-fake-tts-key", SESSION_KEY=key,
        STORAGE_PREFLIGHT="0", ARCHIVE_SESSIONS_TO_S3="0", LOOP_LAG_REPORT_S="5",
        DATA_DIR=str(data_dir), HOST="0.0.0.0", PORT="8080",
        AWS_ACCESS_KEY_ID="load-only-synthetic-access", AWS_SECRET_ACCESS_KEY="load-only-synthetic-secret",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_CONFIG_FILE="/dev/null",
        AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-east-1", S3_BUCKET="rf-load-synthetic",
        RUN_CODE_SECRET="load-only-rating-secret", SURVEY_COMPLETION_CODE="load-only-completion-code",
        VIDEO_SALVAGE_GRACE_SECONDS="86400", LOAD_REQUIRE_ISOLATION="1" if isolated else "0",
    )
    if certificate:
        env["SSL_CERT_FILE"] = str(certificate)
    return env


def _write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


async def certificates(directory):
    directory.mkdir(parents=True, exist_ok=True)
    cert, key, bundle = (directory / name for name in ("cert.pem", "key.pem", "ca-bundle.pem"))
    if not shutil.which("openssl"):
        raise RuntimeUnavailable("openssl is required for the fake's TLS certificate")
    await command("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                  "-subj", "/CN=upstream", "-addext", "subjectAltName=DNS:upstream,DNS:localhost,IP:127.0.0.1",
                  "-keyout", key, "-out", cert)

    def trust_bundle():
        roots = "".join(ssl.DER_cert_to_PEM_cert(root) for root in
                        ssl.create_default_context().get_ca_certs(binary_form=True))
        # Keep public roots for the explicitly enabled live gateway too.
        bundle.write_text(roots + cert.read_text(encoding="ascii"), encoding="ascii")
        for path in (cert, bundle):
            path.chmod(0o644)
        key.chmod(0o600)
    await asyncio.to_thread(trust_bundle)
    return cert, key, bundle


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rss_from_status(text):
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return None


def folded_top20(text):
    counts = {}
    total = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            stack, value = line.rsplit(" ", 1)
            count = int(value)
        except ValueError:
            continue
        total += count
        frame = stack.split(";")[-1]
        counts[frame] = counts.get(frame, 0) + count
    return {"samples": total, "top20": [{"frame": frame, "self_samples": n}
                                          for frame, n in sorted(counts.items(), key=lambda x: -x[1])[:20]]}


class Runtime:
    def __init__(self, directory, *, backend="docker", gateway="fake", image=None,
                 fake_config=None, live_key=None, live_url=None):
        self.directory = Path(directory).resolve()
        self.data = self.directory / "server-data"
        self.backend, self.gateway, self.image = backend, gateway, image
        self.fake_config = fake_config or FakeConfig()
        self.live_key, self.live_url = live_key, live_url
        self.key = secrets.token_hex(20)
        self.prefix = "rf-load-" + secrets.token_hex(5)
        self.network = self.prefix + "-net"
        self.server_name, self.fake_name = self.prefix + "-server", self.prefix + "-fake"
        self.processes = []
        self.tasks = []
        self.containers = []
        self.network_created = False
        self.isolation = {"passed": False, "label": "not isolated, not CPU-capped"}
        self.health = None
        self.server_pid = None

    def _prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(exist_ok=True)
        self.data.chmod(0o777)  # production image runs as uid 1000
        _write_json(self.directory / "fake-config.json", asdict(self.fake_config))

    async def _log(self, stream, source):
        path = self.directory / (source + "-log.jsonl")
        while line := await stream.readline():
            row = {"t": time.time(), "line": line.decode("utf-8", "replace").rstrip()}
            def append():
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            await asyncio.to_thread(append)

    async def _spawn(self, args, source, env=None):
        process = await asyncio.create_subprocess_exec(*map(str, args), cwd=ROOT, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.processes.append(process)
        self.tasks.append(asyncio.create_task(self._log(process.stdout, source)))
        return process

    async def _wait(self, url, *, verify=True, deadline_s=60):
        deadline = time.monotonic() + deadline_s
        async with httpx.AsyncClient(verify=verify, timeout=2, trust_env=False) as http:
            while time.monotonic() < deadline:
                try:
                    response = await http.get(url)
                    if response.status_code == 200:
                        return response.json()
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        raise RuntimeError("load runtime did not become ready; inspect its archived logs")

    async def start(self):
        await asyncio.to_thread(self._prepare)
        self.cert, self.tls_key, self.bundle = await certificates(self.directory)
        self.fake_ssl = await asyncio.to_thread(ssl.create_default_context, cafile=str(self.cert))
        try:
            if self.backend == "docker":
                await self._docker_start()
            else:
                await self._local_start()
            self.health = await self._wait(self.base + "/health")
            if not self.health.get("gateway", {}).get("ok"):
                raise RuntimeError("gateway preflight failed; see upstream-attempts.jsonl and server log")
            if self.health.get("ready") is False:
                raise RuntimeError("server required configuration is not ready")
            def telemetry_exists():
                path = self.data / "upstream-attempts.jsonl"
                return path.exists() and path.stat().st_size > 0
            if not await asyncio.to_thread(telemetry_exists):
                raise RuntimeError("required startup upstream telemetry is missing")
            if self.backend == "docker" and self.gateway == "fake":
                self.isolation = await self.prove_isolation()
                if not self.isolation["passed"]:
                    raise RuntimeError("fake-mode isolation proof failed")
            return self
        except BaseException:
            await self.stop()
            raise

    async def _docker_start(self):
        if not shutil.which("docker"):
            raise RuntimeUnavailable("Docker CLI is absent")
        code, _, error = await command("docker", "info", "--format", "{{.ServerVersion}}", check=False, timeout=10)
        if code:
            raise RuntimeUnavailable("Docker daemon unavailable: " + error.strip())
        if not self.image:
            _, sha, _ = await command("git", "rev-parse", "--short", "HEAD", cwd=ROOT)
            self.image = "rf-load:" + sha.strip()
        code, _, _ = await command("docker", "image", "inspect", self.image, check=False)
        if code:
            await command("docker", "build", "--build-arg", "BUILD_SHA=" + self.image.rsplit(":", 1)[-1],
                          "-f", ROOT / "Dockerfile", "-t", self.image, ROOT, timeout=600)
        network_args = ["docker", "network", "create"]
        if self.gateway == "fake":
            network_args.append("--internal")
        await command(*network_args, self.network)
        self.network_created = True
        _, container, _ = await command(
            "docker", "run", "-d", "--name", self.fake_name, "--network", self.network,
            "--network-alias", "upstream", "--user", f"{os.getuid()}:{os.getgid()}",
            "-p", "127.0.0.1::8443", "-v", f"{ROOT / 'tools'}:/app/tools:ro",
            "-v", f"{self.directory}:/load:ro", self.image,
            "python", "-m", "tools.load.fake_upstream", "--port", "8443",
            "--config", "/load/fake-config.json", "--ssl-cert", "/load/cert.pem", "--ssl-key", "/load/key.pem")
        self.containers.append(self.fake_name)
        self.fake_id = container.strip()
        _, port, _ = await command("docker", "port", self.fake_name, "8443/tcp")
        self.fake_base = "https://127.0.0.1:" + port.strip().rsplit(":", 1)[-1]
        await self._wait(self.fake_base + "/__load/stats", verify=self.fake_ssl)
        env = environment(gateway=self.gateway, fake_url="https://upstream:8443", data_dir="/data",
                          key=self.key, live_key=self.live_key, live_url=self.live_url,
                          certificate="/load/ca-bundle.pem", isolated=self.gateway == "fake")
        env_path = self.directory / "server.env"
        def write_env():
            if any("\n" in str(value) or "\r" in str(value) for value in env.values()):
                raise ValueError("an environment value contains a newline")
            env_path.write_text("".join(f"{name}={value}\n" for name, value in env.items()), encoding="utf-8")
            env_path.chmod(0o600)
        await asyncio.to_thread(write_env)
        try:
            _, container, _ = await command(
                "docker", "run", "-d", "--name", self.server_name, "--network", self.network,
                "--cpus", "0.5", "--memory", "1g", "--cap-add", "SYS_PTRACE",
                "--security-opt", "seccomp=unconfined", "--env-file", env_path,
                "-p", "127.0.0.1::8080", "-v", f"{ROOT / 'tools'}:/app/tools:ro",
                "-v", f"{self.data}:/data", "-v", f"{self.bundle}:/load/ca-bundle.pem:ro",
                self.image, "python", "-m", "tools.load.server")
        finally:
            await asyncio.to_thread(env_path.unlink, missing_ok=True)
        self.containers.append(self.server_name)
        self.server_id = container.strip()
        _, port, _ = await command("docker", "port", self.server_name, "8080/tcp")
        self.base = "http://127.0.0.1:" + port.strip().rsplit(":", 1)[-1]
        await self._spawn(["docker", "logs", "--follow", self.server_name], "server")
        await self._spawn(["docker", "logs", "--follow", self.fake_name], "fake")
        self.isolation = {"passed": False, "label": "live gateway egress enabled, CPU-capped"}

    async def _local_start(self):
        fake_port, server_port = await asyncio.to_thread(lambda: (free_port(), free_port()))
        self.fake_base, self.base = f"https://127.0.0.1:{fake_port}", f"http://127.0.0.1:{server_port}"
        inherited = {k: v for k, v in os.environ.items()
                     if not k.startswith(("AWS_", "ANTHROPIC_", "LITELLM_", "ELEVENLABS_"))
                     and k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
        await self._spawn([sys.executable, "-m", "tools.load.fake_upstream", "--port", fake_port,
                           "--config", self.directory / "fake-config.json", "--ssl-cert", self.cert,
                           "--ssl-key", self.tls_key], "fake", env=inherited)
        await self._wait(self.fake_base + "/__load/stats", verify=self.fake_ssl)
        env = environment(gateway=self.gateway, fake_url=self.fake_base, data_dir=self.data,
                          key=self.key, live_key=self.live_key, live_url=self.live_url, certificate=self.bundle)
        env.update(PORT=str(server_port), HOST="127.0.0.1")
        process = await self._spawn([sys.executable, "-m", "tools.load.server"], "server", env={**inherited, **env})
        self.server_pid = process.pid

    async def fake_stats(self):
        async with httpx.AsyncClient(verify=self.fake_ssl, timeout=5, trust_env=False) as http:
            response = await http.get(self.fake_base + "/__load/stats")
            response.raise_for_status()
            return response.json()

    async def prove_isolation(self):
        _, net, _ = await command("docker", "network", "inspect", self.network)
        _, server, _ = await command("docker", "inspect", self.server_name)
        topology = validate_topology(json.loads(net)[0], json.loads(server)[0], {self.fake_id, self.server_id})
        code, result, _ = await command("docker", "exec", self.server_name, "python", "-m", "tools.load.isolation", check=False)
        proof = json.loads(result)
        return {"passed": topology and code == 0 and proof["passed"], "topology_passed": topology,
                "egress": proof, "label": "internal Docker network, 0.5 CPU, 1 GiB; only server and fake upstream"}

    async def resources(self):
        if self.backend == "docker":
            results = await asyncio.gather(
                command("docker", "stats", "--no-stream", "--format", "{{json .}}", self.server_name, timeout=10),
                command("docker", "exec", self.server_name, "cat", "/proc/1/status", timeout=10))
            row = json.loads(results[0][1])
            return {"t": time.time(), "cpu_percent": float(row["CPUPerc"].rstrip("%")),
                    "rss_bytes": rss_from_status(results[1][1]), "docker_memory_usage": row.get("MemUsage")}
        _, output, _ = await command("ps", "-o", "%cpu=,rss=", "-p", self.server_pid)
        cpu, rss = output.split()
        return {"t": time.time(), "cpu_percent": float(cpu), "rss_bytes": int(rss) * 1024}

    async def profile(self, directory, *, delay=30, seconds=10):
        await asyncio.sleep(delay)
        output = Path(directory) / "py-spy.raw"
        try:
            if self.backend == "docker":
                code, _, _ = await command("docker", "exec", self.server_name, "sh", "-c", "command -v py-spy", check=False)
                if code:
                    return {"status": "unavailable", "reason": "py-spy is absent in the target container"}
                remote = "/data/load-py-spy.raw"
                await command("docker", "exec", "--user", "0", self.server_name, "py-spy", "record", "--pid", "1",
                              "--duration", seconds, "--format", "raw", "--output", remote, timeout=seconds + 15)
                await command("docker", "cp", f"{self.server_name}:{remote}", output)
            elif shutil.which("py-spy"):
                await command("py-spy", "record", "--pid", self.server_pid, "--duration", seconds,
                              "--format", "raw", "--output", output, timeout=seconds + 15)
            else:
                return {"status": "unavailable", "reason": "py-spy is not installed"}
            text = await asyncio.to_thread(output.read_text, encoding="utf-8")
            return {"status": "sampled", "native": False, "duration_s": seconds, **folded_top20(text)}
        except (RuntimeError, OSError) as exc:
            return {"status": "unavailable", "reason": type(exc).__name__ + ": " + str(exc)[-300:]}

    async def stop(self):
        for name in reversed(self.containers):
            await command("docker", "rm", "-f", name, check=False)
        self.containers.clear()
        if self.network_created:
            await command("docker", "network", "rm", self.network, check=False)
            self.network_created = False
        for process in reversed(self.processes):
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def read_encounters(data, timelines):
    events, sizes, settled = {}, {}, True
    for timeline in timelines:
        sid = timeline.get("session_id")
        if not sid:
            settled = False
            continue
        directory = Path(data) / "sessions" / sid
        path = directory / "events.jsonl"
        events[sid] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        manifest = directory / "manifest.json"
        closed = manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")).get("status") == "closed"
        settled = settled and closed and (directory / "conversation_audio.wav").exists()
        sizes[sid] = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
    return events, sizes, settled
