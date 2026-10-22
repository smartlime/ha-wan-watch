DOMAIN = "wan_watch"
PLATFORMS = ["sensor", "binary_sensor"]
EVENT_TYPES = [
    "outage",
    "recovery",
    "degraded",
    "quality_recovered",
    "measurement_failed",
    "measurement_restored",
    "route_changed",
    "internet_down",
    "internet_up",
]
DEFAULT_TARGETS = [
    {"address": "1.1.1.1", "port": 443, "server_name": "one.one.one.one"},
    {"address": "8.8.8.8", "port": 443, "server_name": "dns.google"},
    {"address": "104.16.123.96", "port": 443, "server_name": "www.cloudflare.com"},
]
DEFAULT_SETTINGS = {
    "interval": 30,
    "confirm_samples": 2,
    "ping_count": 5,
    "loss_threshold": 10.0,
    "latency_threshold": 150.0,
    "notify_entities": [],
    "alert_channels": [],
    "alert_types": ["outage", "recovery", "route_changed", "internet_down", "internet_up"],
    "persistent_notifications": True,
    "upload_enabled": False,
    "download_channels": [],
    "download_interval": 10800,
    "download_bytes": 5000000,
}

DEFAULT_DOWNLOAD_TARGET = {
    "address": "162.159.140.220",
    "port": 443,
    "server_name": "speed.cloudflare.com",
    "path": "/__down?bytes={bytes}",
}

DEFAULT_UPLOAD_TARGET = {**DEFAULT_DOWNLOAD_TARGET, "path": "/__up"}
