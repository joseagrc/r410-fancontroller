#!/usr/bin/env python3
"""
Dell R410 fan controller (improved)

Key improvements vs original:
- Uses IPMI SDR temperature sensors (CPU + Inlet + Exhaust)
- Robust parsing (no brittle "two digits before '.'")
- Smooth fan curve (percentage) + hysteresis (prevents oscillation)
- Failsafe: if temps are too high or parsing fails -> return to AUTO
- Keeps a reasonable minimum fan floor to protect PSU/VRM area
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


# ---------------------------
# CONFIG (tune these)
# ---------------------------

POLL_SECONDS = 5  # faster reaction helps avoid heat soak around PSU/VRM

# Manual fan percent limits (0-100).
# IMPORTANT: For many R410, <20-22% can be risky for PSU/VRM heat.
MIN_FAN_PCT = 22
MAX_FAN_PCT = 60  # keep noise controlled; above this, we can fall back to AUTO if needed

# Temperature targets (°C) - tune for your environment
CPU_TARGET = 65        # try to keep CPU around here under load
CPU_WARN = 75          # start pushing harder
CPU_CRIT = 82          # failsafe -> AUTO

INLET_TARGET = 28      # inlet target (depends on room)
INLET_WARN = 32
INLET_CRIT = 38        # failsafe -> AUTO (room too hot / intake restricted)

EXHAUST_TARGET = 45
EXHAUST_WARN = 52
EXHAUST_CRIT = 58      # failsafe -> AUTO (PSU/VRM/backplane heat soak)

# Hysteresis / rate limiting
MIN_CHANGE_PCT = 2     # do not change fan for tiny adjustments
MIN_HOLD_CYCLES = 2    # require N consecutive cycles before applying a new level

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
# IPMI helpers
# ---------------------------

def run(cmd: list[str], timeout: int = 5) -> str:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    return p.stdout


def ipmi_set_manual() -> None:
    # raw 0x30 0x30 0x01 0x00 = set manual fan control
    run(["ipmitool", "raw", "0x30", "0x30", "0x01", "0x00"])


def ipmi_set_auto() -> None:
    # raw 0x30 0x30 0x01 0x01 = set auto fan control
    run(["ipmitool", "raw", "0x30", "0x30", "0x01", "0x01"])


def ipmi_set_fan_pct(pct: int) -> None:
    """
    Set fan PWM percentage on Dell via:
      ipmitool raw 0x30 0x30 0x02 0xff 0xNN
    where NN is 0-100 in hex.
    """
    pct = max(0, min(100, int(pct)))
    hexval = f"0x{pct:02x}"
    run(["ipmitool", "raw", "0x30", "0x30", "0x02", "0xff", hexval])


def read_ipmi_temps() -> Dict[str, int]:
    """
    Parse `ipmitool sdr type Temperature` output into {name: tempC}.
    """
    out = run(["ipmitool", "sdr", "type", "Temperature"], timeout=6)
    temps: Dict[str, int] = {}

    # Typical line: "Inlet Temp      | 23 degrees C      | ok"
    # Some firmwares vary spacing; keep regex tolerant.
    for line in out.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        name = parts[0]
        reading = parts[1]

        m = re.search(r"(-?\d+)\s+degrees\s+C", reading, re.IGNORECASE)
        if not m:
            continue
        temps[name] = int(m.group(1))

    return temps


def pick_temp(temps: Dict[str, int], keys: tuple[str, ...]) -> Optional[int]:
    for k in keys:
        if k in temps:
            return temps[k]
    return None


def get_key_temps(temps: Dict[str, int]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Returns (cpu_max, inlet, exhaust).
    Tries common Dell R410 naming variants.
    """
    cpu1 = pick_temp(temps, ("CPU1 Temp", "CPU 1 Temp", "CPU1", "CPU1 Temperature"))
    cpu2 = pick_temp(temps, ("CPU2 Temp", "CPU 2 Temp", "CPU2", "CPU2 Temperature"))
    cpu_max = None
    if cpu1 is not None and cpu2 is not None:
        cpu_max = max(cpu1, cpu2)
    else:
        cpu_max = cpu1 if cpu1 is not None else cpu2

    inlet = pick_temp(temps, ("Inlet Temp", "System Board Inlet Temp", "Ambient Temp", "Ambient"))
    exhaust = pick_temp(temps, ("Exhaust Temp", "System Board Exhaust Temp", "Exhaust"))

    return cpu_max, inlet, exhaust


# ---------------------------
# Control logic
# ---------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ramp(value: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """
    Linear ramp:
      value<=x0 -> y0
      value>=x1 -> y1
      else interpolate
    """
    if value <= x0:
        return y0
    if value >= x1:
        return y1
    t = (value - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def compute_fan_pct(cpu: Optional[int], inlet: Optional[int], exhaust: Optional[int]) -> Tuple[Optional[int], str]:
    """
    Returns (pct or None meaning AUTO, reason).
    """
    # Failsafe thresholds
    if cpu is None or inlet is None or exhaust is None:
        return None, "missing temp sensor(s) -> AUTO"

    if cpu >= CPU_CRIT:
        return None, f"CPU {cpu}C >= {CPU_CRIT}C -> AUTO"
    if inlet >= INLET_CRIT:
        return None, f"Inlet {inlet}C >= {INLET_CRIT}C -> AUTO"
    if exhaust >= EXHAUST_CRIT:
        return None, f"Exhaust {exhaust}C >= {EXHAUST_CRIT}C -> AUTO"

    # Base floor: protect chassis/PSU/VRM by respecting inlet/exhaust too.
    # CPU contribution
    cpu_pct = ramp(cpu, CPU_TARGET - 10, CPU_WARN, MIN_FAN_PCT, 45)
    cpu_pct = ramp(cpu, CPU_WARN, CPU_CRIT - 1, cpu_pct, 60)

    # Inlet contribution
    inlet_pct = ramp(inlet, INLET_TARGET, INLET_WARN, MIN_FAN_PCT, 35)
    inlet_pct = ramp(inlet, INLET_WARN, INLET_CRIT - 1, inlet_pct, 55)

    # Exhaust contribution (very important for PSU/VRM heat soak)
    exhaust_pct = ramp(exhaust, EXHAUST_TARGET, EXHAUST_WARN, MIN_FAN_PCT, 40)
    exhaust_pct = ramp(exhaust, EXHAUST_WARN, EXHAUST_CRIT - 1, exhaust_pct, 60)

    pct = int(round(max(cpu_pct, inlet_pct, exhaust_pct)))

    # Bound manual mode; if demand is beyond what we allow in manual quiet mode,
    # it is safer to go AUTO than to cap and let the chassis cook.
    if pct > MAX_FAN_PCT:
        return None, f"needed {pct}% > MAX_MANUAL {MAX_FAN_PCT}% -> AUTO"

    pct = max(MIN_FAN_PCT, pct)
    return pct, "manual"


@dataclass
class State:
    last_applied_pct: Optional[int] = None  # None means AUTO
    pending_pct: Optional[int] = None
    pending_cycles: int = 0
    consecutive_read_fails: int = 0


def apply_with_hysteresis(st: State, desired_pct: Optional[int], reason: str) -> None:
    """
    Apply desired fan setting with hysteresis.
    """
    # If AUTO requested
    if desired_pct is None:
        if st.last_applied_pct is not None:
            log.warning("Switching to AUTO (%s)", reason)
            ipmi_set_auto()
            st.last_applied_pct = None
        else:
            log.debug("Staying in AUTO (%s)", reason)
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # Manual requested
    if st.last_applied_pct is None:
        log.info("Switching to MANUAL: %s%% (%s)", desired_pct, reason)
        ipmi_set_manual()
        ipmi_set_fan_pct(desired_pct)
        st.last_applied_pct = desired_pct
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # If difference is tiny, ignore
    if abs(desired_pct - st.last_applied_pct) < MIN_CHANGE_PCT:
        st.pending_pct = None
        st.pending_cycles = 0
        return

    # Hold cycles before applying change
    if st.pending_pct != desired_pct:
        st.pending_pct = desired_pct
        st.pending_cycles = 1
        return

    st.pending_cycles += 1
    if st.pending_cycles >= MIN_HOLD_CYCLES:
        log.info("Fan -> %s%% (cpu/inlet/exhaust driven)", desired_pct)
        ipmi_set_manual()
        ipmi_set_fan_pct(desired_pct)
        st.last_applied_pct = desired_pct
        st.pending_pct = None
        st.pending_cycles = 0


def main() -> None:
    st = State()

    # Start safe: AUTO until we read everything cleanly
    ipmi_set_auto()
    log.info("Started. Initial mode AUTO. Poll=%ss, MIN_FAN=%s%%", POLL_SECONDS, MIN_FAN_PCT)

    while True:
        time.sleep(POLL_SECONDS)

        try:
            temps = read_ipmi_temps()
            cpu, inlet, exhaust = get_key_temps(temps)

            if cpu is None or inlet is None or exhaust is None:
                st.consecutive_read_fails += 1
                log.warning(
                    "Temp read missing (cpu=%s inlet=%s exhaust=%s) fails=%s/%s",
                    cpu, inlet, exhaust, st.consecutive_read_fails, MAX_CONSECUTIVE_READ_FAILS
                )
                if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                    apply_with_hysteresis(st, None, "too many temp read failures")
                continue

            st.consecutive_read_fails = 0

            desired_pct, reason = compute_fan_pct(cpu, inlet, exhaust)

            log.info("Temps: CPUmax=%sC Inlet=%sC Exhaust=%sC -> %s",
                     cpu, inlet, exhaust,
                     ("AUTO" if desired_pct is None else f"{desired_pct}%"))

            apply_with_hysteresis(st, desired_pct, reason)

        except subprocess.TimeoutExpired:
            st.consecutive_read_fails += 1
            log.warning("ipmitool timeout fails=%s/%s", st.consecutive_read_fails, MAX_CONSECUTIVE_READ_FAILS)
            if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                apply_with_hysteresis(st, None, "ipmitool timeout")
        except Exception as e:
            st.consecutive_read_fails += 1
            log.exception("Unexpected error: %s", e)
            if st.consecutive_read_fails >= MAX_CONSECUTIVE_READ_FAILS:
                apply_with_hysteresis(st, None, "unexpected errors")


if __name__ == "__main__":
    main()
