"""Install, remove or inspect the weekly launchd job (macOS).

The job runs `pipeline.py run --trigger schedule` every Monday at 03:00 with the repo's .venv.
If the Mac is asleep then, launchd runs it when the Mac wakes up. Its output goes to
data/logs/launchd.log (each run also has its own log, see the dashboard).

Usage:
    .venv/bin/python scripts/install_schedule.py install
    .venv/bin/python scripts/install_schedule.py uninstall
    .venv/bin/python scripts/install_schedule.py status
    launchctl kickstart gui/$(id -u)/com.xg-commentary.pipeline     # run it once now
"""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import paths

LABEL = "com.xg-commentary.pipeline"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"


def agent() -> dict:
    python = paths.ROOT / ".venv" / "bin" / "python"
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(python), str(paths.ROOT / "scripts" / "pipeline.py"), "run", "--trigger", "schedule"],
        "WorkingDirectory": str(paths.ROOT),
        "StartCalendarInterval": {"Weekday": 1, "Hour": 3, "Minute": 0},
        "StandardOutPath": str(paths.LOGS / "launchd.log"),
        "StandardErrorPath": str(paths.LOGS / "launchd.log"),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                 **({"XG_DATA_DIR": os.environ["XG_DATA_DIR"]} if "XG_DATA_DIR" in os.environ else {})},
    }


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "install":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["launchctl", "bootout", DOMAIN, str(PLIST)], capture_output=True)
        PLIST.write_bytes(plistlib.dumps(agent()))
        subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
        print(f"Installed {PLIST}: every Monday at 03:00.")
    elif cmd == "uninstall":
        subprocess.run(["launchctl", "bootout", DOMAIN, str(PLIST)], capture_output=True)
        PLIST.unlink(missing_ok=True)
        print("Removed the weekly job.")
    elif cmd == "status":
        loaded = subprocess.run(["launchctl", "print", f"{DOMAIN}/{LABEL}"], capture_output=True, text=True)
        print(f"plist: {'present' if PLIST.exists() else 'missing'} ({PLIST})")
        print("loaded in launchd" if loaded.returncode == 0 else "not loaded in launchd")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
