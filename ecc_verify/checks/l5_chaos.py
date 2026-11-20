"""L5: robustness with real ecc runs (scope="slow"): kill/resume chaos and project relocation."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from ecc_verify import config
from ecc_verify import flowrun as F
from ecc_verify.checks.l4_metamorphic import DESIGN, get_base
from ecc_verify.model import Layer, Result, Status
from ecc_verify.registry import check


def _group_alive(pgid: int) -> list[str]:
    p = subprocess.run(["pgrep", "-g", str(pgid), "-a"], capture_output=True, text=True)
    return [l[:120] for l in p.stdout.splitlines()]


@check("L5.chaos_kill_resume", Layer.L5, steps=("CLI", "workspace", "place"), category="fault", severity="critical",
       title="SIGTERM during place then `ecc run --resume` works without manual reconcile", scope="slow")
def chaos_kill_resume(ctx) -> Result:
    case = F.new_case("xtea_chaos", DESIGN, Path(getattr(ctx, "designs_dir", None) or F.get_designs_dir()))
    det: dict = {}
    proc = None
    try:
        cmd = config.current().pin([ctx.ecc, "run", "--project", str(case.root), "--plain"], ctx.cpus)
        proc = subprocess.Popen(cmd, env=F.env_for(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        if not F.wait_for_step_dir(case.ws, "place_dreamplace", 400):
            return Result(Status.ERROR, "never reached place step", details={"rc": proc.poll()})
        time.sleep(3)
        if proc.poll() is not None:
            return Result.skip(f"flow already ended (rc={proc.returncode}) before SIGTERM could be sent in place")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            pass
        det["main_exit"] = proc.poll()
        left = _group_alive(proc.pid)
        det["group_alive_after_sigterm"] = left
        if left:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(5)
            det["group_alive_after_group_term"] = _group_alive(proc.pid)
        det["status_after_kill"] = F.status_json(ctx.ecc, case.root).get("workspace")
        det["run_id"] = F.runtime_run_id(case.root)
        r1 = F.run_ecc(ctx.ecc, case.root, ["run", "--resume"], ctx.cpus, F.full_timeout())
        det["resume_no_reconcile"] = {"rc": r1.rc, "out": r1.tail(500)}
        if r1.ok:
            st = F.status_json(ctx.ecc, case.root).get("workspace")
            F.save_summary("L5_chaos_kill_resume", det)
            return (Result.ok("resume succeeded without reconcile; workspace=" + str(st), details=det) if st == "success"
                    else Result.fail(f"resume rc=0 but workspace status={st}", details=det))
        # known O-1 path: reconcile then resume
        rid = det["run_id"] or F.runtime_run_id(case.root)
        rc_args = ["process", "reconcile", "--workspace", F.WS] + (["--run-id", rid] if rid else [])
        rr = F.run_ecc(ctx.ecc, case.root, rc_args, ctx.cpus, 120, plain=False)
        det["reconcile"] = {"cmd": rr.args, "rc": rr.rc, "out": rr.tail(500)}
        r2 = F.run_ecc(ctx.ecc, case.root, ["run", "--resume"], ctx.cpus, F.full_timeout())
        det["resume_after_reconcile"] = {"rc": r2.rc, "out": r2.tail(400), "secs": r2.secs}
        st = F.status_json(ctx.ecc, case.root).get("workspace")
        det["final_status"] = st
        if r2.ok and st == "success":
            det["final_metrics"] = F.flow_metrics(case.ws)
        F.save_summary("L5_chaos_kill_resume", det)
        recovered = r2.ok and st == "success"
        msg = (f"plain `ecc run --resume` after SIGTERM failed rc={r1.rc}: {r1.tail(200).strip()!r}; "
               f"reconcile+resume {'completed (workspace success)' if recovered else 'ALSO FAILED'}")
        return Result.fail(msg, details=det, expected="resume works", measured=r1.out[-200:])
    finally:
        if proc and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        F.cleanup(case)


@check("L5.relocate", Layer.L5, steps=("CLI", "workspace"), category="fault", severity="major",
       title="A copied finished project still answers `ecc status`", scope="slow")
def relocate(ctx) -> Result:
    b = get_base(ctx)
    if not b["ok"]:
        return Result.skip("baseline run failed")
    src = b["case"].root
    dst = F.runs_dir() / "slow_xtea_relocated"
    shutil.rmtree(dst, ignore_errors=True)
    try:
        shutil.copytree(src, dst)
        r = F.run_ecc(ctx.ecc, dst, ["status"], ctx.cpus, 120, plain=False)
        r_json = F.run_ecc(ctx.ecc, dst, ["status", "--json"], ctx.cpus, 120, plain=False)
        q = F.run_ecc(ctx.ecc, dst, ["report", "qor", "--plain"], ctx.cpus, 120, plain=False)
        det = {"status": {"rc": r.rc, "out": r.tail(500)}, "status_json_rc": r_json.rc, "qor_rc": q.rc,
               "qor_out": q.tail(300)}
        F.save_summary("L5_relocate", det)
        if r.rc == 0 and "manifest_invalid" not in r.out and r_json.rc == 0:
            return Result.ok("ecc status works on relocated project", details=det)
        return Result.fail(f"ecc status on relocated copy rc={r.rc}: {r.tail(250).strip()!r}", details=det,
                           expected="status success", measured=r.out[-200:])
    finally:
        shutil.rmtree(dst, ignore_errors=True)
