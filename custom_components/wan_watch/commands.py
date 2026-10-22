def prefix(driver: str) -> list[str]:
    return ["/opt/bin/busybox"] if driver == "keenetic" else []


def metadata_commands(driver: str) -> list[list[str]]:
    return [
        prefix(driver) + ["ip", "route", "show", "default"],
        ["cat", "/proc/net/dev"],
        ["cat", "/proc/uptime"],
    ]


def link_command(driver: str, channel: dict) -> list[str]:
    if driver == "keenetic" and channel.get("logical_interface"):
        return ["ndmc", "-c", f"show interface {channel['logical_interface']}"]
    return prefix(driver) + ["ip", "link", "show", "dev", channel["interface"]]


def ping_command(driver: str, interface: str, target: dict, count: int) -> list[str]:
    return prefix(driver) + [
        "ping",
        "-I",
        interface,
        "-c",
        str(count),
        "-W",
        "1",
        target["address"],
    ]


def tls_command(interface: str, target: dict) -> list[str]:
    host, port = target["server_name"], target["port"]
    return [
        "curl",
        "-4",
        "--noproxy",
        "*",
        "--interface",
        interface,
        "--resolve",
        f"{host}:{port}:{target['address']}",
        "--connect-timeout",
        "3",
        "--max-time",
        "5",
        "--head",
        "--silent",
        "--output",
        "/dev/null",
        "--write-out",
        "WANWATCH %{http_code} %{time_appconnect}\n",
        f"https://{host}:{port}/",
    ]
