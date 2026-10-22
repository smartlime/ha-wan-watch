import importlib
import tempfile
import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from test_model import config, constants, routers

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
except ImportError as exc:
    raise unittest.SkipTest("Home Assistant is not installed") from exc

coordinator_module = importlib.import_module("ww.coordinator")
sensor_module = importlib.import_module("ww.sensor")
binary_module = importlib.import_module("ww.binary_sensor")
flow_module = importlib.import_module("ww.config_flow")


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config.time_zone = "UTC"
        self.cfg = config()
        self.cfg["targets"] = constants.DEFAULT_TARGETS
        self.entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="wan_watch",
            title="Test",
            data=self.cfg,
            options={},
            source="user",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        self.coordinator = coordinator_module.WanCoordinator(self.hass, self.entry)

    async def asyncTearDown(self):
        await self.coordinator.async_close()
        await self.hass.async_stop(force=True)
        self.directory.cleanup()

    async def test_unknown_entities_and_counters_are_independent(self):
        data = self.coordinator.engine.update(100, routers(100, first=None), True)
        data["channels"]["first"]["counters"] = {"rx_bytes": 42}
        self.coordinator.async_set_updated_data(data)
        binary = binary_module.WanBinarySensor(
            self.coordinator, "first", "available", "Availability"
        )
        self.assertFalse(binary.available)
        counter = sensor_module.WanSensor(
            self.coordinator, "first", "rx_bytes", "Bytes", "B", None, "counters"
        )
        self.assertTrue(counter.available)
        self.assertEqual(counter.native_value, 42)

    async def test_notification_failure_preserves_queue_then_success_removes_it(self):
        c = self.coordinator
        event = {
            "id": 1,
            "time": 100,
            "type": "outage",
            "subject": "first",
            "name": "First",
            "details": {},
        }
        c.pending = [event]
        c.settings["persistent_notifications"] = False
        c.settings["notify_entities"] = ["notify.test"]
        with patch.object(
            type(self.hass.services), "async_call", AsyncMock(side_effect=RuntimeError)
        ):
            await c._deliver()
        self.assertEqual(c.pending, [event])
        self.assertGreater(c.next_retry, 0)
        with patch.object(type(self.hass.services), "async_call", AsyncMock()) as service:
            await c._deliver()
        self.assertEqual(c.pending, [])
        self.assertEqual(service.call_args.kwargs["target"], {"entity_id": ["notify.test"]})

    async def test_stored_queue_respects_new_filters(self):
        c = self.coordinator
        c.pending = [{"id": 1, "type": "outage", "subject": "second"}]
        await c.store.async_save(c._stored())
        await c.async_load()
        self.assertEqual(c.pending, [])

    async def test_config_flow_forms_and_empty_setup(self):
        flow = flow_module.WanWatchConfigFlow()
        flow.hass = self.hass
        result = await flow.async_step_user({"name": "Test"})
        self.assertEqual(result["step_id"], "menu")
        self.assertEqual((await flow.async_step_finish())["step_id"], "choose_router")
        flow.config = self.cfg
        result = await flow.async_step_settings()
        valid = result["data_schema"](
            {**self.cfg["settings"], "targets": constants.DEFAULT_TARGETS, "egress_router": "edge"}
        )
        self.assertEqual(valid["interval"], 30)
        self.assertEqual((await flow.async_step_finish())["type"], "create_entry")

    async def test_polling_runs_local_probe_when_router_collection_fails(self):
        c = self.coordinator
        from ww.model import Probe

        with (
            patch.object(
                coordinator_module, "collect_router", AsyncMock(return_value={"measured": False})
            ),
            patch.object(
                coordinator_module, "local_availability", AsyncMock(return_value=[Probe(True)])
            ),
        ):
            data = await c._async_update_data()
        self.assertTrue(data["internet"])
        self.assertIsNone(data["channels"]["first"]["available"])
