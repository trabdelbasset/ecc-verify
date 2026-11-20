"""Core data model shared by every ecc-verify check (do not change signatures)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"
    ERROR = "ERROR"  # the checker itself crashed


class Layer(str, Enum):
    L0 = "L0-cli"
    L1 = "L1-flow"
    L2 = "L2-integration"
    L3 = "L3-physics"
    L4 = "L4-metamorphic"
    L5 = "L5-robustness"


# coverage columns
CATEGORIES = ("existence", "integrity", "cross-step", "physics", "timing", "metamorphic", "fault")

# coverage rows (flow steps as they appear in flow.json, plus pseudo-steps)
STEPS = (
    "CLI", "workspace", "Synthesis", "preFloorplan", "macroPlacement", "postFloorplan", "preplace",
    "place", "diff_sizing", "CTS", "legalization", "Timing optimization", "route", "filler", "lvs",
    "drc", "antennaCheck", "postRouteLec", "RCX", "sta", "powerAnalysis", "emir", "Harden",
)


@dataclass
class Result:
    status: Status
    message: str = ""
    measured: Any = None
    expected: Any = None
    tolerance: Any = None
    evidence: list[str] = field(default_factory=list)  # "path[:line]" or "path -> selector"
    details: dict = field(default_factory=dict)        # free-form (examples, counts)

    @staticmethod
    def ok(msg: str = "", **kw) -> "Result":
        return Result(Status.PASS, msg, **kw)

    @staticmethod
    def fail(msg: str, **kw) -> "Result":
        return Result(Status.FAIL, msg, **kw)

    @staticmethod
    def warn(msg: str, **kw) -> "Result":
        return Result(Status.WARN, msg, **kw)

    @staticmethod
    def skip(msg: str, **kw) -> "Result":
        return Result(Status.SKIP, msg, **kw)


@dataclass(frozen=True)
class CheckSpec:
    id: str                 # e.g. "L3.pl.no_overlap"
    layer: Layer
    steps: tuple[str, ...]  # coverage rows targeted (members of STEPS)
    category: str           # member of CATEGORIES
    severity: str           # "critical" | "major" | "minor"
    title: str
    scope: str = "design"   # "design" (run per workspace) | "global" (run once, e.g. CLI) | "slow" (needs --slow)
