import importlib
import shlex
import unittest

from test_model import constants

try:
    import asyncssh
except ImportError as exc:
    raise unittest.SkipTest("asyncssh is not installed") from exc

transport = importlib.import_module("ww.transport")


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return False


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.commands = []
        self.ping_error = False
        self.ping_timeout = False
        self.tls_failure = False
        self.server = await asyncssh.create_server(
            Server, "127.0.0.1", 0, server_host_keys=[self.key], process_factory=self.process
        )
        self.router = {
            "driver": "linux",
            "host": "127.0.0.1",
            "port": self.server.get_port(),
            "username": "test",
            "host_key": self.key.get_fingerprint(),
        }
        self.channel = {"id": "standby", "interface": "ppp0"}

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def process(self, process):
        args = shlex.split(process.command)[1:]
        self.commands.append(args)
        code = 0
        if args == ["ip", "route", "show", "default"]:
            text = "default dev eth0 metric 1000\n"
        elif args == ["cat", "/proc/net/dev"]:
            text = "ppp0: 100 1 0 0 0 0 0 0 200 1 0 0 0 0 0 0\n"
        elif args == ["cat", "/proc/uptime"]:
            text = "1234.0 5678.0\n"
        elif args[:3] == ["ip", "link", "show"]:
            text = "2: ppp0: <POINTOPOINT,UP,LOWER_UP> state UNKNOWN\n"
        elif args[0] == "curl":
            code = 28 if self.tls_failure else 0
            text = "WANWATCH 200 0.03\n" if not code else ""
        elif args[0] == "ping":
            if self.ping_timeout:
                process.close()
                return
            code = 127 if self.ping_error else 0
            text = (
                ""
                if code
                else (
                    "5 packets transmitted, 5 packets received, 0% packet loss\n"
                    "round-trip min/avg/max = 1.0/3.0/5.0 ms\n"
                )
            )
        else:
            text, code = "", 126
        process.stdout.write(text)
        process.exit(code)

    async def collect(self):
        return await transport.collect_router(
            self.router, [self.channel], constants.DEFAULT_TARGETS, 5
        )

    async def test_pins_server_key_and_rejects_changed_key(self):
        conn = await transport.connect(self.router)
        conn.close()
        await conn.wait_closed()
        wrong = {
            **self.router,
            "host_key": asyncssh.generate_private_key("ssh-ed25519").get_fingerprint(),
        }
        with self.assertRaises(asyncssh.HostKeyNotVerifiable):
            await transport.connect(wrong)

    async def test_standby_probes_are_interface_bound_for_every_target(self):
        result = await self.collect()
        sample = result["channels"]["standby"]
        self.assertTrue(sample.reachable)
        self.assertTrue(sample.quality.valid)
        self.assertEqual(sample.counters["rx_bytes"], 100)
        self.assertEqual(result["default_interfaces"], ["eth0"])
        probes = [c for c in self.commands if c[0] in ("ping", "curl")]
        self.assertEqual(len(probes), 2 * len(constants.DEFAULT_TARGETS))
        for args in probes:
            flag = "-I" if args[0] == "ping" else "--interface"
            self.assertEqual(args[args.index(flag) + 1], "ppp0")

    async def test_ping_execution_failure_does_not_erase_tls_success(self):
        self.ping_error = True
        sample = (await self.collect())["channels"]["standby"]
        self.assertTrue(sample.reachable)
        self.assertFalse(sample.quality.valid)

    async def test_ping_session_failure_does_not_erase_tls_success(self):
        self.ping_timeout = True
        sample = (await self.collect())["channels"]["standby"]
        self.assertTrue(sample.reachable)
        self.assertFalse(sample.quality.valid)

    async def test_tls_failure_does_not_erase_quality_measurement(self):
        self.tls_failure = True
        sample = (await self.collect())["channels"]["standby"]
        self.assertFalse(sample.reachable)
        self.assertTrue(sample.quality.valid)
