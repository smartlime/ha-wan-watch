import math


def parse_resources(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            ticks = [int(v) for v in parts[1:9]]
            if len(ticks) >= 4 and all(v >= 0 for v in ticks):
                values["cpu_ticks"] = [sum(ticks), ticks[3] + (ticks[4] if len(ticks) > 4 else 0)]
        elif parts[0] in (
            "MemTotal:",
            "MemAvailable:",
            "MemFree:",
            "Buffers:",
            "Cached:",
            "SReclaimable:",
            "Shmem:",
        ):
            values[parts[0][:-1]] = int(parts[1])
        elif len(parts) == 5 and "/" in parts[3]:
            load = float(parts[0])
            if math.isfinite(load) and load >= 0:
                values["load_1m"] = load
    total = values.get("MemTotal", 0)
    if total > 0:
        available = values.get("MemAvailable")
        if available is None and all(k in values for k in ("MemFree", "Buffers", "Cached")):
            available = (
                sum(values[k] for k in ("MemFree", "Buffers", "Cached"))
                + values.get("SReclaimable", 0)
                - values.get("Shmem", 0)
            )
        if available is not None and 0 <= available <= total:
            values["memory_used_percent"] = round(100 * (total - available) / total, 2)
            values["memory_available"] = round(available / 1024, 2)
            values["memory_total"] = round(total / 1024, 2)
    return {k: v for k, v in values.items() if k[0].islower()}


def cpu_percent(previous: dict, current: dict) -> float | None:
    old, new = previous.get("cpu_ticks"), current.get("cpu_ticks")
    if not old or not new or current.get("uptime", 0) <= previous.get("uptime", 0):
        return None
    total, idle = new[0] - old[0], new[1] - old[1]
    if total <= 0 or not 0 <= idle <= total:
        return None
    return round(100 * (total - idle) / total, 2)
