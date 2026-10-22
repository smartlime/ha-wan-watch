from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import WanEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = []
    for channel in coordinator.config["channels"]:
        for key, name in [
            ("available", "Интернет доступен"),
            ("link", "Соединение установлено"),
            ("measurement_valid", "Измерение доступности исправно"),
            ("quality_valid", "Измерение качества исправно"),
        ]:
            entities.append(WanBinarySensor(coordinator, channel["id"], key, name))
    for router in coordinator.config["routers"]:
        entities.append(
            WanBinarySensor(coordinator, router["id"], "measured", "Сбор данных работает")
        )
    entities.append(
        WanBinarySensor(coordinator, "site", "internet", "Интернет доступен из Home Assistant")
    )
    entities.append(
        WanBinarySensor(
            coordinator, "site", "internet_measurement_valid", "Измерение интернета исправно"
        )
    )
    async_add_entities(entities)


class WanBinarySensor(WanEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self):
        return self.sample.get(self.key)

    @property
    def available(self):
        return super().available and self.is_on is not None
