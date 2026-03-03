#!/usr/bin/env python3
"""
Dell PowerEdge R410 Fan Controller (final, robust)

Designed for cases like yours where:
- IPMI SDR Temperature may only provide "Ambient Temp" reliably
- CPU temps are best read via lm-sensors (sensors -j)

Features:
- CPU control via `sensors -j` (robust JSON parse)
- Chassis safeguard via IPMI Ambient/Exhaust if available
- Smooth fan curve + hysteresis (prevents oscillation)
- Failsafe: if temps missing/too high/errors -> revert to AUTO
- Uses absolute path to ipmitool to avoid systemd PATH issues
- Conservative minimum fan to protect PSU/VRM heat soak (common on R410)
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


# ---------------------------
# CONFIG (tune here)
# ---------------------------

# Absolute paths (systemd-safe)
IPMITOOL = "/usr/bin/ipmitool"
SENSORS_BIN = "/usr/bin/sensors"

POLL_SECONDS = 5

# Manual fan bounds (0-100)
# R410 note: <22% often risks PSU/VRM heat soak. Start conservative.
MIN_FAN_PCT = 26
MAX_FAN_PCT = 60  # if the algorithm "needs" above this -> AUTO for safety

# Temperature targets (°C) - safe defaults for dual X5650
CPU_TARGET = 65
CPU_WARN = 75
CPU_CRIT = 82

# Chassis proxy temps (°C) from IPMI (Ambient/Exhaust if present)
AMBIENT_TARGET = 28
AMBIENT_WARN = 32
AMBIENT_CRIT = 38

EXHAUST_TARGET = 45
EXHAUST_WARN = 52
EXHAUST_CRIT = 58

# Hysteresis / stability
MIN_CHANGE_PCT = 2      # ignore tiny changes
MIN_HOLD_CYCLES = 2     # require N consecutive cycles before applying new level

# If we can't read temps N times in a row -> AUTO
MAX_CONSECUTIVE_READ_FAILS = 3


# ---------------------------
# Logging
# ---------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("r410-fan")


# ---------------------------
# Subprocess helper
# ---------------------------

def run(cmd: list[str], timeout: int = 8) -> str:
    p = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return p.stdout


def which_or_none(path: str) -> Optional[str]:
    try:
        out = run(["/usr/bin/env", "bash", "-lc", f"test -x {path} && echo OK || echo NO"], timeout=3).strip()
        return path if out == "OK" else None
    except Exception:
        return None


# ---------------------------
# IPMI raw fan control
# ---------------------------

def ipmi_set_manual() -> None:
    run([IPMITOOL, "raw", "0x30", "0x30", "0x01", "0x00"])


def ipmi_set_auto() -> None:
    run([IPMITOOL, "raw", "0x30", "0x30", "0x01", "0x01"])


def ipmi_set_fan_pct(pct: int) -> None:
    pct = max(0, min(100, int(pct)))
    hexval = f"0x{pct:02x}"
    run([IPMITOOL, "raw", "0x30", "0x30", "0x02", "0xff", hexval])


# ---------------------------
# Read IPMI temps (SDR output parsing)
# ---------------------------

def read_ipmi_temps() -> Dict[str, int]:
    """
    Parse: `ipmitool sdr type Temperature` into {name: tempC}

    Your R410 format example:
      Ambient Temp     | 0Eh | ok  |  7.1 | 27 degrees C

    Many sensors may be "Disabled"; we skip those.
    """
    out = run([IPMITOOL, "sdr", "type", "Temperature"], timeout=10)
    temps: Dict[str, int] = {}

    for line in out.splitlines():
        if "degrees C" not in line:
            continue

        parts = [p.strip() for p in line.split("|")]
        if not parts:
            continue

        name = parts[0]
        if any("Disabled" in p for p in parts):
            continue

        val = None
        for p in parts:
            m = re.search(r"(-?\d+)\s+degrees\s+C", p, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                break
        if val is None:
            continue

        temps[name] = val

    return temps


def pick_temp(temps: Dict[str, int], keys: tuple[str, ...]) -> Optional[int]:
    for k in keys:
        if k in temps:
            return temps[k]
    return None


# ---------------------------
# Read CPU temps via lm-sensors JSON
# ---------------------------

def read_cpu_max_from_sensors() -> Optional[int]:
    """
    Reads CPU max temperature using `sensors -j`.

    Strategy:
    - Parse JSON
    - Collect all numeric values with keys ending "_input"
    - Filter plausibly-realistic CPU temps (10..110)
    - Return max
    """
    if not which_or_none(SENSORS_BIN):
        return None

    try:
        out = run([SENSORS_BIN, "-j"], timeout=10)
        data = json.loads(out)
    except Exception:
        return None

    vals: list[float] = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                else:
                    if isinstance(v, (int, float)) and isinstance(k, str) and k.endswith("_input"):
                        vals.append(float(v))
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk(data)

    # keep plausible temperature values
    temps = [v for v in vals if 10.0 <= v <= 110.0]
    if not temps:
        return None

    return int(round(max(temps)))


# ---------------------------
# Control logic
# ---------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ramp(value: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if value <= x0:
        return y0
    if value >= x1:
        return y1
    t = (value - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def get_key_temps(ipmi_temps: Dict[str, int]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Returns:
      cpu_max (from sensors),
      ambient/inlet (from IPMI, typically "Ambient Temp"),
      exhaust (from IPMI if exists else ambient)
    """
    cpu_max = read_cpu_max_from_sensors()

    ambient = pick_temp(ipmi_temps, ("Ambient Temp", "Inlet Temp", "Ambient", "System Board Inlet Temp"))
    exhaust = pick_temp(ipmi_temps, ("Exhaust Temp", "Exhaust", "System Board Exhaust Temp")) or ambient

    return cpu_max, ambient, exhaust


def compute_fan_pct(cpu: Optional[int], ambient: Optional[int], exhaust: Optional[int]) -> Tuple[Optional[int], str]:
    """
    Returns (pct or None meaning AUTO, reason).
    """
    # Missing any essential reading -> AUTO (safe)
    if cpu is None or ambient is None or exhaust is None:
        return None, "missing sensor(s) -> AUTO"

    # Failsafe thresholds
    if cpu >= CPU_CRIT:
        return None, f"CPU {cpu}C >= {CPU_CRIT}C -> AUTO"
    if ambient >= AMBIENT_CRIT:
        return None, f"Ambient {ambient}C >= {AMBIENT_CRIT}C -> AUTO"
    if exhaust >= EXHAUST_CRIT:
        return None, f"Exhaust {exhaust}C >= {EXHAUST_CRIT}C -> AUTO"

    # Contributions
    cpu_pct = ramp(cpu, CPU_TARGET - 10, CPU_WARN, MIN_FAN_PCT, 45)
    cpu_pct = ramp(cpu, CPU_WARN, CPU_CRIT - 1, cpu_pct, 60)

    amb_pct = ramp(ambient, AMBIENT_TARGET, AMBIENT_WARN, MIN_FAN_PCT, 35)
    amb_pct = ramp(ambient, AMBIENT_WARN, AMBIENT_CRIT - 1, amb_pct, 55)

    exh_pct = ramp(exhaust, EXHAUST_TARGET, EXHAUST_WARN, MIN_FAN_PCT, 40)
    exh_pct = ramp(exhaust, EXHAUST_WARN, EXHAUST_CRIT - 1, exh_pct, 60)

    pct = int(round(max(cpu_pct, amb_pct, exh_pct)))
    pct = max(MIN_FAN_PCT, pct)

    # If demand exceeds allowed manual ceiling, go AUTO rather than cap and risk heat soak.
    if pct > MAX_FAN_PCT:
        return None, f"needed {pct}% > MAX_MANUAL {MAX_FAN_PCT}% -> AUTO"

    return pct, "manual"


@dataclass
class State:
    last_applied_pct: Optional[int] = None  # None => AUTO
    pending_pct: Optional[int] = None
    pending_cycles: int = 0
    consecutive_read_fails: int = 0


def apply_with_hysteresis(st: State, desired_pct: Optional[int], reason: str) -> None:
    # AUTO requested
    if desired_pct is None:
        if st.last_applied_pct is not None:
            log.warning("Switching to AUTO (%s)", reason)
            ipmi_set_auto()
            st.last_applied_pct = None
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # MANUAL requested
    if st.last_applied_pct is None:
        log.info("Switching to MANUAL: %s%% (%s)", desired_pct, reason)
        ipmi_set_manual()
        ipmi_set_fan_pct(desired_pct)
        st.last_applied_pct = desired_pct
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # Ignore tiny differences
    if abs(desired_pct - st.last_applied_pct) < MIN_CHANGE_PCT:
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # Hold cycles before applying
    if st.pending_pct != desired_pct:
        st.pending_pct = desired_pct
        st.pending_cycles = 1
        return

    st.pending_cycles += 1
    if st.pending_cycles >= MIN_HOLD_CYCLES:
        log.info("Fan -> %s%%", desired_pct)
        ipmi_set_manual()
        ipmi_set_fan_pct(desired_pct)
        st.last_applied_pct = desired_pct
        st.pending_pct = None
        st.pending_cycles = 0


def main() -> None:
    # Hard check required binaries
    if not which_or_none(IPMITOOL):
        raise SystemExit(f"ipmitool not found at {IPMITOOL}. Install it or fix IPMITOOL path.")

    if not which_or_none(SENSORS_BIN):
        log.warning("sensors not found at %s. CPU temp reading will fail -> controller will stay AUTO.", SENSORS_BIN)

    st = State()

    # Start safe
    ipmi_set_auto()
    log.info(
        "Started. Initial mode AUTO. Poll=%ss, MIN_FAN=%s%%, MAX_MANUAL=%s%%",
        POLL_SECONDS, MIN_FAN_PCT, MAX_FAN_PCT,
    )

    while True:
        time.sleep(POLL_SECONDS)

        try:
            ipmi_temps = read_ipmi_temps()
            cpu, ambient, exhaust = get_key_temps(ipmi_temps)

            if cpu is None or ambient is None or exhaust is None:
                st.consecutive_read_fails += 1
                log.warning(
                    "Temp read missing (cpu=%s ambient=%s exhaust=%s) fails=%s/%s",
                    cpu, ambient, exhaust, st.consecutive_read_fails, MAX_CONSECUTIVE_READ_FAILS,
                )
                if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                    apply_with_hysteresis(st, None, "too many temp read failures")
                continue

            st.consecutive_read_fails = 0

            desired_pct, reason = compute_fan_pct(cpu, ambient, exhaust)

            log.info(
                "Temps: CPUmax=%sC Ambient=%sC Exhaust=%sC -> %s",
                cpu, ambient, exhaust,
                ("AUTO" if desired_pct is None else f"{desired_pct}%"),
            )

            apply_with_hysteresis(st, desired_pct, reason)

        except subprocess.TimeoutExpired:
            st.consecutive_read_fails += 1
            log.warning("Command timeout fails=%s/%s", st.consecutive_read_fails, MAX_CONSECUTIVE_READ_FAILS)
            if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                apply_with_hysteresis(st, None, "command timeout")
        except Exception as e:
            st.consecutive_read_fails += 1
            log.exception("Unexpected error: %s", e)
            if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                apply_with_hysteresis(st, None, "unexpected errors")


if __name__ == "__main__":
    main()
