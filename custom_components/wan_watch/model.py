from __future__ import annotations

import ipaddress
import math
import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

ID_RE = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
INTERFACE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,63}\Z")


@dataclass
class Probe:
    success: bool | None
    error: str | None = None


@dataclass
class Quality:
    loss: float | None = None
    latency: float | None = None
    jitter: float | None = None
    sent: int = 0
    received: int = 0
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.sent > 0 and self.loss is not None and self.error is None


@dataclass
class ChannelSample:
    measured_at: float
    reachable: bool | None
    link: bool | None = None
    quality: Quality = field(default_factory=Quality)
    quality_targets: dict[str, Quality] = field(default_factory=dict)
    target_successes: int = 0
    target_count: int = 0
    ping_check: str | None = None
    counters: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def reachability(probes: list[Probe]) -> bool | None:
    if any(p.success is True for p in probes):
        return True
    if probes and all(p.success is False for p in probes):
        return False
    return None


def parse_ping(output: str, exit_status: int) -> Quality:
    match = re.search(r"(\d+) packets transmitted,\s*(\d+)(?: packets)? received", output)
    if not match or exit_status not in (0, 1):
        return Quality(error="ping_execution_failed")
    sent, received = map(int, match.groups())
    if sent <= 0 or received > sent:
        return Quality(error="invalid_ping_result")
    summary = re.search(r"(?:round-trip|rtt).*?=\s*([\d.]+)/([\d.]+)/([\d.]+)", output)
    times = [float(v) for v in re.findall(r"time[=<]([\d.]+)\s*ms", output)]
    latency = float(summary[2]) if summary and received else None
    if received and latency is None:
        latency = statistics.mean(times) if times else None
    jitter = (
        statistics.mean(abs(b - a) for a, b in zip(times, times[1:], strict=False))
        if len(times) > 1
        else None
    )
    return Quality(100.0 * (sent - received) / sent, latency, jitter, sent, received)


def parse_tls(output: str, exit_status: int) -> Probe:
    if exit_status in (7, 28, 35, 52, 56):
        return Probe(False, "endpoint_unreachable")
    if exit_status != 0:
        return Probe(None, f"tls_probe_error_{exit_status}")
    match = re.search(r"WANWATCH (\d{3}) ([\d.]+)", output)
    if not match:
        return Probe(None, "invalid_tls_result")
    return Probe(int(match[1]) >= 100 and float(match[2]) > 0)


def aggregate_quality(results: list[Quality]) -> Quality:
    if not results or any(not q.valid for q in results):
        return Quality(error="incomplete_quality_measurement")
    sent = sum(q.sent for q in results)
    received = sum(q.received for q in results)
    timed = [q for q in results if q.latency is not None and q.received]
    jitters = [q.jitter for q in results if q.jitter is not None]
    return Quality(
        loss=100 * (sent - received) / sent,
        latency=sum(q.latency * q.received for q in timed) / sum(q.received for q in timed)
        if timed
        else None,
        jitter=statistics.mean(jitters) if jitters else None,
        sent=sent,
        received=received,
    )


def parse_counters(output: str) -> dict[str, dict[str, int]]:
    result = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        interface, values = line.split(":", 1)
        fields = values.split()
        if len(fields) != 16 or not all(x.isdigit() for x in fields):
            continue
        result[interface.strip()] = {
            "rx_bytes": int(fields[0]),
            "rx_errors": int(fields[2]),
            "rx_drops": int(fields[3]),
            "tx_bytes": int(fields[8]),
            "tx_errors": int(fields[10]),
            "tx_drops": int(fields[11]),
        }
    return result


def parse_default_routes(output: str) -> list[str]:
    routes = []
    current = None
    for line in output.splitlines():
        if not line.startswith("default"):
            if current is not None and line.lstrip().startswith("nexthop "):
                routes[current][1].extend(re.findall(r"\bdev ([\w.:-]+)", line))
            else:
                current = None
            continue
        metric = re.search(r"\bmetric (\d+)", line)
        devices = re.findall(r"\bdev ([\w.:-]+)", line)
        routes.append((int(metric[1]) if metric else 0, devices))
        current = len(routes) - 1
    if not routes:
        return []
    best = min(m for m, _ in routes)
    return sorted({d for m, devices in routes if m == best for d in devices})


def parse_ping_checks(output: str) -> dict[str, str]:
    result = {}
    for block in re.split(r"\binterface:\s*\n", output)[1:]:
        name = re.search(r"^\s*name:\s*(\S+)", block, re.M)
        status = re.search(r"^\s*status:\s*(.+)", block, re.M)
        if name and status:
            result[name[1]] = status[1].strip()
    return result


def validate_topology(routers: list[dict], channels: list[dict]) -> None:
    router_ids = [r["id"] for r in routers]
    channel_ids = [c["id"] for c in channels]
    if len(set(router_ids)) != len(router_ids) or len(set(channel_ids)) != len(channel_ids):
        raise ValueError("duplicate_id")
    if set(router_ids) & set(channel_ids) or {"site", "internet", "route"} & set(
        router_ids + channel_ids
    ):
        raise ValueError("duplicate_id")
    interfaces = [(c["router_id"], c["interface"]) for c in channels]
    if len(set(interfaces)) != len(interfaces):
        raise ValueError("duplicate_interface")
    if not all(ID_RE.fullmatch(x) for x in router_ids + channel_ids):
        raise ValueError("invalid_id")
    for router in routers:
        if router.get("driver") not in ("keenetic", "linux"):
            raise ValueError("invalid_driver")
        if not router.get("host") or not 1 <= int(router.get("port", 22)) <= 65535:
            raise ValueError("invalid_router")
    graph = {r: [] for r in router_ids}
    for channel in channels:
        if channel["router_id"] not in graph:
            raise ValueError("missing_router")
        if not INTERFACE_RE.fullmatch(channel["interface"]):
            raise ValueError("invalid_interface")
        if channel.get("logical_interface") and not INTERFACE_RE.fullmatch(
            channel["logical_interface"]
        ):
            raise ValueError("invalid_interface")
        upstream = channel.get("upstream_router")
        if upstream:
            if upstream not in graph:
                raise ValueError("missing_router")
            graph[channel["router_id"]].append(upstream)

    def visit(node: str, path: set[str]) -> None:
        if node in path:
            raise ValueError("topology_cycle")
        for next_node in graph[node]:
            visit(next_node, path | {node})

    for node in graph:
        visit(node, set())


def validate_targets(targets: list[dict]) -> None:
    if not 1 <= len(targets) <= 8:
        raise ValueError("invalid_targets")
    for target in targets:
        ipaddress.IPv4Address(target["address"])
        if not 1 <= int(target["port"]) <= 65535:
            raise ValueError("invalid_port")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", target["server_name"]):
            raise ValueError("invalid_server_name")


def active_providers(root: str, routers: dict, channels: list[dict]) -> list[str] | None:
    def resolve(router_id: str, visited: set) -> list[str] | None:
        if router_id in visited:
            return None
        router = routers.get(router_id)
        if not router or not router.get("measured"):
            return None
        devices = router.get("default_interfaces", [])
        if not devices:
            return []
        results = []
        for interface in devices:
            candidates = [
                c for c in channels if c["router_id"] == router_id and c["interface"] == interface
            ]
            if len(candidates) != 1:
                return None
            channel = candidates[0]
            upstream = channel.get("upstream_router")
            names = resolve(upstream, visited | {router_id}) if upstream else [channel["id"]]
            if names is None:
                return None
            results.extend(names)
        return sorted(set(results))

    return resolve(root, set())


class Debounce:
    def __init__(self, count: int, stable: Any = None):
        self.count = count
        self.stable = stable
        self.candidate = None
        self.samples = 0
        self.initialized = stable is not None

    def update(self, value: Any) -> tuple[Any, bool]:
        if value is None:
            self.candidate, self.samples = None, 0
            return None, False
        if not self.initialized:
            self.stable, self.initialized = value, True
            return value, False
        if value == self.stable:
            self.candidate, self.samples = None, 0
            return value, False
        self.samples = self.samples + 1 if value == self.candidate else 1
        self.candidate = value
        if self.samples >= self.count:
            self.stable, self.candidate, self.samples = value, None, 0
            return value, True
        return self.stable, False


class AvailabilityHistory:
    def __init__(self, points: list | None = None):
        self.points: list[list] = points or []

    def observe(self, now: float, state: bool | None, max_gap: float) -> None:
        if self.points and now - self.points[-1][0] > max_gap:
            self.points.append([self.points[-1][0] + max_gap, None])
        self.points.append([now, state])
        cutoff = now - 86400
        while len(self.points) > 1 and self.points[1][0] < cutoff:
            self.points.pop(0)

    def statistics(
        self, now: float, max_gap: float, window: int = 86400
    ) -> dict[str, float | None]:
        up = down = 0.0
        cutoff = now - window
        suffix = "24h" if window == 86400 else "20m"
        for index, (start, state) in enumerate(self.points):
            end = self.points[index + 1][0] if index + 1 < len(self.points) else now
            duration = max(0.0, min(now, end, start + max_gap) - max(start, cutoff))
            if state is True:
                up += duration
            elif state is False:
                down += duration
        known = up + down
        return {
            f"availability_{suffix}": round(100 * up / known, 3) if known else None,
            f"coverage_{suffix}": round(100 * known / window, 3),
            f"downtime_{suffix}": round(down, 1),
        }


def quality_state(quality: Quality, loss_threshold: float, latency_threshold: float) -> str | None:
    if not quality.valid:
        return None
    if quality.loss >= loss_threshold or (
        quality.latency is not None and quality.latency >= latency_threshold
    ):
        return "poor"
    return "good"


def safe_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else None


def serialize_sample(sample: ChannelSample) -> dict:
    return asdict(sample)


def validate_download_target(target):
    validate_targets([target])
    path = target.get("path", "")
    if not re.fullmatch(r"/[a-zA-Z0-9_./?=&%{}-]+", path) or path.count("{bytes}") != 1:
        raise ValueError("invalid_download_path")
    if "{" in path.replace("{bytes}", "") or "}" in path.replace("{bytes}", ""):
        raise ValueError("invalid_download_path")


def validate_upload_target(target):
    validate_targets([target])
    if not re.fullmatch(r"/[a-zA-Z0-9_./?=&%-]+", target.get("path", "")):
        raise ValueError("invalid_upload_path")
