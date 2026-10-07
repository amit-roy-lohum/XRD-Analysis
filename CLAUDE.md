# XRD Pipeline — Claude Code Context

## What this project is

A semi-automated XRD analysis pipeline for crystalline inorganic materials . Two analysis levels:

- **Level 1 — Phase identification**: Which phases are present? Uses DARA (CederGroupHub, `dara-xrd` PyPI package, requires BGMN binary).
- **Level 2 — Rietveld refinement**: Extract lattice params, occupancies, phase fractions. Does initial refinement using BGMN an later uses GSAS2. Uses GSAS-II Python scripting (`GSASIIscriptable`).

Both levels have mandatory human checkpoints before proceeding.

## Delivery rules (IMPORTANT)

- **Only `.ipynb` notebooks — no `.py` files, no module folders.**
- All code is inline in notebook cells. Notebooks are fully self-contained.
- One notebook per analysis level.


## Key technical decisions

| Decision | Choice |
|---|---|
| Data format | `.xy` / `.xye` text (2- or 3-column) |
| Phase ID engine | `dara-xrd` (BGMN backend)|
| Rietveld engine | GSAS-II + `GSASIIscriptable` |
| Workflow style | Interactive Jupyter notebooks |



Convergence: χ² must decrease each stage; revert + flag if it increases. Rwp targets: <10% excellent, <15% acceptable, >20% = human review.
