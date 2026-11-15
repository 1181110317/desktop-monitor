"""Windows regressions for native buffers, counters and configuration."""
import ctypes
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if sys.platform != "win32":
    raise unittest.SkipTest("Desktop Monitor uses Windows APIs")

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("monitor_under_test", str(ROOT / "monitor.pyw"))
spec = importlib.util.spec_from_loader(loader.name, loader)
mon = importlib.util.module_from_spec(spec)
loader.exec_module(mon)


class NativeBufferTests(unittest.TestCase):
    def test_failed_dib_allocation_releases_dcs_without_writing(self):
        with patch.object(mon._u32, "GetDC", return_value=10), \
             patch.object(mon._u32, "ReleaseDC") as release, \
             patch.object(mon._g32, "CreateCompatibleDC", return_value=20), \
             patch.object(mon._g32, "CreateDIBSection", return_value=0), \
             patch.object(mon._g32, "SelectObject") as select, \
             patch.object(mon._g32, "DeleteDC") as delete:
            with self.assertRaises(OSError):
                with mon.dib_surface(20, 20):
                    self.fail("Invalid buffers must never be exposed")
            select.assert_not_called()
            delete.assert_called_once_with(20)
            release.assert_called_once_with(0, 10)

    def test_failed_memory_dc_releases_screen_dc(self):
        with patch.object(mon._u32, "GetDC", return_value=10), \
             patch.object(mon._u32, "ReleaseDC") as release, \
             patch.object(mon._g32, "CreateCompatibleDC", return_value=0):
            with self.assertRaises(OSError):
                with mon.dib_surface(20, 20):
                    pass
            release.assert_called_once_with(0, 10)

    def test_layered_pixels_are_premultiplied_bgra(self):
        from contextlib import contextmanager
        from PIL import Image
        storage = ctypes.create_string_buffer(4)
        @contextmanager
        def fake_surface(w, h):
            yield 1, 2, ctypes.cast(storage, ctypes.c_void_p)
        strip = mon.LayeredStrip(SimpleNamespace())
        strip.hwnd = 99
        with patch.object(mon, "dib_surface", fake_surface), \
             patch.object(mon._u32, "UpdateLayeredWindow", return_value=True):
            strip.update(Image.new("RGBA", (1, 1), (255, 128, 64, 128)))
        self.assertEqual(storage.raw, bytes((32, 64, 128, 128)))

    def test_move_only_does_not_allocate_uninitialized_bitmap(self):
        strip = mon.LayeredStrip(SimpleNamespace())
        strip.hwnd = 99
        with patch.object(mon, "dib_surface") as allocate, patch.object(mon._u32, "SetWindowPos") as move:
            strip.update(None, -100, 20)
        allocate.assert_not_called()
        self.assertEqual(move.call_args.args[2:4], (-100, 20))


class NetworkTests(unittest.TestCase):
    def test_native_row_layout_matches_windows_abi(self):
        self.assertEqual(mon.MIB_IF_ROW2.InOctets.offset, 1208)
        self.assertEqual(mon.MIB_IF_ROW2.OutOctets.offset, 1280)
        self.assertEqual(ctypes.sizeof(mon.MIB_IF_ROW2), 1352)

    def test_equal_counters_on_distinct_adapters_are_both_counted(self):
        stride = ctypes.sizeof(mon.MIB_IF_ROW2)
        buffer = ctypes.create_string_buffer(8 + stride * 3)
        ctypes.cast(buffer, ctypes.POINTER(mon.wt.ULONG))[0] = 3
        for index, (alias, luid, flags) in enumerate((("WLAN", 1, 1), ("Ethernet", 2, 1), ("Filter", 3, 3))):
            row = mon.MIB_IF_ROW2.from_buffer(buffer, 8 + index * stride)
            row.Alias, row.InterfaceLuid.Value = alias, luid
            row.Type, row.Mtu, row.OperStatus, row.Flags = 6, 1500, 1, flags
            row.InOctets, row.OutOctets, row.InUcastPkts = 10000, 7000, 55
        def get_table(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            return 0
        with patch.object(mon._iphlp, "GetIfTable2", side_effect=get_table), \
             patch.object(mon._iphlp, "FreeMibTable") as free:
            rx, tx, names = mon.net_io()
        self.assertEqual((rx, tx), (20000, 14000))
        self.assertEqual(names, ["WLAN", "Ethernet"])
        free.assert_called_once()

    def test_adapter_switch_does_not_invent_speed_or_totals(self):
        stats = mon.Stats.__new__(mon.Stats)
        stats.prev_cpu, stats.prev_net, stats.net_names = (0, 0, 0), (100, 50, 1), ["WLAN"]
        stats.peak_cpu = stats.peak_mem = stats.total_down = stats.total_up = 0
        stats.cores = 8
        stats.gpu = SimpleNamespace(snapshot=lambda: {})
        stats.temp = SimpleNamespace(snapshot=lambda: (None, ""))
        stats.proc = SimpleNamespace(snapshot=lambda: ([], 0))
        with patch.object(mon, "net_io", return_value=(10 ** 12, 10 ** 12, ["Ethernet"])), \
             patch.object(mon, "cpu_times", return_value=(10, 30, 20)), \
             patch.object(mon.time, "monotonic", return_value=2):
            state = stats.sample()
        self.assertEqual((state["down"], state["up"], state["total_down"], state["total_up"]), (0, 0, 0, 0))


class ConfigurationTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(value, encoding="utf-8")
            with patch.object(mon, "CFG_PATH", str(path)):
                return mon.MonitorApp.load_cfg(None)

    def test_non_objects_and_truncated_json_use_defaults(self):
        for text in ('[]', 'null', '"text"', '{"mode":'):
            with self.subTest(text=text):
                cfg = self.load(text)
                self.assertEqual((cfg["mode"], cfg["interval"], cfg["alpha"]), ("taskbar", 1000, 1.0))

    def test_invalid_values_are_sanitized_before_creating_windows(self):
        cfg = self.load(json.dumps({"mode": {}, "card_theme": "bad", "interval": "oops",
                                   "alpha": float("nan"), "x": "outside", "y": float("inf"), "locked": "false"}))
        self.assertEqual((cfg["mode"], cfg["card_theme"], cfg["interval"], cfg["alpha"]), ("taskbar", "dark", 1000, 1.0))
        self.assertIsNone(cfg["x"])
        self.assertIsNone(cfg["y"])
        self.assertFalse(cfg["locked"])

    def test_saved_tray_mode_survives_restart(self):
        self.assertEqual(self.load('{"mode":"tray"}')["mode"], "tray")


class ThreadLifecycleTests(unittest.TestCase):
    def test_workers_can_be_stopped_and_joined(self):
        for cls in (mon.GpuReader, mon.TempReader, mon.ProcReader):
            with self.subTest(worker=cls.__name__):
                worker = cls()
                worker.stop()
                worker.start()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())


class TaskbarPreferencesTests(unittest.TestCase):
    load = ConfigurationTests.load

    def test_legacy_styles_migrate_without_losing_other_preferences(self):
        for old, new in (("text", "minimal"), ("pill", "glass"), ("outline", "outline")):
            cfg = self.load(json.dumps({"style": old, "interval": 2000, "card_theme": "light"}))
            self.assertEqual((cfg["style"], cfg["interval"], cfg["card_theme"]), (new, 2000, "light"))

    def test_invalid_taskbar_options_use_supported_defaults(self):
        cfg = self.load(json.dumps({"style": [], "strip_layout": {}, "strip_profile": "unknown",
                                    "strip_accent": None, "strip_size": 100, "strip_indicators": "false"}))
        for key, default in mon.STRIP_DEFAULTS.items():
            self.assertEqual(cfg[key], default)
        self.assertTrue(cfg["strip_indicators"])


class TaskbarMeasurementsTests(unittest.TestCase):
    def test_zero_gpu_and_empty_battery_are_not_missing(self):
        state = {"cpu": 0, "mem_pct": 0, "gpu": {"util": 0, "temp": None}, "battery": {"pct": 0}}
        hardware = mon.strip_metrics(state, "hardware")
        self.assertEqual([metric["value"] for metric in hardware], ["0", "0", "0", "—"])
        self.assertEqual(mon.strip_metrics(state, "battery")[2]["value"], "0")

    def test_network_profile_distinguishes_rates_from_session_totals(self):
        metrics = mon.strip_metrics({"down": 12_500_000, "up": 840_000, "total_down": 1024 ** 3, "total_up": 0}, "network")
        self.assertEqual([(m["value"], m["unit"]) for m in metrics],
                         [("12.5", "MB/s"), ("840", "KB/s"), ("1.0", "GB"), ("0", "B")])

    def test_classic_spacing_never_reduces_original_font_sizes(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.scale, app._font_cache = 1.5, {}
        app.cfg = {**mon.STRIP_DEFAULTS, "strip_profile": "balanced"}
        app.state = {"cpu": 100, "mem_pct": 68, "up": 840000, "down": 12600000}
        widths = []
        for spacing in mon.STRIP_SIZES:
            app.cfg["strip_size"] = spacing
            geometry = app._classic_geometry()
            self.assertEqual((geometry["label_px"], geometry["value_px"]), (13, 19))
            self.assertEqual([m["key"] for m in geometry["records"]], ["cpu", "mem", "up", "down"])
            widths.append(geometry["width"])
        self.assertLess(widths[0], widths[-1])


if __name__ == "__main__":
    unittest.main()
