"""Shared macOS subprocess helpers.

Every tool that shells out to a native macOS utility (`sysctl`, `vm_stat`,
`df`, `pmset`, `git`, `open`, `pgrep`, ...) goes through `run_native` so
timeout handling, error wrapping, and argv-only invocation live in one place
instead of being reimplemented per tool module.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeResult:
    """Outcome of a native command. Never raises on a non-zero exit."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_native(
    argv: list[str],
    *,
    timeout: float = 10.0,
    cwd: Path | str | None = None,
) -> NativeResult:
    """Run a native command by argv list. Never uses a shell, never raises.

    Callers decide what a non-zero exit or timeout means for their tool; this
    wrapper only guarantees the process is invoked safely and bounded.
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        log.warning("native command timed out: %s", argv)
        return NativeResult(
            argv=argv, returncode=-1, stdout=stdout, stderr=stderr, timed_out=True
        )
    except FileNotFoundError as exc:
        return NativeResult(argv=argv, returncode=-1, stdout="", stderr=str(exc))
    except OSError as exc:
        log.warning("native command failed: %s (%s)", argv, exc)
        return NativeResult(argv=argv, returncode=-1, stdout="", stderr=str(exc))

    return NativeResult(
        argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


# -- vm_stat --------------------------------------------------------------

def parse_vm_stat(output: str) -> dict[str, int]:
    """Parse `vm_stat` output into page counts keyed by field name.

    Lines look like `Pages free:                          123456.`. The page
    size line (`Mach Virtual Memory Statistics: (page size of 16384 bytes)`)
    is captured separately under the `page_size` key.
    """
    stats: dict[str, int] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Mach Virtual Memory Statistics"):
            start = line.find("page size of")
            if start != -1:
                digits = "".join(c for c in line[start:] if c.isdigit())
                if digits:
                    stats["page_size"] = int(digits)
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            stats[key.strip().lower().replace(" ", "_")] = int(value)
    return stats


# -- df ---------------------------------------------------------------------

def parse_df(output: str) -> dict[str, str] | None:
    """Parse the second line of `df -H /` (or similar) into named fields.

    The column layout isn't fixed: some macOS/APFS volumes add `iused ifree
    %iused` columns between `Capacity` and `Mounted on`, which a fixed
    field-index split would misattribute. Instead, `Capacity` is located as
    the first percentage-shaped token from position 4 onward, and `Mounted
    on` is taken as the last token (mount paths with spaces are the one case
    this doesn't handle, and are rare enough to accept).

    Returns None if the output doesn't have the expected two-line shape.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    fields = lines[1].split()
    if len(fields) < 5:
        return None

    capacity_idx = next(
        (i for i, f in enumerate(fields[4:], start=4) if re.fullmatch(r"\d+%", f)), None
    )
    if capacity_idx is None:
        return None

    return {
        "filesystem": fields[0],
        "size": fields[1],
        "used": fields[2],
        "available": fields[3],
        "capacity": fields[capacity_idx],
        "mounted_on": fields[-1],
    }


# -- pmset --------------------------------------------------------------

def parse_pmset_batt(output: str) -> dict[str, object] | None:
    """Parse `pmset -g batt` output.

    Typical shape:
        Now drawing from 'AC Power'
         -InternalBattery-0 (id=123)	87%; charging; 0:20 remaining present: true

    Returns None if this machine reports no battery (desktop Mac).
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return None
    if "InternalBattery" not in output and "Battery" not in output:
        return None

    source_line = lines[0]
    power_source = None
    if "'" in source_line:
        try:
            power_source = source_line.split("'")[1]
        except IndexError:
            power_source = None

    battery_line = next((l for l in lines[1:] if "%" in l), None)
    if battery_line is None:
        return {"power_source": power_source}

    # Match the percentage directly rather than concatenating every digit in
    # a token — a token like "(id=20971619) 18%" contains unrelated digits
    # (the battery id) that a naive digit-join would fold into the percent.
    percent_match = re.search(r"(\d{1,3})%", battery_line)
    percent = int(percent_match.group(1)) if percent_match else None
    charging = "charging" in battery_line and "discharging" not in battery_line

    return {
        "power_source": power_source,
        "percent": percent,
        "charging": charging,
        "raw": battery_line.strip(),
    }
