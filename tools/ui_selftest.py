import os, sys, time, io, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util
spec = importlib.util.spec_from_file_location("mon", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor.pyw"))
mon = importlib.util.module_from_spec(spec); spec.loader.exec_module(mon)

app = mon.MonitorApp()
root = app.root
root.update()
print("initial mode:", app.mode, "size:", root.winfo_width(), root.winfo_height(), "scale", round(app.scale, 2))
for m in ("taskbar", "card", "mini", "taskbar"):
    app.set_mode(m)
    root.update()
    print(f"  {m:8s} -> {root.winfo_width()}x{root.winfo_height()} at {root.winfo_x()},{root.winfo_y()}")
    assert app.mode == m and root.winfo_width() > 100
assert app.mode == "taskbar"
print("strip width (design):", app.strip_w)

# 菜单构建（不弹出）
menu = app.build_menu()
print("menu entries:", menu.index("end") + 1)
root.update()

# 层级切换
for layer in ("bottom", "normal", "top"):
    app.set_layer(layer)
    root.update()
    print("layer", layer, "topmost", root.wm_attributes("-topmost"))

# 配置持久化
app.cfg["alpha"] = 0.96
app.save_cfg()
cfg = json.load(open(mon.CFG_PATH, encoding="utf-8"))
print("config:", cfg)
assert cfg["layer"] == "top"

# 自启动开关（读状态即可，不改注册表）
print("autostart registered:", app.is_autostart())

# 连续采样 3 次
for i in range(3):
    app.tick(); root.update(); time.sleep(0.4)
print("state keys:", sorted(app.state.keys()))
print("cpu", round(app.state["cpu"],1), "mem%", round(app.state["mem_pct"],1),
      "down", round(app.state["down"]), "up", round(app.state["up"]),
      "gpu", app.state["gpu"].get("util"), "gputemp", app.state["gpu"].get("temp"))
app.quit()
print("UI SELFTEST OK")
