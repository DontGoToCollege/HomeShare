"""Build HomeShare.exe from homeshare.py and keep every version.

    python build.py

Runs the tests, then puts the new exe at HomeShare.exe and a copy at Versions/Archive/HomeShare-<VERSION>.exe.
Raise VERSION in homeshare.py first: a version that's already archived is refused, so no build is ever lost.
A running HomeShare is closed first (the owner's standing wish), since Windows won't replace an exe that's in use,
and the new version is started again afterwards.
"""
import re, shutil, subprocess, sys, tempfile, time, urllib.request, venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBS = ["pyinstaller", "pillow", "pystray", "segno", "mutagen", "pypdfium2"]
WORK = Path(tempfile.gettempdir()) / "hs-build"  # short path: pip fails in very long folder names


def run(*cmd, cwd=WORK):
    subprocess.run([str(c) for c in cmd], cwd=cwd, check=True)


def homeshare_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HomeShare.exe", "/NH"], capture_output=True, text=True).stdout
    return "HomeShare.exe" in out


def close_homeshare():
    """Close HomeShare like its Close button does (so open pages show "closed"), and force it if it won't go.
    Returns whether it was running."""
    if not homeshare_running():
        return False
    for port in (80, 8080, 8000, 8888):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/stop", data=b"{}", headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=2)
            break
        except OSError:
            pass
    for _ in range(20):
        if not homeshare_running():
            break
        time.sleep(0.5)
    else:
        subprocess.run(["taskkill", "/F", "/IM", "HomeShare.exe"], capture_output=True)
        time.sleep(1)
    print("Closed the running HomeShare so the new version could replace it.")
    return True


def start_homeshare(exe):
    """Start HomeShare via Windows (WMI) rather than as this script's child, so it keeps running after the build.
    --background: pages that were open reconnect by themselves, so no extra browser tab."""
    cmd = f'"{exe}" --background'
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{cmd}'}}"],
                   capture_output=True, check=True)


version = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"', (HERE / "homeshare.py").read_text("utf-8"), re.M).group(1)
archive = HERE / "Versions" / "Archive" / f"HomeShare-{version}.exe"
if archive.exists():
    sys.exit(f"Version {version} is already archived. Raise VERSION in homeshare.py, then build again.")

py = WORK / "venv" / "Scripts" / "python.exe"
if not py.exists():
    venv.create(WORK / "venv", with_pip=True)
    run(py, "-m", "pip", "install", "-q", *LIBS)
run(py, "test_homeshare.py", cwd=HERE)  # never ship a version that fails its tests
shutil.copy(HERE / "homeshare.py", WORK)
run(py, "-c", "import homeshare as h; h.tray_image(256).save('hs.ico', sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])")
run(py, "-m", "PyInstaller", "--onefile", "--noconsole", "--name", "HomeShare", "--icon", "hs.ico",
    "--hidden-import", "pystray._win32", "--collect-all", "pypdfium2", "--collect-all", "pypdfium2_raw",
    "--clean", "--noconfirm", "--log-level", "WARN", "homeshare.py")

archive.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(WORK / "dist" / "HomeShare.exe", archive)
was_running = close_homeshare()
shutil.copy2(archive, HERE / "HomeShare.exe")
print(f"Built HomeShare {version}: HomeShare.exe (copy kept in {archive.relative_to(HERE)})")
if was_running:
    start_homeshare(HERE / "HomeShare.exe")
    print("Started the new HomeShare again; open pages reconnect by themselves.")
