import os, time, importlib.util
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("mon", os.path.join(here, "monitor.pyw"))
mon = importlib.util.module_from_spec(spec); spec.loader.exec_module(mon)

def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)

app = mon.MonitorApp()

def step1():
    log(f"start mode={app.mode} state={app.root.state()} geom={app.root.geometry()}")
    app.show_detail()
    log(f"show_detail -> mode={app.mode} popup={app.popup} state={app.root.state()} geom={app.root.geometry()}")

def step2():
    log("step2 enter")
    app.hide_detail()
    log(f"hide_detail -> mode={app.mode} popup={app.popup} state={app.root.state()} geom={app.root.geometry()}")

def step3():
    log("step3 enter")
    app.set_mode("tray")
    log(f"set tray -> mode={app.mode} state={app.root.state()}")

def step4():
    app.toggle_detail()
    log(f"tray toggle -> mode={app.mode} popup={app.popup} state={app.root.state()} geom={app.root.geometry()}")

def step5():
    app.toggle_detail()
    log(f"toggle back -> mode={app.mode} popup={app.popup} state={app.root.state()}")

app.root.after(1200, step1)
app.root.after(5000, step2)
app.root.after(8000, step3)
app.root.after(10000, step4)
app.root.after(14000, step5)
app.root.after(17000, app.quit)
app.root.mainloop()
log("done")
