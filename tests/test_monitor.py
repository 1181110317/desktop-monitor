"""Windows regressions for native buffers, counters and configuration."""
import ctypes
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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


class TaskbarInteractionTests(unittest.TestCase):
    def test_choice_menu_entries_open_settings_instead_of_cycling_values(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.mode, app.style, app.card_theme = "taskbar", "glass", "dark"
        app.popup = app.locked = False
        app.cfg = {**mon.STRIP_DEFAULTS, "style": "glass", "strip_items": ["cpu", "mem", "up", "down"]}
        app.open_settings, app.cycle = Mock(), Mock()
        rows = {row[1]: row for row in app.menu_rows()}
        for label, tab in (("任务栏样式", "appearance"), ("展示排版", "appearance"),
                           ("自由选择指标", "metrics"), ("显示模式", "panel"), ("面板主题", "panel")):
            with self.subTest(label=label):
                app.open_settings.reset_mock()
                rows[label][3]()
                app.open_settings.assert_called_once_with(tab)
        app.cycle.assert_not_called()
        self.assertEqual((app.style, app.cfg["strip_layout"]), ("glass", "classic"))

    def popup(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.root, app.hide_detail = Mock(), Mock()
        app._popup_win, app._closing, app._mouse_down = True, False, False
        app._inside_popup = Mock(return_value=False)
        app._inside_launcher = Mock(return_value=False)
        app._owns_foreground = Mock(return_value=True)
        return app

    def test_completed_click_cannot_dismiss_new_popup(self):
        app = self.popup()
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=1):
            app._popup_watch()
        app.hide_detail.assert_not_called()
        app.root.after.assert_called_once()

    def test_new_outside_press_dismisses_popup(self):
        app = self.popup()
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=0x8000):
            app._popup_watch()
        app.hide_detail.assert_called_once()
        app.root.after.assert_not_called()

    def test_completed_click_cannot_dismiss_new_menu(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.root, app.close_menu, app._menu_win = Mock(), Mock(), Mock()
        app._closing, app._menu_mouse_down = False, False
        app._menu_rect, app._menu_ts = (-100, -100, 1, 1), 0
        app._owns_foreground = Mock(return_value=True)
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=1):
            app._menu_watch()
        app.close_menu.assert_not_called()

    def test_fast_outside_click_is_dismissed_on_focus_change(self):
        app = self.popup()
        app._popup_focus_owned = True
        app._owns_foreground.return_value = False
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=0):
            app._popup_watch()
        app.hide_detail.assert_called_once()

    def test_tray_click_waits_for_toggle_instead_of_dismissing_on_mouse_down(self):
        app = self.popup()
        app._popup_focus_owned = True
        app._owns_foreground.return_value = False
        app._inside_popup.return_value = app._inside_launcher.return_value = True
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=0x8000):
            app._popup_watch()
        app.hide_detail.assert_not_called()

    def test_launcher_bounds_include_the_tray_icon(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.mode, app._tb_rect = "tray", None
        app.tray = SimpleNamespace(rect=lambda: (100, 200, 24, 24))
        self.assertTrue(app._inside_launcher(110, 210))
        self.assertFalse(app._inside_launcher(99, 210))

    def test_strip_show_never_activates_and_is_idempotent(self):
        strip = mon.LayeredStrip(SimpleNamespace())
        strip.hwnd = 99
        with patch.object(mon._u32, "IsWindowVisible", return_value=False), \
             patch.object(mon._u32, "ShowWindow") as show:
            strip.show()
        show.assert_called_once_with(99, 8)  # SW_SHOWNA
        with patch.object(mon._u32, "IsWindowVisible", return_value=True), \
             patch.object(mon._u32, "ShowWindow") as show:
            strip.show()
        show.assert_not_called()

    def test_strip_does_not_reorder_itself_when_already_on_top(self):
        strip = mon.LayeredStrip(SimpleNamespace())
        strip.hwnd, strip.x, strip.y, strip.w, strip.h = 99, 10, 20, 200, 40
        with patch.object(mon._u32, "IsWindowVisible", return_value=True), \
             patch.object(mon._u32, "WindowFromPoint", return_value=99), \
             patch.object(mon, "taskbar_hwnd", return_value=88), \
             patch.object(mon._u32, "IsChild", return_value=False), \
             patch.object(mon._u32, "SetWindowPos") as move:
            strip.raise_top()
        move.assert_not_called()

    def test_strip_recovers_only_when_covered_by_the_taskbar(self):
        strip = mon.LayeredStrip(SimpleNamespace())
        strip.hwnd, strip.x, strip.y, strip.w, strip.h = 99, 10, 20, 200, 40
        for covering, expected in ((88, 1), (77, 0)):
            with patch.object(mon._u32, "IsWindowVisible", return_value=True), \
                 patch.object(mon._u32, "WindowFromPoint", return_value=covering), \
                 patch.object(mon, "taskbar_hwnd", return_value=88), \
                 patch.object(mon._u32, "IsChild", return_value=False), \
                 patch.object(mon._u32, "SetWindowPos") as move:
                strip.raise_top()
                self.assertEqual(move.call_count, expected)

    def test_popup_is_drawn_before_becoming_visible(self):
        app = self.popup()
        app._popup_after, app.card_win = None, Mock()
        app._ensure_card_win, app.layout_popup, app.apply_card_alpha = Mock(), Mock(), Mock()
        events = []
        app.draw_card = lambda: events.append("draw")
        app.card_win.deiconify.side_effect = lambda: events.append("show")
        with patch.object(mon._u32, "GetAsyncKeyState", return_value=0):
            app.show_popup()
        self.assertEqual(events, ["draw", "show"])


class TaskbarDockingTests(unittest.TestCase):
    @staticmethod
    def widgets_state(**values):
        keys = ("started", "stop", "key", "rect", "stamp")
        saved = {key: mon._WIDGETS[key] for key in keys}
        mon._WIDGETS.update(values)

        class Restore:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                mon._WIDGETS.update(saved)

        return Restore()

    def test_strip_docks_left_of_a_weather_button_that_sits_next_to_the_tray(self):
        tray_left, width, margin = 3282, 195, 12
        self.assertEqual(mon.dock_right_x(0, width, margin, tray_left),
                         tray_left - margin - width)
        self.assertEqual(mon.dock_right_x(0, width, margin, tray_left, (3036, 2088, 228, 72)),
                         3036 - margin - width)

    def test_far_or_invalid_widgets_keep_the_regular_tray_anchor(self):
        tray_left, width, margin = 2188, 195, 12
        expected = tray_left - margin - width
        for widgets in ((200, 1392, 152, 48),        # widgets button at the far left
                        (1500, 1392, 152, 48),       # not adjacent to the tray
                        (2188, 1392, 152, 48),       # starts at the tray
                        (2100, 1392, -1, 48)):      # unusable rectangle
            with self.subTest(widgets=widgets):
                self.assertEqual(mon.dock_right_x(0, width, margin, tray_left, widgets), expected)

    def test_docking_never_leaves_the_taskbar_when_space_is_tight(self):
        self.assertEqual(mon.dock_right_x(0, 2000, 12, 1500, (1300, 1392, 152, 48)), 12)

    def test_weather_lookup_is_skipped_before_windows_11(self):
        with patch.object(mon.sys, "getwindowsversion", return_value=SimpleNamespace(build=19045)), \
             patch.object(mon, "_uia_widget_rect") as lookup:
            self.assertIsNone(mon.taskbar_widgets_rect())
        lookup.assert_not_called()

    def test_taskbar_docking_reads_the_background_measurement(self):
        with self.widgets_state(started=True, rect=(3036, 2088, 228, 72)), \
             patch.object(mon.sys, "getwindowsversion", return_value=SimpleNamespace(build=22631)), \
             patch.object(mon, "_uia_widget_rect") as lookup:
            self.assertEqual(mon.taskbar_widgets_rect(), (3036, 2088, 228, 72))
        lookup.assert_not_called()

    def test_widgets_watch_reuses_a_fresh_layout_and_remeasures_changes(self):
        now = time.monotonic()
        with self.widgets_state(started=True, key=("old",), rect=(1, 2, 3, 4), stamp=now), \
             patch.object(mon, "_taskbar_widgets_key", return_value=("old",)), \
             patch.object(mon, "_uia_widget_rect") as lookup:
            mon._widgets_watch_tick()
            lookup.assert_not_called()
        with self.widgets_state(started=True, key=("old",), rect=(1, 2, 3, 4), stamp=now), \
             patch.object(mon, "_taskbar_widgets_key", return_value=("new", 7)), \
             patch.object(mon, "_uia_widget_rect", return_value=(50, 60, 70, 80)) as lookup:
            mon._widgets_watch_tick()
            lookup.assert_called_once_with("new")
            self.assertEqual(mon._WIDGETS["rect"], (50, 60, 70, 80))
            self.assertEqual(mon._WIDGETS["key"], ("new", 7))

    def test_widgets_watch_remeasures_after_the_safety_interval(self):
        with self.widgets_state(started=True, key=("same",), rect=(1, 2, 3, 4), stamp=0.0), \
             patch.object(mon, "_taskbar_widgets_key", return_value=("same",)), \
             patch.object(mon, "_uia_widget_rect", return_value=None) as lookup:
            mon._widgets_watch_tick()
            lookup.assert_called_once_with("same")


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

    def test_old_presets_migrate_to_editable_items(self):
        for profile, (_, items) in mon.STRIP_PROFILES.items():
            cfg = self.load(json.dumps({"strip_profile": profile, "strip_size": "large"}))
            self.assertEqual(cfg["strip_items"], list(items))
            self.assertEqual(cfg["strip_profile"], profile)
            self.assertEqual(cfg["strip_size"], "large")

    def test_custom_order_survives_loading_and_round_trip(self):
        cfg = self.load(json.dumps({"strip_profile": "custom", "strip_items": ["down", "vram", "cpu_temp"]}))
        self.assertEqual(cfg["strip_profile"], "custom")
        self.assertEqual(cfg["strip_items"], ["down", "vram", "cpu_temp"])
        self.assertEqual(self.load(json.dumps(cfg))["strip_items"], cfg["strip_items"])

    def test_duplicate_unknown_and_malformed_items_are_removed(self):
        cfg = self.load(json.dumps({"strip_items": ["gpu", "bogus", "gpu", {}, None, "disk"]}))
        self.assertEqual(cfg["strip_items"], ["gpu", "disk"])
        self.assertEqual(cfg["strip_profile"], "custom")
        for value in ([], None, "cpu", {"cpu": True}, ["bogus"]):
            cfg = self.load(json.dumps({"strip_profile": "network", "strip_items": value}))
            self.assertEqual(cfg["strip_items"], list(mon.STRIP_PROFILES["network"][1]))

    def test_items_take_precedence_over_a_stale_preset(self):
        cfg = self.load(json.dumps({"strip_profile": "hardware", "strip_items": ["cpu"]}))
        self.assertEqual((cfg["strip_profile"], cfg["strip_items"]), ("custom", ["cpu"]))


class TaskbarMeasurementsTests(unittest.TestCase):
    def test_custom_metrics_use_the_requested_order_and_existing_readings(self):
        state = {"gpu": {"mem_used": 2048, "mem_total": 8192}, "cpu_temp": 65,
                 "disk_total": 1000, "disk_free": 400, "cpu": 0}
        keys = ["disk", "cpu_temp", "vram", "cpu"]
        metrics = mon.strip_metrics(state, items=keys)
        self.assertEqual([m["key"] for m in metrics], keys)
        self.assertEqual([(m["value"], m["unit"]) for m in metrics], [("60", "%"), ("65", "°C"), ("25", "%"), ("0", "%")])

    def test_unavailable_or_nonfinite_new_sensors_show_placeholder(self):
        state = {"cpu_temp": float("nan"), "gpu": {"mem_used": 1, "mem_total": 0}, "disk_total": 0, "disk_free": 0}
        metrics = mon.strip_metrics(state, items=["cpu_temp", "vram", "disk"])
        self.assertTrue(all(m["value"] == "—" and m["level"] is None for m in metrics))

    def test_explicit_profile_preview_ignores_custom_selection(self):
        cfg = {"strip_profile": "custom", "strip_items": ["cpu"]}
        self.assertEqual(mon.selected_strip_items(cfg, strip_profile="network"), list(mon.STRIP_PROFILES["network"][1]))

    def test_classic_any_item_count_preserves_fonts_and_removes_empty_columns(self):
        app = mon.MonitorApp.__new__(mon.MonitorApp)
        app.scale, app._font_cache, app.state = 1.5, {}, {}
        app.cfg = {**mon.STRIP_DEFAULTS}
        widths = []
        for count in (1, 3, 6, len(mon.STRIP_METRICS)):
            items = list(mon.STRIP_METRICS)[:count]
            app.cfg["strip_items"] = items
            geometry = app._classic_geometry()
            self.assertEqual((geometry["label_px"], geometry["value_px"]), (13, 19))
            self.assertEqual([m["key"] for m in geometry["records"]], items)
            self.assertEqual(len(geometry["columns"]), (count + 1) // 2)
            self.assertEqual(len(geometry["separators"]), len(geometry["columns"]) - 1)
            self.assertEqual(sum(c["count"] for c in geometry["columns"]), count)
            self.assertEqual(geometry["height"], 24 if count == 1 else 40)
            widths.append(geometry["width"])
        self.assertEqual(widths, sorted(set(widths)))

    def test_layout_cells_do_not_shrink_when_more_metrics_are_selected(self):
        for layout in ("micro", "duo", "inline", "gauges", "trends"):
            reference = 2 if layout in ("micro", "duo") else 4
            gap = 2 if layout == "micro" else (5 if layout == "duo" else 0)
            expected = (mon.STRIP_LAYOUTS[layout][1] - 4 - gap * (reference - 1)) / reference
            for count in (1, 3, 6, 13):
                columns = (count + 1) // 2 if reference == 2 else count
                cell = (mon.strip_design_width(layout, count) - 4 - gap * (columns - 1)) / columns
                self.assertAlmostEqual(cell, expected)

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
