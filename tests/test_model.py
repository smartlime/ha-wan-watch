import importlib
import pathlib
import sys
import types
import unittest

PACKAGE = pathlib.Path(__file__).parents[1] / "custom_components" / "wan_watch"
namespace = types.ModuleType("ww")
namespace.__path__ = [str(PACKAGE)]
sys.modules["ww"] = namespace
model = importlib.import_module("ww.model")
engine = importlib.import_module("ww.engine")
constants = importlib.import_module("ww.const")


def config():
    import copy

    return {
        "name": "Test network",
        "egress_router": "edge",
        "routers": [
            {"id": "edge", "name": "Edge", "host": "192.0.2.1", "driver": "linux"},
            {"id": "gateway", "name": "Gateway", "host": "192.0.2.2", "driver": "keenetic"},
        ],
        "channels": [
            {
                "id": "uplink",
                "name": "Uplink",
                "router_id": "edge",
                "interface": "eth0",
                "upstream_router": "gateway",
            },
            {"id": "first", "name": "First", "router_id": "gateway", "interface": "eth1"},
            {"id": "second", "name": "Second", "router_id": "gateway", "interface": "ppp0"},
        ],
        "settings": {**copy.deepcopy(constants.DEFAULT_SETTINGS), "alert_channels": ["first"]},
    }


def routers(now, first=True, second=True, quality=None):
    return {
        "edge": {
            "measured": True,
            "default_interfaces": ["eth0"],
            "channels": {"uplink": model.ChannelSample(now, True)},
        },
        "gateway": {
            "measured": True,
            "default_interfaces": ["eth1"],
            "channels": {
                "first": model.ChannelSample(now, first, quality=quality or model.Quality()),
                "second": model.ChannelSample(now, second),
            },
        },
    }


class ModelTests(unittest.TestCase):
    def test_target_change_starts_new_baseline_without_false_recovery(self):
        c = config()
        e = engine.Engine(c)
        e.update(100, routers(100), False)
        e.update(130, routers(130), False)
        c["targets"] = [{"address": "192.0.2.10", "port": 443, "server_name": "example.com"}]
        changed = engine.Engine(c, e.export())
        state = changed.update(160, routers(160), True)
        self.assertEqual(changed.new_events, [])
        self.assertTrue(state["internet"])
        self.assertEqual(state["downtime_24h"], 0)

    def test_restart_preserves_outage_and_recovers_once(self):
        e = engine.Engine(config())
        e.update(100, routers(100), True)
        e.update(130, routers(130, first=False), True)
        e.update(160, routers(160, first=False), True)
        saved_event = dict(e.events[-1]["details"]["last_outage"])
        e = engine.Engine(config(), e.export())
        e.update(300, routers(300), True)
        e.update(330, routers(330), True)
        self.assertEqual([x["type"] for x in e.new_events], ["recovery"])
        self.assertEqual(e.events[-2]["details"]["last_outage"], saved_event)
        self.assertEqual(e.last_outages["first"]["ended_at"], 330)

    def test_quality_aggregates_all_targets_and_exposes_partial_failure(self):
        a = model.Quality(loss=0, latency=10, jitter=1, sent=5, received=5)
        b = model.Quality(loss=100, sent=5, received=0)
        q = model.aggregate_quality([a, b])
        self.assertTrue(q.valid)
        self.assertEqual((q.loss, q.latency), (50, 10))
        self.assertFalse(model.aggregate_quality([a, model.Quality(error="timeout")]).valid)

    def test_namespace_and_interface_collisions_are_rejected(self):
        c = config()
        c["channels"][0]["id"] = "edge"
        with self.assertRaises(ValueError):
            model.validate_topology(c["routers"], c["channels"])
        c = config()
        c["channels"][2]["interface"] = "eth1"
        with self.assertRaises(ValueError):
            model.validate_topology(c["routers"], c["channels"])

    def test_zero_replies_is_valid_loss_not_missing_measurement(self):
        q = model.parse_ping("5 packets transmitted, 0 packets received, 100% packet loss", 1)
        self.assertTrue(q.valid)
        self.assertEqual(q.loss, 100)
        self.assertIsNone(q.latency)

    def test_execution_error_is_not_packet_loss(self):
        self.assertFalse(model.parse_ping("ping: Operation not permitted", 2).valid)
        self.assertIsNone(model.parse_ping("ping: not found", 127).loss)

    def test_busybox_and_linux_ping(self):
        for text in [
            "5 packets transmitted, 4 packets received, 20% packet loss\n"
            "round-trip min/avg/max = 1.0/3.0/5.0 ms",
            "5 packets transmitted, 4 received, 20% packet loss\n"
            "rtt min/avg/max/mdev = 1.0/3.0/5.0/1.0 ms",
        ]:
            q = model.parse_ping(text, 0)
            self.assertEqual((q.sent, q.received, q.loss, q.latency), (5, 4, 20, 3))

    def test_quality_failure_does_not_mean_internet_failure(self):
        state = engine.Engine(config()).update(
            100, routers(100, quality=model.Quality(error="permission")), True
        )
        self.assertTrue(state["channels"]["first"]["available"])
        self.assertFalse(state["channels"]["first"]["quality_valid"])

    def test_failed_management_is_unknown_not_outage(self):
        e = engine.Engine(config())
        e.update(100, routers(100), True)
        state = e.update(130, {"gateway": {"measured": False, "error": "ssh_error"}}, True)
        self.assertIsNone(state["channels"]["first"]["available"])
        self.assertIsNone(state["active_providers"])
        self.assertTrue(state["internet"])
        self.assertFalse(any(x["type"] == "outage" for x in e.new_events))

    def test_outage_requires_consecutive_evidence_and_recovery_emits_once(self):
        e = engine.Engine(config())
        e.update(100, routers(100), True)
        e.update(130, routers(130, first=False), True)
        self.assertEqual(e.new_events, [])
        e.update(160, routers(160, first=False), True)
        self.assertEqual([x["type"] for x in e.new_events], ["outage"])
        e.update(190, routers(190, first=False), True)
        self.assertEqual(e.new_events, [])
        e.update(220, routers(220), True)
        e.update(250, routers(250), True)
        self.assertEqual([x["type"] for x in e.new_events], ["recovery"])

    def test_unknown_breaks_failure_streak(self):
        d = model.Debounce(2)
        d.update(True)
        d.update(False)
        d.update(None)
        self.assertEqual(d.update(False), (True, False))

    def test_resolves_chain_and_reports_unmapped_route_as_unknown(self):
        r = routers(100)
        self.assertEqual(model.active_providers("edge", r, config()["channels"]), ["first"])
        r["gateway"]["default_interfaces"] = ["ppp0"]
        self.assertEqual(model.active_providers("edge", r, config()["channels"]), ["second"])
        r["gateway"]["default_interfaces"] = ["eth9"]
        self.assertIsNone(model.active_providers("edge", r, config()["channels"]))

    def test_route_event_contains_old_and_new_provider(self):
        e = engine.Engine(config())
        e.update(100, routers(100), True)
        r = routers(130)
        r["gateway"]["default_interfaces"] = ["ppp0"]
        e.update(130, r, True)
        e.update(160, r, True)
        event = next(x for x in e.new_events if x["type"] == "route_changed")
        self.assertEqual(event["details"]["previous"], ["first"])
        self.assertEqual(event["details"]["current"], ["second"])

    def test_route_metric_and_multipath(self):
        self.assertEqual(
            model.parse_default_routes(
                "default via 192.0.2.1 dev eth0 metric 20\ndefault dev ppp0 metric 10"
            ),
            ["ppp0"],
        )
        self.assertEqual(
            model.parse_default_routes(
                "default nexthop via 192.0.2.1 dev eth0 weight 1 nexthop dev ppp0 weight 1"
            ),
            ["eth0", "ppp0"],
        )

    def test_history_excludes_unknown_and_unobserved_restart_gap(self):
        h = model.AvailabilityHistory()
        h.observe(100, True, 60)
        h.observe(130, False, 60)
        h.observe(160, None, 60)
        h.observe(190, True, 60)
        result = h.statistics(220, 60)
        self.assertAlmostEqual(result["availability_24h"], 66.667, places=3)
        self.assertEqual(result["downtime_24h"], 30)
        h.observe(1000, True, 60)
        self.assertLess(h.statistics(1000, 60)["coverage_24h"], 1)

    def test_notifications_filter_provider_and_event_type(self):
        settings = config()["settings"]
        self.assertTrue(engine.event_selected({"type": "outage", "subject": "first"}, settings))
        self.assertFalse(engine.event_selected({"type": "outage", "subject": "second"}, settings))
        self.assertFalse(engine.event_selected({"type": "degraded", "subject": "first"}, settings))
        self.assertFalse(
            engine.event_selected(
                {"type": "outage", "subject": "first"}, {**settings, "alert_channels": []}
            )
        )

    def test_rejects_command_injection_and_cycles(self):
        c = config()
        c["channels"][0]["interface"] = "eth0; reboot"
        with self.assertRaises(ValueError):
            model.validate_topology(c["routers"], c["channels"])
        c = config()
        c["channels"][1]["upstream_router"] = "edge"
        with self.assertRaisesRegex(ValueError, "topology_cycle"):
            model.validate_topology(c["routers"], c["channels"])

    def test_tls_certificate_failure_is_monitor_error(self):
        self.assertIsNone(model.parse_tls("", 60).success)
        self.assertFalse(model.parse_tls("", 28).success)
        self.assertTrue(model.parse_tls("WANWATCH 405 0.12", 0).success)

    def test_all_targets_must_fail_before_declaring_down(self):
        self.assertIsNone(model.reachability([model.Probe(False), model.Probe(None)]))
        self.assertTrue(model.reachability([model.Probe(True), model.Probe(None)]))
        self.assertFalse(model.reachability([model.Probe(False), model.Probe(False)]))


if __name__ == "__main__":
    unittest.main()
