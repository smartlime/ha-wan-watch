from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .model import (
    AvailabilityHistory,
    ChannelSample,
    Debounce,
    active_providers,
    quality_state,
)


class Engine:
    def __init__(self, config: dict, saved: dict | None = None):
        self.config = config
        self.settings = config["settings"]
        topology = {
            "targets": config.get("targets", []),
            "egress": config["egress_router"],
            "channels": [
                {
                    k: c.get(k)
                    for k in (
                        "id",
                        "router_id",
                        "interface",
                        "logical_interface",
                        "upstream_router",
                    )
                }
                for c in config["channels"]
            ],
            "routers": [{k: r.get(k) for k in ("id", "host", "driver")} for r in config["routers"]],
        }
        self.signature = hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest()
        self.saved = saved or {}
        if self.saved.get("signature") != self.signature:
            self.saved = {k: v for k, v in self.saved.items() if k in ("sequence", "events")}
        self.sequence = self.saved.get("sequence", 0)
        self.events = self.saved.get("events", [])[-256:]
        self.filters = {
            key: Debounce(self.settings["confirm_samples"], value)
            for key, value in self.saved.get("stable", {}).items()
        }
        self.histories = {
            key: AvailabilityHistory(points)
            for key, points in self.saved.get("histories", {}).items()
        }
        self.last_outages = self.saved.get("last_outages", {})
        self.snapshot: dict = {}
        self.new_events: list[dict] = []

    def _filter(self, key: str, value: Any) -> tuple[Any, bool]:
        if key not in self.filters:
            self.filters[key] = Debounce(self.settings["confirm_samples"])
        return self.filters[key].update(value)

    def _event(self, now: float, kind: str, subject: str, name: str, **details) -> None:
        self.sequence += 1
        event = {
            "id": self.sequence,
            "time": now,
            "type": kind,
            "subject": subject,
            "name": name,
            "details": deepcopy(details),
        }
        self.events.append(event)
        self.events = self.events[-256:]
        self.new_events.append(event)

    def _history(self, key: str, now: float, state: bool | None) -> dict:
        history = self.histories.setdefault(key, AvailabilityHistory())
        max_gap = self.settings["interval"] * 2
        history.observe(now, state, max_gap)
        return history.statistics(now, max_gap)

    def update(self, now: float, routers: dict, local_reachable: bool | None) -> dict:
        self.new_events = []
        channels = {}
        for channel in self.config["channels"]:
            cid = channel["id"]
            router = routers.get(channel["router_id"], {})
            sample = router.get("channels", {}).get(cid)
            if not router.get("measured") or sample is None:
                sample = ChannelSample(now, None, error=router.get("error", "no_router_data"))
            stable, changed = self._filter(f"channel:{cid}", sample.reachable)
            history = self.histories.get(cid)
            gap = (
                history
                and history.points
                and now - history.points[-1][0] > self.settings["interval"] * 2
            )
            if sample.reachable is None or gap:
                previous = self.last_outages.get(cid)
                if previous and previous.get("ended_at") is None:
                    previous["contains_unknown"] = True
            measured = sample.reachable is not None
            measure_state, measure_changed = self._filter(f"measurement:{cid}", measured)
            if measure_changed:
                self._event(
                    now,
                    "measurement_restored" if measure_state else "measurement_failed",
                    cid,
                    channel["name"],
                )
            if changed:
                event_type = "recovery" if stable else "outage"
                if stable:
                    previous = self.last_outages.get(cid)
                    if previous and previous.get("ended_at") is None:
                        previous["ended_at"] = now
                        previous["duration"] = now - previous["started_at"]
                else:
                    self.last_outages[cid] = {"started_at": now, "ended_at": None}
                self._event(
                    now, event_type, cid, channel["name"], last_outage=self.last_outages.get(cid)
                )
            qstate = quality_state(
                sample.quality, self.settings["loss_threshold"], self.settings["latency_threshold"]
            )
            stable_quality, quality_changed = self._filter(f"quality:{cid}", qstate)
            if quality_changed and stable is True:
                self._event(
                    now,
                    "degraded" if stable_quality == "poor" else "quality_recovered",
                    cid,
                    channel["name"],
                    loss=sample.quality.loss,
                    latency=sample.quality.latency,
                )
            data = asdict(sample)
            data.update(
                id=cid,
                name=channel["name"],
                router_id=channel["router_id"],
                upstream_router=channel.get("upstream_router"),
                available=stable,
                measurement_valid=measured,
                quality_valid=sample.quality.valid,
                quality_state=stable_quality,
                last_outage=self.last_outages.get(cid),
            )
            data.update(self._history(cid, now, sample.reachable))
            channels[cid] = data
        internet, changed = self._filter("internet", local_reachable)
        if changed:
            self._event(
                now, "internet_up" if internet else "internet_down", "internet", self.config["name"]
            )
        raw_providers = active_providers(
            self.config["egress_router"], routers, self.config["channels"]
        )
        previous = self.filters.get("route")
        old = previous.stable if previous else None
        providers, route_changed = self._filter("route", raw_providers)
        if route_changed:
            names = {c["id"]: c["name"] for c in self.config["channels"]}
            self._event(
                now,
                "route_changed",
                "route",
                self.config["name"],
                previous=old,
                current=providers,
                previous_names=[names.get(x, x) for x in old or []],
                current_names=[names.get(x, x) for x in providers or []],
            )
        self.snapshot = {
            "schema_version": 1,
            "name": self.config["name"],
            "measured_at": now,
            "internet": internet,
            "internet_measurement_valid": local_reachable is not None,
            "active_providers": providers,
            "channels": channels,
            "routers": {
                rid: {k: v for k, v in r.items() if k != "channels"} for rid, r in routers.items()
            },
            "events": self.events,
            "sequence": self.sequence,
            **self._history("internet", now, local_reachable),
        }
        return self.snapshot

    def export(self) -> dict:
        return {
            "signature": self.signature,
            "sequence": self.sequence,
            "events": self.events,
            "stable": {k: f.stable for k, f in self.filters.items() if f.initialized},
            "histories": {k: h.points for k, h in self.histories.items()},
            "last_outages": self.last_outages,
        }


def event_selected(event: dict, settings: dict) -> bool:
    if event["type"] not in settings["alert_types"]:
        return False
    selected = settings["alert_channels"]
    if not selected:
        return False
    if event["subject"] == "internet":
        return "internet" in selected
    if event["type"] == "route_changed":
        ids = set(event["details"].get("previous") or []) | set(
            event["details"].get("current") or []
        )
        return bool(ids & set(selected)) or "internet" in selected
    return event["subject"] in selected


def format_event(event: dict, tz) -> str:
    labels = {
        "outage": "Канал отключился",
        "recovery": "Канал восстановился",
        "degraded": "Качество связи ухудшилось",
        "quality_recovered": "Качество связи восстановилось",
        "measurement_failed": "Измерения недоступны",
        "measurement_restored": "Измерения восстановились",
        "route_changed": "Активный канал изменился",
        "internet_down": "Интернет недоступен из Home Assistant",
        "internet_up": "Интернет доступен из Home Assistant",
    }
    stamp = datetime.fromtimestamp(event["time"], UTC).astimezone(tz).strftime("%d.%m %H:%M:%S")
    text = f"{stamp}: {event['name']}. {labels[event['type']]}."
    if event["type"] == "route_changed":
        before = ", ".join(event["details"]["previous_names"]) or "нет маршрута"
        after = ", ".join(event["details"]["current_names"]) or "нет маршрута"
        text += f" Был: {before}. Стал: {after}."
    if event["type"] == "degraded":
        text += f" Потери: {event['details']['loss']:.1f}%."
    return text
