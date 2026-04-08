from pathlib import Path
import time

import pandas as pd
import streamlit as st


# -----------------------------------------------------------------------------
# Core configuration for this app.
# -----------------------------------------------------------------------------
# These are the four suspension outputs we track and display in the UI.
SETPOINT_COLS = ["FL_Setpoint", "FR_Setpoint", "BL_Setpoint", "BR_Setpoint"]
# Default value used when a setpoint has not been set yet.
DEFAULT_SETPOINT = 0.0
# Sensor logs are expected in ./logs/*.csv relative to this file.
LOG_DIR = Path(__file__).parent / "logs"

# Example format for incoming sensor logs.
#
# Notes:
# - "Time" is required by this app's timeline logic.
# - Any additional columns are treated as sensor channels.
# - Sensor names can be anything (Throttle, ShockFL, GPS_Speed, etc.).
SENSOR_CSV_EXAMPLE = """Time,SteeringAngle,ThrottlePct,BrakePct,ShockFL,ShockFR,ShockBL,ShockBR
0,0.0,0.12,0.00,67.1,67.3,66.9,67.0
1,1.8,0.15,0.00,67.0,67.2,66.8,66.9
2,3.2,0.21,0.00,66.8,67.0,66.7,66.8
3,2.4,0.19,0.05,66.9,67.1,66.8,66.9
"""

# Example format for processed output setpoints.
#
# Notes:
# - This is the exact schema produced and downloadable from the app.
# - One row per time tick, with the four corner setpoints.
OUTPUT_CSV_EXAMPLE = """Time,FL_Setpoint,FR_Setpoint,BL_Setpoint,BR_Setpoint
0,0.80,0.80,0.80,0.80
1,0.82,0.79,0.80,0.81
2,0.85,0.81,0.79,0.80
3,0.83,0.80,0.78,0.79
"""


class TickValue(int):
    """Integer-like tick value that is also callable for compatibility with time()."""

    def __call__(self) -> int:
        return int(self)


def list_log_files() -> list[Path]:
    # Return every CSV in ./logs so the dropdown can be auto-populated.
    if not LOG_DIR.exists():
        # Missing directory is allowed; UI will show a warning.
        return []
    return sorted(LOG_DIR.glob("*.csv"))


def load_sensor_log(log_path: Path) -> pd.DataFrame:
    # Read CSV into a dataframe; each non-Time column is considered a sensor.
    df = pd.read_csv(log_path)

    # Normalize headers so lookups are stable even if CSV headers contain extra spaces.
    df.columns = [str(col).strip() for col in df.columns]

    # If the user forgot a Time column, we synthesize one as 0..N-1.
    if "Time" not in df.columns:
        df.insert(0, "Time", range(len(df)))

    # Coerce Time into integer ticks and normalize any bad values to 0.
    df["Time"] = pd.to_numeric(df["Time"], errors="coerce").fillna(0).astype(int)

    # Always keep rows sorted by time so slider playback is predictable.
    return df.sort_values("Time").reset_index(drop=True)


def init_setpoints_from_sensor_log(sensor_df: pd.DataFrame) -> pd.DataFrame:
    # Build a setpoint table aligned to sensor log time ticks.
    # This gives us a setpoint row for each sensor timestamp.
    if sensor_df.empty:
        # Safe fallback when no log is loaded yet.
        base = pd.DataFrame({"Time": [0]})
    else:
        base = pd.DataFrame({"Time": sensor_df["Time"].astype(int).tolist()})

    # Initialize every setpoint column to a known value.
    for col in SETPOINT_COLS:
        base[col] = DEFAULT_SETPOINT

    return base


def ensure_state() -> None:
    # Streamlit reruns the script frequently; session_state persists between reruns.
    # We define all app-level state keys here once.
    st.session_state.setdefault("selected_log", None)
    st.session_state.setdefault("sensor_log_df", pd.DataFrame({"Time": [0]}))
    st.session_state.setdefault(
        "processed_setpoints_df",
        init_setpoints_from_sensor_log(st.session_state["sensor_log_df"]),
    )
    st.session_state.setdefault("current_time", 0)
    st.session_state.setdefault("max_time", 0)
    st.session_state.setdefault("play", False)
    # Playback speed in timeline ticks per second.
    st.session_state.setdefault("ticks_per_second", 5.0)
    # last_tick can be useful for future non-blocking play logic.
    st.session_state.setdefault("last_tick", time.time())
    # Starter code shown in the code editor tab.
    st.session_state.setdefault("code_text", """shock = max(0.0, min(100.0, get_sensor('ThrottlePct')))
set_each(
    fl=shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5),
    bl=shock/2, 
    fr=shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5),
    br=shock/2
)
# see Docs tab for available functions and example scripts""")


def clamp_time(value: int, max_time: int) -> int:
    # Clamp any user/app timeline movement into valid slider bounds.
    return max(0, min(int(value), int(max_time)))


def get_row_at_time(df: pd.DataFrame, current_time: int) -> pd.Series:
    # Return the row that best represents the requested time.
    # Priority:
    # 1) exact Time == current_time
    # 2) nearest previous Time <= current_time
    # 3) first row fallback
    if df.empty or "Time" not in df.columns:
        return pd.Series(dtype="object")

    exact = df[df["Time"] == current_time]
    if not exact.empty:
        return exact.iloc[0]

    previous = df[df["Time"] <= current_time]
    if not previous.empty:
        return previous.iloc[-1]

    return df.iloc[0]


def get_previous_row(df: pd.DataFrame, current_time: int) -> pd.Series:
    # Return the previous row strictly before current_time.
    # Used for delta calculations in output metrics.
    if df.empty or "Time" not in df.columns:
        return pd.Series(dtype="object")

    previous = df[df["Time"] < current_time]
    if previous.empty:
        return pd.Series(dtype="object")

    return previous.iloc[-1]


def execute_user_code(
    code_text: str,
    current_time: int,
    sensor_df: pd.DataFrame,
    setpoints_df: pd.DataFrame,
) -> tuple[pd.DataFrame, str | None]:
    # Execute user-authored setpoint code in a constrained runtime.
    #
    # Inputs available to user code:
    # - set(value): assign same value to all 4 corners at current time
    # - set_each(...): assign per-corner values
    # - get_sensor(name): read a sensor value at current time
    # - time: current timeline tick
    # - sensor: pandas Series for current sensor row
    #
    # Returns:
    # - updated dataframe if successful
    # - original dataframe + error string if execution fails
    #
    # Behavior note:
    # - Applies the code across ALL time ticks, not just the currently selected one.
    updated = setpoints_df.copy()

    if "Time" not in updated.columns:
        updated.insert(0, "Time", [current_time] * len(updated) if len(updated) > 0 else [current_time])

    # Use the union of times from output + sensor logs so code can process everything available.
    all_times = set(pd.to_numeric(updated["Time"], errors="coerce").fillna(0).astype(int).tolist())
    if not sensor_df.empty and "Time" in sensor_df.columns:
        all_times.update(pd.to_numeric(sensor_df["Time"], errors="coerce").fillna(0).astype(int).tolist())
    if not all_times:
        all_times = {int(current_time)}

    missing_times = sorted(all_times.difference(set(updated["Time"].astype(int).tolist())))
    if missing_times:
        new_rows = [{"Time": t, **{col: DEFAULT_SETPOINT for col in SETPOINT_COLS}} for t in missing_times]
        updated = pd.concat([updated, pd.DataFrame(new_rows)], ignore_index=True)

    for col in SETPOINT_COLS:
        if col not in updated.columns:
            updated[col] = DEFAULT_SETPOINT

    updated = updated.sort_values("Time").reset_index(drop=True)

    # Deliberately small set of builtins for safer execution.
    safe_builtins = {
        "min": min,
        "max": max,
        "abs": abs,
        "round": round,
        "float": float,
        "int": int,
    }

    for tick in sorted(all_times):
        # Locate target output row and source sensor row for this tick.
        row_index = updated.index[updated["Time"].astype(int) == int(tick)][0]
        sensor_row = get_row_at_time(sensor_df, int(tick))

        # Helper: set all four setpoints at once.
        def set_all(value: float) -> None:
            for col in SETPOINT_COLS:
                updated.at[row_index, col] = float(value)

        # Helper: set one or more corners individually.
        def set_each(
            fl: float | None = None,
            fr: float | None = None,
            bl: float | None = None,
            br: float | None = None,
        ) -> None:
            mapping = {
                "FL_Setpoint": fl,
                "FR_Setpoint": fr,
                "BL_Setpoint": bl,
                "BR_Setpoint": br,
            }
            for col, value in mapping.items():
                if value is not None:
                    updated.at[row_index, col] = float(value)

        # Helper: fetch a sensor by name with optional default.
        def get_sensor(name: str, default: float = 0.0) -> float:
            if sensor_row.empty:
                return float(default)

            lookup = str(name).strip().lower()
            raw_value = None

            # Fast path: exact match as provided.
            if name in sensor_row.index:
                raw_value = sensor_row[name]
            else:
                # Robust path: case-insensitive + whitespace-insensitive match.
                for column_name in sensor_row.index:
                    if str(column_name).strip().lower() == lookup:
                        raw_value = sensor_row[column_name]
                        break

            if raw_value is None:
                return float(default)

            # Treat blank/NA cells as missing sensor values and use default.
            if pd.isna(raw_value):
                return float(default)

            if isinstance(raw_value, str):
                raw_value = raw_value.strip()
                if raw_value == "":
                    return float(default)

            try:
                return float(raw_value)
            except (TypeError, ValueError):
                return float(default)

        # Scope exposed to user code.
        runtime_scope = {
            "set": set_all,
            "set_each": set_each,
            "get_sensor": get_sensor,
            "time": TickValue(int(tick)),
            "current_time": int(tick),
            "sensor": sensor_row,
        }

        try:
            # Execute user code for this tick.
            exec(code_text, {"__builtins__": safe_builtins}, runtime_scope)
        except Exception as exc:
            # Keep existing data if code fails; surface error with failing tick.
            return setpoints_df, f"Time {int(tick)}: {exc}"

    return updated, None


# Streamlit page setup should happen before most UI rendering.
st.set_page_config(layout="wide")

# Hide Streamlit menu + deploy button
hide_streamlit_style = """
<style>
#MainMenu {visibility: hidden;}
header {visibility: hidden;}
footer {visibility: hidden;}
[data-testid="stToolbar"] {display: none;}
</style>
"""
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

# Make output metric cards larger and prevent text clipping for long numeric values.
st.markdown(
    """
<style>
[data-testid="stButton"] > button,
[data-testid="stDownloadButton"] > button {
    min-height: 3rem;
    padding: 0.6rem 0.9rem;
    font-size: 1.05rem;
    border-radius: 0.7rem;
}

[data-testid="stLinkButton"] a {
    min-height: 3rem;
    padding: 0.6rem 0.9rem;
    border-radius: 0.7rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}

[data-testid="stMetric"] {
    padding: 0.9rem 1.1rem;
    border-radius: 0.75rem;
    min-height: 7.2rem;
}

[data-testid="stMetricValue"] {
    font-size: 2rem;
    line-height: 1.15;
    white-space: normal;
    overflow: visible;
    text-overflow: clip;
}

[data-testid="stMetricDelta"] {
    font-size: 1rem;
}
</style>
""",
    unsafe_allow_html=True,
)

# Initialize app state on first run.
ensure_state()

# Discover available log files for dropdown.
log_paths = list_log_files()
log_options = [path.name for path in log_paths]

if not log_options:
    st.warning("No CSV logs found in ./logs. Add logs to enable sensor playback.")

if st.session_state["selected_log"] not in log_options and log_options:
    # Auto-select the first discovered log.
    st.session_state["selected_log"] = log_options[0]

with st.container():
    log_col, timing_col, download_col, logo_col = st.columns([1.5, 3, 1, 2])

    with log_col:
        # Left-side controls for selecting/opening input logs.
        log_col_selector, log_col_link = st.columns([5, 1])
        with log_col_selector:
            selected_option = st.selectbox(
                "Select Log:",
                log_options if log_options else ["No logs available"],
                label_visibility="collapsed",
                disabled=not log_options,
            )

            if log_options and selected_option != st.session_state["selected_log"]:
                # Track currently selected file name.
                st.session_state["selected_log"] = selected_option

            if log_options:
                # Load selected CSV.
                selected_path = next(path for path in log_paths if path.name == st.session_state["selected_log"])
                loaded_sensor_df = load_sensor_log(selected_path)
                if (
                    st.session_state["sensor_log_df"].empty
                    or st.session_state["selected_log"] != st.session_state.get("loaded_log_name")
                ):
                    # Only reset tables when switching logs (not every rerun).
                    st.session_state["sensor_log_df"] = loaded_sensor_df
                    st.session_state["processed_setpoints_df"] = init_setpoints_from_sensor_log(loaded_sensor_df)
                    st.session_state["max_time"] = int(loaded_sensor_df["Time"].max()) if not loaded_sensor_df.empty else 0
                    st.session_state["current_time"] = clamp_time(st.session_state["current_time"], st.session_state["max_time"])
                    st.session_state["loaded_log_name"] = st.session_state["selected_log"]

        with log_col_link:
            if log_options:
                selected_path = next(path for path in log_paths if path.name == st.session_state["selected_log"])
                st.link_button("", selected_path.resolve().as_uri(), icon="↗️", help="Open log CSV in new tab")
            else:
                st.button("", icon="↗️", disabled=True)

    with timing_col:
        # Timeline controls (reset, back, play/pause, forward, direct slider drag).
        timing_col_reset, timing_col_backstep, timing_col_play, timing_col_frontstep, timing_col_speed = st.columns(
            [1, 1, 1, 1, 3],
            # vertical_alignment="center",
            gap="small",
        )

        with timing_col_reset:
            reset = st.button("🔃", help="Reset timeline", use_container_width=True)

        with timing_col_backstep:
            backstep = st.button("⬅️", help="Step back", use_container_width=True)

        with timing_col_play:
            play_label = "⏸️" if st.session_state["play"] else "▶️"
            toggle_play = st.button(play_label, help="Play/Pause", use_container_width=True)

        with timing_col_frontstep:
            frontstep = st.button("➡️", help="Step forward", use_container_width=True)

        with timing_col_speed:
            # Keep speed label + control on one tight horizontal line on the right.
            # make the text right next to the slider, and vertically centered with the buttons to the left.

            speed_label_col, speed_slider_col = st.columns([3, 7], vertical_alignment="center", gap="small")
            with speed_label_col:
                speed_label_col.markdown("**Ticks/s:**")
            with speed_slider_col:
                speed_value = st.slider(
                    "Ticks/sec",
                    min_value=0.1,
                    max_value=30.0,
                    value=(st.session_state["ticks_per_second"]),
                    step=1.0,
                    label_visibility="collapsed",
                    width="stretch"
                )
            st.session_state["ticks_per_second"] = float(speed_value)

        if reset:
            # Reset always returns to the first tick and pauses playback.
            st.session_state["play"] = False
            st.session_state["current_time"] = 0

        if backstep:
            # Manual stepping pauses playback to prevent competing timeline updates.
            st.session_state["play"] = False
            st.session_state["current_time"] = clamp_time(
                st.session_state["current_time"] - 1,
                st.session_state["max_time"],
            )

        if frontstep:
            # Manual stepping pauses playback to prevent competing timeline updates.
            st.session_state["play"] = False
            st.session_state["current_time"] = clamp_time(
                st.session_state["current_time"] + 1,
                st.session_state["max_time"],
            )

        if toggle_play:
            # Toggle playback mode.
            st.session_state["play"] = not st.session_state["play"]
            st.session_state["last_tick"] = time.time()

        # Primary timeline slider.
        # Disabled during playback so only one source controls time movement.
        slider_time = st.slider(
            "Timeline",
            0,
            st.session_state["max_time"],
            st.session_state["current_time"],
            label_visibility="collapsed",
            width="stretch",
            disabled=st.session_state["play"],
        )
        if slider_time != st.session_state["current_time"] and not st.session_state["play"]:
            # Sync slider movement back into session state.
            st.session_state["current_time"] = slider_time

    with download_col:
        # Quick run stats.
        st.caption(f"Time: {st.session_state['current_time']} / {st.session_state['max_time']}")
        # st.caption(f"Rows: {len(st.session_state['sensor_log_df'])}")
        st.caption(f"Speed: {st.session_state['ticks_per_second']:.1f} ticks/sec")

    with logo_col:
        st.subheader("CWRU Baja - [cwru-baja](https://github.com/cwru-baja)", divider="red")

    st.divider()

sensor_log_df = st.session_state["sensor_log_df"]
processed_setpoints_df = st.session_state["processed_setpoints_df"]
current_time = st.session_state["current_time"]
max_time = st.session_state["max_time"]

with st.container():
    input_col, output_col = st.columns(2)
    with input_col:
        st.subheader("Input", divider="green")
        sensors_tab, code_tab, docs_tab = st.tabs(["Sensors", "Code", "Docs"], default="Code")

        with sensors_tab:
            # Build a display table from one row of sensor data at current time.
            # We rotate from "wide" (columns are sensors) to a 2-column table.
            sensor_row = get_row_at_time(sensor_log_df, current_time)
            sensor_names = [name for name in sensor_row.index if name != "Time"]
            sensors_df = pd.DataFrame(
                {
                    "Name": sensor_names,
                    "Value": [sensor_row[name] for name in sensor_names],
                }
            )
            st.dataframe(sensors_df, hide_index=True, use_container_width=True)

            # Read-only mirror slider to show synchronized time in this tab.
            st.slider(
                "Sensor_Timeline",
                0,
                max_time,
                current_time,
                label_visibility="collapsed",
                width="stretch",
                disabled=True,
            )

        with code_tab:
            # User code editor for setpoint rules.
            code_text = st.text_area(
                "Code Area",
                key="code_text",
                label_visibility="collapsed",
                height=200,
                placeholder="""shock = max(0.0, min(100.0, get_sensor('ThrottlePct')))
set_each(
    fl=shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5),
    bl=shock/2, 
    fr=shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5),
    br=shock/2
)
# see Docs tab for available functions and example scripts""",
            )

            # Prepare an "apply" result so save can always export the latest computed output.
            prepared_df, prepared_error = execute_user_code(
                code_text,
                current_time,
                sensor_log_df,
                processed_setpoints_df,
            )

            # Keep Apply and Save on the same row.
            apply_col, save_col = st.columns(2)

            with apply_col:
                # Apply current code across all timeline ticks.
                if st.button("Apply to all times", use_container_width=True):
                    if prepared_error:
                        st.error(f"Code error: {prepared_error}")
                    else:
                        st.session_state["processed_setpoints_df"] = prepared_df
                        st.success("Applied code across all times")

            with save_col:
                # Save downloads CSV that includes the same apply logic first.
                save_clicked = st.download_button(
                    "Save (apply + download)",
                    data=prepared_df.to_csv(index=False).encode("utf-8") if not prepared_error else b"",
                    file_name="processed_setpoints.csv",
                    mime="text/csv",
                    use_container_width=True,
                    disabled=prepared_error is not None,
                )

                if save_clicked and not prepared_error:
                    st.session_state["processed_setpoints_df"] = prepared_df
                    st.success("Applied code across all times and downloaded CSV")

            if prepared_error:
                st.caption(f"Save disabled until code is valid. Latest error: {prepared_error}")

            st.caption("See the Docs tab for available functions and example scripts.")

        with docs_tab:
            st.subheader("Documentation")

            st.markdown("### What this code runner does")
            st.markdown(
                """
- When you press **Apply to all times**, your script is executed once for every timeline tick.
- On each tick, your script can read sensor data and update setpoints for that same tick.
- If your script errors on any tick, processing stops and you get the first failing tick in the error message.
"""
            )

            st.markdown("### Function reference")

            st.markdown("#### set(value)")
            st.markdown("- Inputs: `value` (float-like)")
            st.markdown("- Output: none")
            st.markdown("- Explanation: Sets FL, FR, BL, and BR to the same value for the current tick.")
            st.code("set(0.8)", language="python")

            st.markdown("#### set_each(fl=None, fr=None, bl=None, br=None)")
            st.markdown("- Inputs: optional float-like values for each corner")
            st.markdown("- Output: none")
            st.markdown("- Explanation: Sets only the corners you provide; omitted corners keep existing values.")
            st.code("set_each(fl=0.9, fr=0.9, bl=0.75, br=0.75)", language="python")

            st.markdown("#### get_sensor(name, default=0.0)")
            st.markdown("- Inputs: `name` (string sensor column name), `default` (float-like fallback)")
            st.markdown("- Output: float")
            st.markdown(
                "- Explanation: Reads a sensor value for the current tick. If missing/blank/invalid, returns `default`."
            )
            st.code("throttle = get_sensor('ThrottlePct', 0.0)", language="python")

            st.markdown("#### time()")
            st.markdown("- Inputs: none")
            st.markdown("- Output: int")
            st.markdown("- Explanation: Returns the current tick index being processed.")
            st.code("cur = time()", language="python")

            st.markdown("#### min(a, b), max(a, b)")
            st.markdown("- Inputs: numeric values")
            st.markdown("- Output: numeric value")
            st.markdown("- Explanation: Clamp or compare values in your control logic.")
            st.code("target = min(1.0, max(0.0, 0.35 + get_sensor('ThrottlePct', 0.0)))", language="python")

            st.markdown("#### abs(x)")
            st.markdown("- Inputs: numeric value")
            st.markdown("- Output: numeric value")
            st.markdown("- Explanation: Absolute magnitude, useful for distance from center/zero.")
            st.code("steer_mag = abs(get_sensor('SteeringAngle', 0.0))", language="python")

            st.markdown("#### round(x, ndigits=0)")
            st.markdown("- Inputs: numeric value, optional precision")
            st.markdown("- Output: numeric value")
            st.markdown("- Explanation: Rounds computed values before applying them.")
            st.code("set(round(0.6 + get_sensor('ThrottlePct', 0.0) * 0.4, 3))", language="python")

            st.markdown("#### float(x), int(x)")
            st.markdown("- Inputs: value convertible to number")
            st.markdown("- Output: converted number")
            st.markdown("- Explanation: Explicit numeric conversion when needed.")
            st.code("raw = get_sensor('ShockFL', 67.0)\nset(float(raw) / 100.0)", language="python")

            st.markdown("### Runtime variables")
            st.code(
                """time
- Type: int-like value
- Meaning: current tick (same value returned by time())

current_time
- Type: int
- Meaning: alias for current tick value

sensor
- Type: row-like object
- Meaning: sensor values for current tick; e.g. sensor['ThrottlePct']
""",
                language="python",
            )

            st.markdown("### What you can NOT use")
            st.markdown(
                """
- `import` statements are not supported.
- File and OS operations are not available (`open`, `os`, `pathlib`, etc.).
- Network calls are not available (`requests`, sockets, etc.).
- Most builtins are not available (`sum`, `len`, `print`, `range`, `list`, `dict`, etc.).
- App internals are not in scope (`st`, `pd`, `SETPOINT_COLS`, and other module globals).
- You cannot directly edit arbitrary rows; each execution step updates the current tick being processed.
"""
            )

            st.markdown("### Sensor CSV (input)")
            st.code(SENSOR_CSV_EXAMPLE, language="csv")
            st.caption("Place files with this shape in ./logs and select them from the dropdown.")

            st.markdown("### Processed setpoints CSV (output)")
            st.code(OUTPUT_CSV_EXAMPLE, language="csv")
            st.caption("This is the same structure produced by the Save button in the Code tab.")

            st.markdown("### Example scripts")
            st.code(
                """# 1) Constant setpoint everywhere
set(0.8)

# 2) Corner-specific static values
set_each(fl=0.92, fr=0.92, bl=0.78, br=0.78)

# 3) Direct throttle mapping
throttle = get_sensor('ThrottlePct', 0.0)
set(0.5 + 0.5 * throttle)

# 4) Clamp with min/max to keep values in bounds
throttle = get_sensor('ThrottlePct', 0.0)
target = min(1.0, max(0.0, 0.35 + throttle * 0.9))
set(target)

# 5) Brake-biased front loading
brake = get_sensor('BrakePct', 0.0)
set_each(
    fl=0.70 + 0.30 * brake,
    fr=0.70 + 0.30 * brake,
    bl=0.70 - 0.10 * brake,
    br=0.70 - 0.10 * brake,
)

# 6) Left-right split from steering
steer = get_sensor('SteeringAngle', 0.0)
split = max(-0.12, min(0.12, steer / 300.0))
base = 0.8
set_each(fl=base + split, bl=base + split, fr=base - split, br=base - split)

# 7) Use time as a value
phase = (time % 40) / 40.0
set(0.7 + 0.2 * phase)

# 8) Use time() as a function (supported)
t = time()
if t < 100:
    set(0.75)
else:
    set(0.85)

# 9) Mixed sensor fusion example
throttle = get_sensor('ThrottlePct', 0.0)
brake = get_sensor('BrakePct', 0.0)
shock_fl = get_sensor('ShockFL', 67.0)
heave = max(-0.05, min(0.05, (shock_fl - 67.0) * 0.02))
base = 0.65 + throttle * 0.25 - brake * 0.20
set_each(
    fl=base + heave,
    fr=base + heave,
    bl=base - heave,
    br=base - heave,
)

# 10) Minimal safe fallback style
value = get_sensor('SomeMissingSensor', 0.8)
set(min(1.0, max(0.0, value)))
""",
                language="python",
            )

            st.markdown("### Invalid examples (do not use)")
            st.code(
                """# Not allowed: imports
import math

# Not allowed: filesystem
f = open('out.txt', 'w')

# Not allowed: unavailable builtins
set(sum([0.1, 0.2, 0.3]))

# Not allowed: app globals
print(st.session_state)
""",
                language="python",
            )

    with output_col:
        st.subheader("Output", divider="orange")

        _, live_output, car_img, _ = st.columns([1, 6, 3, 1])

        with live_output:
            # Pull current and previous setpoint rows for metric and delta display.
            setpoint_row = get_row_at_time(st.session_state["processed_setpoints_df"], current_time)
            previous_row = get_previous_row(st.session_state["processed_setpoints_df"], current_time)

            FL_setpoint = float(setpoint_row.get("FL_Setpoint", DEFAULT_SETPOINT))
            FR_setpoint = float(setpoint_row.get("FR_Setpoint", DEFAULT_SETPOINT))
            BL_setpoint = float(setpoint_row.get("BL_Setpoint", DEFAULT_SETPOINT))
            BR_setpoint = float(setpoint_row.get("BR_Setpoint", DEFAULT_SETPOINT))

            FL_delta = FL_setpoint - float(previous_row.get("FL_Setpoint", FL_setpoint))
            FR_delta = FR_setpoint - float(previous_row.get("FR_Setpoint", FR_setpoint))
            BL_delta = BL_setpoint - float(previous_row.get("BL_Setpoint", BL_setpoint))
            BR_delta = BR_setpoint - float(previous_row.get("BR_Setpoint", BR_setpoint))

            FL_cell, FR_cell = st.columns(2, vertical_alignment="center")
            BL_cell, BR_cell = st.columns(2)

            with FL_cell:
                st.metric(label="FL", value=f"{FL_setpoint:.2f}", delta=f"{FL_delta:+.2f}")

            with FR_cell:
                st.metric(label="FR", value=f"{FR_setpoint:.2f}", delta=f"{FR_delta:+.2f}")

            with BL_cell:
                st.metric(label="BL", value=f"{BL_setpoint:.2f}", delta=f"{BL_delta:+.2f}")

            with BR_cell:
                st.metric(label="BR", value=f"{BR_setpoint:.2f}", delta=f"{BR_delta:+.2f}")

        with car_img:
            st.image(
                "https://png.pngtree.com/png-vector/20230110/ourmid/pngtree-car-top-view-image-png-image_6557068.png",
                width="stretch",
            )

        st.slider(
            "Suspension_Timeline",
            0,
            max_time,
            current_time,
            label_visibility="collapsed",
            width="stretch",
            disabled=True,
        )

if st.session_state["play"]:
    # Keep playback moving at configured ticks-per-second while preserving user controls.
    ticks_per_second = max(float(st.session_state.get("ticks_per_second", 1.0)), 0.1)
    time.sleep(1.0 / ticks_per_second)
    st.session_state["current_time"] = clamp_time(
        st.session_state["current_time"] + 1,
        st.session_state["max_time"],
    )
    if st.session_state["current_time"] >= st.session_state["max_time"]:
        st.session_state["play"] = False
    st.rerun()
