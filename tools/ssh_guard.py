import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "wan_watch"))
from commands import (
    download_command,
    link_command,
    metadata_commands,
    ping_command,
    resource_command,
    tls_command,
)
from const import DEFAULT_DOWNLOAD_TARGET
from model import INTERFACE_RE, validate_download_target, validate_targets


def generate(config):
    driver = config["driver"]
    if driver not in ("linux", "keenetic") or not 2 <= config["ping_count"] <= 20:
        raise ValueError("invalid_probe_configuration")
    validate_targets(config["targets"])
    commands = metadata_commands(driver)
    commands.append(resource_command())
    if driver == "keenetic":
        commands.append(["ndmc", "-c", "show ping-check"])
    for channel in config["channels"]:
        for key in ("interface", "logical_interface"):
            if channel.get(key) and not INTERFACE_RE.fullmatch(channel[key]):
                raise ValueError("invalid_interface")
        commands.append(link_command(driver, channel))
        if channel.get("download"):
            commands.append(["curl", "--disable", "--version"])
            target = config.get("download_target", DEFAULT_DOWNLOAD_TARGET)
            size = config.get("download_bytes", 5000000)
            validate_download_target(target)
            if not isinstance(size, int) or not 1000000 <= size <= 20000000:
                raise ValueError("invalid_download_size")
            commands.append(download_command(channel["interface"], target, size))
        for target in config["targets"]:
            commands.extend(
                [
                    tls_command(channel["interface"], target),
                    ping_command(driver, channel["interface"], target, config["ping_count"]),
                ]
            )
    lines = ["#!/bin/sh", "LC_ALL=C", "export LC_ALL", 'case "$SSH_ORIGINAL_COMMAND" in']
    for command in sorted({shlex.join(args) for args in commands}):
        lines.append(f"  {shlex.quote('LC_ALL=C ' + command)}) exec {command} ;;")
    lines.extend(["  *) exit 126 ;;", "esac", ""])
    return "\n".join(lines)


if __name__ == "__main__":
    sys.stdout.write(generate(json.load(sys.stdin)))
