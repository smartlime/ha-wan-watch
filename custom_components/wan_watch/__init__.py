from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.const import KEY_HASS

from .const import DOMAIN, PLATFORMS
from .coordinator import WanCoordinator


class StatusView(HomeAssistantView):
    url = "/api/wan_watch/status"
    name = "api:wan_watch:status"
    requires_auth = True

    async def get(self, request):
        hass = request.app[KEY_HASS]
        sites = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator and coordinator.data:
                sites.append({"entry_id": entry.entry_id, **coordinator.data})
        return self.json({"schema_version": 1, "sites": sites})


async def async_setup(hass, config):
    hass.http.register_view(StatusView)
    return True


async def async_setup_entry(hass, entry):
    coordinator = WanCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_load()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
