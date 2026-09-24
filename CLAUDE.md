# XRD Pipeline — Claude Code Context

## What this project is

A semi-automated XRD analysis pipeline for crystalline inorganic materials (primary target: NMC — Li-Ni-Mn-Co-O system). Two analysis levels:

- **Level 1 — Phase identification**: Which phases are present? Uses DARA (CederGroupHub, `dara-xrd` PyPI package, requires BGMN binary) with a pymatgen peak-matching fallback when BGMN is not installed.
- **Level 2 — Rietveld refinement**: Extract lattice params, occupancies, phase fractions. Uses GSAS-II Python scripting (`GSASIIscriptable`).

Both levels have mandatory human checkpoints before proceeding.

## Delivery rules (IMPORTANT)

- **Only `.ipynb` notebooks — no `.py` files, no module folders.**
- All code is inline in notebook cells. Notebooks are fully self-contained.
- One notebook per analysis level.

## Current state

- **Phase A complete**: `notebooks/notebook_01_phase_id.ipynb` — full Level 1 pipeline (load → preprocess → MP fetch → simulate → score → CP1 human selection → save checkpoint).
- **Phase B not started**: `notebook_02_rietveld.ipynb` — GSAS-II staged Rietveld refinement. Blocked until GSAS-II is installed.
- **Phase C not started**: LLM integration at checkpoints + synthesis project link.

## Key technical decisions

| Decision | Choice |
|---|---|
| Data format | `.xy` / `.xye` text (2- or 3-column) |
| Chemical system | NMC (Li-Ni-Mn-Co-O), pre-cached from Materials Project |
| Phase ID engine | `dara-xrd` (BGMN backend); pymatgen fallback if BGMN absent |
| Rietveld engine | GSAS-II + `GSASIIscriptable` (not yet installed) |
| Workflow style | Interactive Jupyter notebooks |

## Install requirements

See `INSTALL.md` for:
- BGMN binary (for DARA)
- GSAS-II (manual install, not pip)
- Materials Project API key (`MP_API_KEY` env var)

```
pip install numpy scipy matplotlib pymatgen mp-api tqdm ipywidgets dara-xrd
```

## Notebook 01 — what it does

1. Load `.xy`/`.xye` → `XRDPattern` dataclass
2. SNIP background subtraction + Rachinger Kα2 strip
3. Fetch NMC phase candidates from Materials Project (disk-cached)
4. Simulate patterns with `pymatgen.analysis.diffraction.xrd.XRDCalculator` (disk-cached per phase)
5. Score candidates: DARA (if BGMN present) or peak-matching
6. Visualise top-5 overlays + peak maps
7. **CP1**: human fills `SELECTED_INDICES`, saves `checkpoints/cp1_selection.json` + `.pkl`

## Phase B plan (notebook 02 — not yet built)

GSAS-II staged Rietveld, reading from `cp1_selected_candidates.pkl`. Refinement order:
1. Background (Chebyshev, 3 then 6 terms)
2. Instrument zero
3. Scale factor
4. Lattice parameters
5. Uiso (thermal displacement)
6. Atomic positions
7. Site occupancies (last — most correlated)
8. Phase fractions (last)

Convergence: χ² must decrease each stage; revert + flag if it increases. Rwp targets: <10% excellent, <15% acceptable, >20% = human review.

## Broader project context

This pipeline feeds into the `synthesis/` project (NMC811 calcination kinetics) — XRD phase fractions validate ReactCA phase-evolution predictions vs. experiment.
