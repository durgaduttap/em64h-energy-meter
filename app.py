import streamlit as st
import pandas as pd
from datetime import datetime
import plotly.graph_objects as go
from pymodbus.client import ModbusSerialClient as ModbusClient
from collections import deque

# --- CONFIGURATION SETTINGS ---
SERIAL_PORT = 'COM3'  # Change to match your Device Manager (e.g., /dev/ttyUSB0 on Linux)
METER_SLAVE_ID = 1  # Configured ID on the EM64 front panel
POLL_INTERVAL = 2  # Data refresh interval in seconds

# NOTE: Schneider manuals use 1-based indexing (e.g., 3908).
# Pymodbus uses 0-based indexing. If your read values look like garbage data,
# the register offset is likely shifted. Toggle this base address between 3907 and 3906.
START_ADDRESS = 3906


# --- PERSISTENT HARDWARE INTERFACE ---
class ModbusHardwareManager:
    """
    Manages a persistent connection to the EM64 meter.
    Prevents frequent port opening/closing cycles which cause serial locks.
    """

    def __init__(self):
        self.client = ModbusClient(
            port=SERIAL_PORT,
            baudrate=9600,
            parity='E',
            stopbits=1,
            bytesize=8,
            timeout=1
        )

    def fetch_metrics(self):
        data = {"success": False, "V_avg": 0.0, "I_avg": 0.0, "kW_total": 0.0, "error_msg": ""}

        # Connect if not already connected
        if not self.client.connected:
            if not self.client.connect():
                data["error_msg"] = f"Failed to open serial port {SERIAL_PORT}. Check connection or permissions."
                return data

        try:
            # Block read 40 registers starting from our calculated base address
            result = self.client.read_holding_registers(address=START_ADDRESS, count=40, slave=METER_SLAVE_ID)

            if result.isError():
                data["error_msg"] = f"Modbus Protocol Error: {result}"
                return data

            # Safeguard: Ensure we received the expected number of registers
            if len(result.registers) < 38:
                data["error_msg"] = "Received incomplete register frame from meter."
                return data

            # Extract the 2-register slices relative to our start offset
            v_ln_slice = result.registers[6:8]  # Avg Line-to-Neutral Voltage
            i_avg_slice = result.registers[22:24]  # Avg System Current
            kw_slice = result.registers[36:38]  # Total Active Power (kW)

            # Strict error parsing for decoding operations
            try:
                data["V_avg"] = round(self.client.convert_from_registers(
                    v_ln_slice, data_type=self.client.DATATYPE.FLOAT32, word_order="big"
                ), 2)

                data["I_avg"] = round(self.client.convert_from_registers(
                    i_avg_slice, data_type=self.client.DATATYPE.FLOAT32, word_order="big"
                ), 2)

                data["kW_total"] = round(self.client.convert_from_registers(
                    kw_slice, data_type=self.client.DATATYPE.FLOAT32, word_order="big"
                ), 2)

                data["success"] = True

            except (ValueError, Exception) as decode_err:
                data["error_msg"] = f"Data Unpacking Error (Possible register shift): {decode_err}"

        except Exception as e:
            data["error_msg"] = f"Hardware Communication Exception: {e}"
            # Force a close on critical exception so it re-initializes cleanly next pass
            self.client.close()

        return data


# --- STREAMLIT UI SETUP ---
st.set_page_config(page_title="EM64 Real-time Power Monitor", layout="wide")
st.title("Schneider EM64 Live Web Dashboard")
st.markdown("Streaming live sub-meter measurements securely via Modbus RTU.")

# Initialize global hardware manager inside the session state to insulate multi-user interference
if "hardware_manager" not in st.session_state:
    st.session_state.hardware_manager = ModbusHardwareManager()

# Initialize optimized history buffer (Memory-bounded queue instead of heavy Pandas concatenations)
if "history_queue" not in st.session_state:
    st.session_state.history_queue = deque(maxlen=50)


# --- ISOLATED REFRESH FRAGMENT ---
@st.fragment(run_every=POLL_INTERVAL)
def live_dashboard_fragment():
    """
    Renders and isolates live telemetry updates.
    Keeps user interactions from breaking the background hardware polling frequency.
    """
    metrics_placeholder = st.empty()
    chart_placeholder = st.empty()

    # Query the persistent serial resource
    raw_metrics = st.session_state.hardware_manager.fetch_metrics()

    if raw_metrics["success"]:
        # Store true datetime objects for accurate linear scaling on Plotly's time axis
        current_time = datetime.now()

        st.session_state.history_queue.append({
            "Timestamp": current_time,
            "Voltage": raw_metrics["V_avg"],
            "Current": raw_metrics["I_avg"],
            "Power": raw_metrics["kW_total"]
        })

        # Convert memory queue cleanly to a temporary dataframe for rendering
        df_history = pd.DataFrame(list(st.session_state.history_queue))

        # 1. Update Metric Display Cards
        with metrics_placeholder.container():
            col1, col2, col3 = st.columns(3)
            col1.metric(label="Average Voltage (L-N)", value=f"{raw_metrics['V_avg']} V")
            col2.metric(label="Average Current", value=f"{raw_metrics['I_avg']} A")
            col3.metric(label="Total Active Power", value=f"{raw_metrics['kW_total']} kW")

        # 2. Update Trend Chart
        with chart_placeholder.container():
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df_history["Timestamp"],
                y=df_history["Power"],
                mode='lines+markers',
                name='Total Power (kW)',
                line=dict(color='#00FF00', width=2)
            ))

            fig.update_layout(
                title="Real-time Load Profile Trend (kW)",
                xaxis_title="Time",
                yaxis_title="Kilowatts (kW)",
                xaxis=dict(type='date', tickformat='%H:%M:%S'),  # Force true linear timeline layout
                template="plotly_dark",
                height=400,
                margin=dict(l=20, r=20, t=40, b=20)
            )
            st.plotly_chart(fig, use_container_width=True)

    else:
        # Gracefully present connection errors without completely crashing the dashboard layout
        with metrics_placeholder.container():
            st.warning(f"⚠️ Communication Disrupted: {raw_metrics['error_msg']}")

        # Keep displaying the historical graph context even during a temporary data drop
        if len(st.session_state.history_queue) > 0:
            df_history = pd.DataFrame(list(st.session_state.history_queue))
            with chart_placeholder.container():
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=df_history["Timestamp"], y=df_history["Power"], mode='lines',
                                         line=dict(color='#FFA500')))
                fig.update_layout(title="Load Profile Trend (Stale Data Display)", template="plotly_dark", height=400)
                st.plotly_chart(fig, use_container_width=True)


# Run the isolated UI fragment
live_dashboard_fragment()
