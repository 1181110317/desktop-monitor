"""Verify the built application in its output folder and in a Chinese path."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check(binary, config):
    env = os.environ.copy()
    env["DM_CONFIG_PATH"] = str(config)
    subprocess.run([str(binary), "--selftest"], env=env, cwd=binary.parent,
                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=30, check=True)
    error = config.parent / "error.log"
    if error.exists():
        raise RuntimeError(error.read_text(encoding="utf-8"))


if __name__ == "__main__":
    built = ROOT / "dist" / "DesktopMonitor"
    with tempfile.TemporaryDirectory(prefix="desktop-monitor-打包验证-") as folder:
        temp = Path(folder).resolve()
        # TemporaryDirectory cleans this exact, freshly created directory on exit.
        if temp.parent != Path(tempfile.gettempdir()).resolve():
            raise RuntimeError("Unexpected temporary directory")
        check(built / "DesktopMonitor.exe", temp / "original" / "config.json")
        copied = temp / "中文目录" / "DesktopMonitor"
        shutil.copytree(built, copied)
        check(copied / "DesktopMonitor.exe", temp / "unicode" / "config.json")
    print("PACKAGED SELFTEST OK: output folder and Chinese path")
