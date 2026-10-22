import importlib
import unittest

from test_model import constants, model

resources = importlib.import_module("ww.resources")
download = importlib.import_module("ww.download")
commands = importlib.import_module("ww.commands")


class ResourceDownloadTests(unittest.TestCase):
    def test_cpu_uses_interval_excludes_guest_and_rejects_reboot(self):
        old = resources.parse_resources("cpu 100 0 100 800 0 0 0 0 50 0\n")
        new = resources.parse_resources("cpu 150 0 150 900 0 0 0 0 80 0\n")
        old["uptime"], new["uptime"] = 10, 20
        self.assertEqual(resources.cpu_percent(old, new), 50)
        self.assertIsNone(resources.cpu_percent({}, new))
        new["uptime"] = 5
        self.assertIsNone(resources.cpu_percent(old, new))

    def test_memory_available_and_old_kernel_fallback(self):
        data = resources.parse_resources(
            "MemTotal: 102400 kB\nMemAvailable: 25600 kB\n0.75 0.4 0.3 1/200 400\n"
        )
        self.assertEqual(data["memory_used_percent"], 75)
        self.assertEqual(data["memory_available"], 25)
        self.assertEqual(data["load_1m"], 0.75)
        data = resources.parse_resources(
            "MemTotal: 100 kB\nMemFree: 20 kB\nBuffers: 10 kB\n"
            "Cached: 40 kB\nShmem: 5 kB\nSReclaimable: 5 kB\n"
        )
        self.assertEqual(data["memory_used_percent"], 30)
        self.assertEqual(resources.parse_resources("MemTotal: 100 kB\n"), {})

    def test_complete_download_only_partial_or_error_is_not_zero_speed(self):
        self.assertEqual(
            download.parse_download("WANDOWN 200 5000000 2500000 2", 0, 5000000)["download_mbps"],
            20,
        )
        for text, code in [
            ("WANDOWN 403 1 1 1", 0),
            ("WANDOWN 200 100 100 1", 0),
            ("WANDOWN 200 5000000 2500000 2", 28),
            ("", 126),
        ]:
            result = download.parse_download(text, code, 5000000)
            self.assertIsNone(result["download_mbps"])
            self.assertEqual(result["download_status"], "failed")

    def test_download_has_strict_interface_size_and_time_bounds(self):
        args = commands.download_command("ppp0", constants.DEFAULT_DOWNLOAD_TARGET, 5000000)
        for flag, value in [
            ("--interface", "if!ppp0"),
            ("--max-filesize", "5000000"),
            ("--max-time", "20"),
            ("--range", "0-4999999"),
            ("--noproxy", "*"),
        ]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertNotIn("--location", args)
        self.assertNotIn("--retry", args)
        model.validate_download_target(constants.DEFAULT_DOWNLOAD_TARGET)
        for path in ["/x;id?bytes={bytes}", "/x?b={other}"]:
            with self.assertRaises(ValueError):
                model.validate_download_target({**constants.DEFAULT_DOWNLOAD_TARGET, "path": path})

    def test_short_window_clips_old_outage_and_tracks_unknown_time(self):
        history = model.AvailabilityHistory([[0, False], [1000, True], [1500, None], [1800, True]])
        stats = history.statistics(2000, 1000, 1200)
        self.assertEqual(stats["downtime_20m"], 200)
        self.assertAlmostEqual(stats["availability_20m"], 700 / 9, places=3)
        self.assertEqual(stats["coverage_20m"], 75)

    def test_upload_source_is_bounded_and_contains_no_local_files(self):
        import shlex

        args = commands.upload_command("keenetic", "ppp0", constants.DEFAULT_UPLOAD_TARGET, 5000000)
        self.assertEqual(args[:2], ["sh", "-c"])
        source, request = args[2].split(" | ")
        self.assertEqual(
            shlex.split(source), ["/opt/bin/busybox", "head", "-c", "5000000", "/dev/zero"]
        )
        curl = shlex.split(request)
        self.assertEqual(curl[curl.index("--interface") + 1], "if!ppp0")
        self.assertEqual(curl[curl.index("--data-binary") + 1], "@-")
        self.assertEqual(curl[curl.index("--max-time") + 1], "20")
