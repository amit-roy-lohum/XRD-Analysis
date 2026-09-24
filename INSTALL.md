# Installation Guide

## 1. Python environment

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

---

## 2. BGMN (required for DARA phase identification)

BGMN is a standalone Rietveld software used internally by `dara-xrd`.

### Download
- Website: https://www.bgmn.de
- Download the Windows binary (`bgmn.exe` + accompanying files)
- Extract to a stable directory, e.g. `C:\bgmn\`

### Configure
Set the path in `config.py` (or via environment variable):
```python
BGMN_PATH = r"C:\bgmn\bgmn.exe"
```
Or set environment variable:
```bash
setx BGMN_PATH "C:\bgmn\bgmn.exe"
```

### Verify
```bash
C:\bgmn\bgmn.exe --version
```

---

## 3. GSAS-II (required for Rietveld refinement — Phase B)

GSAS-II is not pip-installable. It ships its own Python interpreter and must be installed separately.

### Download & Install
1. Go to: https://subversion.xray.aps.anl.gov/trac/pyGSAS
2. Download the Windows installer (`gsas2full` package)
3. Run installer — installs to e.g. `C:\GSASII\`

### Python scripting path
The scripting interface (`GSASIIscriptable.py`) is inside the GSAS-II installation.
Add the path to `config.py`:
```python
GSASII_PATH = r"C:\GSASII"
```
Or set environment variable:
```bash
setx GSASII_PATH "C:\GSASII"
```

### Verify
```python
import sys
sys.path.insert(0, r"C:\GSASII")
import GSASIIscriptable as G2sc
print("GSAS-II loaded OK")
```

---

## 4. Materials Project API key

Register at https://materialsproject.org to get a free API key.

Set in environment or `.env` file:
```bash
setx MP_API_KEY "your_key_here"
```

Or pass directly to `PipelineConfig(mp_api_key="your_key")`.

---

## 5. Verify Phase A setup

```bash
cd xrd_pipeline
python -c "from ingestion.parser import load_xrd; print('parser OK')"
python -c "from phase_id.candidate_fetch import fetch_candidates; print('fetch OK')"
```
