"""Streamlit XRD Rietveld Refinement App — powered by rietx."""

import io
import tempfile
import warnings
import zipfile
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st

import rietx

try:
    from agentic_refinement import run_agentic_refinement, AgenticResult
    _AGENTIC_AVAILABLE = True
except ImportError:
    _AGENTIC_AVAILABLE = False

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ── Paths ────────────────────────────────────────────────────────────────────
APP_DIR = Path(__file__).resolve().parent
PRESET_CIFS = {
    "NMC811 (R-3m)": [APP_DIR / "preset_cifs" / "NMC811_R3m.cif"],
    "NCA (R-3m)": [APP_DIR / "preset_cifs" / "NCA_R3m.cif"],
    "Beta-Li2TiO3 (C2/c)": [APP_DIR / "preset_cifs" / "cod_1515995.cif"],
    "LAS glass-ceramic (4-phase)": [
        APP_DIR / "preset_cifs" / "SiO2_154_(cod_9005017)-1.cif",
        APP_DIR / "preset_cifs" / "LiAlSiO4_180_(cod_9002378)-7.cif",
        APP_DIR / "preset_cifs" / "SiO2_92_(cod_9009686)-3.cif",
        APP_DIR / "preset_cifs" / "Li4Al3.99996Si8.00004O24_96_(cod_9008140)-None.cif",
    ],
}

# Recommended refinement parameters per preset (texture, background, 2-theta range)
PRESET_PARAMS = {
    "NMC811 (R-3m)":          {"texture": "None",                 "sh_order": 8, "bg_terms": 6,  "tt_min": 10.0, "tt_max": 90.0},
    "NCA (R-3m)":             {"texture": "None",                 "sh_order": 8, "bg_terms": 6,  "tt_min": 10.0, "tt_max": 90.0},
    "Beta-Li2TiO3 (C2/c)":   {"texture": "Spherical Harmonics",  "sh_order": 8, "bg_terms": 8,  "tt_min": 10.0, "tt_max": 90.0},
    "LAS glass-ceramic (4-phase)": {"texture": "Spherical Harmonics", "sh_order": 4, "bg_terms": 12, "tt_min": 10.0, "tt_max": 80.0},
}

RWP_THRESHOLDS = {
    "Excellent": 0.10,
    "Acceptable": 0.15,
    "Marginal": 0.20,
}

PLAN_PRESETS = list(rietx.PLAN_PRESETS.keys())


def rwp_quality(rwp: float) -> str:
    if rwp < 0.10:
        return "Excellent"
    if rwp < 0.15:
        return "Acceptable"
    if rwp < 0.20:
        return "Marginal"
    return "Poor"


def rwp_color(quality: str) -> str:
    return {
        "Excellent": "green",
        "Acceptable": "blue",
        "Marginal": "orange",
        "Poor": "red",
    }.get(quality, "gray")


# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="XRD Rietveld Refinement",
    page_icon="🔬",
    layout="wide",
)

st.title("XRD Rietveld Refinement")
st.caption(f"Powered by rietx {rietx.__version__}")

# ── Sidebar: Configuration ───────────────────────────────────────────────────
with st.sidebar:
    st.header("Configuration")

    # -- CIF source --
    st.subheader("Crystal Structure")
    cif_source = st.radio("CIF source", ["Built-in preset", "Upload CIF"], horizontal=True)

    if cif_source == "Built-in preset":
        preset_name = st.selectbox("Select structure", list(PRESET_CIFS.keys()))
        cif_paths = PRESET_CIFS[preset_name]
        uploaded_cifs = None

        n_phases = len(cif_paths)
        if n_phases > 1:
            st.caption(f"Multiphase preset: {n_phases} phases")

        # -- Recommended settings toggle --
        use_recommended = st.checkbox("Use recommended settings", value=True)
    else:
        uploaded_cifs = st.file_uploader(
            "Upload .cif file(s)", type=["cif"], accept_multiple_files=True,
        )
        cif_paths = None
        preset_name = None
        use_recommended = False

    # Resolve recommended params (or fall back to manual defaults)
    rec = PRESET_PARAMS.get(preset_name, {}) if use_recommended else {}

    # -- Texture model --
    st.subheader("Texture Correction")
    if use_recommended and "texture" in rec:
        texture_model = rec["texture"]
        st.info(f"Preset: {texture_model}" + (f" (order {rec['sh_order']})" if texture_model == "Spherical Harmonics" else ""))
        sh_order = rec.get("sh_order")
        md_axis = None
    else:
        texture_model = st.selectbox(
            "Texture model",
            ["None", "Spherical Harmonics", "March-Dollase"],
        )
        sh_order = None
        md_axis = None
        if texture_model == "Spherical Harmonics":
            sh_order = st.slider("SH order", min_value=2, max_value=10, value=8, step=2)
        elif texture_model == "March-Dollase":
            st.caption("Preferred orientation axis (hkl)")
            col_h, col_k, col_l = st.columns(3)
            md_h = col_h.number_input("h", value=0, step=1)
            md_k = col_k.number_input("k", value=0, step=1)
            md_l = col_l.number_input("l", value=2, step=1)
            md_axis = (int(md_h), int(md_k), int(md_l))

    # -- Background --
    st.subheader("Background")
    if use_recommended and "bg_terms" in rec:
        n_bg_terms = rec["bg_terms"]
        st.info(f"Preset: {n_bg_terms} Chebyshev terms")
    else:
        n_bg_terms = st.slider("Chebyshev terms", min_value=3, max_value=12, value=6)

    # -- 2-theta range --
    st.subheader("2-theta range")
    if use_recommended and "tt_min" in rec:
        tt_min = rec["tt_min"]
        tt_max = rec["tt_max"]
        st.info(f"Preset: {tt_min:.0f} - {tt_max:.0f} deg")
    else:
        col_min, col_max = st.columns(2)
        tt_min = col_min.number_input("Min", value=10.0, step=1.0)
        tt_max = col_max.number_input("Max", value=90.0, step=1.0)

    # -- Refinement mode --
    st.subheader("Refinement Mode")
    mode_options = ["Single Pass"]
    if _AGENTIC_AVAILABLE:
        mode_options.append("Agentic (AI-driven)")
    refinement_mode = st.radio(
        "Mode",
        mode_options,
        horizontal=True,
        help="Agentic mode uses Claude to iteratively improve the fit" if _AGENTIC_AVAILABLE else "Single pass refinement",
    )

    # -- Refinement plan --
    st.subheader("Refinement Plan")
    plan_name = st.selectbox("Plan", PLAN_PRESETS, index=PLAN_PRESETS.index("mccusker_structural"))

    if refinement_mode == "Agentic (AI-driven)":
        st.subheader("Agentic Settings")
        rwp_target_pct = st.number_input("Rwp target (%)", value=5.0, min_value=1.0, max_value=30.0, step=0.5)
        max_agent_iterations = st.slider("Max iterations", min_value=3, max_value=20, value=10)
        agent_model = st.selectbox("Claude model", [
            "sonnet",
            "haiku",
            "opus",
        ], help="Sonnet is a good balance; Haiku is faster; Opus is most capable")

    # -- Radiation --
    st.subheader("Instrument")
    radiation = st.selectbox("Radiation", ["CuKa", "CoKa", "MoKa", "FeKa", "CrKa", "AgKa"])


# ── Main area: File upload ───────────────────────────────────────────────────
st.header("1. Upload XRD Data")
uploaded_files = st.file_uploader(
    "Upload .xy or .xye files (2-theta, intensity)",
    type=["xy", "xye"],
    accept_multiple_files=True,
)

if uploaded_files:
    st.success(f"{len(uploaded_files)} file(s) uploaded")


# ── Preview raw data ─────────────────────────────────────────────────────────
def parse_xy_bytes(content: bytes) -> tuple[np.ndarray, np.ndarray]:
    """Parse .xy file content into two-theta and intensity arrays."""
    lines = content.decode("utf-8", errors="ignore").strip().splitlines()
    tt, intensity = [], []
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            try:
                tt.append(float(parts[0]))
                intensity.append(float(parts[1]))
            except ValueError:
                continue
    return np.array(tt), np.array(intensity)


if uploaded_files:
    with st.expander("Preview raw patterns", expanded=False):
        fig_raw, ax_raw = plt.subplots(figsize=(10, 4))
        for uf in uploaded_files:
            tt, intensity = parse_xy_bytes(uf.getvalue())
            ax_raw.plot(tt, intensity, label=uf.name, linewidth=0.8)
        ax_raw.set_xlabel("2θ (degrees)")
        ax_raw.set_ylabel("Intensity")
        ax_raw.legend(fontsize=7)
        ax_raw.set_title("Raw XRD patterns")
        st.pyplot(fig_raw)
        plt.close(fig_raw)


# ── Load structure helper ────────────────────────────────────────────────────
def load_structure(cif_paths, uploaded_cifs) -> rietx.Structure:
    """Load structure from preset path(s) or uploaded file(s)."""
    if cif_paths is not None:
        all_phases = []
        for cp in cif_paths:
            s = rietx.Structure.from_cif(str(cp))
            all_phases.extend(s.phases)
        return rietx.Structure(phases=all_phases)
    elif uploaded_cifs:
        all_phases = []
        for uc in uploaded_cifs:
            with tempfile.NamedTemporaryFile(suffix=".cif", delete=False) as tmp:
                tmp.write(uc.getvalue())
                tmp.flush()
                s = rietx.Structure.from_cif(tmp.name)
                all_phases.extend(s.phases)
        return rietx.Structure(phases=all_phases)
    else:
        st.error("No CIF file provided.")
        st.stop()


def apply_texture(structure: rietx.Structure, texture_model, sh_order, md_axis):
    """Apply texture correction to all phases."""
    for phase in structure.phases:
        if texture_model == "Spherical Harmonics":
            phase.texture = rietx.SphericalHarmonicTexture(order=sh_order)
        elif texture_model == "March-Dollase":
            phase.preferred_orientation = rietx.PreferredOrientation(axis=md_axis)
    return structure


# ── Run refinement ───────────────────────────────────────────────────────────
st.header("2. Refinement")

if not uploaded_files:
    st.info("Upload .xy files above to begin.")
    st.stop()

if cif_source == "Upload CIF" and not uploaded_cifs:
    st.warning("Please upload one or more CIF files in the sidebar.")
    st.stop()

if cif_source == "Built-in preset" and cif_paths is None:
    st.warning("No preset CIF paths configured.")
    st.stop()

run_button = st.button("Run Refinement", type="primary", use_container_width=True)

if run_button and refinement_mode == "Single Pass":
    results_list = []
    result_objects = {}
    refinement_objects = {}
    data_objects = {}
    refined_cifs = {}

    progress_bar = st.progress(0, text="Starting refinement...")

    for i, uf in enumerate(uploaded_files):
        sample_name = Path(uf.name).stem
        progress_bar.progress(
            (i) / len(uploaded_files),
            text=f"Refining {sample_name} ({i+1}/{len(uploaded_files)})...",
        )

        # Write uploaded data to temp file for rietx
        with tempfile.NamedTemporaryFile(suffix=".xy", delete=False, mode="wb") as tmp:
            tmp.write(uf.getvalue())
            tmp_path = tmp.name

        try:
            # Load data
            data = rietx.read_pattern(tmp_path)

            # Fresh structure each time
            structure = load_structure(cif_paths, uploaded_cifs)
            structure = apply_texture(structure, texture_model, sh_order, md_axis)

            # Instrument
            instrument = rietx.Instrument.bragg_brentano(radiation=radiation)
            instrument.background = rietx.BackgroundChebyshev.with_terms(n_bg_terms)

            # Refine
            ref = rietx.Refinement(structure, instrument)
            result = ref.fit(
                data,
                plan=plan_name,
                two_theta_limits=(tt_min, tt_max),
                telemetry=False,
            )

            stats = result.statistics
            quality = rwp_quality(stats.rwp)

            # Extract lattice parameters (all phases)
            n_ph = len(structure.phases)
            lattice_all = {}
            for ph_idx in range(n_ph):
                lattice_all[ph_idx] = {}
                for pname in ["a", "b", "c", "alpha", "beta", "gamma"]:
                    try:
                        p = result.parameter(f"phases.{ph_idx}.cell.{pname}")
                        if p.stderr is not None:
                            lattice_all[ph_idx][pname] = {"value": p.value, "stderr": p.stderr}
                    except Exception:
                        pass

            # Save refined CIF to bytes
            with tempfile.NamedTemporaryFile(suffix=".cif", delete=False) as tmp_cif:
                ref.write_cif(tmp_cif.name)
                cif_bytes = Path(tmp_cif.name).read_bytes()
            refined_cifs[sample_name] = cif_bytes

            results_list.append({
                "Sample": sample_name,
                "Status": str(result.status),
                "Rwp (%)": round(stats.rwp * 100, 2),
                "Rp (%)": round(stats.rp * 100, 2),
                "χ²": round(stats.chi2, 4),
                "GoF": round(stats.gof, 4),
                "N phases": n_ph,
                "N params": stats.n_free_parameters,
                "Quality": quality,
                "Warnings": len(result.diagnostics),
            })
            for pname, vals in lattice_all.get(0, {}).items():
                results_list[-1][pname] = round(vals["value"], 6)

            result_objects[sample_name] = result
            refinement_objects[sample_name] = ref
            data_objects[sample_name] = data

        except Exception as e:
            results_list.append({
                "Sample": sample_name,
                "Status": "FAILED",
                "Rwp (%)": None,
                "Rp (%)": None,
                "χ²": None,
                "GoF": None,
                "N params": None,
                "Quality": "Failed",
                "Warnings": None,
            })
            st.error(f"**{sample_name}** failed: {e}")

    progress_bar.progress(1.0, text="Refinement complete!")

    # Store results in session state for persistence
    st.session_state["results_list"] = results_list
    st.session_state["result_objects"] = result_objects
    st.session_state["refinement_objects"] = refinement_objects
    st.session_state["data_objects"] = data_objects
    st.session_state["refined_cifs"] = refined_cifs

# ── Agentic refinement ──────────────────────────────────────────────────────
if run_button and refinement_mode == "Agentic (AI-driven)":
    results_list = []
    result_objects = {}
    refinement_objects = {}
    data_objects = {}
    refined_cifs = {}
    agent_results = {}

    for i, uf in enumerate(uploaded_files):
        sample_name = Path(uf.name).stem

        # Write uploaded data to temp file
        with tempfile.NamedTemporaryFile(suffix=".xy", delete=False, mode="wb") as tmp:
            tmp.write(uf.getvalue())
            tmp_path = tmp.name

        # Resolve CIF paths for the agent
        if cif_paths is not None:
            agent_cif_paths = [Path(p) for p in cif_paths]
        elif uploaded_cifs:
            agent_cif_paths = []
            for uc in uploaded_cifs:
                with tempfile.NamedTemporaryFile(suffix=".cif", delete=False) as tc:
                    tc.write(uc.getvalue())
                    agent_cif_paths.append(Path(tc.name))
        else:
            st.error("No CIF files available.")
            continue

        # Build texture code for the agent's setup script
        tex_code = ""
        if texture_model == "Spherical Harmonics" and sh_order:
            tex_code = f"for phase in structure.phases:\n    phase.texture = rx.SphericalHarmonicTexture(order={sh_order})"
        elif texture_model == "March-Dollase" and md_axis:
            tex_code = f"for phase in structure.phases:\n    phase.preferred_orientation = rx.PreferredOrientation(axis={md_axis})"

        try:
            st.subheader(f"Agentic refinement: {sample_name}")
            status_text = st.empty()
            status_text.info("Claude agent subprocess is running... (this may take a few minutes)")

            agentic_result = run_agentic_refinement(
                cif_paths=agent_cif_paths,
                data_path=Path(tmp_path),
                two_theta_limits=(tt_min, tt_max),
                rwp_target_pct=rwp_target_pct,
                max_iterations=max_agent_iterations,
                material_name=preset_name or sample_name,
                radiation=radiation,
                n_bg_terms=n_bg_terms,
                initial_plan=plan_name,
                texture_code=tex_code,
                model=agent_model,
            )

            # Show finish status
            reason_emoji = {"target_met": "✅", "converged_best_effort": "🔶", "abstain": "🔴"}
            emoji = reason_emoji.get(agentic_result.reason, "❓")
            status_text.markdown(
                f"**Result:** {emoji} {agentic_result.reason} — {agentic_result.summary}"
            )

            # Show iteration history if available
            if agentic_result.history:
                st.caption(f"{agentic_result.n_iterations} iterations")
                st.dataframe(pd.DataFrame(agentic_result.history), hide_index=True)

            # Show agent's raw output in expander
            with st.expander("Agent raw output"):
                st.code(agentic_result.raw_output[:5000], language="text")

            # Load the refined result back into rietx for downstream display
            if agentic_result.final_rwp_pct is not None:
                data = rietx.read_pattern(tmp_path)
                # Try loading the agent's refined CIF; fall back to original
                # CIFs if the refined CIF has multiple data blocks that
                # rietx.Structure.from_cif cannot handle.
                try:
                    structure = load_structure(
                        [agentic_result.refined_cif_path] if agentic_result.refined_cif_path else cif_paths,
                        None,
                    )
                except Exception:
                    structure = load_structure(cif_paths, uploaded_cifs)
                instrument = rietx.Instrument.bragg_brentano(radiation=radiation)
                instrument.background = rietx.BackgroundChebyshev.with_terms(n_bg_terms)
                apply_texture(structure, texture_model, sh_order, md_axis)
                ref = rietx.Refinement(structure, instrument)
                result = ref.fit(data, plan=plan_name, two_theta_limits=(tt_min, tt_max), telemetry=False)

                stats = result.statistics
                quality = rwp_quality(agentic_result.final_rwp_pct / 100.0)

                if agentic_result.refined_cif_path and agentic_result.refined_cif_path.exists():
                    cif_bytes = agentic_result.refined_cif_path.read_bytes()
                else:
                    with tempfile.NamedTemporaryFile(suffix=".cif", delete=False) as tmp_cif:
                        ref.write_cif(tmp_cif.name)
                        cif_bytes = Path(tmp_cif.name).read_bytes()
                refined_cifs[sample_name] = cif_bytes

                results_list.append({
                    "Sample": sample_name,
                    "Status": f"{agentic_result.status} ({agentic_result.reason})",
                    "Rwp (%)": agentic_result.final_rwp_pct,
                    "Rp (%)": round(stats.rp * 100, 2),
                    "χ²": agentic_result.final_chi2,
                    "GoF": agentic_result.final_gof,
                    "N phases": len(structure.phases),
                    "N params": stats.n_free_parameters,
                    "Quality": quality,
                    "Warnings": len(result.diagnostics),
                    "Agent iters": agentic_result.n_iterations,
                })

                result_objects[sample_name] = result
                refinement_objects[sample_name] = ref
                data_objects[sample_name] = data
                agent_results[sample_name] = agentic_result
            else:
                results_list.append({
                    "Sample": sample_name,
                    "Status": f"FAILED ({agentic_result.reason})",
                    "Rwp (%)": None, "Rp (%)": None, "χ²": None,
                    "GoF": None, "N params": None,
                    "Quality": "Failed", "Warnings": None,
                })

        except Exception as e:
            results_list.append({
                "Sample": sample_name,
                "Status": "FAILED",
                "Rwp (%)": None, "Rp (%)": None, "χ²": None,
                "GoF": None, "N params": None,
                "Quality": "Failed", "Warnings": None,
            })
            st.error(f"**{sample_name}** failed: {e}")

    st.session_state["results_list"] = results_list
    st.session_state["result_objects"] = result_objects
    st.session_state["refinement_objects"] = refinement_objects
    st.session_state["data_objects"] = data_objects
    st.session_state["refined_cifs"] = refined_cifs
    st.session_state["agent_results"] = agent_results

# ── Display results ──────────────────────────────────────────────────────────
if "results_list" in st.session_state:
    results_list = st.session_state["results_list"]
    result_objects = st.session_state["result_objects"]
    refinement_objects = st.session_state.get("refinement_objects", {})
    data_objects = st.session_state.get("data_objects", {})
    refined_cifs = st.session_state["refined_cifs"]

    # -- Summary table --
    st.header("3. Results")
    df = pd.DataFrame(results_list)

    def color_quality(val):
        colors = {
            "Excellent": "background-color: #d4edda",
            "Acceptable": "background-color: #d1ecf1",
            "Marginal": "background-color: #fff3cd",
            "Poor": "background-color: #f8d7da",
            "Failed": "background-color: #f8d7da",
        }
        return colors.get(val, "")

    st.dataframe(
        df.style.map(color_quality, subset=["Quality"]),
        use_container_width=True,
        hide_index=True,
    )

    # -- Agent iteration history --
    agent_results = st.session_state.get("agent_results", {})
    if agent_results:
        st.header("Agent Iteration History")
        for sname, ar in agent_results.items():
            if ar.history:
                st.subheader(f"{sname} — {ar.n_iterations} iterations → {ar.reason}")
                st.caption(ar.summary)

                hist_df = pd.DataFrame(ar.history)
                st.dataframe(hist_df, hide_index=True, use_container_width=True)

                # Rwp convergence chart
                rwp_col = next((c for c in hist_df.columns if "rwp" in c.lower()), None)
                if len(hist_df) > 1 and rwp_col:
                    fig_conv, ax_conv = plt.subplots(figsize=(8, 3))
                    ax_conv.plot(range(len(hist_df)), hist_df[rwp_col], "o-", color="steelblue", linewidth=2)
                    ax_conv.set_xlabel("Step")
                    ax_conv.set_ylabel("Rwp (%)")
                    ax_conv.set_title(f"{sname} — Rwp convergence")
                    fig_conv.tight_layout()
                    st.pyplot(fig_conv)
                    plt.close(fig_conv)

    # -- Per-sample details --
    st.header("4. Fit Details")

    if len(result_objects) == 1:
        sample_name = list(result_objects.keys())[0]
        selected_sample = sample_name
    else:
        selected_sample = st.selectbox(
            "Select sample",
            list(result_objects.keys()),
        )

    if selected_sample and selected_sample in result_objects:
        result = result_objects[selected_sample]
        stats = result.statistics

        # Use agent's reported values if available (the re-fit from CIF
        # loses the agent's optimised instrument state, so its Rwp is worse)
        agent_results = st.session_state.get("agent_results", {})
        ar = agent_results.get(selected_sample)
        display_rwp = ar.final_rwp_pct / 100.0 if ar and ar.final_rwp_pct is not None else stats.rwp
        display_chi2 = ar.final_chi2 if ar and ar.final_chi2 is not None else stats.chi2
        display_gof = ar.final_gof if ar and ar.final_gof is not None else stats.gof

        # -- Fit quality badge --
        quality = rwp_quality(display_rwp)
        color = rwp_color(quality)
        st.markdown(
            f"### {selected_sample} &nbsp; "
            f"<span style='color:{color}; font-size:1.1em;'>Rwp = {display_rwp*100:.2f}% ({quality})</span>",
            unsafe_allow_html=True,
        )

        # -- Fit plot --
        fig = result.plot()
        st.pyplot(fig)
        plt.close(fig)

        # -- Lattice parameters --
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Lattice Parameters")
            # Detect number of phases from refined parameters
            phase_indices = set()
            for p in result.parameters:
                if p.path.startswith("phases.") and ".cell." in p.path:
                    phase_indices.add(int(p.path.split(".")[1]))

            for ph_idx in sorted(phase_indices):
                if len(phase_indices) > 1:
                    try:
                        ph_name = result.parameter(f"phases.{ph_idx}.scale").path.replace(".scale", "")
                    except Exception:
                        ph_name = f"phases.{ph_idx}"
                    st.caption(f"Phase {ph_idx}")

                lattice_rows = []
                for pname in ["a", "b", "c", "alpha", "beta", "gamma"]:
                    try:
                        p = result.parameter(f"phases.{ph_idx}.cell.{pname}")
                        if p.stderr is not None:
                            unit = "deg" if pname in ("alpha", "beta", "gamma") else "Å"
                            lattice_rows.append({
                                "Parameter": pname,
                                "Value": f"{p.value:.6f}",
                                "Std Error": f"{p.stderr:.6f}",
                                "Unit": unit,
                            })
                    except Exception:
                        pass
                if lattice_rows:
                    st.dataframe(pd.DataFrame(lattice_rows), hide_index=True)

        with col2:
            st.subheader("Fit Statistics")
            st.metric("Rwp", f"{display_rwp*100:.2f}%")
            st.metric("Rp", f"{stats.rp*100:.2f}%")
            st.metric("χ²", f"{display_chi2:.4f}")
            st.metric("GoF", f"{display_gof:.4f}")
            if ar and ar.final_rwp_pct is not None:
                st.caption(f"Agent best · re-fit from CIF: {stats.rwp*100:.2f}%")

        # -- All refined parameters --
        with st.expander("All Refined Parameters"):
            param_rows = []
            for p in result.parameters:
                if p.stderr is not None:
                    param_rows.append({
                        "Parameter": p.path,
                        "Value": f"{p.value:.6f}",
                        "Std Error": f"{p.stderr:.6f}",
                    })
            if param_rows:
                st.dataframe(pd.DataFrame(param_rows), hide_index=True, height=400)

        # -- Diagnostics --
        with st.expander(f"Diagnostics ({len(result.diagnostics)} warnings)"):
            if result.diagnostics:
                for d in result.diagnostics:
                    lines = str(d).split("\n")
                    header = lines[0]
                    if "warning" in header.lower():
                        st.warning(str(d))
                    elif "info" in header.lower():
                        st.info(str(d))
                    else:
                        st.text(str(d))
            else:
                st.success("No diagnostics reported.")

    # -- Peak Indexing --
    st.header("5. Peak Indexing")

    if selected_sample and selected_sample in refinement_objects and selected_sample in data_objects:
        ref_obj = refinement_objects[selected_sample]
        data_obj = data_objects[selected_sample]

        # Pick peaks
        inst_pk = rietx.Instrument.bragg_brentano(radiation=radiation)
        peaks = rietx.pick_peaks(data_obj, inst_pk, two_theta_range=(tt_min, tt_max))
        usable = peaks.usable()

        # Reflection table from refinement (hkl list)
        rt = ref_obj.reflection_table()
        pred_tt = np.array([r.two_theta for r in rt])

        MATCH_TOL = 0.15  # degrees

        # Build matched table
        peak_rows = []
        n_matched = 0
        for j, p in enumerate(usable):
            diffs = np.abs(pred_tt - p.two_theta)
            idx_best = int(np.argmin(diffs))
            delta = diffs[idx_best]
            r = rt[idx_best]

            if delta <= MATCH_TOL:
                hkl_str = f"({r.h}, {r.k}, {r.l})"
                matched = True
                n_matched += 1
            else:
                hkl_str = "?"
                matched = False

            peak_rows.append({
                "#": j + 1,
                "2θ obs (°)": round(p.two_theta, 4),
                "d (Å)": round(p.d, 4),
                "Intensity": round(p.intensity, 1),
                "FWHM (°)": round(p.fwhm, 4),
                "hkl": hkl_str,
                "2θ pred (°)": round(r.two_theta, 4) if matched else "—",
                "Δ2θ (°)": round(delta, 4) if matched else "—",
            })

        st.markdown(f"**{len(usable)}** peaks detected, **{n_matched}** matched to hkl indices (tolerance {MATCH_TOL}°)")

        # Table
        df_peaks = pd.DataFrame(peak_rows)
        st.dataframe(df_peaks, hide_index=True, use_container_width=True)

        # Stick plot
        with st.expander("Peak indexing plot", expanded=True):
            tt_arr = np.array(data_obj.two_theta)
            int_arr = np.array(data_obj.intensity)

            fig_idx, ax_idx = plt.subplots(figsize=(12, 4))
            ax_idx.plot(tt_arr, int_arr, "k-", linewidth=0.5, label="Observed")

            # Tick marks for predicted reflections
            ax_idx.vlines(
                [r.two_theta for r in rt],
                -max(int_arr) * 0.08, -max(int_arr) * 0.02,
                colors="blue", linewidth=0.8, label="Predicted hkl",
            )

            # Mark observed peaks
            obs_tt = [p.two_theta for p in usable]
            obs_int = [p.intensity for p in usable]
            ax_idx.plot(obs_tt, obs_int, "rv", markersize=5, label="Observed peaks")

            # Label matched hkl on strongest peaks
            for p in sorted(usable, key=lambda x: -x.intensity)[:10]:
                diffs = np.abs(pred_tt - p.two_theta)
                idx = int(np.argmin(diffs))
                if diffs[idx] <= MATCH_TOL:
                    r = rt[idx]
                    ax_idx.annotate(
                        f"({r.h}{r.k}{r.l})",
                        (p.two_theta, p.intensity),
                        textcoords="offset points", xytext=(0, 10),
                        fontsize=7, ha="center", color="blue",
                    )

            ax_idx.set_xlabel("2θ (degrees)")
            ax_idx.set_ylabel("Intensity")
            ax_idx.set_title(f"{selected_sample} — Peak Indexing")
            ax_idx.set_xlim(tt_min, tt_max)
            ax_idx.legend(fontsize=8, loc="upper right")
            fig_idx.tight_layout()
            st.pyplot(fig_idx)
            plt.close(fig_idx)

        # Full reflection table
        with st.expander("Full reflection table (all predicted reflections)"):
            rt_rows = []
            for r in rt:
                rt_rows.append({
                    "h": r.h, "k": r.k, "l": r.l,
                    "2θ (°)": round(r.two_theta, 4),
                    "d (Å)": round(r.d, 4),
                    "Multiplicity": r.multiplicity,
                    "Intensity": round(r.intensity, 2),
                })
            st.dataframe(pd.DataFrame(rt_rows), hide_index=True, use_container_width=True, height=400)
    else:
        st.info("Run a refinement to see peak indexing.")

    # -- CIF 3D Visualizer --
    st.header("6. Crystal Structure")

    if selected_sample and selected_sample in refined_cifs:
        cif_text = refined_cifs[selected_sample].decode("utf-8", errors="ignore")

        view_style = st.selectbox(
            "Display style",
            ["Ball and Stick", "Stick", "Sphere"],
        )
        supercell = st.checkbox("Show 2×2×2 supercell", value=True)
        show_labels = st.checkbox("Show atom labels", value=True)

        style_map = {
            "Ball and Stick": "{'stick': {'radius': 0.15}, 'sphere': {'radius': 0.4}}",
            "Stick": "{'stick': {'radius': 0.2}}",
            "Sphere": "{'sphere': {'radius': 0.6}}",
        }
        style_js = style_map[view_style]

        supercell_js = ""
        if supercell:
            supercell_js = "viewer.addUnitCell(); viewer.replicateUnitCell(2, 2, 2);"

        viewer_html = f"""
        <script src="https://3Dmol.org/build/3Dmol-min.js"></script>
        <div id="viewer" style="width: 100%; height: 500px; position: relative;"></div>
        <script>
            var viewer = $3Dmol.createViewer("viewer", {{backgroundColor: "white"}});
            var cifData = `{cif_text.replace(chr(96), "'").replace(chr(92), chr(92)+chr(92))}`;
            viewer.addModel(cifData, "cif");
            {supercell_js}
            viewer.setStyle({{}}, {style_js});
            viewer.addUnitCell({{box: true, color: "black", linewidth: 1.0}});
            {"" if not show_labels else """
            var atoms = viewer.getModel().selectedAtoms({});
            for (var i = 0; i < atoms.length; i++) {
                var atom = atoms[i];
                viewer.addLabel(atom.elem, {
                    position: atom,
                    fontSize: 12,
                    fontColor: "black",
                    backgroundOpacity: 0.7,
                    backgroundColor: "white",
                    borderColor: "gray",
                    borderThickness: 1,
                    alignment: "center",
                });
            }
            """}
            viewer.zoomTo();
            viewer.render();
        </script>
        """
        st.components.v1.html(viewer_html, height=520, scrolling=False)
    else:
        st.info("Run a refinement to visualize the crystal structure.")

    # -- Downloads --
    st.header("7. Downloads")

    col_dl1, col_dl2 = st.columns(2)

    with col_dl1:
        if selected_sample and selected_sample in refined_cifs:
            st.download_button(
                f"Download {selected_sample} refined CIF",
                data=refined_cifs[selected_sample],
                file_name=f"{selected_sample}_refined.cif",
                mime="chemical/x-cif",
            )

    with col_dl2:
        if len(refined_cifs) > 1:
            # Zip all CIFs
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for name, cif_bytes in refined_cifs.items():
                    zf.writestr(f"{name}_refined.cif", cif_bytes)
            zip_buffer.seek(0)
            st.download_button(
                "Download all refined CIFs (zip)",
                data=zip_buffer.getvalue(),
                file_name="refined_cifs.zip",
                mime="application/zip",
            )

    # Parameters CSV download
    if selected_sample and selected_sample in result_objects:
        result = result_objects[selected_sample]
        csv_rows = []
        for p in result.parameters:
            if p.stderr is not None:
                csv_rows.append({
                    "parameter": p.path,
                    "value": p.value,
                    "stderr": p.stderr,
                })
        if csv_rows:
            csv_df = pd.DataFrame(csv_rows)
            st.download_button(
                f"Download {selected_sample} parameters (CSV)",
                data=csv_df.to_csv(index=False),
                file_name=f"{selected_sample}_parameters.csv",
                mime="text/csv",
            )
