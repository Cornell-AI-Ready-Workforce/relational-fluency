"""Process/container lifecycle, TLS, resource sampling, and close completion."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import os
import platform
from urllib.parse import urlparse
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import sys
import time

import httpx

from tools.load.fake_upstream import FakeConfig
from tools.load.checkout import validate_checkout
from tools.load.calibrate import calibrate, compare
from tools.load.inspect import settled as encounters_settled
from tools.load.isolation import validate_topology
from tools.load.measure import JsonlTail
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
                certificate=None, isolated=False, production=None, encounter_seconds=None):
    """Apply harness overrides AFTER the checked-in task environment."""
    env = dict(production if production is not None else production_environment())
    if not env.get("REALTIME_MODEL", "").startswith("cascade"):
        raise ValueError("this harness requires the production cascade model")
    if gateway == "live" and not live_key:
        raise ValueError("live gateway needs an explicitly loaded gateway key")
    gateway_host = urlparse(live_url or env.get("LLM_BASE_URL", "")).hostname or ""
    env.update(
        LOAD_PRODUCTION_GATEWAY_HOST=gateway_host,
        LLM_BASE_URL=live_url if gateway == "live" else fake_url,
        ELEVENLABS_WS_URL=fake_url.replace("https://", "wss://").replace("http://", "ws://")
                          + "/v1/text-to-dialogue/stream-input",
        ANTHROPIC_API_KEY=live_key if gateway == "live" else "load-only-gateway-key",
        LITELLM_API_KEY=live_key if gateway == "live" else "load-only-gateway-key",
        ELEVENLABS_API_KEY="load-only-fake-tts-key", SESSION_KEY=key,
        STORAGE_PREFLIGHT="0", ARCHIVE_SESSIONS_TO_S3="0", LOOP_LAG_REPORT_S="5",
        RF_LOAD_FAKE_TTS_ONLY="1",
        DATA_DIR=str(data_dir), HOST="0.0.0.0", PORT="8080",
        AWS_ACCESS_KEY_ID="load-only-synthetic-access", AWS_SECRET_ACCESS_KEY="load-only-synthetic-secret",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_CONFIG_FILE="/dev/null",
        AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-east-1", S3_BUCKET="rf-load-synthetic",
        RUN_CODE_SECRET="load-only-rating-secret", SURVEY_COMPLETION_CODE="load-only-completion-code",
        VIDEO_SALVAGE_GRACE_SECONDS="86400", LOAD_REQUIRE_ISOLATION="1" if isolated else "0",
    )
    if certificate:
        env["SSL_CERT_FILE"] = str(certificate)
    if encounter_seconds is not None:
        env.update(ENCOUNTER_MIN_SECONDS="0", ENCOUNTER_WRAP_SECONDS=str(encounter_seconds * 0.8),
                   ENCOUNTER_MAX_SECONDS=str(encounter_seconds))
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
                 fake_config=None, live_key=None, live_url=None, cpu_scale=1.0, fargate_reference=None, profile_image=None,
                 encounter_seconds=None, server_checkout=None, server_environment=None):
        self.directory = Path(directory).resolve()
        self.data = self.directory / "server-data"
        self.backend, self.gateway, self.image = backend, gateway, image
        self.fake_config = fake_config or FakeConfig()
        self.server_checkout = Path(server_checkout).resolve() if server_checkout else ROOT
        self.server_environment = dict(server_environment or {})
        if server_checkout and (backend != "local" or not (self.server_checkout / "server/app.py").is_file()):
            raise ValueError("server checkout requires the local backend and server/app.py")
        if server_checkout:
            validate_checkout(self.server_checkout)
        self.live_key, self.live_url = live_key, live_url
        self.encounter_seconds = encounter_seconds
        self.cpu_scale, self.fargate_reference, self.profile_image = cpu_scale, fargate_reference, profile_image
        self.metadata = {"storage_mount_type": "host directory" if backend == "local" else "Docker named volume",
                         "capacity_scope": "this host; no Fargate reference", "cpu_scale": cpu_scale}
        self.resource_rows, self.observer_errors = [], []
        self.observer_log_task = None
        self.observer_last_output = None
        self.observer_malformed_lines = 0
        self._event_tails = {}
        self.volume_created = False
        self.key = secrets.token_hex(20)
        self.prefix = "rf-load-" + secrets.token_hex(5)
        self.network = self.prefix + "-net"
        self.volume = self.prefix + "-data"
        self.relay_name, self.observer_name = self.prefix + "-relay", self.prefix + "-observer"
        self.server_name, self.fake_name = self.prefix + "-server", self.prefix + "-fake"
        self.processes = []
        self.tasks = []
        self.containers = []
        self.network_created = False
        self.isolation = {"passed": False, "label": "not isolated, not CPU-capped"}
        self.health = None
        self.server_pid = None

    def production(self):
        values = (production_environment() if self.server_checkout == ROOT else
                  production_environment(self.server_checkout / "infra/terraform"))
        return {**values, **self.server_environment}

    def _prepare(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(exist_ok=True)
        self.data.chmod(0o777)  # production image runs as uid 1000
        self.fake_config.model_ids = sorted({value for key, value in self.production().items() if "MODEL" in key})
        _write_json(self.directory / "fake-config.json", asdict(self.fake_config))
        # Reports default to tools/load/reports. Mounting the checkout's entire
        # tools directory would also expose server.env and exported recordings.
        code = self.directory / "code"
        for package in ("load", "sim"):
            target = code / package
            target.mkdir(parents=True, exist_ok=True)
            files = list((ROOT / "tools" / "load").glob("*.py")) if package == "load" else [
                ROOT / "tools" / "sim" / name for name in ("stim.py", "analyze.py")]
            for source in files:
                shutil.copyfile(source, target / source.name)
        shutil.copytree(ROOT / "tools" / "sim" / "stim", code / "sim" / "stim", dirs_exist_ok=True)

    async def _log(self, stream, source):
        path = self.directory / (source + "-log.jsonl")
        while line := await stream.readline():
            row = {"t": time.time(), "line": line.decode("utf-8", "replace").rstrip()}
            if source == "observer":
                self.observer_last_output = time.monotonic()
                try:
                    event = json.loads(row["line"])
                    kind = event["kind"]
                    field = {"resource": "sample", "error": "detail", "attempt": "event", "encounter": "event"}[kind]
                    value = event[field]
                    if not isinstance(value, str if kind == "error" else dict):
                        raise ValueError("invalid observer payload")
                except (ValueError, KeyError, TypeError):
                    self.observer_malformed_lines += 1
                    self.observer_errors.append("malformed observer line")
                else:
                    if kind == "resource":
                        self.resource_rows.append(value)
                    elif kind == "error":
                        self.observer_errors.append(value)
                    elif kind == "encounter":
                        def write_event():
                            with (self.data / "encounter-events.jsonl").open("a", encoding="utf-8") as fh:
                                fh.write(json.dumps({**value, "session_id": event["session_id"]}) + "\n")
                        await asyncio.to_thread(write_event)
                    else:
                        def write_attempt():
                            with (self.data / "upstream-attempts.jsonl").open("a", encoding="utf-8") as fh:
                                fh.write(json.dumps(value) + "\n")
                        await asyncio.to_thread(write_attempt)
            def append():
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            await asyncio.to_thread(append)

    async def _spawn(self, args, source, env=None):
        process = await asyncio.create_subprocess_exec(*map(str, args), cwd=ROOT, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self.processes.append(process)
        task = asyncio.create_task(self._log(process.stdout, source))
        self.tasks.append(task)
        if source == "observer":
            self.observer_log_task = task
            self.observer_last_output = time.monotonic()
        return process

    def observer_telemetry(self):
        """Health of the stdout-to-attempt-file path, independent of request traffic."""
        monitored = self.backend == "docker"
        age = time.monotonic() - self.observer_last_output if self.observer_last_output is not None else None
        ended = self.observer_log_task is None or self.observer_log_task.done()
        failure = None
        if monitored:
            if self.observer_errors:
                failure = "observer error: " + self.observer_errors[-1]
            elif ended:
                failure = "observer log task ended"
            elif age is None or age > 2:
                failure = "observer output stale for more than 2 s"
        return {"failure": failure, "monitored": monitored, "output_age_s": age,
                "log_task_ended": ended, "malformed_lines": self.observer_malformed_lines,
                "errors": list(self.observer_errors)}

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
            deadline = time.monotonic() + 5
            while not await asyncio.to_thread(telemetry_exists) and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
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
        code, info, error = await command("docker", "info", "--format", "{{json .}}", check=False, timeout=10)
        if code:
            raise RuntimeUnavailable("Docker daemon unavailable: " + error.strip())
        info = json.loads(info)
        self.metadata.update(docker_architecture=info["Architecture"], docker_vm_cpu_count=info["NCPU"])
        if platform.system() == "Darwin":
            _, cpu, _ = await command("sysctl", "-n", "machdep.cpu.brand_string")
        else:
            cpu = await asyncio.to_thread(lambda: next((line.split(":", 1)[1].strip() for line in
                           Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines() if line.startswith("model name")), platform.processor()))
        self.metadata["host_cpu_model"] = cpu.strip()
        if not self.image:
            _, sha, _ = await command("git", "rev-parse", "--short", "HEAD", cwd=ROOT)
            self.image = "rf-load:" + sha.strip()
        code, _, _ = await command("docker", "image", "inspect", self.image, check=False)
        if code:
            await command("docker", "build", "--build-arg", "BUILD_SHA=" + self.image.rsplit(":", 1)[-1],
                          "-f", ROOT / "Dockerfile", "-t", self.image, ROOT, timeout=600)
        caps = ["--cpus", str(0.5 * self.cpu_scale), "--memory", "1g"]
        tools = ["-v", f"{self.directory / 'code'}:/app/tools:ro"]
        calibration_name = self.prefix + "-calibration"
        self.containers.append(calibration_name)
        _, score, _ = await command("docker", "run", "--rm", "--name", calibration_name,
                                   "--network", "none", *caps, *tools, self.image,
                                   "python", "-m", "tools.load.calibrate", timeout=40)
        await asyncio.to_thread(self._calibration, json.loads(score))
        network_args = ["docker", "network", "create"]
        if self.gateway == "fake":
            network_args.append("--internal")
        await command(*network_args, self.network)
        self.network_created = True
        await command("docker", "volume", "create", self.volume)
        self.volume_created = True
        # Docker copies the image's uid-1000-owned /data into this fresh volume.
        self.containers.append(self.fake_name)
        _, container, _ = await command(
            "docker", "run", "-d", "--name", self.fake_name, "--network", self.network,
            "--network-alias", "upstream", "--user", f"{os.getuid()}:{os.getgid()}", *tools,
            "-v", f"{self.directory / 'cert.pem'}:/load/cert.pem:ro",
            "-v", f"{self.directory / 'key.pem'}:/load/key.pem:ro",
            "-v", f"{self.directory / 'fake-config.json'}:/load/fake-config.json:ro", self.image,
            "python", "-m", "tools.load.fake_upstream", "--port", "8443",
            "--config", "/load/fake-config.json", "--ssl-cert", "/load/cert.pem", "--ssl-key", "/load/key.pem")
        self.fake_id = container.strip()
        deadline = time.monotonic() + 30
        while True:
            try:
                await self.fake_stats()
                break
            except (RuntimeError, ValueError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("fake did not start")
                await asyncio.sleep(0.2)
        env = environment(gateway=self.gateway, fake_url="https://upstream:8443", data_dir="/data",
                          key=self.key, live_key=self.live_key, live_url=self.live_url,
                          certificate="/load/ca-bundle.pem", isolated=self.gateway == "fake",
                          encounter_seconds=self.encounter_seconds, production=self.production())
        env_path = self.directory / "server.env"
        def write_env():
            if any("\n" in str(value) or "\r" in str(value) for value in env.values()):
                raise ValueError("an environment value contains a newline")
            env_path.write_text("".join(f"{name}={value}\n" for name, value in env.items()), encoding="utf-8")
            env_path.chmod(0o600)
        await asyncio.to_thread(write_env)
        self.containers.append(self.server_name)
        try:
            _, container, _ = await command(
                "docker", "run", "-d", "--name", self.server_name, "--network", self.network,
                "--network-alias", "server", *caps, "--env-file", env_path, *tools,
                "-v", f"{self.volume}:/data", "-v", f"{self.bundle}:/load/ca-bundle.pem:ro",
                self.image, "python", "-m", "tools.load.server")
        finally:
            await asyncio.to_thread(env_path.unlink, missing_ok=True)
        self.server_id = container.strip()
        # Start on the ordinary bridge so Docker actually publishes the port.
        self.containers.append(self.relay_name)
        _, relay, _ = await command("docker", "run", "-d", "--name", self.relay_name,
                                    "--network", "bridge", "-p", "127.0.0.1::8080", *tools,
                                    self.image, "python", "-m", "tools.load.relay")
        self.relay_id = relay.strip()
        await command("docker", "network", "connect", self.network, self.relay_name)
        _, port, _ = await command("docker", "port", self.relay_name, "8080/tcp")
        self.base = "http://127.0.0.1:" + port.strip().rsplit(":", 1)[-1]
        self.containers.append(self.observer_name)
        await command("docker", "run", "-d", "--name", self.observer_name, "--user", "0",
                      "--network", "none", "--pid", "container:" + self.server_name, "--cgroupns", "host",
                      "-v", "/sys/fs/cgroup:/host-cgroup:ro", "-v", f"{self.volume}:/data:ro", *tools,
                      self.image, "python", "-m", "tools.load.observer")
        for name, source in ((self.server_name, "server"), (self.fake_name, "fake"), (self.observer_name, "observer")):
            await self._spawn(["docker", "logs", "--follow", name], source)
        self.isolation = {"passed": False, "label": "live gateway egress enabled; host CPU quota, 1 GiB"}

    def _calibration(self, score):
        self.metadata["cpu_calibration"] = score
        self.metadata["server_cpu_quota"] = 0.5 * self.cpu_scale if self.backend == "docker" else None
        if self.fargate_reference:
            reference = json.loads(Path(self.fargate_reference).read_text(encoding="utf-8"))
            self.metadata.update(fargate_reference=reference, host_to_fargate_score_ratio=compare(score, reference),
                                 capacity_scope="this host; Fargate reference ratio reported, no capacity extrapolation")

    async def _local_start(self):
        self.metadata.update(host_cpu_model=platform.processor() or platform.machine(),
                             docker_architecture=None, docker_vm_cpu_count=None)
        await asyncio.to_thread(self._calibration, await asyncio.to_thread(calibrate))
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
                          key=self.key, live_key=self.live_key, live_url=self.live_url, certificate=self.bundle,
                          encounter_seconds=self.encounter_seconds, production=self.production())
        env.update(PORT=str(server_port), HOST="127.0.0.1")
        if self.server_checkout != ROOT:
            env["RF_LOAD_SERVER_CHECKOUT"] = str(self.server_checkout)
        process = await self._spawn([sys.executable, "-m", "tools.load.server"], "server", env={**inherited, **env})
        self.server_pid = process.pid

    async def fake_stats(self):
        if self.backend == "docker":
            _, output, _ = await command("docker", "exec", self.fake_name, "python", "-m", "tools.load.inspect", "fake")
            return json.loads(output)
        async with httpx.AsyncClient(verify=self.fake_ssl, timeout=5, trust_env=False) as http:
            response = await http.get(self.fake_base + "/__load/stats")
            response.raise_for_status()
            return response.json()

    def event_rows(self):
        paths = [self.data / "encounter-events.jsonl"] if self.backend == "docker" else list(
            (self.data / "sessions").glob("*/events.jsonl"))
        rows = []
        for path in paths:
            tail = self._event_tails.setdefault(path, JsonlTail(path))
            rows.extend(tail.read())
        return rows

    async def keepalive_probe(self):
        if self.backend == "docker":
            _, output, _ = await command("docker", "exec", self.fake_name, "python", "-m", "tools.load.probe",
                                         "--config", "/load/fake-config.json", timeout=20)
            return json.loads(output)
        from tools.load.probe import probe
        return await asyncio.to_thread(probe, self.fake_base, self.cert, self.fake_config.http_keepalive_s)

    async def prove_isolation(self):
        _, net, _ = await command("docker", "network", "inspect", self.network)
        _, server, _ = await command("docker", "inspect", self.server_name)
        _, fake, _ = await command("docker", "inspect", self.fake_name)
        _, relay, _ = await command("docker", "inspect", self.relay_name)
        network, server, fake, relay = json.loads(net)[0], json.loads(server)[0], json.loads(fake)[0], json.loads(relay)[0]
        topology = validate_topology(network, server, {self.fake_id, self.server_id, self.relay_id})
        topology &= validate_topology(network, fake, {self.fake_id, self.server_id, self.relay_id})
        relay_networks = relay["NetworkSettings"]["Networks"]
        topology &= (len(relay_networks) == 2 and self.network in relay_networks and "bridge" in relay_networks
                     and relay["Config"]["Cmd"] == ["python", "-m", "tools.load.relay"]
                     and not (server["HostConfig"].get("PortBindings") or fake["HostConfig"].get("PortBindings")))
        bindings = relay["HostConfig"].get("PortBindings") or {}
        topology &= set(bindings) == {"8080/tcp"} and all(b["HostIp"] == "127.0.0.1" for b in bindings.get("8080/tcp", []))
        code, result, _ = await command("docker", "exec", self.server_name, "python", "-m", "tools.load.isolation", check=False)
        proof = json.loads(result)
        return {"passed": bool(topology and code == 0 and proof["passed"]), "topology_passed": bool(topology),
                "egress": proof, "members": [self.server_name, self.fake_name, self.relay_name],
                "relay_target": "server:8080", "label": "internal Docker network; server, fake, fixed relay; host CPU quota, 1 GiB"}

    async def resources(self):
        if self.backend == "docker":
            if self.observer_errors:
                raise RuntimeError("resource observer: " + self.observer_errors[-1])
            if not self.resource_rows:
                return {"t": time.time(), "cpu_percent": None, "rss_bytes": None}
            if time.time() - self.resource_rows[-1]["t"] > 3:
                raise RuntimeError("resource observer stopped producing fresh samples")
            return dict(self.resource_rows[-1])
        _, output, _ = await command("ps", "-o", "%cpu=,rss=", "-p", self.server_pid)
        cpu, rss = output.split()
        return {"t": time.time(), "cpu_percent": float(cpu), "rss_bytes": int(rss) * 1024}

    async def settled(self, timelines):
        ids = [t["session_id"] for t in timelines if t.get("session_id")]
        if self.backend == "docker":
            _, output, _ = await command("docker", "exec", self.observer_name, "python", "-m", "tools.load.inspect", "settled", *ids)
            return json.loads(output)["settled"]
        return await asyncio.to_thread(encounters_settled, self.data, ids)

    async def settled_sessions(self, session_ids):
        """One container exec returns independent close results for the whole batch."""
        if self.backend == "docker":
            _, output, _ = await command("docker", "exec", self.observer_name, "python", "-m",
                                         "tools.load.inspect", "settled-sessions", *session_ids)
            return set(json.loads(output)["settled_sessions"])
        return await asyncio.to_thread(lambda: {sid for sid in session_ids
                                               if encounters_settled(self.data, [sid])})

    async def export(self, timelines):
        if self.backend == "docker" and timelines:
            # Only after the participants stop; this copies each durable artifact once.
            target = self.data / "sessions"
            await asyncio.to_thread(target.mkdir, exist_ok=True)
            for sid in dict.fromkeys(t["session_id"] for t in timelines):
                destination = target / sid
                await asyncio.to_thread(destination.mkdir, exist_ok=True)
                await command("docker", "cp", f"{self.server_name}:/data/sessions/{sid}/.", destination, timeout=300)

    async def kill_server(self):
        if self.backend == "docker":
            await command("docker", "kill", self.server_name, check=False)
        else:
            for process in self.processes:
                if process.pid == self.server_pid and process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()

    async def profile_available(self):
        if self.backend == "docker":
            if not self.profile_image:
                return False
            code, _, _ = await command("docker", "run", "--rm", "--network", "none", self.profile_image,
                                        "py-spy", "--version", check=False)
            return code == 0
        return bool(shutil.which("py-spy"))

    async def profile(self, directory, *, delay=30, seconds=10):
        await asyncio.sleep(delay)
        output = Path(directory) / "py-spy.raw"
        name = self.prefix + "-profile"
        try:
            if self.backend == "docker":
                self.containers.append(name)
                await command("docker", "run", "--name", name, "--network", "none", "--user", "0",
                              "--pid", "container:" + self.server_name, "--cap-add", "SYS_PTRACE", self.profile_image,
                              "py-spy", "record", "--nonblocking", "--pid", "1", "--duration", seconds,
                              "--format", "raw", "--output", "/tmp/py-spy.raw", timeout=seconds + 30)
                await command("docker", "cp", f"{name}:/tmp/py-spy.raw", output)
            elif shutil.which("py-spy"):
                await command("py-spy", "record", "--nonblocking", "--pid", self.server_pid, "--duration", seconds,
                              "--format", "raw", "--output", output, timeout=seconds + 15)
            else:
                return {"status": "unavailable", "reason": "py-spy is not installed"}
            text = await asyncio.to_thread(output.read_text, encoding="utf-8")
            return {"status": "sampled", "native": False, "nonblocking": True, "duration_s": seconds, **folded_top20(text)}
        except (RuntimeError, OSError) as exc:
            return {"status": "unavailable", "reason": type(exc).__name__ + ": " + str(exc)[-300:]}
        finally:
            if self.backend == "docker":
                await command("docker", "rm", "-f", name, check=False)

    async def stop(self):
        for name in reversed(self.containers):
            await command("docker", "rm", "-f", name, check=False)
        self.containers.clear()
        if self.network_created:
            await command("docker", "network", "rm", self.network, check=False)
            self.network_created = False
        if self.volume_created:
            await command("docker", "volume", "rm", self.volume, check=False)
            self.volume_created = False
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
        events[sid] = JsonlTail(path).read()
        settled = settled and encounters_settled(data, [sid])
        size = 0
        for artifact in directory.rglob("*"):
            try:
                if artifact.is_file():
                    size += artifact.stat().st_size
            except FileNotFoundError:
                pass  # A timed-out close can still rename a temporary mix.
        sizes[sid] = size
    return events, sizes, settled
