from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class WanEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, subject, key, name):
        super().__init__(coordinator)
        self.subject = subject
        self.key = key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{subject}_{key}"
        self._attr_translation_key = key
        channel = next((c for c in coordinator.config["channels"] if c["id"] == subject), None)
        router = next((r for r in coordinator.config["routers"] if r["id"] == subject), None)
        device_name = (
            channel["name"] if channel else router["name"] if router else coordinator.entry.title
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{subject}")},
            name=f"WAN Watch {device_name}",
            manufacturer="WAN Watch",
            model="Channel" if channel else "Router" if router else "Network",
        )

    @property
    def sample(self):
        data = self.coordinator.data or {}
        if self.subject == "site":
            return data
        return data.get("channels", {}).get(
            self.subject, data.get("routers", {}).get(self.subject, {})
        )

    @property
    def extra_state_attributes(self):
        sample = self.sample
        attrs = {"subject_id": self.subject}
        for key in ("error", "last_outage"):
            if key in sample:
                attrs[key] = sample[key]
        return attrs
