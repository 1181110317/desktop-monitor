"""Build the same self-contained Windows executable locally and in CI."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Build DesktopMonitor.exe on Windows.")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--windowed", "--name", "DesktopMonitor", "--icon", str(ROOT / "icon.ico"),
        "--add-data", f"{ROOT / 'icon.ico'};.",
        "--add-data", f"{ROOT / 'icon.png'};.", str(ROOT / "monitor.pyw"),
    ], cwd=ROOT, check=True)
    print(ROOT / "dist" / "DesktopMonitor.exe")
