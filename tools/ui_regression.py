"""Exercise real Windows/Tk/GDI paths without changing user preferences."""
import ctypes
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="desktop-monitor-ui-") as folder:
        os.environ["DM_CONFIG_PATH"] = str(Path(folder) / "config.json")
        loader = importlib.machinery.SourceFileLoader("monitor_ui_test", str(ROOT / "monitor.pyw"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mon = importlib.util.module_from_spec(spec)
        loader.exec_module(mon)
        app = mon.MonitorApp()
        failures = []
        app.root.report_callback_exception = lambda *exc: failures.append(exc)

        def pump(seconds=0.05):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                app.root.update()
                time.sleep(0.01)

        try:
            pump()
            app.set_mode("taskbar")
            strip = app.strip
            assert strip and strip.hwnd
            assert strip._proc(strip.hwnd, strip.WM_MOUSEACTIVATE, 0, 0) == strip.MA_NOACTIVATE
            # A release that started elsewhere must not toggle the taskbar popup.
            with patch.object(app, "ui_call") as enqueue:
                strip._proc(strip.hwnd, strip.WM_LBUTTONUP, 0, 0)
                strip._proc(strip.hwnd, strip.WM_RBUTTONUP, 0, 0)
                enqueue.assert_not_called()
                strip._proc(strip.hwnd, strip.WM_LBUTTONDOWN, 0, 0)
                strip._proc(strip.hwnd, strip.WM_LBUTTONUP, 0, 0)
                enqueue.assert_called_once_with("toggle")
                enqueue.reset_mock()
                strip._proc(strip.hwnd, strip.WM_RBUTTONDOWN, 0, 0)
                strip._proc(strip.hwnd, strip.WM_RBUTTONUP, 0, 0)
                enqueue.assert_called_once_with("menu")
            with patch.object(app.root, "withdraw") as withdraw, patch.object(mon._u32, "ShowWindow") as show:
                app.dock_now()
                app.dock_now()
                withdraw.assert_not_called()
                show.assert_not_called()
            # Exercise the actual menu canvas bindings, not only their callback functions.
            for label, tab in (("任务栏样式", "appearance"), ("展示排版", "appearance"),
                               ("自由选择指标", "metrics"), ("显示模式", "panel"), ("面板主题", "panel")):
                before = dict(app.cfg)
                app.show_menu(400, 100)
                pump(0.08)
                menu = app._menu_win
                app.show_menu(400, 100)  # Duplicate tray/context notifications keep one menu open.
                assert app._menu_win is menu and menu.winfo_exists()
                row_y = 8
                for kind, text, value, action, height in app._menu_rows_data:
                    if text == label:
                        row_y += height / 2
                        break
                    row_y += height
                else:
                    raise AssertionError(label)
                app._menu_canvas.event_generate("<Motion>", x=30, y=round(row_y * app._menu_scale))
                assert len(app._menu_canvas.find_all()) == 1
                app._menu_canvas.event_generate("<Button-1>", x=30, y=round(row_y * app._menu_scale))
                pump(0.08)
                assert app._menu_win is None
                assert app._set_win and app._set_win.winfo_exists() and app._settings_tab == tab, label
                assert app.cfg == before, (label, before, app.cfg)
                app._close_settings()
            app.show_detail()
            with patch.object(mon._u32, "GetAsyncKeyState", return_value=1), patch.object(app, "_owns_foreground", return_value=True):
                pump(0.3)
                assert app._popup_win, "The opening click must not dismiss the popup"
            # Both the strip and tray launchers must wait for button-up to toggle once.
            app.hide_detail()
            for mode in ("card", "mini", "tray", "taskbar", "card", "tray"):
                app.set_mode(mode)
                pump()
                app.dock_loop()
                pump()
                assert app.mode == mode
                if app.strip is not None:
                    assert bool(mon._u32.IsWindowVisible(app.strip.hwnd)) == (mode == "taskbar")
                if mode == "tray":
                    assert app.root.state() == "withdrawn"
            app.show_detail()
            pump()
            assert app.mode == "tray" and app._popup_win
            app.hide_detail()
            assert app.mode == "tray" and app.root.state() == "withdrawn"

            for theme in mon.CARD_THEMES:
                app.set_card_theme(theme)
                for mode in ("card", "mini"):
                    app.set_mode(mode)
                    pump()
                    assert app.canvas.find_all()
                app.set_mode("taskbar")
                app.show_detail()
                pump()
                app.card_action(330, 388, 1)
                pump()
                assert app._proc_expanded
                app.card_action(330, 388, 1)
                assert not app._proc_expanded
                timers = app.root.tk.splitlist(app.root.tk.call("after", "info"))
                assert len([t for t in timers if "_popup_watch" in str(app.root.tk.call("after", "info", t))]) == 1
                app.hide_detail()
            for tab in ("appearance", "metrics", "panel", "behavior", "about"):
                app.open_settings(tab)
                pump()
                assert app._set_win.winfo_exists()
            app._close_settings()
            # Arbitrary selection must resize every style without compressing its cells.
            for count in (1, 3, 6, len(mon.STRIP_METRICS)):
                items = list(mon.STRIP_METRICS)[:count]
                assert app.set_strip_items(items)
                for layout in mon.STRIP_LAYOUTS:
                    app.set_strip_option("strip_layout", layout)
                    for style in mon.STRIP_STYLES:
                        app.set_style(style)
                        assert app.strip.w == app.strip_w
                        painter = mon.StripPainter(app, app.strip_w, app.strip.h)
                        assert [m["key"] for m in painter.metrics] == items
                        image = app._compose_strip(app.strip_w, app.strip.h)
                        assert image.size == (app.strip_w, app.strip.h)
                saved = json.loads(Path(mon.CFG_PATH).read_text(encoding="utf-8"))
                assert mon.MonitorApp.load_cfg(app)["strip_items"] == saved["strip_items"] == items
            app.set_strip_option("strip_layout", "classic")
            app.set_style("minimal")
            app.open_settings("metrics")
            pump()
            assert app._settings_preview.width() == app.strip_w
            app._settings_preview_canvas.xview_moveto(1)
            assert app._settings_preview_canvas.xview()[0] > 0
            assert app.set_strip_items(["cpu"])
            pump()
            app._settings_metric_checks["cpu"].invoke()
            pump()
            assert app.cfg["strip_items"] == ["cpu"], "Last metric must remain selected"
            app._settings_metric_checks["down"].invoke()
            pump()
            assert app.cfg["strip_items"] == ["cpu", "down"]
            assert app._settings_preview.width() == app.strip_w
            order = app._settings_metric_order
            order.focus_force()
            order.event_generate("<Alt-Up>")
            pump()
            assert app.cfg["strip_items"] == ["down", "cpu"]
            app.set_strip_option("strip_accent", "blue")
            assert app.cfg["strip_items"] == ["down", "cpu"]
            app.set_strip_option("strip_profile", "balanced")
            assert app.cfg["strip_items"] == ["cpu", "mem", "up", "down"]
            app._close_settings()
            # Layout changes must resize the native strip while preserving docking and preferences.
            app.set_mode("taskbar")
            for layout in mon.STRIP_LAYOUTS:
                app.set_strip_option("strip_layout", layout)
                for style in mon.STRIP_STYLES:
                    app.set_style(style)
                    if app.strip:
                        assert app.strip.w == app.strip_w
                    for profile in mon.STRIP_PROFILES:
                        image = app._compose_strip(app.strip_w, round(48 * app.scale), strip_profile=profile)
                        assert image.mode == "RGBA" and image.size[0] == app.strip_w
            app.set_strip_option("strip_profile", "hardware")
            app.set_strip_option("strip_accent", "violet")
            app.set_strip_option("strip_size", "large")
            saved = json.loads(Path(mon.CFG_PATH).read_text(encoding="utf-8"))
            assert (saved["strip_profile"], saved["strip_accent"], saved["strip_size"]) == ("hardware", "violet", "large")
            app.open_settings("appearance")
            pump()
            app._settings_canvas.yview_moveto(0.5)
            pump()
            previous_scroll = app._settings_canvas.yview()[0]
            app.set_style("glass")
            pump()
            assert abs(app._settings_canvas.yview()[0] - previous_scroll) < 0.03, (previous_scroll, app._settings_canvas.yview())
            app._close_settings()
            app.set_card_theme("dark")
            app.set_mode("card")
            app.set_alpha(0.8)
            pump()
            assert abs(app.root.attributes("-alpha") - 0.8) < 0.01
            for interval in (500, 2000, 1000):
                app.set_interval(interval)
            timers = app.root.tk.splitlist(app.root.tk.call("after", "info"))
            tick_timers = [t for t in timers if "tick" in str(app.root.tk.call("after", "info", t))]
            assert len(tick_timers) == 1, tick_timers
            cfg = json.loads(Path(mon.CFG_PATH).read_text(encoding="utf-8"))
            assert cfg["alpha"] == 0.8 and cfg["interval"] == 1000

            for mode in ("taskbar", "card", "mini", "tray"):
                app.set_mode(mode)
                with patch.object(mon, "fullscreen_foreground", return_value=True), \
                     patch.object(mon, "same_monitor", return_value=True):
                    app._sync_fullscreen()
                    app.dock_now(force=True)
                    assert app._fs_hidden
                    assert app.root.state() == "withdrawn"
                    if app.strip:
                        assert not mon._u32.IsWindowVisible(app.strip.hwnd)
                with patch.object(mon, "fullscreen_foreground", return_value=False):
                    app._sync_fullscreen()
                assert app.mode == mode and not app._fs_hidden

            app.set_mode("taskbar")
            for edge in (0, 1, 2, 3):
                with patch.object(mon, "taskbar_position", return_value=(0, 0, 800, 60, edge)):
                    app.dock_now(force=True)
                    rect = app.card_rect()
                    assert rect[2] > 0 and rect[3] > 0
            app.set_mode("card")
            with patch.object(app, "work_area", return_value=(-1920, -900, 1920, 860)):
                x, y, w, h = app.card_rect()
                assert -1920 <= x <= -w and -900 <= y <= -40 - h

            app.set_mode("taskbar")
            mon._k32.GetCurrentProcess.restype = ctypes.c_void_p
            mon._u32.GetGuiResources.argtypes = [ctypes.c_void_p, mon.wt.DWORD]
            process = mon._k32.GetCurrentProcess()
            before = mon._u32.GetGuiResources(process, 0)
            if app.strip is not None:
                for _ in range(60):
                    app.strip.update(app._compose_strip(app.strip.w, app.strip.h))
            for i in range(60):
                app.tray.update_tip(f"Regression check {i}")
            after = mon._u32.GetGuiResources(process, 0)
            assert after - before < 5, (before, after)
            assert not failures, failures
            error_log = Path(folder) / "error.log"
            assert not error_log.exists(), error_log.read_text(encoding="utf-8") if error_log.exists() else ""
            print("UI SELFTEST OK: taskbar mouse messages, menu navigation, popup stability, modes, themes, metric selection/order/persistence, all layouts/styles, settings, timers, fullscreen, edges, GDI resources", flush=True)
        finally:
            app.quit()


if __name__ == "__main__":
    main()
