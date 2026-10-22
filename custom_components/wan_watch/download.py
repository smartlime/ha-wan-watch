import math
import re


def parse_download(text, exit_status, size):
    result = {"download_mbps": None, "download_status": "failed"}
    match = re.search(r"^WANDOWN (\d+) ([\d.]+) ([\d.]+) ([\d.]+)$", text.strip(), re.M)
    if match is None:
        return result
    code, received, speed, duration = map(float, match.groups())
    if not all(math.isfinite(x) for x in (received, speed, duration)):
        return result
    result.update(download_bytes=received, download_duration=duration)
    if exit_status == 0 and code in (200, 206) and received == size and speed > 0 and duration > 0:
        result.update(download_mbps=round(speed * 8 / 1_000_000, 3), download_status="ok")
    return result
