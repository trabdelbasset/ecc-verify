"""L0: CLI contract checks (global scope; run once)."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ecc_verify.model import Layer, Result
from ecc_verify.registry import check
from ecc_verify import config
from ecc_verify.util import run, scratch_copy, jload

_ENV = {"COLUMNS": "200", "NO_COLOR": "1"}
_cache: dict = {}


def _help(ecc, path):
    return run([ecc, *path, "--help"], env=_ENV, timeout=config.current().timeout("cli_help"))


def _subcommands(text):
    out, inbox = [], False
    for ln in text.splitlines():
        if re.match(r"^\s*╭─ Commands", ln):
            inbox = True
            continue
        if inbox and ln.lstrip().startswith("╰"):
            break
        if inbox:
            m = re.match(r"^│\s+(\S+)\s{2,}", ln)
            if m:
                out.append(m.group(1))
    return out


def discover(ecc):
    """Walk the command tree via --help. Returns {tuple(path): (rc, text)}."""
    if ecc in _cache:
        return _cache[ecc]
    tree = {}
    frontier = [()]
    while frontier:
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda p: _help(ecc, p), frontier))
        nxt = []
        for p, (rc, out, err) in zip(frontier, res):
            tree[p] = (rc, out + err)
            if rc == 0:
                nxt += [p + (c,) for c in _subcommands(out)]
        frontier = nxt
    _cache[ecc] = tree
    return tree


@check("L0.help", Layer.L0, steps=("CLI",), category="existence", severity="major",
       title="Every command and subcommand --help exits 0", scope="global")
def help_all(ctx):
    tree = discover(ctx.ecc)
    bad = {" ".join(p) or "<root>": rc for p, (rc, _) in tree.items() if rc != 0}
    if bad:
        return Result.fail(f"{len(bad)} commands fail --help", measured=bad, expected="rc 0 everywhere")
    return Result.ok(f"{len(tree)} commands/subcommands exit 0", measured=len(tree))


_JSON_CMDS = [["status"], ["report", "qor"], ["doctor"], ["version"], ["param", "list", "--all"],
              ["signoff", "check"], ["report", "project"], ["diagnose", "run"]]


@check("L0.json", Layer.L0, steps=("CLI",), category="integrity", severity="major",
       title="--json commands emit parseable JSON on a finished workspace", scope="global")
def json_cmds(ctx):
    if not ctx.workspaces:
        return Result.skip("no workspaces")
    src = ctx.workspaces[0].project_dir
    for w in ctx.workspaces:
        if w.name == "xtea":
            src = w.project_dir
    proj = scratch_copy(src, "l0_" + src.name)
    rcs, bad, warn = {}, [], []
    for c in _JSON_CMDS:
        cmd = [ctx.ecc, *c, "--json"] if c != ["version"] else [ctx.ecc, "version", "--json"]
        rc, out, err = run(cmd, cwd=proj, timeout=config.current().timeout("cli_json"), env=_ENV)
        key = " ".join(c)
        rcs[key] = rc
        try:
            json.loads(out)
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            bad.append(f"{key}: rc={rc} stdout not JSON ({(out or err)[:80]!r})")
        elif rc != 0:
            warn.append(f"{key}: rc={rc} with valid JSON payload (nonzero rc with success payload)")
    if bad:
        return Result.fail("; ".join(bad), measured=rcs, details={"rcs": rcs})
    if warn:
        return Result.warn("; ".join(warn), measured=rcs, details={"rcs": rcs})
    return Result.ok("all JSON parse, rc 0", measured=rcs)


@check("L0.flags_consistency", Layer.L0, steps=("CLI",), category="cross-step", severity="minor",
       title="--project/--workspace accepted consistently across commands", scope="global")
def flags(ctx):
    tree = discover(ctx.ecc)
    only_proj, only_ws = [], []
    for p, (rc, text) in tree.items():
        if not p or rc != 0 or "Commands" in text and re.search(r"╭─ Commands", text):
            continue
        hp = bool(re.search(r"--project\b", text))
        hw = bool(re.search(r"--workspace\b", text))
        if hp and not hw:
            only_proj.append(" ".join(p))
        elif hw and not hp:
            only_ws.append(" ".join(p))
    if only_proj or only_ws:
        return Result.warn(f"{len(only_proj)} commands accept only --project, {len(only_ws)} only --workspace",
                           measured={"only --project": only_proj, "only --workspace": only_ws},
                           expected="both flags on every project-scoped command")
    return Result.ok("consistent")


@check("L0.version_identity", Layer.L0, steps=("CLI",), category="integrity", severity="minor",
       title="version identity: commit present, nightly not reporting a stable version", scope="global")
def version_identity(ctx):
    rc, out, _ = run([ctx.ecc, "version", "--json"], env=_ENV)
    if rc != 0:
        return Result.fail(f"ecc version rc={rc}")
    v = json.loads(out)
    msgs = []
    commit = v.get("build_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        return Result.fail(f"build_commit missing/malformed: {commit!r}", measured=v)
    ecc_path = Path(ctx.ecc).resolve()
    nightly = re.search(r"nightly[./-]*v?([\w.\-]*nightly\.\d{8})", str(ecc_path))
    rc2, out2, _ = run([ctx.ecc, "--version"], env=_ENV)
    ver = str(v.get("ecc", ""))
    if nightly and "nightly" not in ver and "nightly" not in out2.lower():
        msgs.append(f"install is {nightly.group(1)} but version reports stable-looking {ver!r} ({out2.strip()!r})")
    bd = str(v.get("build_date", ""))
    nd = re.search(r"nightly\.(\d{8})", str(ecc_path))
    if nd and bd and bd != nd.group(1):
        msgs.append(f"build_date {bd} differs from nightly tag {nd.group(1)}")
    if msgs:
        return Result.warn("; ".join(msgs), measured=v, evidence=[f"{ecc_path}"])
    return Result.ok(f"commit {commit[:8]}, version {ver}", measured=v)
