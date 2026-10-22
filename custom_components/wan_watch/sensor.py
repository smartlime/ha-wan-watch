from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass

from .entity import WanEntity

CHANNEL_SENSORS = [
    ("loss", "Потери пакетов", "%", None, "quality"),
    ("latency", "Задержка", "ms", SensorDeviceClass.DURATION, "quality"),
    ("jitter", "Колебание задержки", "ms", SensorDeviceClass.DURATION, "quality"),
    ("quality_state", "Качество связи", None, None, None),
    ("ping_check", "Оценка роутера", None, None, None),
    ("availability_24h", "Доступность за сутки", "%", None, None),
    ("coverage_24h", "Покрытие измерениями за сутки", "%", None, None),
    ("downtime_24h", "Простой за сутки", "s", SensorDeviceClass.DURATION, None),
    ("rx_bytes", "Принято данных", "B", SensorDeviceClass.DATA_SIZE, "counters"),
    ("tx_bytes", "Передано данных", "B", SensorDeviceClass.DATA_SIZE, "counters"),
    ("rx_errors", "Ошибки приема", None, None, "counters"),
    ("tx_errors", "Ошибки передачи", None, None, "counters"),
    ("rx_drops", "Отброшено при приеме", None, None, "counters"),
    ("tx_drops", "Отброшено при передаче", None, None, "counters"),
    ("last_outage_at", "Последнее отключение", None, SensorDeviceClass.TIMESTAMP, None),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = []
    for channel in coordinator.config["channels"]:
        for definition in CHANNEL_SENSORS:
            entities.append(WanSensor(coordinator, channel["id"], *definition))
    for router in coordinator.config["routers"]:
        entities.append(
            WanSensor(
                coordinator,
                router["id"],
                "uptime",
                "Время работы",
                "s",
                SensorDeviceClass.DURATION,
                None,
            )
        )
    entities.extend(
        [
            WanSensor(
                coordinator, "site", "active_providers", "Активный провайдер", None, None, None
            ),
            WanSensor(
                coordinator,
                "site",
                "availability_24h",
                "Доступность интернета за сутки",
                "%",
                None,
                None,
            ),
            WanSensor(
                coordinator,
                "site",
                "coverage_24h",
                "Покрытие измерениями за сутки",
                "%",
                None,
                None,
            ),
            WanSensor(
                coordinator, "site", "notification_queue", "Очередь уведомлений", None, None, None
            ),
        ]
    )
    async_add_entities(entities)


class WanSensor(WanEntity, SensorEntity):
    def __init__(self, coordinator, subject, key, name, unit, device_class, group):
        super().__init__(coordinator, subject, key, name)
        self.value_group = group
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        if group == "counters":
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        elif unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        sample = self.sample
        if self.key == "last_outage_at":
            outage = sample.get("last_outage")
            return datetime.fromtimestamp(outage["started_at"], UTC) if outage else None
        if self.key == "active_providers":
            ids = sample.get("active_providers")
            if ids is None:
                return None
            names = {c["id"]: c["name"] for c in self.coordinator.config["channels"]}
            return ", ".join(names.get(cid, cid) for cid in ids) or "none"
        return (
            (sample.get(self.value_group) or {}).get(self.key)
            if self.value_group
            else sample.get(self.key)
        )

    @property
    def available(self):
        if self.value_group == "quality" and not self.sample.get("quality_valid"):
            return False
        return super().available and (self.native_value is not None or self.key == "last_outage_at")
