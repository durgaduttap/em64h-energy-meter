import streamlit as st
import pandas as pd
import threading
import time
from datetime import datetime
import plotly.graph_objects as go
from pymodbus.client import ModbusSerialClient as ModbusClient
from collections import deque

# --- CONFIGURATION SETTINGS ---
SERIAL_PORT = 'COM3'  # Change to match your Device Manager (e.g., /dev/ttyUSB0)
METER_SLAVE_ID = 1  # Configured ID on the EM64 front panel
POLL_INTERVAL = 2  # Data refresh interval in seconds

# Base register configuration for Schneider EM64 (0-Based addressing adjustment)
START_ADDRESS_1 = 3906  # Voltage, Current, Frequency, PF block
START_ADDRESS_2 = 3940  # kW, kVAR, kVA power block

# --- THREAD-SAFE GLOBAL STORAGE ---
@st.cache_resource
def get_shared_telemetry():
    """
    Initializes a single global dictionary and thread lock.
    Streamlit caches this memory space globally across ALL connected user tabs.
    """
    return {
        "data": {
            "metrics": {},
            "history": deque(maxlen=100),
            "success": False,
            "error_msg": "Initializing background polling engine...",
            "last_updated": None
        },
        "lock": threading.Lock()
    }

# Retrieve the single, unified global resource
telemetry_resource = get_shared_telemetry()
shared_data = telemetry_resource["data"]
lock = telemetry_resource["lock"]


# --- BACKEND HARDWARE POLLING ENGINE ---
def modbus_background_worker():
    """
    Runs continuously in a dedicated background thread.
    Exclusively owns COM3 to completely prevent multi-tab port lockups.
    """
    client = ModbusClient(
        port=SERIAL_PORT,
        baudrate=9600,
        parity='E',
        stopbits=1,
        bytesize=8,
        timeout=1
    )

    while True:
        if not client.connected:
            if not client.connect():
                with lock:
                    shared_data["success"] = False
                    shared_data["error_msg"] = f"Port Locked or Offline: Cannot open {SERIAL_PORT}."
                time.sleep(5)
                continue

        try:
            # FIX: Split the 76-register read into two smaller chunks (40 registers each)
            # This completely bypasses firmware buffer limit crashes on older EM64 meters.
            res1 = client.read_holding_registers(address=START_ADDRESS_1, count=34, slave=METER_SLAVE_ID)
            time.sleep(0.05)  # Brief bus silence pause recommended for RS-485 stability
            res2 = client.read_holding_registers(address=START_ADDRESS_2, count=26, slave=METER_SLAVE_ID)

            if res1.isError() or res2.isError():
                with lock:
                    shared_data["success"] = False
                    shared_data["error_msg"] = f"Modbus Exception Received. Block1: {res1}, Block2: {res2}"
                time.sleep(POLL_INTERVAL)
                continue

            # Helper inner function to safely decode float32 register pairs
            def unpack_float(result_obj, start_idx):
                reg_slice = result_obj.registers[start_idx:start_idx + 2]
                return client.convert_from_registers(
                    reg_slice, data_type=client.DATATYPE.FLOAT32, word_order="big"
                )

            # Build data mapping structure locally to minimize lock times
            local_m = {}

            # Block 1 Unpacking (Indices relative to 3906)
            local_m["V1"] = round(unpack_float(res1, 0), 2)  # 3907: V_R-N
            local_m["V2"] = round(unpack_float(res1, 2), 2)  # 3909: V_Y-N
            local_m["V3"] = round(unpack_float(res1, 4), 2)  # 3911: V_B-N
            local_m["V_avg"] = round(unpack_float(res1, 6), 2)  # 3913: Avg L-N

            local_m["V12"] = round(unpack_float(res1, 8), 2)  # 3915: V_R-Y
            local_m["V23"] = round(unpack_float(res1, 10), 2)  # 3917: V_Y-B
            local_m["V31"] = round(unpack_float(res1, 12), 2)  # 3919: V_B-R
            local_m["Vll_avg"] = round(unpack_float(res1, 14), 2)  # 3921: Avg L-L

            # FIX: Step-slice pointer mapping shifted to +2 offsets to eliminate overlap calculations
            local_m["I1"] = round(unpack_float(res1, 16), 2)  # 3923: Current R
            local_m["I2"] = round(unpack_float(res1, 18), 2)  # 3925: Current Y (Fixed from 17 to 18)
            local_m["I3"] = round(unpack_float(res1, 20), 2)  # 3927: Current B
            local_m["I_avg"] = round(unpack_float(res1, 22), 2)  # 3929: Avg Current

            local_m["PF1"] = round(unpack_float(res1, 24), 2)  # 3931: PF R
            local_m["PF2"] = round(unpack_float(res1, 26), 2)  # 3933: PF Y
            local_m["PF3"] = round(unpack_float(res1, 28), 2)  # 3935: PF B
            local_m["PF_avg"] = round(unpack_float(res1, 30), 3)  # 3937: Avg PF
            local_m["Freq"] = round(unpack_float(res1, 32), 2)  # 3939: Freq

            # Block 2 Unpacking (Indices relative to 3940)
            local_m["kW1"] = round(unpack_float(res2, 1), 2)  # 3941: kW R
            local_m["kW2"] = round(unpack_float(res2, 3), 2)  # 3943: kW Y
            local_m["kW3"] = round(unpack_float(res2, 5), 2)  # 3945: kW B
            local_m["kW_total"] = round(unpack_float(res2, 7), 2)  # 3947: Total kW

            local_m["kVAR1"] = round(unpack_float(res2, 9), 2)  # 3949
            local_m["kVAR2"] = round(unpack_float(res2, 11), 2)  # 3951
            local_m["kVAR3"] = round(unpack_float(res2, 13), 2)  # 3953
            local_m["kVAR_total"] = round(unpack_float(res2, 15), 2)  # 3955

            local_m["kVA1"] = round(unpack_float(res2, 17), 2)  # 3957
            local_m["kVA2"] = round(unpack_float(res2, 19), 2)  # 3959
            local_m["kVA3"] = round(unpack_float(res2, 21), 2)  # 3961
            local_m["kVA_total"] = round(unpack_float(res2, 23), 2)  # 3963

            # Securely commit updates to the global dictionary
            t_now = datetime.now()
            with lock:
                shared_data["metrics"] = local_m
                shared_data["success"] = True
                shared_data["last_updated"] = t_now
                shared_data["history"].append({
                    "Timestamp": t_now,
                    "Total Power (kW)": local_m["kW_total"],
                    "Avg Voltage (V)": local_m["V_avg"],
                    "Avg Current (A)": local_m["I_avg"]
                })

        except Exception as e:
            with lock:
                shared_data["success"] = False
                shared_data["error_msg"] = f"Worker Exception: {e}"
            client.close()

        time.sleep(POLL_INTERVAL)


# --- START THE DAEMON THREAD ON SERVER COLD-START ---
# Thread scans background state independently of user sessions
if not any(t.name == "EM64_Daemon" for t in threading.enumerate()):
    daemon_thread = threading.Thread(target=modbus_background_worker, name="EM64_Daemon", daemon=True)
    daemon_thread.start()

# --- STREAMLIT USER INTERFACE FRAMEWORK ---
st.set_page_config(page_title="EM64 Industrial Telemetry Console", layout="wide")
st.title("Schneider EM64 Complete Telemetry Console")
st.markdown("Isolated Architecture Profile: Direct background engine streaming.")


# --- ISOLATED DASHBOARD FRAGMENT FOR THE WEB USER ---
@st.fragment(run_every=POLL_INTERVAL)
def ui_dashboard_fragment():
    """
    Pulls data from memory via thread-safe global scopes.
    Tab minimizes or browser delays do not interrupt data logging consistency.
    """
    # Safely duplicate snapshot properties to avoid mutations during UI draw
    with lock:
        success = shared_data["success"]
        error_msg = shared_data["error_msg"]
        m = shared_data["metrics"].copy() if shared_data["metrics"] else None
        history_snapshot = list(shared_data["history"])
        last_up = shared_data["last_updated"]

    if success and m:
        # Check tracking heartbeat latency
        time_str = last_up.strftime("%H:%M:%S") if last_up else "N/A"
        st.caption(f"🟢 Core Hardware Link Active | Last Data Packet Received: {time_str}")

        df_history = pd.DataFrame(history_snapshot)

        # --- ROW 1: PRIMARY KPIs ---
        st.subheader("📊 System Summary Metrics")
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("Avg L-N Voltage", f"{m['V_avg']} V")
        col2.metric("Avg L-L Voltage", f"{m['Vll_avg']} V")
        col3.metric("Avg Current", f"{m['I_avg']} A")
        col4.metric("Total Active Power", f"{m['kW_total']} kW")
        col5.metric("System Frequency", f"{m['Freq']} Hz")

        st.markdown("---")

        # --- ROW 2: PHASE MATRIX OVERVIEW ---
        st.subheader("⚡ Phase Breakdown Matrix")
        phase_table = {
            "Measurement Parameter": [
                "Voltage (Line-to-Neutral)", "Voltage (Line-to-Line)",
                "Current (RMS)", "Active Power (kW)",
                "Reactive Power (kVAR)", "Apparent Power (kVA)", "Power Factor (PF)"
            ],
            "Phase R (L1)": [f"{m['V1']} V", f"{m['V12']} V", f"{m['I1']} A", f"{m['kW1']} kW", f"{m['kVAR1']} kVAR",
                             f"{m['kVA1']} kVA", m["PF1"]],
            "Phase Y (L2)": [f"{m['V2']} V", f"{m['V23']} V", f"{m['I2']} A", f"{m['kW2']} kW", f"{m['kVAR2']} kVAR",
                             f"{m['kVA2']} kVA", m["PF2"]],
            "Phase B (L3)": [f"{m['V3']} V", f"{m['V31']} V", f"{m['I3']} A", f"{m['kW3']} kW", f"{m['kVAR3']} kVAR",
                             f"{m['kVA3']} kVA", m["PF3"]],
            "Total System Value": ["—", f"{m['Vll_avg']} V", "—", f"{m['kW_total']} kW", f"{m['kVAR_total']} kVAR",
                                   f"{m['kVA_total']} kVA", m["PF_avg"]]
        }
        st.table(pd.DataFrame(phase_table))

        st.markdown("---")

        # --- ROW 3: PLOTLY GRAPH ---
        st.subheader("📈 Historical Data Trend Logging")
        if not df_history.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=df_history["Timestamp"], y=df_history["Total Power (kW)"], mode='lines+markers',
                                     name='Total Power (kW)', line=dict(color='#00FF00', width=2)))
            fig.add_trace(go.Scatter(x=df_history["Timestamp"], y=df_history["Avg Current (A)"], mode='lines',
                                     name='Avg Current (A)', line=dict(color='#00FFFF'), yaxis="y2"))

            fig.update_layout(
                template="plotly_dark", height=400,
                margin=dict(l=20, r=20, t=20, b=20),
                xaxis=dict(type='date', tickformat='%H:%M:%S'),
                yaxis=dict(title="Active Power (kW)", titlefont=dict(color="#00FF00"), tickfont=dict(color="#00FF00")),
                yaxis2=dict(title="Current (A)", titlefont=dict(color="#00FFFF"), tickfont=dict(color="#00FFFF"),
                            overlaying="y", side="right")
            )
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.error(f"⚠️ App Waiting for Data: {error_msg}")
        # Graph cache preservation safety layer
        if history_snapshot:
            df_history = pd.DataFrame(history_snapshot)
            fig = go.Figure(go.Scatter(x=df_history["Timestamp"], y=df_history["Total Power (kW)"], mode='lines',
                                       line=dict(color='orange')))
            fig.update_layout(template="plotly_dark", height=350, title="Cached Offline History Data View")
            st.plotly_chart(fig, use_container_width=True)


# Call UI fragment engine
ui_dashboard_fragment()
