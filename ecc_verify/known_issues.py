"""Known issues: check id (+ optional design list) -> BUG id + note.  A FAIL covered by an entry is *known*, not *new*.

known_issues.toml::

    [[known]]
    check  = "L3.sta.drv_clean"          # exact id, glob ("L5.*"), or a list of them
    designs = ["aes", "BM64"]            # optional; omitted = every design (the global pseudo-design is "<global>")
    bugs   = ["BUG-02"]                  # or  bug = "BUG-02"
    note   = "max_transition/max_cap violations are not gated"

Per-design ``verify.toml`` ``[[expected_fail]]`` tables use the same keys (``check``, ``bug(s)``, ``note``) and are scoped
to that design.
"""
from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class KnownIssue:
    checks: tuple[str, ...]
    designs: tuple[str, ...] | None
    bugs: tuple[str, ...]
    note: str = ""
    source: str = "known_issues.toml"

    def matches(self, check: str, design: str) -> bool:
        if not any(fnmatch.fnmatchcase(check, c) for c in self.checks):
            return False
        return self.designs is None or any(fnmatch.fnmatchcase(design, d) for d in self.designs)

    @property
    def label(self) -> str:
        return ", ".join(self.bugs) if self.bugs else "known"


def _as_tuple(v) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(str(x) for x in v)


def _entry(d: dict, source: str, force_designs: tuple[str, ...] | None = None) -> KnownIssue:
    checks = _as_tuple(d.get("check", d.get("checks")))
    if not checks:
        raise ValueError(f"{source}: entry without `check`: {d}")
    designs = force_designs if force_designs is not None else (_as_tuple(d["designs"]) if "designs" in d else None)
    return KnownIssue(checks, designs, _as_tuple(d.get("bugs", d.get("bug"))), str(d.get("note", "")), source)


class KnownIssues:
    def __init__(self, entries: list[KnownIssue] | None = None, path: str = ""):
        self.entries = list(entries or [])
        self.path = path
        self._used: set[int] = set()

    @classmethod
    def load(cls, path: str | Path) -> "KnownIssues":
        p = Path(path)
        with open(p, "rb") as f:
            d = tomllib.load(f)
        return cls([_entry(e, p.name) for e in d.get("known", [])], str(p))

    @classmethod
    def packaged(cls) -> "KnownIssues":
        txt = resources.files("ecc_verify").joinpath("data/known_issues.toml").read_text()
        d = tomllib.loads(txt)
        return cls([_entry(e, "packaged known_issues.toml") for e in d.get("known", [])], "(packaged)")

    @classmethod
    def resolve(cls, explicit: str | None = None, cwd: Path | None = None) -> "KnownIssues":
        """explicit path > ./known_issues.toml > the list shipped with the package."""
        if explicit:
            return cls.load(explicit)
        local = (cwd or Path.cwd()) / "known_issues.toml"
        return cls.load(local) if local.is_file() else cls.packaged()

    def with_design_configs(self, design_cfg: dict) -> "KnownIssues":
        """Return a copy extended with the per-design verify.toml [[expected_fail]] tables."""
        extra = []
        for design, dc in (design_cfg or {}).items():
            for e in (dc or {}).get("expected_fail", []) or []:
                extra.append(_entry(e, f"{design}/verify.toml", (design,)))
        return KnownIssues(self.entries + extra, self.path)

    def lookup(self, check: str, design: str) -> KnownIssue | None:
        for i, e in enumerate(self.entries):
            if e.matches(check, design):
                self._used.add(i)
                return e
        return None

    def unused(self) -> list[KnownIssue]:
        """Entries that never matched a failing result: candidates for removal (the bug may be fixed)."""
        return [e for i, e in enumerate(self.entries) if i not in self._used]
