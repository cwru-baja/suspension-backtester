



# how to run:
# 1) pip install streamlit pandas
# 2) place CSV logs in ./logs relative to this file
# 3) streamlit run app.py







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
        # Return the integer value so user code can call time() like a function.
        return int(self)


def list_log_files() -> list[Path]:
    # Return every CSV in ./logs so the dropdown can be auto-populated.
    if not LOG_DIR.exists():
        # Missing directory is allowed; UI will show a warning.
        return []
    # Sort file paths for deterministic dropdown ordering.
    return sorted(LOG_DIR.glob("*.csv"))


def load_sensor_log(log_path: Path) -> pd.DataFrame:
    # Read CSV into a dataframe; each non-Time column is considered a sensor.
    df = pd.read_csv(log_path)

    # Normalize headers so lookups are stable even if CSV headers contain extra spaces.
    df.columns = [str(col).strip() for col in df.columns]

    # If the user forgot a Time column, we synthesize one as 0..N-1.
    if "Time" not in df.columns:
        # Insert synthetic tick values if the source file omitted the Time column.
        df.insert(0, "Time", range(len(df)))

    # Coerce Time into integer ticks and normalize any bad values to 0.
    df["Time"] = pd.to_numeric(df["Time"], errors="coerce").fillna(0).astype(int)

    # Always keep rows sorted by time so slider playback is predictable.
    # Reset index so downstream code can use row positions safely.
    return df.sort_values("Time").reset_index(drop=True)


def init_setpoints_from_sensor_log(sensor_df: pd.DataFrame) -> pd.DataFrame:
    # Build a setpoint table aligned to sensor log time ticks.
    # This gives us a setpoint row for each sensor timestamp.
    if sensor_df.empty:
        # Safe fallback when no log is loaded yet.
        base = pd.DataFrame({"Time": [0]})
    else:
        # Copy timeline ticks from the loaded sensor log.
        base = pd.DataFrame({"Time": sensor_df["Time"].astype(int).tolist()})

    # Initialize every setpoint column to a known value.
    for col in SETPOINT_COLS:
        # Seed each wheel setpoint with the configured default value.
        base[col] = DEFAULT_SETPOINT

    # Return a fully initialized output dataframe.
    return base


def ensure_state() -> None:
    # Streamlit reruns the script frequently; session_state persists between reruns.
    # We define all app-level state keys here once.
    st.session_state.setdefault("selected_log", None)
    # Keep a minimal default sensor dataframe so table rendering never crashes.
    st.session_state.setdefault("sensor_log_df", pd.DataFrame({"Time": [0]}))
    st.session_state.setdefault(
        "processed_setpoints_df",
        # Build output rows that align with sensor timeline ticks.
        init_setpoints_from_sensor_log(st.session_state["sensor_log_df"]),
    )
    # Track the currently selected timeline tick.
    st.session_state.setdefault("current_time", 0)
    # Mirror key for the interactive timeline slider widget.
    st.session_state.setdefault("timeline_slider", 0)
    # Track the furthest available time value from the loaded log.
    st.session_state.setdefault("max_time", 0)
    # Playback switch for the timeline autoplay loop.
    st.session_state.setdefault("play", False)
    # Playback speed in timeline ticks per second.
    st.session_state.setdefault("ticks_per_second", 5.0)
    # last_tick can be useful for future non-blocking play logic.
    st.session_state.setdefault("last_tick", time.time())
    # Auto-apply code once on first load and whenever code text changes.
    st.session_state.setdefault("code_needs_auto_apply", True)
    st.session_state.setdefault("last_auto_applied_code_text", None)
    st.session_state.setdefault("last_auto_applied_log_name", None)
    st.session_state.setdefault("auto_apply_error", None)
    # Starter code shown in the code editor tab.
    st.session_state.setdefault("code_text", """shock = max(0.0, min(100.0, get_sensor('ThrottlePct')))
set_each(
    fl=abs(shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5)),
    bl=shock/2, 
    fr=abs(shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5)),
    br=shock/2
)
# see Docs tab for available functions and example scripts""")


def clamp_time(value: int, max_time: int) -> int:
    # Clamp any user/app timeline movement into valid slider bounds.
    # Inner min keeps us from going above max_time.
    # Outer max keeps us from going below zero.
    return max(0, min(int(value), int(max_time)))


def sync_timeline_time(value: int) -> None:
    # Canonical timeline state lives in current_time.
    # Clamp to protect against invalid values from any UI interaction.
    clamped = clamp_time(value, st.session_state["max_time"])
    # Persist the normalized time back into session state.
    st.session_state["current_time"] = clamped


def handle_reset() -> None:
    # Stop playback before resetting so autoplay does not immediately move again.
    st.session_state["play"] = False
    # Send timeline to the first tick.
    sync_timeline_time(0)


def handle_backstep() -> None:
    # Stop playback when user manually steps.
    st.session_state["play"] = False
    # Move one tick left and clamp via sync_timeline_time.
    sync_timeline_time(st.session_state["current_time"] - 1)


def handle_frontstep() -> None:
    # Stop playback when user manually steps.
    st.session_state["play"] = False
    # Move one tick right and clamp via sync_timeline_time.
    sync_timeline_time(st.session_state["current_time"] + 1)


def handle_toggle_play() -> None:
    # Compute new play state by flipping the old one.
    next_play_state = not st.session_state["play"]
    # Persist the new play/pause state.
    st.session_state["play"] = next_play_state

    if next_play_state:
        # If play is pressed at the end, restart from the beginning.
        if st.session_state["current_time"] >= st.session_state["max_time"]:
            sync_timeline_time(0)
        # Record the instant playback was resumed.
        st.session_state["last_tick"] = time.time()


def handle_timeline_slider_change() -> None:
    # Slider only controls time while paused.
    if st.session_state["play"]:
        # Ignore slider updates while playback loop owns timeline movement.
        return
    # Mirror slider selection into canonical timeline state.
    sync_timeline_time(int(st.session_state["timeline_slider"]))


def handle_code_text_change() -> None:
    # Apply logic after user stops editing (widget change event).
    # Flag that code needs to be re-run on next render.
    st.session_state["code_needs_auto_apply"] = True


def get_row_at_time(df: pd.DataFrame, current_time: int) -> pd.Series:
    # Return the row that best represents the requested time.
    # Priority:
    # 1) exact Time == current_time
    # 2) nearest previous Time <= current_time
    # 3) first row fallback
    if df.empty or "Time" not in df.columns:
        # Return an empty row-like object when there is no valid time-indexed data.
        return pd.Series(dtype="object")

    # First preference: exact time tick match.
    exact = df[df["Time"] == current_time]
    if not exact.empty:
        # Use the first exact match row.
        return exact.iloc[0]

    # Second preference: nearest previous row.
    previous = df[df["Time"] <= current_time]
    if not previous.empty:
        # Use last row not exceeding current_time.
        return previous.iloc[-1]

    # Final fallback: first row if current_time is before all timestamps.
    return df.iloc[0]


def get_previous_row(df: pd.DataFrame, current_time: int) -> pd.Series:
    # Return the previous row strictly before current_time.
    # Used for delta calculations in output metrics.
    if df.empty or "Time" not in df.columns:
        # Return an empty row when no valid prior lookup is possible.
        return pd.Series(dtype="object")

    # Collect rows strictly before the requested tick.
    previous = df[df["Time"] < current_time]
    if previous.empty:
        # No prior row exists at time zero or before first sample.
        return pd.Series(dtype="object")

    # Return the nearest previous row.
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
    # - set_all(value): assign same value to all 4 corners at current time
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
    # Work on a copy so failures never partially mutate the original dataframe.
    updated = setpoints_df.copy()

    if "Time" not in updated.columns:
        # Ensure output dataframe always has a Time column for tick alignment.
        updated.insert(0, "Time", [current_time] * len(updated) if len(updated) > 0 else [current_time])

    # Use the union of times from output + sensor logs so code can process everything available.
    # Coerce values to ints so tick iteration is stable.
    all_times = set(pd.to_numeric(updated["Time"], errors="coerce").fillna(0).astype(int).tolist())
    if not sensor_df.empty and "Time" in sensor_df.columns:
        # Include sensor timeline ticks even if output dataframe is missing some.
        all_times.update(pd.to_numeric(sensor_df["Time"], errors="coerce").fillna(0).astype(int).tolist())
    if not all_times:
        # Guarantee at least one iteration tick.
        all_times = {int(current_time)}

    # Add output rows for any timeline ticks that are currently missing.
    missing_times = sorted(all_times.difference(set(updated["Time"].astype(int).tolist())))
    if missing_times:
        # Create rows with default setpoints for each missing tick.
        new_rows = [{"Time": t, **{col: DEFAULT_SETPOINT for col in SETPOINT_COLS}} for t in missing_times]
        # Append missing rows and keep existing rows intact.
        updated = pd.concat([updated, pd.DataFrame(new_rows)], ignore_index=True)

    for col in SETPOINT_COLS:
        if col not in updated.columns:
            # Backfill any missing setpoint columns to maintain output schema.
            updated[col] = DEFAULT_SETPOINT

    # Sort by time so each tick maps to deterministic row order.
    updated = updated.sort_values("Time").reset_index(drop=True)

    # Deliberately small set of builtins for safer execution.
    safe_builtins = {
        # Numeric comparison helper.
        "min": min,
        # Numeric comparison helper.
        "max": max,
        # Magnitude helper for sensor deltas.
        "abs": abs,
        # Precision helper for output rounding.
        "round": round,
        # Explicit numeric conversion helper.
        "float": float,
        # Explicit integer conversion helper.
        "int": int,
    }

    # Evaluate user code for each available tick.
    for tick in sorted(all_times):
        # Locate target output row and source sensor row for this tick.
        # This index points to the row we mutate for this tick.
        row_index = updated.index[updated["Time"].astype(int) == int(tick)][0]
        # Fetch the best matching sensor row for this tick.
        sensor_row = get_row_at_time(sensor_df, int(tick))

        # Helper: set all four setpoints at once.
        def set_all(value: float) -> None:
            # Apply one value to all wheel corners.
            for col in SETPOINT_COLS:
                # Write normalized numeric value into each setpoint cell.
                updated.at[row_index, col] = float(value)

        # Helper: set one or more corners individually.
        def set_each(
            fl: float | None = None,
            fr: float | None = None,
            bl: float | None = None,
            br: float | None = None,
        ) -> None:
            # Build mapping between function arguments and output columns.
            mapping = {
                "FL_Setpoint": fl,
                "FR_Setpoint": fr,
                "BL_Setpoint": bl,
                "BR_Setpoint": br,
            }
            for col, value in mapping.items():
                if value is not None:
                    # Only overwrite corners explicitly provided by user code.
                    updated.at[row_index, col] = float(value)

        # Helper: fetch a sensor by name with optional default.
        def get_sensor(name: str, default: float = 0.0) -> float:
            if sensor_row.empty:
                # If no sensor row is available, return fallback immediately.
                return float(default)

            # Normalize requested sensor name for tolerant matching.
            lookup = str(name).strip().lower()
            # Initialize as missing until a match is found.
            raw_value = None

            # Fast path: exact match as provided.
            if name in sensor_row.index:
                # Read sensor directly using exact column name.
                raw_value = sensor_row[name]
            else:
                # Robust path: case-insensitive + whitespace-insensitive match.
                for column_name in sensor_row.index:
                    if str(column_name).strip().lower() == lookup:
                        # Read first normalized-name match.
                        raw_value = sensor_row[column_name]
                        break

            if raw_value is None:
                # Unknown sensor name, use caller-provided fallback.
                return float(default)

            # Treat blank/NA cells as missing sensor values and use default.
            if pd.isna(raw_value):
                # Missing/NA values are treated as absent sensor data.
                return float(default)

            if isinstance(raw_value, str):
                # Normalize string sensor values before numeric conversion.
                raw_value = raw_value.strip()
                if raw_value == "":
                    # Empty strings also fall back to default.
                    return float(default)

            try:
                # Convert parsed sensor value into a float for user math.
                return float(raw_value)
            except (TypeError, ValueError):
                # Conversion failures are treated as missing sensor values.
                return float(default)

        # Scope exposed to user code.
        runtime_scope = {
            # API function to set all corners at current tick.
            "set_all": set_all,
            # API function to set specific corners at current tick.
            "set_each": set_each,
            # API function to read sensors with fallback handling.
            "get_sensor": get_sensor,
            # Current tick object; works as both value and callable time().
            "time": TickValue(int(tick)),
            # Alias for current tick as plain int.
            "current_time": int(tick),
            # Row-like sensor object for direct indexing in user scripts.
            "sensor": sensor_row,
        }

        try:
            # Execute user code for this tick.
            exec(code_text, {"__builtins__": safe_builtins}, runtime_scope)
        except Exception as exc:
            # Keep existing data if code fails; surface error with failing tick.
            return setpoints_df, f"Time {int(tick)}: {exc}"

    # Return fully computed setpoint table and no error.
    return updated, None


# Streamlit page setup should happen before most UI rendering.
# Configure a wide layout so side-by-side panels fit comfortably.
st.set_page_config(layout="wide")

# Hide Streamlit menu + deploy button
# Define custom CSS to hide default Streamlit chrome.
hide_streamlit_style = """
<style>
#MainMenu {visibility: hidden;}
header {visibility: hidden;}
footer {visibility: hidden;}
[data-testid="stToolbar"] {display: none;}
</style>
"""
# Inject CSS into page.
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
# Convert file paths to plain filenames for selectbox options.
log_options = [path.name for path in log_paths]

if not log_options:
    # Inform the user that no input data is currently available.
    st.warning("No CSV logs found in ./logs. Add logs to enable sensor playback.")

if st.session_state["selected_log"] not in log_options and log_options:
    # Auto-select the first discovered log.
    st.session_state["selected_log"] = log_options[0]

with st.container():
    # Top row layout: log controls, timeline controls, stats, and project title.
    log_col, timing_col, download_col, logo_col = st.columns([2, 3, 1, 2])

    with log_col:
        # Left-side controls for selecting/opening input logs.
        log_col_selector, log_col_link = st.columns([5, 1])
        with log_col_selector:
            # Dropdown of discovered log files.
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
                # Resolve selected filename to full path object.
                selected_path = next(path for path in log_paths if path.name == st.session_state["selected_log"])
                # Parse and normalize selected sensor log file.
                loaded_sensor_df = load_sensor_log(selected_path)
                if (
                    st.session_state["sensor_log_df"].empty
                    or st.session_state["selected_log"] != st.session_state.get("loaded_log_name")
                ):
                    # Only reset tables when switching logs (not every rerun).
                    # Store sensor dataframe used by Sensors tab and code runtime.
                    st.session_state["sensor_log_df"] = loaded_sensor_df
                    # Rebuild setpoint table to align with new sensor timeline.
                    st.session_state["processed_setpoints_df"] = init_setpoints_from_sensor_log(loaded_sensor_df)
                    # Cache maximum available timeline tick for slider bounds.
                    st.session_state["max_time"] = int(loaded_sensor_df["Time"].max()) if not loaded_sensor_df.empty else 0
                    # Keep current_time valid relative to newly loaded data.
                    st.session_state["current_time"] = clamp_time(st.session_state["current_time"], st.session_state["max_time"])
                    # Keep timeline slider widget synchronized.
                    st.session_state["timeline_slider"] = st.session_state["current_time"]
                    # Trigger auto-apply of code against newly loaded data.
                    st.session_state["code_needs_auto_apply"] = True
                    # Track which log is currently loaded to avoid redundant resets.
                    st.session_state["loaded_log_name"] = st.session_state["selected_log"]

        with log_col_link:
            if log_options:
                # Recompute selected path for link button target.
                selected_path = next(path for path in log_paths if path.name == st.session_state["selected_log"])
                # Open CSV via file:// URL in a new tab.
                st.link_button("", selected_path.resolve().as_uri(), icon="↗️", help="Open log CSV in new tab")
            else:
                # Placeholder button when no logs exist.
                st.button("", icon="↗️", disabled=True)

    with timing_col:
        # Timeline controls (reset, back, play/pause, forward, direct slider drag).
        timing_col_reset, timing_col_backstep, timing_col_play, timing_col_frontstep, timing_col_speed = st.columns(
            [1, 1, 1, 1, 3],
            # vertical_alignment="center",
            gap="small",
        )

        with timing_col_reset:
            # Jump timeline back to zero.
            st.button(
                "🔃",
                help="Reset timeline",
                width="stretch",
                on_click=handle_reset,
                key="timeline_reset_btn",
            )

        with timing_col_backstep:
            # Move timeline back by one tick.
            st.button(
                "⬅️",
                help="Step back",
                width="stretch",
                on_click=handle_backstep,
                key="timeline_backstep_btn",
            )

        with timing_col_play:
            # Choose icon based on play state.
            play_label = "⏸️" if st.session_state["play"] else "▶️"
            # Toggle playback loop.
            st.button(
                play_label,
                help="Play/Pause",
                width="stretch",
                on_click=handle_toggle_play,
                key="timeline_play_btn",
            )

        with timing_col_frontstep:
            # Move timeline forward by one tick.
            st.button(
                "➡️",
                help="Step forward",
                width="stretch",
                on_click=handle_frontstep,
                key="timeline_frontstep_btn",
            )

        with timing_col_speed:
            # Keep speed label + control on one tight horizontal line on the right.
            # make the text right next to the slider, and vertically centered with the buttons to the left.

            speed_label_col, speed_slider_col = st.columns([3.5, 7], vertical_alignment="center", gap="small")
            with speed_label_col:
                # Display speed label near slider.
                speed_label_col.markdown("**Ticks/s:**")
            with speed_slider_col:
                # Let user control playback speed in whole ticks per second.
                speed_value = st.slider(
                    "Ticks/sec",
                    min_value=1.0,
                    max_value=10.0,
                    value=(st.session_state["ticks_per_second"]),
                    step=1.0,
                    label_visibility="collapsed",
                    width="stretch"
                )
            # Persist selected playback speed.
            st.session_state["ticks_per_second"] = float(speed_value)

        # Primary timeline slider.
        # Disabled during playback so only one source controls time movement.
        # IMPORTANT: update widget state before instantiation in this run.
        if st.session_state.get("timeline_slider") != st.session_state["current_time"]:
            # Keep slider value synced with canonical timeline value.
            st.session_state["timeline_slider"] = st.session_state["current_time"]

        # Main interactive timeline slider.
        st.slider(
            "Timeline",
            0,
            st.session_state["max_time"],
            key="timeline_slider",
            label_visibility="collapsed",
            width="stretch",
            disabled=st.session_state["play"],
            on_change=handle_timeline_slider_change,
        )

    with download_col:
        # Quick run stats.
        # Show current position in timeline.
        st.caption(f"Time: {st.session_state['current_time']} / {st.session_state['max_time']}")
        # st.caption(f"Rows: {len(st.session_state['sensor_log_df'])}")
        # Show playback speed setting.
        st.caption(f"Speed: {st.session_state['ticks_per_second']:.1f} ticks/sec")

    with logo_col:
        # Project header with repo link.
        st.subheader("CWRU Baja - [cwru-baja](https://github.com/cwru-baja)", divider="red")

    # Separate top controls from main content area.
    st.divider()

# Pull key dataframes/state values into local variables for readability.
sensor_log_df = st.session_state["sensor_log_df"]
processed_setpoints_df = st.session_state["processed_setpoints_df"]
current_time = st.session_state["current_time"]
max_time = st.session_state["max_time"]

with st.container():
    # Main split view: input tools on left, output metrics on right.
    input_col, output_col = st.columns(2)
    with input_col:
        # Input pane includes sensors, code editor, and documentation tabs.
        st.subheader("Input", divider="green")
        sensors_tab, code_tab, docs_tab = st.tabs(["Sensors", "Code", "Docs"], default="Code")

        with sensors_tab:
            # Build a display table from one row of sensor data at current time.
            # We rotate from "wide" (columns are sensors) to a 2-column table.
            sensor_row = get_row_at_time(sensor_log_df, current_time)
            # Exclude Time from sensor name list so table only shows actual channels.
            sensor_names = [name for name in sensor_row.index if name != "Time"]
            # Build long-form table for cleaner display in Streamlit.
            sensors_df = pd.DataFrame(
                {
                    "Name": sensor_names,
                    "Value": [sensor_row[name] for name in sensor_names],
                }
            )
            # Render sensor name/value table.
            st.dataframe(sensors_df, hide_index=True, width="stretch")

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
            # Keep editor content in session_state key code_text.
            code_text = st.text_area(
                "Code Area",
                key="code_text",
                on_change=handle_code_text_change,
                label_visibility="collapsed",
                height=200,
                placeholder="""shock = max(0.0, min(100.0, get_sensor('ThrottlePct')))
set_each(
    fl=abs(shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5)),
    bl=shock/2, 
    fr=abs(shock/2 - abs(get_sensor('SteeringAngle', 0.0)/5)),
    br=shock/2
)
# see Docs tab for available functions and example scripts""",
            )

            # Prepare an "apply" result so save can always export the latest computed output.
            # Run user code now so both Apply and Save use the same computed result.
            prepared_df, prepared_error = execute_user_code(
                code_text,
                current_time,
                sensor_log_df,
                processed_setpoints_df,
            )

            if st.session_state.get("code_needs_auto_apply", False):
                if prepared_error:
                    # Cache error and avoid overwriting output dataframe when code fails.
                    st.session_state["auto_apply_error"] = prepared_error
                else:
                    # Persist computed setpoints into app state.
                    st.session_state["processed_setpoints_df"] = prepared_df
                    # Clear previous error if this apply succeeded.
                    st.session_state["auto_apply_error"] = None
                    # Track source code text that produced current output dataframe.
                    st.session_state["last_auto_applied_code_text"] = st.session_state["code_text"]
                    # Track log file paired with last successful apply.
                    st.session_state["last_auto_applied_log_name"] = st.session_state.get("selected_log")
                # Consume the auto-apply request flag for this rerun.
                st.session_state["code_needs_auto_apply"] = False

            # Keep Apply and Save on the same row.
            apply_col, save_col = st.columns(2)

            with apply_col:
                # Apply current code across all timeline ticks.
                if st.button("Apply to all times", width="stretch"):
                    if prepared_error:
                        # Surface runtime error from execute_user_code.
                        st.error(f"Code error: {prepared_error}")
                    else:
                        # Persist successful apply result.
                        st.session_state["processed_setpoints_df"] = prepared_df
                        # Clear stale auto-apply error state.
                        st.session_state["auto_apply_error"] = None
                        # Store latest successful source code snapshot.
                        st.session_state["last_auto_applied_code_text"] = st.session_state["code_text"]
                        # Store log context used for that code snapshot.
                        st.session_state["last_auto_applied_log_name"] = st.session_state.get("selected_log")
                        # Confirm successful processing to the user.
                        st.success("Applied code across all times")

            with save_col:
                # Save downloads CSV that includes the same apply logic first.
                # Button stays disabled while code contains errors.
                save_clicked = st.download_button(
                    "Save (apply + download)",
                    data=prepared_df.to_csv(index=False).encode("utf-8") if not prepared_error else b"",
                    file_name="processed_setpoints.csv",
                    mime="text/csv",
                    width="stretch",
                    disabled=prepared_error is not None,
                )

                if save_clicked and not prepared_error:
                    # Keep in-memory output aligned with downloaded content.
                    st.session_state["processed_setpoints_df"] = prepared_df
                    # Clear stale errors after successful save/apply.
                    st.session_state["auto_apply_error"] = None
                    # Track current code as last successful apply source.
                    st.session_state["last_auto_applied_code_text"] = st.session_state["code_text"]
                    # Track active log name for reproducibility.
                    st.session_state["last_auto_applied_log_name"] = st.session_state.get("selected_log")
                    # Notify user of combined apply+download success.
                    st.success("Applied code across all times and downloaded CSV")

            if prepared_error:
                # Explain why save is disabled.
                st.caption(f"Save disabled until code is valid. Latest error: {prepared_error}")

            if st.session_state.get("auto_apply_error"):
                # Show sticky auto-apply status when code has unresolved issues.
                st.caption(f"Auto-apply paused due to code error: {st.session_state['auto_apply_error']}")

            # Guide user toward in-app API and examples.
            st.caption("See the Docs tab for available functions and example scripts.")

        with docs_tab:
            # In-app API docs and examples for user code authoring.
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

            st.markdown("#### set_all(value)")
            st.markdown("- Inputs: `value` (float-like)")
            st.markdown("- Output: none")
            st.markdown("- Explanation: Sets FL, FR, BL, and BR to the same value for the current tick.")
            st.code("set_all(0.8)", language="python")

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
            st.code("set_all(round(0.6 + get_sensor('ThrottlePct', 0.0) * 0.4, 3))", language="python")

            st.markdown("#### float(x), int(x)")
            st.markdown("- Inputs: value convertible to number")
            st.markdown("- Output: converted number")
            st.markdown("- Explanation: Explicit numeric conversion when needed.")
            st.code("raw = get_sensor('ShockFL', 67.0)\nset_all(float(raw) / 100.0)", language="python")

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
set_all(0.8)

# 2) Corner-specific static values
set_each(fl=0.92, fr=0.92, bl=0.78, br=0.78)

# 3) Direct throttle mapping
throttle = get_sensor('ThrottlePct', 0.0)
set_all(0.5 + 0.5 * throttle)

# 4) Clamp with min/max to keep values in bounds
throttle = get_sensor('ThrottlePct', 0.0)
target = min(1.0, max(0.0, 0.35 + throttle * 0.9))
set_all(target)

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
set_all(0.7 + 0.2 * phase)

# 8) Use time() as a function (supported)
t = time()
if t < 100:
    set_all(0.75)
else:
    set_all(0.85)

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
set_all(min(1.0, max(0.0, value)))
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
set_all(sum([0.1, 0.2, 0.3]))

# Not allowed: app globals
print(st.session_state)
""",
                language="python",
            )

    with output_col:
        # Output pane visualizes computed suspension setpoints.
        st.subheader("Output", divider="orange")

        # Center output metrics and car image with surrounding spacing columns.
        _, live_output, car_img, _ = st.columns([1, 6, 3, 1])

        with live_output:
            # Pull current and previous setpoint rows for metric and delta display.
            setpoint_row = get_row_at_time(st.session_state["processed_setpoints_df"], current_time)
            previous_row = get_previous_row(st.session_state["processed_setpoints_df"], current_time)

            # Parse current tick values, defaulting safely when missing.
            FL_setpoint = float(setpoint_row.get("FL_Setpoint", DEFAULT_SETPOINT))
            FR_setpoint = float(setpoint_row.get("FR_Setpoint", DEFAULT_SETPOINT))
            BL_setpoint = float(setpoint_row.get("BL_Setpoint", DEFAULT_SETPOINT))
            BR_setpoint = float(setpoint_row.get("BR_Setpoint", DEFAULT_SETPOINT))

            # Compute deltas from previous tick for metric trend arrows.
            FL_delta = FL_setpoint - float(previous_row.get("FL_Setpoint", FL_setpoint))
            FR_delta = FR_setpoint - float(previous_row.get("FR_Setpoint", FR_setpoint))
            BL_delta = BL_setpoint - float(previous_row.get("BL_Setpoint", BL_setpoint))
            BR_delta = BR_setpoint - float(previous_row.get("BR_Setpoint", BR_setpoint))

            # Arrange front and rear wheel metric cards.
            FL_cell, FR_cell = st.columns(2, vertical_alignment="center")
            BL_cell, BR_cell = st.columns(2)

            with FL_cell:
                # Front-left metric card.
                st.metric(label="FL", value=f"{FL_setpoint:.2f}", delta=f"{FL_delta:+.2f}")

            with FR_cell:
                # Front-right metric card.
                st.metric(label="FR", value=f"{FR_setpoint:.2f}", delta=f"{FR_delta:+.2f}")

            with BL_cell:
                # Rear-left metric card.
                st.metric(label="BL", value=f"{BL_setpoint:.2f}", delta=f"{BL_delta:+.2f}")

            with BR_cell:
                # Rear-right metric card.
                st.metric(label="BR", value=f"{BR_setpoint:.2f}", delta=f"{BR_delta:+.2f}")

        with car_img:
            # Decorative top-down car reference image.
            st.image(
                "https://png.pngtree.com/png-vector/20230110/ourmid/pngtree-car-top-view-image-png-image_6557068.png",
                width="stretch",
            )

        # Read-only mirror of current output timeline position.
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
    # Guard against invalid or zero speed values.
    ticks_per_second = max(float(st.session_state.get("ticks_per_second", 1.0)), 0.1)
    # Wait exactly one frame interval for configured playback speed.
    time.sleep(1.0 / ticks_per_second)
    # Advance timeline by one tick.
    sync_timeline_time(st.session_state["current_time"] + 1)
    if st.session_state["current_time"] >= st.session_state["max_time"]:
        # Stop automatically when reaching end of timeline.
        st.session_state["play"] = False
    # Trigger a new run so UI reflects updated timeline position.
    st.rerun()
