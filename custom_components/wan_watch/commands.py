import shlex


def prefix(driver: str) -> list[str]:
    return ["/opt/bin/busybox"] if driver == "keenetic" else []


def metadata_commands(driver: str) -> list[list[str]]:
    return [
        prefix(driver) + ["ip", "route", "show", "default"],
        ["cat", "/proc/net/dev"],
        ["cat", "/proc/uptime"],
    ]


def resource_command() -> list[str]:
    return ["cat", "/proc/stat", "/proc/meminfo", "/proc/loadavg"]


def download_command(interface: str, target: dict, size: int) -> list[str]:
    host, port = target["server_name"], target["port"]
    return [
        "curl",
        "--disable",
        "-4",
        "--noproxy",
        "*",
        "--interface",
        f"if!{interface}",
        "--resolve",
        f"{host}:{port}:{target['address']}",
        "--connect-timeout",
        "3",
        "--max-time",
        "20",
        "--max-filesize",
        str(size),
        "--range",
        f"0-{size - 1}",
        "--header",
        "Accept-Encoding: identity",
        "--silent",
        "--output",
        "/dev/null",
        "--write-out",
        "WANDOWN %{http_code} %{size_download} %{speed_download} %{time_total}\\n",
        f"https://{host}:{port}{target['path'].replace('{bytes}', str(size))}",
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


def upload_command(driver: str, interface: str, target: dict, size: int) -> list[str]:
    host, port = target["server_name"], target["port"]
    source = prefix(driver) + ["head", "-c", str(size), "/dev/zero"]
    curl = [
        "curl",
        "--disable",
        "-4",
        "--noproxy",
        "*",
        "--interface",
        f"if!{interface}",
        "--resolve",
        f"{host}:{port}:{target['address']}",
        "--connect-timeout",
        "3",
        "--max-time",
        "20",
        "--silent",
        "--output",
        "/dev/null",
        "--max-filesize",
        str(size),
        "--data-binary",
        "@-",
        "--write-out",
        "WANUP %{http_code} %{size_upload} %{speed_upload} %{time_total}\\n",
        f"https://{host}:{port}{target['path']}",
    ]
    return ["sh", "-c", shlex.join(source) + " | " + shlex.join(curl)]
