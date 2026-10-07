"""debug_winlock.py
Diagnoses [WinError 32] "file used by another process" in the phase_loop pipeline.

Run from the project root:
    python debug_winlock.py

Sections (run top-to-bottom; comment out sections you've already passed):
  SECTION 1 – temp-file ops (mkstemp, unlink)
  SECTION 2 – CIF read via pymatgen (file handle leak?)
  SECTION 3 – Ray init (does Ray lock anything in the project dir?)
  SECTION 4 – single-file search_phases (num_cpus=1, then num_cpus=default)
  SECTION 5 – who holds the lock (Windows handle scanner)
"""

import os, re, sys, time, traceback, subprocess, ctypes, ctypes.wintypes
from pathlib import Path

# ── repo paths (edit if needed) ──────────────────────────────────────────────
PROJECT_DIR   = Path(__file__).parent
DATA_DIR      = PROJECT_DIR / "all_xy_files"
CIFS_DIR      = PROJECT_DIR / "cifs"
WAVELENGTH    = "Cu"
INSTRUMENT    = "Aeris-fds-Pixcel1d-Medipix3"
EXPRESS_MODE  = False

sep = "=" * 70

# ─────────────────────────────────────────────────────────────────────────────
# Helper: print which processes hold a lock on a file (Windows only)
# Uses the built-in `openfiles` command (requires admin) or handle64.exe if present.
# Falls back to a pure-Python method via RestartManager API.
# ─────────────────────────────────────────────────────────────────────────────
def who_locks(path: str) -> list[str]:
    """Return list of 'PID: <n>  Name: <name>' strings for processes locking `path`."""
    results = []
    path = str(Path(path).resolve())

    # Method 1: Windows RestartManager API (no admin needed, Vista+)
    try:
        RM_WRITE_STATUS = 0
        CCH_RM_MAX_APP_NAME = 255
        CCH_RM_MAX_SVC_NAME = 63

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [("dwProcessId", ctypes.wintypes.DWORD),
                        ("ProcessStartTime", ctypes.wintypes.FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [("Process",         RM_UNIQUE_PROCESS),
                        ("strAppName",      ctypes.c_wchar * (CCH_RM_MAX_APP_NAME + 1)),
                        ("strServiceShortName", ctypes.c_wchar * (CCH_RM_MAX_SVC_NAME + 1)),
                        ("ApplicationType", ctypes.c_int),
                        ("AppStatus",       ctypes.wintypes.ULONG),
                        ("TSSessionId",     ctypes.wintypes.DWORD),
                        ("bRestartable",    ctypes.c_bool)]

        rstrtmgr = ctypes.WinDLL("Rstrtmgr.dll")
        session_handle = ctypes.wintypes.DWORD()
        session_key    = ctypes.create_unicode_buffer(ccchBuf := ctypes.c_ulong(256).value)
        ret = rstrtmgr.RmStartSession(ctypes.byref(session_handle), 0, session_key)
        if ret == 0:
            lpcchBuf = ctypes.c_uint(len(path) + 1)
            files    = (ctypes.c_wchar_p * 1)(path)
            rstrtmgr.RmRegisterResources(session_handle, 1, files, 0, None, 0, None)
            nProcInfoNeeded = ctypes.c_uint(0)
            nProcInfo       = ctypes.c_uint(0)
            lpdwRebootReasons = ctypes.wintypes.DWORD(RM_WRITE_STATUS)
            # first call to get count
            rstrtmgr.RmGetList(session_handle, ctypes.byref(nProcInfoNeeded),
                               ctypes.byref(nProcInfo), None,
                               ctypes.byref(lpdwRebootReasons))
            n = nProcInfoNeeded.value
            if n:
                proc_info = (RM_PROCESS_INFO * n)()
                nProcInfo = ctypes.c_uint(n)
                rstrtmgr.RmGetList(session_handle, ctypes.byref(nProcInfoNeeded),
                                   ctypes.byref(nProcInfo), proc_info,
                                   ctypes.byref(lpdwRebootReasons))
                for i in range(nProcInfo.value):
                    pid  = proc_info[i].Process.dwProcessId
                    name = proc_info[i].strAppName
                    results.append(f"PID: {pid}  Name: {name}")
            rstrtmgr.RmEndSession(session_handle)
    except Exception as e:
        results.append(f"[RestartManager unavailable: {e}]")

    return results or ["[no lockers found or access denied]"]


# ─────────────────────────────────────────────────────────────────────────────
# Patch: intercept every open() / Path.unlink to log WinError 32 in context
# ─────────────────────────────────────────────────────────────────────────────
_real_open   = open
_real_unlink = Path.unlink

def _patched_open(file, mode="r", *args, **kwargs):
    try:
        return _real_open(file, mode, *args, **kwargs)
    except PermissionError as e:
        if getattr(e, "winerror", None) == 32:
            print(f"\n[LOCK INTERCEPT] open({file!r}, {mode!r})")
            print(f"  Error: {e}")
            for holder in who_locks(str(file)):
                print(f"  Holder -> {holder}")
            traceback.print_stack()
        raise

def _patched_unlink(self, missing_ok=False):
    try:
        return _real_unlink(self, missing_ok=missing_ok)
    except PermissionError as e:
        if getattr(e, "winerror", None) == 32:
            print(f"\n[LOCK INTERCEPT] Path.unlink({self!r})")
            print(f"  Error: {e}")
            for holder in who_locks(str(self)):
                print(f"  Holder -> {holder}")
            traceback.print_stack()
        raise

import builtins
builtins.open = _patched_open
Path.unlink   = _patched_unlink


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — basic temp-file round-trip (validates mkstemp fix)
# ─────────────────────────────────────────────────────────────────────────────
print(sep)
print("SECTION 1: temp-file round-trip")
print(sep)
try:
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".cif")
    os.write(fd, b"data_test\n_cell_length_a 4.0\n")
    os.close(fd)
    content = Path(tmp).read_text()
    Path(tmp).unlink()
    print(f"  OK — wrote and deleted temp file: {tmp}")
except Exception:
    traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — CIF handle leak check via pymatgen
# ─────────────────────────────────────────────────────────────────────────────
print()
print(sep)
print("SECTION 2: pymatgen CifParser handle leak")
print(sep)
try:
    cif_files = sorted(CIFS_DIR.glob("*.cif"))[:3]
    if not cif_files:
        print("  SKIP — no CIF files found in cifs/")
    else:
        from pymatgen.io.cif import CifParser
        for cif_path in cif_files:
            try:
                parser = CifParser(str(cif_path), occupancy_tolerance=10.0)
                _ = parser.parse_structures(primitive=False)
                # Try to delete a copy — checks if pymatgen left a handle open
                import shutil
                tmp_copy = Path(tempfile.mktemp(suffix=".cif"))
                shutil.copy2(cif_path, tmp_copy)
                tmp_copy.unlink()
                print(f"  OK  {cif_path.name}")
            except Exception:
                traceback.print_exc()
except ImportError:
    print("  pymatgen not importable; skip")


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — Ray init (does it lock project files?)
# ─────────────────────────────────────────────────────────────────────────────
print()
print(sep)
print("SECTION 3: Ray init / shutdown cycle")
print(sep)
try:
    import ray
    if ray.is_initialized():
        print("  Shutting down stale Ray…")
        ray.shutdown()
        time.sleep(2)

    print("  Initialising Ray (num_cpus=1, no working_dir)…")
    ray.init(num_cpus=1)          # <-- no runtime_env; avoids dir-packaging
    print(f"  Ray alive: {ray.is_initialized()}")

    @ray.remote
    def _dummy():
        return "hello from worker"

    result = ray.get(_dummy.remote())
    print(f"  Ray task result: {result}")
    ray.shutdown()
    print("  Ray shut down cleanly.")
except Exception:
    traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — single search_phases call (num_cpus=1 first)
# ─────────────────────────────────────────────────────────────────────────────
print()
print(sep)
print("SECTION 4: search_phases on ONE file (num_cpus=1)")
print(sep)

def strip_xy_header(path: Path) -> Path:
    clean_path = path.parent / (path.stem + "_clean.xy")
    if clean_path.exists() and clean_path.stat().st_mtime >= path.stat().st_mtime:
        return clean_path
    lines = path.read_text(encoding="utf-8").splitlines()
    data_lines = [l for l in lines if re.match(r"^\s*[\d.\-]", l)]
    clean_path.write_text("\n".join(data_lines), encoding="utf-8")
    return clean_path

xy_files = sorted(f for f in DATA_DIR.glob("*.xy") if not f.stem.endswith("_clean"))

if not xy_files:
    print(f"  SKIP — no .xy files in {DATA_DIR}")
else:
    target = xy_files[0]
    print(f"  File: {target.name}")
    clean_path = strip_xy_header(target)
    print(f"  Clean copy: {clean_path.name}")

    curated_cifs = sorted(CIFS_DIR.resolve().glob("*.cif"))
    print(f"  CIF count: {len(curated_cifs)}")

    try:
        import ray
        from dara import search_phases

        if ray.is_initialized():
            ray.shutdown()
            time.sleep(2)

        # --- Test A: num_cpus=1  (serialises all BGMN workers) ---
        print("\n  [Test A] num_cpus=1 …")
        ray.init(num_cpus=1)
        try:
            results = search_phases(
                pattern_path=str(clean_path),
                phases=curated_cifs,
                wavelength=WAVELENGTH,
                instrument_profile=INSTRUMENT,
                express_mode=EXPRESS_MODE,
            )
            print(f"  Test A OK — {len(results)} solution(s)")
        except PermissionError:
            traceback.print_exc()
        finally:
            ray.shutdown()
            time.sleep(2)

        # --- Test B: default num_cpus (parallel) ---
        print("\n  [Test B] default num_cpus (parallel) …")
        ray.init()
        try:
            results = search_phases(
                pattern_path=str(clean_path),
                phases=curated_cifs,
                wavelength=WAVELENGTH,
                instrument_profile=INSTRUMENT,
                express_mode=EXPRESS_MODE,
            )
            print(f"  Test B OK — {len(results)} solution(s)")
        except PermissionError:
            traceback.print_exc()
        finally:
            ray.shutdown()

    except ImportError as e:
        print(f"  SKIP — import error: {e}")
    except Exception:
        traceback.print_exc()


print()
print(sep)
print("Debug run complete. Look above for [LOCK INTERCEPT] or traceback output.")
print(sep)
