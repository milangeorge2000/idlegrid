"""The owner's machine always comes first. These checks decide when a node may work."""

import datetime as dt
import platform
import re
import subprocess
from dataclasses import dataclass


def _sh(*cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


def on_ac_power() -> bool:
    if platform.system() != "Darwin":
        return True
    out = _sh("pmset", "-g", "batt")
    return "AC Power" in out or "Battery Power" not in out


def user_idle_seconds() -> float:
    if platform.system() != "Darwin":
        return 1e9
    m = re.search(r'"HIDIdleTime" = (\d+)', _sh("ioreg", "-c", "IOHIDSystem"))
    return int(m.group(1)) / 1e9 if m else 1e9


def thermally_throttled() -> bool:
    if platform.system() != "Darwin":
        return False
    m = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", _sh("pmset", "-g", "therm"))
    return bool(m and int(m.group(1)) < 80)


@dataclass
class Rules:
    require_ac: bool = True
    pause_when_active: bool = False
    idle_seconds: int = 120
    hours: tuple[int, int] | None = None   # e.g. (23, 7) = only between 11pm and 7am

    def check(self) -> tuple[bool, str | None]:
        """Returns (paused, reason)."""
        if self.hours:
            h, (start, end) = dt.datetime.now().hour, self.hours
            inside = start <= h < end if start < end else (h >= start or h < end)
            if not inside:
                return True, f"outside schedule {start}:00-{end}:00"
        if self.require_ac and not on_ac_power():
            return True, "on battery"
        if self.pause_when_active and user_idle_seconds() < self.idle_seconds:
            return True, "owner is using the computer"
        if thermally_throttled():
            return True, "too hot"
        return False, None
