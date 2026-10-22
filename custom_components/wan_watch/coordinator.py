from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .engine import Engine, event_selected, format_event
from .model import reachability
from .transport import collect_router, local_availability

LOGGER = logging.getLogger(__name__)


class WanCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self.config = dict(entry.data)
        self.settings = self.config["settings"]
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

    def _stored(self):
        return {"engine": self.engine.export(), "pending": self.pending}

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
        data = self.engine.update(time.time(), dict(results[:-1]), reachability(results[-1]))
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
        if self.notification_task and not self.notification_task.done():
            self.notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.notification_task
        await self.store.async_save(self._stored())
