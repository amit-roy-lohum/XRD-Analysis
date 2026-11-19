"""Agentic Rietveld refinement loop — Claude CLI subprocess drives rietx."""

import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── System prompt for the Claude subprocess ──────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert Rietveld refinement agent. You refine XRD data by writing \
and executing a SINGLE Python script that calls the rietx library.

## Setup
A file `_agent_state.py` in the working directory provides:
- `ref` — a `rietx.Refinement` object (structure + instrument loaded)
- `data` — a `rietx.PatternData` object (the XRD pattern)
- `TT_LIMITS` — tuple of (min, max) 2-theta in degrees

## CRITICAL: Write ONE script, not many
Write a SINGLE self-contained Python script that does everything: initial fit, \
diagnostic triage, improvement loop, and result saving. Do NOT write separate \
scripts for each step. The script should run start-to-finish in one execution.

## Goal
Achieve the best possible Rwp for the given XRD pattern, targeting \
Rwp < {rwp_target_pct:.1f}%. Rwp alone decides nothing (rietx SKILL rule 16) — \
the diagnostics decide when to stop.

## The refinement loop

Write your script following this structure:

```python
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, "<working_dir>")
from _agent_state import ref, data, TT_LIMITS
import rietx as rx

# Define save_checkpoint to write BOTH JSON and CIF on every call
def save_checkpoint(result, iteration, reason, history_so_far,
                    addressed=None, remaining=None):
    import json
    data_out = {{
        "final_rwp_pct": round(result.statistics.rwp * 100, 2),
        "final_chi2": round(result.statistics.chi2, 4),
        "final_gof": round(getattr(result.statistics, 'gof', 0), 4),
        "status": str(result.status),
        "n_iterations": iteration,
        "reason": reason,
        "summary": f"Checkpoint at iter {{iteration}}: {{reason}}",
        "diagnostics_addressed": addressed or [],
        "diagnostics_remaining": remaining or [],
        "history": history_so_far,
    }}
    with open("<working_dir>/_agent_result.json", "w") as f:
        json.dump(data_out, f, indent=2)
    try:
        rx.write_refinement_cif(result, ref.structure, ref.instrument,
                                "<working_dir>/_agent_refined.cif")
    except Exception:
        pass  # CIF write may fail for multi-phase

# ── 1. Initial fit ──
result = ref.fit(data, plan="mccusker_structural", two_theta_limits=TT_LIMITS,
                 stage_reports=True, telemetry=False)
print(f"Initial: Rwp={{result.statistics.rwp*100:.2f}}% chi2={{result.statistics.chi2:.4f}}")

# Save checkpoint immediately (JSON + CIF)
save_checkpoint(result, 0, "initial_fit_checkpoint", history)

# ── 2. Diagnostic triage (branch on d.code, never on d.message) ──
codes = {{d.code for d in result.diagnostics}}

# 2a. BACKGROUND_ABSORPTION — background too flexible, eating Biso/scale
if "BACKGROUND_ABSORPTION" in codes:
    # Reduce background terms or stiffen; do NOT trust ADPs from this fit
    ...

# 2b. BISO_NEGATIVE — unphysical displacement parameters
if "BISO_NEGATIVE" in codes:
    neg_paths = [d.where[0] for d in result.diagnostics if d.code == "BISO_NEGATIVE"]
    for p in neg_paths:
        ref.hold([p])  # hold at a physical value
    result = ref.run_stage(data, rx.Stage("fix_neg_biso", ["phases.*.atoms.*.biso"]),
                           two_theta_limits=TT_LIMITS)

# 2c. BISO_UNUSUALLY_LARGE — check species/occupancy/absorption first
if "BISO_UNUSUALLY_LARGE" in codes:
    # Hold or constrain the offending Biso values
    ...

# 2d. FLOOR_HIT — parameter at its lower bound, effectively absent
if "FLOOR_HIT" in codes:
    floor_paths = [d.where[0] for d in result.diagnostics if d.code == "FLOOR_HIT"]
    ref.hold(floor_paths)  # hold at zero — the data says absent
    result = ref.run_stage(data, rx.Stage("hold_floors", []),
                           two_theta_limits=TT_LIMITS)

# 2e. HIGH_CORRELATION — fix one member or free in separate stages
if "HIGH_CORRELATION" in codes:
    for d in result.diagnostics:
        if d.code == "HIGH_CORRELATION" and len(d.where) >= 2:
            ref.hold([d.where[1]])  # hold second member
    result = ref.run_stage(data, rx.Stage("fix_correlations", []),
                           two_theta_limits=TT_LIMITS)

# 2f. DATA_SUPPORT_LOW — over-parameterized, hold least-determined params
if "DATA_SUPPORT_LOW" in codes:
    # Use result.identifiability to find least-determined params and hold them
    ...

# ── 3. Report-driven improvement loop ──
MAX_ITERS = {max_iterations}
for iteration in range(MAX_ITERS):
    report = rx.build_report(result)
    if report.abstained_reason:
        print(f"Report abstained: {{report.abstained_kind}}")
        break
    if not report.suggested_actions:
        break

    improved = False
    for action in report.suggested_actions:
        if action.vetoed_by:
            continue
        outcome = rx.report.predict_then_verify(ref, data, action)
        if outcome.accepted:
            stage = rx.Stage("apply", list(action.parameter_paths))
            result = ref.run_stage(data, stage, two_theta_limits=TT_LIMITS)
            improved = True
            save_checkpoint(result, iteration+1, reason="in_progress")
            break
    if not improved:
        break

# ── 4. Final save ──
save_checkpoint(result, n_iterations, reason=final_reason)
rx.write_refinement_cif(result, ref.structure, ref.instrument,
                        "<working_dir>/_agent_refined.cif")
```

## Key API patterns
```python
# Diagnostics — always branch on code
for d in result.diagnostics:
    print(d.level, d.code, d.where, d.suggestion)

# Report with suggested actions
report = rx.build_report(result)
# Or: report = ref.report(plan="mccusker_structural")

# Predict-then-verify (safe: tries on a branch)
outcome = rx.report.predict_then_verify(ref, data, action)

# Compare exchangeable params (zero vs displacement)
comparison = rx.report.compare_rivals(ref, data, finding)

# Hold parameters to prevent them from being freed
ref.hold(["phases.0.extinction", "phases.1.extinction"])

# Manual stage
result = ref.run_stage(data, rx.Stage("label", ["phases.*.cell.*"]),
                       two_theta_limits=TT_LIMITS)

# Save CIF (use rx.write_refinement_cif, not ref.write_cif)
rx.write_refinement_cif(result, ref.structure, ref.instrument, "output.cif")
```

## Diagnostic handling rules (from rietx SKILL.md §7)
| Code | Action |
|---|---|
| BACKGROUND_ABSORPTION | Stiffen background (fewer terms). Do NOT trust ADPs/scales. |
| BISO_NEGATIVE | Hold at physical value (0.5-1.0 Å²). Check species/occupancy. |
| BISO_UNUSUALLY_LARGE | Check species, occupancy, absorption. Hold if unphysical. |
| FLOOR_HIT | Hold at zero — the parameter is absent. |
| HIGH_CORRELATION | Fix one member of the pair. Never quote both as measured. |
| FLAT_DIRECTION | Fix one member. The data cannot separate them. |
| DATA_SUPPORT_LOW | Hold least-determined params from identifiability. |
| PHASE_UNCONSTRAINED | Do not quote anything about this phase. |
| TEXTURE_NOT_POSITIVE | Lower SH order or hold highest-order terms. |

## Stop conditions (from SKILL.md §10)
Stop when ALL THREE hold:
1. Every diagnostic is understood and resolved or reported as a caveat
2. report.abstained_reason is unset and no remaining region above significance gate
3. The next parameter group fails its t-ratio or ΔBIC

Whether Rwp is still falling decides NEITHER way.

## Rules
- Write ONE script. Do not write multiple scripts.
- After each action, print current Rwp% and chi2.
- Never free exchangeable parameters simultaneously.
- Do NOT exceed {max_iterations} improvement iterations after the initial fit.
- Save checkpoint IMMEDIATELY after initial fit, then after each improvement.
- Branch on diagnostic CODES, never on message text.
- Use rx.write_refinement_cif() for CIF output (not ref.write_cif).

## Output format
Save `_agent_result.json` with:
```python
result_data = {{
    "final_rwp_pct": round(result.statistics.rwp * 100, 2),
    "final_chi2": round(result.statistics.chi2, 4),
    "final_gof": round(result.statistics.gof, 4),
    "status": str(result.status),
    "n_iterations": <int>,
    "reason": "target_met" | "converged_best_effort" | "abstain",
    "summary": "<what you did, which diagnostics you addressed, and why you stopped>",
    "diagnostics_addressed": [<list of diagnostic codes you handled>],
    "diagnostics_remaining": [<list of unresolved diagnostic codes>],
    "history": [<list of dicts with action, rwp_pct, chi2 per step>],
}}
```
"""


# ── Setup script template ────────────────────────────────────────────────────

SETUP_SCRIPT = '''\
"""Agent state — written by the agentic refinement harness."""
import rietx as rx

structure = rx.Structure.from_cif({cif_path!r})
instrument = rx.Instrument.bragg_brentano(radiation={radiation!r})
instrument.background = rx.BackgroundChebyshev.with_terms({n_bg_terms})
data = rx.read_pattern({data_path!r})
TT_LIMITS = ({tt_min}, {tt_max})

{texture_code}

ref = rx.Refinement(structure, instrument)
'''


@dataclass
class AgenticResult:
    """Result from the agentic refinement subprocess."""

    final_rwp_pct: float | None
    final_chi2: float | None
    final_gof: float | None
    status: str
    reason: str
    summary: str
    n_iterations: int
    history: list[dict]
    refined_cif_path: Path | None
    raw_output: str


def run_agentic_refinement(
    cif_paths: list[Path],
    data_path: Path,
    *,
    two_theta_limits: tuple[float, float] = (10.0, 90.0),
    rwp_target_pct: float = 5.0,
    max_iterations: int = 10,
    material_name: str = "unknown",
    radiation: str = "CuKa",
    n_bg_terms: int = 6,
    initial_plan: str = "mccusker_structural",
    texture_code: str = "",
    model: str = "sonnet",
) -> AgenticResult:
    """
    Run agentic refinement via Claude CLI subprocess.

    Claude writes and executes Python code against rietx to iteratively
    improve the refinement.

    Parameters
    ----------
    cif_paths : list of Path
        CIF file paths for the structure(s).
    data_path : Path
        Path to the .xy data file.
    rwp_target_pct : float
        Target Rwp in percent (e.g. 5.0 means 5%).
    max_iterations : int
        Max improvement iterations the agent should attempt.
    material_name : str
        Name of the material for context.
    model : str
        Claude model alias ("sonnet", "haiku", "opus").
    """
    work_dir = Path(tempfile.mkdtemp(prefix="rietx_agent_"))

    # Handle multi-phase: merge CIFs by loading and re-saving
    if len(cif_paths) == 1:
        cif_path_str = str(cif_paths[0])
    else:
        cif_path_str = str(cif_paths[0])
        extra_lines = []
        for cp in cif_paths[1:]:
            extra_lines.append(
                f"_extra = rx.Structure.from_cif({str(cp)!r})\n"
                f"structure.phases.extend(_extra.phases)"
            )
        texture_code = "\n".join(extra_lines) + "\n" + texture_code

    # Write the setup script
    setup_code = SETUP_SCRIPT.format(
        cif_path=cif_path_str,
        radiation=radiation,
        n_bg_terms=n_bg_terms,
        data_path=str(data_path),
        tt_min=two_theta_limits[0],
        tt_max=two_theta_limits[1],
        texture_code=texture_code,
    )
    setup_path = work_dir / "_agent_state.py"
    setup_path.write_text(setup_code, encoding="utf-8")

    # Build the prompt
    prompt = (
        f"You are refining XRD data for {material_name}. "
        f"Target: Rwp < {rwp_target_pct:.1f}%. "
        f"Max {max_iterations} improvement iterations. "
        f"Suggested starting plan: '{initial_plan}'. "
        f"The state file is at: {setup_path}\n\n"
        f"Work in the directory: {work_dir}\n\n"
        f"Start by running the initial fit, check Rwp, then iterate using "
        f"suggest/predict_then_verify to improve. Write _agent_result.json when done."
    )

    system = SYSTEM_PROMPT.format(rwp_target_pct=rwp_target_pct, max_iterations=max_iterations)

    # On Windows, prefer claude.cmd over the POSIX shell script
    import sys
    claude_bin = shutil.which("claude.cmd") if sys.platform == "win32" else shutil.which("claude")
    if claude_bin is None:
        claude_bin = shutil.which("claude")
    if claude_bin is None:
        raise FileNotFoundError("Could not find 'claude' CLI in PATH")

    # Write system prompt to file to avoid Windows cmd argument length limits
    system_prompt_path = work_dir / "_system_prompt.txt"
    system_prompt_path.write_text(system, encoding="utf-8")

    cmd = [
        claude_bin,
        "-p",
        "--model", model,
        "--output-format", "text",
        "--system-prompt-file", str(system_prompt_path),
        "--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep",
        "--dangerously-skip-permissions",
        "--max-budget-usd", "2.0",
    ]

    logger.info(f"Launching agent subprocess...")

    try:
        # Use Popen with CREATE_NEW_PROCESS_GROUP on Windows so we can
        # kill the entire process tree on timeout.
        kwargs = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(work_dir),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(cmd, **kwargs)
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=900)
        except subprocess.TimeoutExpired:
            # Kill entire process tree on Windows
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                proc.kill()
            proc.wait()
            # Still check if the agent wrote a result file before timing out
            result_path = work_dir / "_agent_result.json"
            if result_path.exists():
                try:
                    result_data = json.loads(result_path.read_text(encoding="utf-8"))
                    cif_path = work_dir / "_agent_refined.cif"
                    return AgenticResult(
                        final_rwp_pct=result_data.get("final_rwp_pct"),
                        final_chi2=result_data.get("final_chi2"),
                        final_gof=result_data.get("final_gof"),
                        status=result_data.get("status", "timeout"),
                        reason=result_data.get("reason", "converged_best_effort"),
                        summary=result_data.get("summary", "") + " (timed out)",
                        n_iterations=result_data.get("n_iterations", 0),
                        history=result_data.get("history", []),
                        refined_cif_path=cif_path if cif_path.exists() else None,
                        raw_output="TIMEOUT — but result file found",
                    )
                except (json.JSONDecodeError, KeyError):
                    pass
            return AgenticResult(
                final_rwp_pct=None, final_chi2=None, final_gof=None,
                status="timeout", reason="abstain",
                summary="Agent subprocess timed out after 15 minutes.",
                n_iterations=0, history=[], refined_cif_path=None,
                raw_output="TIMEOUT",
            )

        raw_output = stdout + "\n" + stderr
    except subprocess.TimeoutExpired:
        return AgenticResult(
            final_rwp_pct=None, final_chi2=None, final_gof=None,
            status="timeout", reason="abstain",
            summary="Agent subprocess timed out after 15 minutes.",
            n_iterations=0, history=[], refined_cif_path=None,
            raw_output="TIMEOUT",
        )
    except Exception as e:
        return AgenticResult(
            final_rwp_pct=None, final_chi2=None, final_gof=None,
            status="error", reason="abstain",
            summary=f"Agent subprocess failed: {e}",
            n_iterations=0, history=[], refined_cif_path=None,
            raw_output=str(e),
        )

    # Parse the result file
    result_path = work_dir / "_agent_result.json"
    cif_path = work_dir / "_agent_refined.cif"

    if result_path.exists():
        try:
            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            return AgenticResult(
                final_rwp_pct=result_data.get("final_rwp_pct"),
                final_chi2=result_data.get("final_chi2"),
                final_gof=result_data.get("final_gof"),
                status=result_data.get("status", "unknown"),
                reason=result_data.get("reason", "converged_best_effort"),
                summary=result_data.get("summary", ""),
                n_iterations=result_data.get("n_iterations", 0),
                history=result_data.get("history", []),
                refined_cif_path=cif_path if cif_path.exists() else None,
                raw_output=raw_output,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse agent result: {e}")

    # Fallback: agent didn't write result file
    return AgenticResult(
        final_rwp_pct=None, final_chi2=None, final_gof=None,
        status="incomplete", reason="abstain",
        summary="Agent did not produce a result file. Check raw output.",
        n_iterations=0, history=[], refined_cif_path=None,
        raw_output=raw_output,
    )
