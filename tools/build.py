"""Build a portable Windows folder and ZIP, including Unicode-path support."""
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("Build DesktopMonitor.exe on Windows.")
    output = (ROOT / "dist" / "DesktopMonitor").resolve()
    if not output.is_relative_to(ROOT.resolve()):
        raise SystemExit("Build output must stay inside the repository.")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--windowed", "--name", "DesktopMonitor", "--icon", str(ROOT / "icon.ico"),
        "--add-data", f"{ROOT / 'icon.ico'};.",
        "--add-data", f"{ROOT / 'icon.png'};.", str(ROOT / "monitor.pyw"),
    ], cwd=ROOT, check=True)
    archive = shutil.make_archive(str(ROOT / "dist" / "DesktopMonitor-Windows-x64"),
                                  "zip", ROOT / "dist", "DesktopMonitor")
    print(output / "DesktopMonitor.exe")
    print(archive)
