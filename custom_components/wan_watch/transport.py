from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
import ssl
import time

import asyncssh

from .commands import (
    download_command,
    link_command,
    metadata_commands,
    ping_command,
    resource_command,
    tls_command,
)
from .download import parse_download
from .model import (
    ChannelSample,
    Probe,
    Quality,
    aggregate_quality,
    parse_counters,
    parse_default_routes,
    parse_ping,
    parse_ping_checks,
    parse_tls,
    reachability,
)
from .resources import parse_resources


class PinnedClient(asyncssh.SSHClient):
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint

    def validate_host_public_key(self, host, addr, port, key):
        return key.get_fingerprint() == self.fingerprint


async def discover_fingerprint(host: str, port: int) -> str:
    key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port=port), 8)
    if key is None:
        raise ValueError("missing_host_key")
    return key.get_fingerprint()


async def connect(router: dict):
    keys = []
    if router.get("private_key"):
        keys.append(asyncssh.import_private_key(router["private_key"]))
    return await asyncio.wait_for(
        asyncssh.connect(
            router["host"],
            port=router.get("port", 22),
            username=router["username"],
            password=router.get("password") or None,
            client_keys=keys,
            agent_path=None,
            known_hosts=(),
            client_factory=lambda: PinnedClient(router["host_key"]),
            connect_timeout=6,
            login_timeout=8,
            keepalive_interval=10,
            keepalive_count_max=2,
        ),
        10,
    )


async def command(conn, args: list[str], limit: float = 10):
    return await conn.run("LC_ALL=C " + shlex.join(args), timeout=limit, check=False)


async def local_availability(targets: list[dict]) -> list[Probe]:
    context = await asyncio.to_thread(ssl.create_default_context)

    async def probe(target):
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    target["address"],
                    target["port"],
                    ssl=context,
                    server_hostname=target["server_name"],
                ),
                5,
            )
            return Probe(True)
        except ssl.SSLCertVerificationError:
            return Probe(None, "certificate_verification_failed")
        except (OSError, TimeoutError, ssl.SSLError):
            return Probe(False, "endpoint_unreachable")
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), 1)

    return await asyncio.gather(*(probe(t) for t in targets))


async def collect_router(
    router: dict, channels: list[dict], targets: list[dict], ping_count: int
) -> dict:
    now = time.time()
    result = {"measured": False, "measured_at": now, "channels": {}, "error": None}
    try:
        conn = await connect(router)
    except (OSError, asyncssh.Error, TimeoutError, ValueError) as exc:
        result["error"] = type(exc).__name__
        return result
    async with conn:
        busybox = router["driver"] == "keenetic"
        semaphore = asyncio.Semaphore(4)

        async def run(args, limit=10):
            async with semaphore:
                return await command(conn, args, limit)

        try:
            routes, counters, uptime = await asyncio.gather(
                *(run(args) for args in metadata_commands(router["driver"]))
            )
            if any(r.exit_status != 0 for r in (routes, counters, uptime)):
                result["error"] = "router_metadata_failed"
                return result
            result.update(
                measured=True,
                default_interfaces=parse_default_routes(routes.stdout),
                uptime=float(uptime.stdout.split()[0]),
            )
            counter_data = parse_counters(counters.stdout)
            try:
                resources = await run(resource_command())
                if resources.exit_status == 0:
                    result.update(parse_resources(resources.stdout))
            except (OSError, asyncssh.Error, TimeoutError, ValueError, IndexError):
                pass
            check_data = {}
            if busybox:
                check = await run(["ndmc", "-c", "show ping-check"])
                if check.exit_status == 0:
                    check_data = parse_ping_checks(check.stdout)

            async def channel_probe(channel):
                interface = channel["interface"]
                logical = channel.get("logical_interface", "")
                link = None
                status = await run(link_command(router["driver"], channel))
                if busybox and logical:
                    match = re.search(r"^\s*link:\s*(up|down)", status.stdout, re.M)
                    link = match[1] == "up" if match else None
                else:
                    if status.exit_status == 0:
                        link = "LOWER_UP" in status.stdout or bool(
                            re.search(r"state UP\b", status.stdout)
                        )

                async def tls_probe(target):
                    try:
                        response = await run(tls_command(interface, target), 7)
                        return parse_tls(response.stdout, response.exit_status)
                    except (OSError, asyncssh.Error, TimeoutError) as exc:
                        return Probe(None, type(exc).__name__)

                async def ping_probe(target):
                    try:
                        ping = await run(
                            ping_command(router["driver"], interface, target, ping_count),
                            ping_count + 4,
                        )
                        return parse_ping(ping.stdout, ping.exit_status)
                    except (OSError, asyncssh.Error, TimeoutError) as exc:
                        return Quality(error=type(exc).__name__)

                probes = await asyncio.gather(*(tls_probe(t) for t in targets))
                qualities = await asyncio.gather(*(ping_probe(t) for t in targets))
                sample = ChannelSample(
                    measured_at=time.time(),
                    reachable=reachability(probes),
                    link=link,
                    quality=aggregate_quality(qualities),
                    quality_targets={
                        t["address"]: q for t, q in zip(targets, qualities, strict=True)
                    },
                    target_successes=sum(p.success is True for p in probes),
                    target_count=len(probes),
                    ping_check=check_data.get(logical),
                    counters=counter_data.get(interface, {}),
                    error=next((p.error for p in probes if p.success is None), None),
                )
                if link is False and sample.reachable is None:
                    sample.reachable = False
                return channel["id"], sample

            async def safe_channel(channel):
                try:
                    return await channel_probe(channel)
                except (OSError, asyncssh.Error, TimeoutError, ValueError) as exc:
                    return channel["id"], ChannelSample(time.time(), None, error=type(exc).__name__)

            result["channels"] = dict(await asyncio.gather(*(safe_channel(c) for c in channels)))
        except (OSError, asyncssh.Error, TimeoutError, ValueError, IndexError) as exc:
            result["measured"] = False
            result["error"] = type(exc).__name__
    return result


async def collect_download(router, channel, target, size):
    try:
        async with await connect(router) as conn:
            version = await command(conn, ["curl", "--disable", "--version"], 5)
            match = re.match(r"curl (\d+)\.(\d+)\.(\d+)", version.stdout)
            if version.exit_status or not match or tuple(map(int, match.groups())) < (8, 4, 0):
                return {
                    "download_mbps": None,
                    "download_status": "failed",
                    "download_error": "curl_8_4_required",
                }
            response = await command(conn, download_command(channel["interface"], target, size), 23)
        return parse_download(response.stdout, response.exit_status, size)
    except (OSError, asyncssh.Error, TimeoutError, ValueError):
        return {"download_mbps": None, "download_status": "failed"}
