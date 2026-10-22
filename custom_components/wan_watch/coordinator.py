from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_DOWNLOAD_TARGET, DEFAULT_SETTINGS, DEFAULT_UPLOAD_TARGET, DOMAIN
from .engine import Engine, event_selected, format_event
from .model import reachability
from .resources import cpu_percent
from .transport import collect_download, collect_router, collect_upload, local_availability

LOGGER = logging.getLogger(__name__)


class WanCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.config = dict(entry.data)
        self.settings = {**DEFAULT_SETTINGS, **self.config["settings"]}
        self.config["settings"] = self.settings
        self.downloads = {}
        self.download_task = None
        self.previous_routers = {}
        self.store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.engine = Engine(self.config)
        self.pending: list[dict] = []
        self.notification_task = None
        self.next_retry = 0.0
        self.retry_delay = 30
        super().__init__(
            hass,
            LOGGER,
            name=f"WAN Watch {entry.title}",
            update_interval=timedelta(seconds=self.settings["interval"]),
            config_entry=entry,
        )

    async def async_load(self):
        saved = await self.store.async_load() or {}
        self.engine = Engine(self.config, saved.get("engine"))
        self.pending = [e for e in saved.get("pending", []) if event_selected(e, self.settings)]
        self.downloads = saved.get("downloads", {})

    def _stored(self):
        return {
            "engine": self.engine.export(),
            "pending": self.pending,
            "downloads": self.downloads,
        }

    async def _async_update_data(self):
        targets = self.config["targets"]

        async def router_task(router):
            channels = [c for c in self.config["channels"] if c["router_id"] == router["id"]]
            try:
                budget = 15 + len(channels) * ((len(targets) + 3) // 4) * (
                    self.settings["ping_count"] + 12
                )
                async with asyncio.timeout(budget):
                    value = await collect_router(
                        router, channels, targets, self.settings["ping_count"]
                    )
                return router["id"], value
            except TimeoutError:
                return router["id"], {
                    "measured": False,
                    "error": "collection_timeout",
                    "channels": {},
                }

        tasks = [router_task(router) for router in self.config["routers"]]
        results = await asyncio.gather(*tasks, local_availability(targets))
        router_data = dict(results[:-1])
        for rid, value in router_data.items():
            value["cpu_percent"] = cpu_percent(self.previous_routers.get(rid, {}), value)
        self.previous_routers = router_data
        data = self.engine.update(time.time(), router_data, reachability(results[-1]))
        self._attach_downloads(data)
        if self.settings["download_channels"] and (
            self.download_task is None or self.download_task.done()
        ):
            self.download_task = self.entry.async_create_background_task(
                self.hass, self._download_due(), "WAN Watch bounded downloads"
            )
        selected = [
            event for event in self.engine.new_events if event_selected(event, self.settings)
        ]
        for event in self.engine.new_events:
            self.hass.bus.async_fire(f"{DOMAIN}_event", {**event, "entry_id": self.entry.entry_id})
        if selected:
            self.pending.extend(selected)
            self.pending = self.pending[-256:]
            await self.store.async_save(self._stored())
        else:
            self.store.async_delay_save(self._stored, 120)
        data["notification_queue"] = len(self.pending)
        if (
            self.pending
            and time.time() >= self.next_retry
            and (self.notification_task is None or self.notification_task.done())
        ):
            self.notification_task = self.entry.async_create_background_task(
                self.hass, self._deliver(), "WAN Watch notifications"
            )
        return data

    def _download_signature(self, channel):
        router = next(r for r in self.config["routers"] if r["id"] == channel["router_id"])
        return [
            router["host"],
            channel["interface"],
            self.settings.get("download_target", DEFAULT_DOWNLOAD_TARGET),
            self.settings["download_bytes"],
            self.settings["upload_enabled"],
            self.settings.get("upload_target", DEFAULT_UPLOAD_TARGET),
        ]

    def _attach_downloads(self, data):
        now = time.time()
        for channel in self.config["channels"]:
            sample = data["channels"].get(channel["id"], {})
            cached = self.downloads.get(channel["id"], {})
            status = cached.get("download_status", "not_measured")
            if channel["id"] not in self.settings["download_channels"]:
                status = "disabled"
            elif cached.get("signature") != self._download_signature(channel):
                status = "not_measured"
            elif status == "running" and (self.download_task is None or self.download_task.done()):
                status = "interrupted"
            elif (
                now - cached.get("download_checked_at", 0)
                > self.settings["download_interval"] + 120
            ):
                status = "stale"
            sample.update({k: v for k, v in cached.items() if k != "signature"})
            sample["download_status"] = status
            if status != "ok":
                sample["download_mbps"] = None
            upload_status = cached.get("upload_status", "not_measured")
            if (
                not self.settings["upload_enabled"]
                or channel["id"] not in self.settings["download_channels"]
            ):
                upload_status = "disabled"
            elif cached.get("signature") != self._download_signature(channel):
                upload_status = "not_measured"
            elif upload_status == "running" and (
                self.download_task is None or self.download_task.done()
            ):
                upload_status = "interrupted"
            elif (
                now - cached.get("upload_checked_at", 0) > self.settings["download_interval"] + 120
            ):
                upload_status = "stale"
            sample["upload_status"] = upload_status
            if upload_status != "ok":
                sample["upload_mbps"] = None

    async def _download_due(self):
        for channel in self.config["channels"]:
            cid = channel["id"]
            if cid not in self.settings["download_channels"]:
                continue
            cached = self.downloads.get(cid, {})
            if (
                time.time() - cached.get("download_checked_at", 0)
                < self.settings["download_interval"]
            ):
                continue
            router = next(r for r in self.config["routers"] if r["id"] == channel["router_id"])
            self.downloads[cid] = {
                "signature": self._download_signature(channel),
                "download_checked_at": time.time(),
                "download_status": "running",
                "download_mbps": None,
            }
            await self.store.async_save(self._stored())
            result = await collect_download(
                router,
                channel,
                self.settings.get("download_target", DEFAULT_DOWNLOAD_TARGET),
                self.settings["download_bytes"],
            )
            self.downloads[cid].update(result)
            if self.settings["upload_enabled"]:
                self.downloads[cid].update(
                    upload_status="running", upload_checked_at=time.time(), upload_mbps=None
                )
                await self.store.async_save(self._stored())
                upload = await collect_upload(
                    router,
                    channel,
                    self.settings.get("upload_target", DEFAULT_UPLOAD_TARGET),
                    self.settings["download_bytes"],
                )
                self.downloads[cid].update(upload)
            await self.store.async_save(self._stored())
            if self.data:
                self._attach_downloads(self.data)
                self.async_set_updated_data(self.data)

    async def _deliver(self):
        events = self.pending[:10]
        message = "\n".join(format_event(e, ZoneInfo(self.hass.config.time_zone)) for e in events)
        try:
            if self.settings["persistent_notifications"]:
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "notification_id": f"wan_watch_{self.entry.entry_id}",
                        "title": f"WAN Watch: {self.entry.title}",
                        "message": message,
                    },
                    blocking=True,
                )
            entities = self.settings["notify_entities"]
            if entities:
                async with asyncio.timeout(12):
                    await self.hass.services.async_call(
                        "notify",
                        "send_message",
                        {
                            "message": message,
                        },
                        target={"entity_id": entities},
                        blocking=True,
                    )
            delivered = {e["id"] for e in events}
            self.pending = [e for e in self.pending if e["id"] not in delivered]
            self.retry_delay = 30
            self.next_retry = 0
            await self.store.async_save(self._stored())
        except Exception as exc:
            LOGGER.warning(
                "Notification delivery failed (%s); events remain queued", type(exc).__name__
            )
            self.next_retry = time.time() + self.retry_delay
            self.retry_delay = min(600, self.retry_delay * 2)

    async def async_close(self):
        if self.download_task and not self.download_task.done():
            self.download_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.download_task
        if self.notification_task and not self.notification_task.done():
            self.notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.notification_task
        await self.store.async_save(self._stored())
