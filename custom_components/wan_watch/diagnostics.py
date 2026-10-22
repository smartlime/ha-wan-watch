from homeassistant.components.diagnostics import async_redact_data

REDACT = {"host", "username", "password", "private_key", "host_key", "notify_entities"}


async def async_get_config_entry_diagnostics(hass, entry):
    return {
        "config": async_redact_data(dict(entry.data), REDACT),
        "snapshot": entry.runtime_data.data,
    }
