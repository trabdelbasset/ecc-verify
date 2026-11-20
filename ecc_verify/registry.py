"""Check registry (shared interface; do not change signatures).

Usage in a checks module:

    from ecc_verify.registry import check
    from ecc_verify.model import Layer, Result

    @check("L3.pl.no_overlap", Layer.L3, steps=("place", "legalization"), category="physics",
           severity="critical", title="No overlapping standard cells")
    def no_overlap(ws):            # ws: ecc_verify.workspace.Workspace (scope="design")
        ...
        return Result.ok("0 overlaps", measured=0, expected=0)

Global checks (scope="global") receive a `ctx` object instead of a workspace
(ctx.ecc: path to ecc binary, ctx.root: projects root, ctx.workspaces: list[Workspace]).
Slow checks (scope="slow") also receive ctx and only run with --slow.
"""
from __future__ import annotations

import traceback
from typing import Callable

from ecc_verify.model import CATEGORIES, STEPS, CheckSpec, Layer, Result, Status

REGISTRY: dict[str, tuple[CheckSpec, Callable]] = {}


def check(id: str, layer: Layer, *, steps: tuple[str, ...], category: str, severity: str,
          title: str, scope: str = "design"):
    if category not in CATEGORIES:
        raise ValueError(f"{id}: unknown category {category}")
    for s in steps:
        if s not in STEPS:
            raise ValueError(f"{id}: unknown step {s}")

    def deco(fn: Callable):
        if id in REGISTRY:
            raise ValueError(f"duplicate check id {id}")
        REGISTRY[id] = (CheckSpec(id, layer, tuple(steps), category, severity, title, scope), fn)
        return fn

    return deco


def run_check(fn: Callable, arg) -> Result:
    """Never raises: a crashing checker becomes Status.ERROR with the traceback."""
    try:
        r = fn(arg)
        if not isinstance(r, Result):
            return Result(Status.ERROR, f"check returned {type(r).__name__}, not Result")
        return r
    except Exception as e:  # noqa: BLE001
        return Result(Status.ERROR, f"{type(e).__name__}: {e}", details={"traceback": traceback.format_exc()})


def coverage_matrix(layers: set[Layer] | None = None) -> dict[str, dict[str, int]]:
    """rows=STEPS, cols=CATEGORIES -> number of implemented checks targeting the cell."""
    m = {s: {c: 0 for c in CATEGORIES} for s in STEPS}
    for spec, _ in REGISTRY.values():
        if layers and spec.layer not in layers:
            continue
        for s in spec.steps:
            m[s][spec.category] += 1
    return m


def load_all_checks() -> None:
    """Import every module in ecc_verify.checks so their @check decorators register."""
    import importlib
    import pkgutil

    import ecc_verify.checks as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"ecc_verify.checks.{mod.name}")
