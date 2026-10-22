# AutoDPT — Software Requirements Specification

| Item | Value |
|---|---|
| Document | AutoDPT-SRS |
| Version | 1.3 (v1.2 + bench check for connection and controllability, NET-09, pulled forward from Phase 2) |
| Date | 2026-10-22 |
| Owner | Everest (HW, traction inverter) |
| Status | Released for implementation (Phase 0–2); open points in §13 |
| Intended reader | Software implementer (Claude Code) and the HW/lab engineer who commissions the bench |

---

## 0. How to use this document (instructions for the implementer)

1. This SRS is the single source of truth. Put it in the repository as `docs/SRS.md`. Add a `CLAUDE.md` at the repo root that says: *"Implement per docs/SRS.md. Requirement IDs must appear in docstrings/tests that implement them. Do not invent instrument commands — verify against the manuals in docs/manuals/ and mark unverified commands with `# UNVERIFIED`."*
2. Put the instrument programming manuals (LeCroy MAUI Remote Control & Automation Manual, Tektronix AFG31000 Programmer Manual, Magna-Power SL/SLx SCPI manual) and the result template `template/FS820_6IN1_Wphase_HS_400V_50-950A_Ron3_Roff6.6_125oC.xlsx` (supplied; layout specified in §8.3) and the ready-made `template/template_map.yaml` (delivered with this SRS) into the repo. If you have copies of IEC 60747-9 and IEC 60747-8, keep them outside the repo (licensed) and use them to verify the threshold presets (§7.3).
3. Implement in the phase order of §12. **The analysis library (Phase 1) and the mock instruments come before any real-instrument code.** Nothing in Phases 0–1 may need hardware.
4. Keywords: **shall** = mandatory, **should** = desirable, **may** = optional. Each requirement has an ID `<AREA>-<nn>`. Tags: **[NEW]** = added in this revision, **[CHG]** = changed vs. the draft, no tag = carried over.
5. Where this document gives a SCPI/remote command, it is a *starting point to verify*, not a verified fact (Appendix C).

---

## 1. Purpose and scope

AutoDPT is a Windows desktop application (Python) that automates double pulse testing (DPT) of IGBT and SiC MOSFET power devices/modules on a half-bridge DPT fixture. It:

- controls a DC supply (Magna-Power SL1000-1.5/UI+LXI), a function generator (Tektronix AFG31022) and an oscilloscope (Teledyne LeCroy WaveRunner 8108HD) over Ethernet;
- executes test matrices (V_DC × I_test × gate conditions × temperature × repetitions) with interlocked, state-machine-controlled sequencing;
- acquires waveforms, computes switching parameters (E_on, E_off, E_rec, Q_rr, t_rr, I_rrm, delays, rise/fall, dv/dt, di/dt, overshoot, L_σ) with three de-skew methods;
- stores raw data + metadata (HDF5), exports CSV in the exact result-template format, Excel, and PDF reports.

**Out of scope (v1):** closed-loop temperature control (interface reserved, §6.12), short-circuit testing, gate-driver programming, any function that the safety of personnel depends on (§2.4).

### 1.1 Definitions

| Term | Meaning |
|---|---|
| DUT | Device under test (switch being characterised) |
| Complementary device | Other switch of the half-bridge, held OFF; its diode freewheels |
| V_DC | DC-link voltage at the switching instant (measured, not the PSU setpoint) |
| I_test | Target switching current |
| t1, t2, t3 | First pulse, off-time, second pulse (§3.2 of draft, unchanged) |
| Test point | One combination of matrix parameters; may contain N repetitions |
| Shot | One fired double pulse with one acquisition |
| Run | One executed test matrix (one output folder) |
| HAL | Hardware abstraction layer (driver interfaces with real and mock implementations) |

---

## 2. System context and constraints

### 2.1 Hardware

| Equipment | Model | Transport (default → alternative) | Notes |
|---|---|---|---|
| Oscilloscope | Teledyne LeCroy WaveRunner 8108HD | **VXI-11 (LXI)** via PyVISA `TCPIP0::<ip>::inst0::INSTR` → VICP `VICP::<ip>` (needs NI-VISA + LeCroy VICP Passport) | 8 ch, 12-bit. LeCroy's VXI-11 stack accepts any remote command, so VICP is not required. Select in MAUI: Utilities > Utilities Setup > Remote. |
| Function generator | Tektronix AFG31022 | VXI-11 `TCPIP0::<ip>::inst0::INSTR` → raw socket `TCPIP0::<ip>::<port>::SOCKET` | 2 ch, 25 MHz. **Role: logic-level timing source only (0/5 V pulses of the correct width into the gate-driver input).** The gate-driver interface and its supplies perform level shifting to the device-specific V_GE/V_GS (e.g. +15/−8 V IGBT, +18/−5 V SiC). TekVISA not required; pyvisa-py or NI-VISA both work for VXI-11/socket. |
| DC-link monitor | LAN/USB bench DMM (recommended) or isolated divider + DAQ | SCPI over VXI-11/socket/USB | Independent DC-link voltage reading for SAF-04 — setup in Appendix F. |
| DC supply | Magna-Power SL1000-1.5/UI+LXI | SCPI over TCP socket `TCPIP0::<ip>::<port>::SOCKET` (port per manual, configurable) | 1000 V / 1.5 A / 1.5 kW. Source only — **cannot sink** (see PSU-08). Has a dedicated 5 V interlock input (latching) whose state is queryable over SCPI. |
| Network | TP-Link Archer C50 | Wired LAN | LAN ports are **10/100 Mbit/s only** (see NFR-PERF-03), and there are **4 LAN ports** — laptop + 3 instruments use all of them. A LAN DMM or DIO module needs an extra switch (or USB). |
| Controller | Windows 10/11 laptop | Python 3.11+ | |

### 2.2 Decisions and verified facts that shaped this spec

| Topic | Finding / decision | Consequence |
|---|---|---|
| Pulse generation approach | Decision (Everest): the AFG only has to deliver the correct 0/5 V pulse train with the correct widths; level shifting to the semiconductor-specific gate voltages is done by the gate-driver circuit and its supplies. The software builds the double pulse itself; no vendor DPT application is used. | AFG-02/03: arbitrary waveform, fixed logic levels, burst ×1, software trigger. |
| Switching definitions | Decision (Everest): IGBT → IEC 60747-9; MOSFET → IEC 60747-8. | Threshold preset selected automatically from the DUT type (§7.3). |
| AFG31022 arbitrary waveforms | Datasheet: Basic-mode ARB length 2–131,072 points; sample clock depends on model and waveform length; 16 Mpts memory per channel (128 Mpts option); 20 Vpp max into high-Z, 10 Vpp into 50 Ω. | Waveform builder must respect point-count and sample-clock limits (from config). Output level checks must account for the load-impedance setting (AFG-03). |
| LeCroy LXI | LeCroy implements a full VXI-11 stack (any command/query), LXI Class C. | VXI-11 is the default transport → no proprietary driver needed. |
| Magna-Power SL | Interlock input is a 5 V dry-contact, latching soft-fault; clear requires restoring +5 V **and** a clear command; faults readable over SCPI. | Cheapest interlock readback path: wire E-stop/door chain to the PSU interlock (Appendix A, option A). |
| Archer C50 | 4 × 10/100 Mbit/s LAN ports. | Waveform transfer time is bandwidth-limited; record lengths must be managed (NFR-PERF-03). |

### 2.3 Architecture overview

```text
 ┌──────────────────────────── Windows laptop: AutoDPT ────────────────────────────┐
 │  GUI (PySide6)  ── signals/slots ──  Sequencer + SafetyStateMachine (QThread)    │
 │        │                                   │                                     │
 │        │                       ┌───────────┼──────────────┬───────────────┐      │
 │        │                    PSU HAL     AFG HAL       Scope HAL     Aux HAL      │
 │        │                  (real|mock) (real|mock)   (real|mock)  DIO/Temp (rsv)  │
 │        │                       │           │              │                      │
 │  Analysis lib (pure NumPy, no Qt, no VISA)  ◄─── raw waveforms ───┘              │
 │  Data layer (HDF5 / CSV / Excel / PDF)                                           │
 └──────────────────────────────────────────────────────────────────────────────────┘
          │ TCP/IP (config + data only, never safety)
     TP-Link Archer C50 (isolated lab LAN)
          │                │                 │
     WaveRunner 8108HD   AFG31022 ──logic──► gate-driver interface ─► DUT
          │ probes                           (HW enable, max-on-time limiter)
     DPT fixture: DC-link C, bleeder, load L, DUT, complementary device
     Magna SL1000 ──(series R / diode per fixture design)──► DC-link ◄── DC-link monitor (DMM, Appendix F)
     Bleeder strings + NC discharge relay at the capacitor terminals (Appendix E)
     HW safety chain: E-stop, enclosure interlock → PSU interlock, discharge relay, HV lamp
```

### 2.4 Safety principle (binding on every requirement)

Software is a **convenience and data layer, never a protection layer**. Personnel safety and DUT/fixture protection rely on hardware: E-stop, enclosure interlock, fail-safe DC-link discharge, independent HV indicator, gate-driver DESAT/overcurrent protection, a hardware max-on-time limiter in the gate path, and the PSU's own OVP/OCP/interlock. The software shall behave as if any of its own checks could fail, and shall never present a software reading as a safety confirmation ("HV PRESENT" is informational; the hardware lamp is authoritative).

---

## 3. Review findings on the draft (what changed and why)

| # | Severity | Draft item | Finding | Resolution in this SRS |
|---|---|---|---|---|
| R1 | Critical | FR-SAF-04 | PSU readback may not reflect DC-link voltage after output-off if the fixture has a blocking diode / series resistor, or if the PSU disconnects its output. A "0 V" reading could coexist with a charged link. | Dedicated DC-link monitor (Appendix F); PSU readback secondary; plausibility checks vs. PSU while charging and vs. bleeder τ while discharging (SAF-04, SAF-09, SAF-12). |
| R2 | Critical | FR-PSU-03 | The PSU cannot sink. Going from a high to a low V_DC (or to 0 V) depends on the bleeder. A sweep ordered high→low will wait minutes or test at the wrong voltage. | Bleeder + fail-safe active discharge designed in Appendix E; ascending V order by default; explicit bleed-down wait with timeout from R_bleed·C (PSU-08, SEQ-08). |
| R3 | Critical | FR-AFG-03/04 | The AFG only supplies 0/5 V logic (agreed), but the level is only right if the AFG's load-impedance setting matches the gate-driver input: programmed 5 V with a 50 Ω setting into a high-Z input gives ≈ 10 V at the logic pin. Burst-mode idle level and output-enable glitches were also not addressed — a glitch is a gate pulse. | Impedance read-back verification, idle-level = LOW waveform construction, commissioning tests for glitch/idle (AFG-03, AFG-07, VER-HW-01). |
| R4 | Major | §3.3 / FR-ANA-01 | Diode current is not measured directly in the default channel map; E_rec/Q_rr/t_rr need i_D and v_D. Draft does not say how they are obtained. | i_D = I_L − i_C with I_L from linear extrapolation (or inductor-current channel if present); v_D from complementary-device channel or derived (ANA-11/12). |
| R5 | Major | FR-SEQ-03 | t1 correction alone ignores DC-link droop: with C = 1 mF, 450 V, 600 A, L = 20 µH, the pulse draws ≈ 9.9 mC → ≈ 9.9 V droop (2.2 %). Results must use V_DC measured at the switching instant, not the setpoint. | LC-aware initial t1, droop estimate + warning, V_DC from waveform (SEQ-03, ANA-07, CFG-03). |
| R6 | Major | FR-SCO-03 | Archer C50 is 100 Mbit/s. Long t1 at low voltage (e.g. 50 V, 600 A, 20 µH → t1 = 240 µs) at 5 GS/s × 8 ch × 2 B ≈ 21 MB/shot → ≈ 2–3 s transfer. | Transfer only enabled channels; configurable record strategy; gigabit switch recommended (NFR-PERF-03). |
| R7 | Major | §3.4 statistic de-skew | Method underspecified; can be ill-conditioned when the L_σ·di/dt dip is small vs. noise (low V_DC, small L_σ). | Algorithm fully specified with residual/confidence and invalid flag (ANA-20…23). |
| R8 | Major | FR-SEQ-01 | No stop rules: a sweep could continue into higher current after an overshoot approaching the DUT rating or after a DESAT event. | Stop rules on overshoot margin, invalid shots, faults (SEQ-09). |
| R9 | Major | Probes | Rogowski coils droop (LF cutoff) and have no DC; shunts have pulse-energy limits; probe bandwidth can bias rise times and energies. | Probe library with droop compensation, pulse-energy check, bandwidth-limited-edge flag (CFG-01, ANA-05, ANA-30). |
| R10 | Minor | FR-DATA-02 | Template now supplied (.xlsx, 59 columns + a normalisation block with formulas). Several sample values reveal definition issues in the tool that produced it — see §8.4. | Column-by-column mapping (§8.3); xlsx writer reproduces header, units and normalisation block (DATA-02, DATA-06, DATA-10). |
| R11 | Minor | Network | Archer C50 is a Wi-Fi router; HV bench should not be reachable from Wi-Fi/WAN. | Network setup requirements (NET-07). |
| R12 | Major | §3.3 E_on/E_off windows | The draft's windows (E_on from 10 % I_C, E_off from 10 % V_CE) are not the IEC 60747-9 windows, which start on the gate edge (10 % / 90 % V_GE, referenced to 0 V). E_off from 90 % V_GE includes I·V_CE(sat)·t_d(off) — a few % of E_off. | Presets per standard, auto-selected by DUT type (ANA-15): IEC 60747-9 (IGBT), IEC 60747-8 (MOSFET), plus a LEGACY preset = draft table, for comparison with old data. |

---

## 4. Software architecture requirements

- **ARC-01** The application shall be layered: `gui` → `sequencer` → `hal` (drivers) and `analysis` / `data`. The `analysis` package shall depend only on NumPy/SciPy (no Qt, no VISA), so it runs offline (ANA-40).
- **ARC-02** Every instrument shall be accessed through an abstract interface (`PowerSupply`, `PulseGenerator`, `Oscilloscope`, `DcLinkMonitor`, `DigitalIO`, `TemperatureSource`, `TemperatureController`) with at least two implementations: real and mock.
- **ARC-03** Each real instrument session shall be owned by exactly one worker object; VISA sessions shall not be shared between threads. Commands are serialized through that worker. A **priority safe-off path** (AFG output OFF, PSU output OFF) shall pre-empt queued commands (NFR-PERF-01).
- **ARC-04** The sequencer and safety state machine shall run in a worker thread (QThread); the GUI thread shall never block on instrument I/O.
- **ARC-05** Configuration shall be validated with typed schemas (pydantic v2) at load time; invalid configs shall be rejected with a message naming the field.
- **ARC-06** Instrument limits (AFG sample clock and ARB length, scope max sample rate per channel count and memory, PSU ranges) shall be stored in `config/instruments/*.yaml`, not hard-coded.

### 4.1 Recommended stack

| Purpose | Package |
|---|---|
| Instrument I/O | `pyvisa` with `pyvisa-py` backend (default) or NI-VISA (required only for VICP) |
| GUI | `PySide6`, `pyqtgraph` (interactive waveforms) |
| Numerics | `numpy`, `scipy` |
| Config | `pydantic` v2, `PyYAML` (YAML) + JSON |
| Data | `h5py`, `pandas`, `openpyxl` |
| Report | `matplotlib` (figures), `reportlab` (PDF) |
| Tests | `pytest`, `pytest-qt`, `hypothesis` (optional) |
| Quality | `ruff`, `mypy` (analysis + hal packages strict) |
| Packaging | `PyInstaller` (one-folder build) |

### 4.2 Repository layout (target)

```text
autodpt/
  app.py                     # entry point, --sim flag
  config/                    # pydantic models + defaults + instruments/*.yaml
  hal/                       # base.py, psu_magna.py, afg_tek31k.py, scope_lecroy.py, mock/*.py, dio.py, temp.py
  sequencer/                 # state_machine.py, sequencer.py, pulse_planner.py, safety.py
  analysis/                  # preprocess.py, edges.py, switching.py, recovery.py, deskew.py, validate.py, synth.py
  data/                      # hdf5_store.py, csv_template.py, excel.py, eventlog.py
  report/                    # pdf_report.py, figures.py
  gui/                       # main_window.py, screens/*.py, widgets/*.py
tests/                       # unit, integration (mock), golden waveforms
docs/                        # SRS.md, manuals/, commissioning.md
template/                    # FS820_6IN1_Wphase_HS_400V_50-950A_Ron3_Roff6.6_125oC.xlsx + template_map.yaml
```

---

## 5. Safety state machine

- **SAF-01 [CHG]** The software shall implement the state machine below. Output permissions are enforced inside the HAL wrapper (a command that is not permitted in the current state shall raise and be logged), not only in the GUI.

| State | PSU output | AFG output | Entry actions | Leaves to |
|---|---|---|---|---|
| DISCONNECTED | — | — | — | IDLE (all required instruments connected and verified) |
| IDLE | OFF | OFF | assert AFG OFF, PSU OFF | CONFIGURED |
| CONFIGURED | OFF | OFF | instruments configured, limits checked (SAF-03) | HV_CHARGING (after operator HV confirmation, SAF-02), IDLE |
| HV_CHARGING | ON | OFF | ramp to target; wait for in-tolerance V_DC | HV_READY, FAULT, DISCHARGING |
| HV_READY | ON | OFF | V_DC stable | ARMED, HV_CHARGING (new V), DISCHARGING, OPERATOR_ACTION |
| ARMED | ON | ON (idle LOW) | scope armed & confirmed, AFG waveform loaded & verified | PULSING, HV_READY (disarm) |
| PULSING | ON | ON | software trigger sent; wait for scope acquisition | ACQUIRING (on trigger) or FAULT (timeout) |
| ACQUIRING | ON | **OFF** (first action) | AFG output OFF, then waveform transfer | ANALYSING |
| ANALYSING | ON | OFF | analysis, validation, t1 update | HV_READY (next shot), DISCHARGING, FAULT |
| OPERATOR_ACTION | OFF | OFF | prompt (Rg / V_GE / DUT / temperature change) | DISCHARGING → then CONFIGURED |
| DISCHARGING | OFF | OFF | PSU 0 V + OFF; monitor DC-link V until < V_safe or timeout | IDLE, FAULT |
| FAULT | OFF | OFF | safe-off; latch reason; require operator acknowledge | IDLE (after ack and V < V_safe) |

- **SAF-02** Before HV_CHARGING the operator shall confirm a checklist (enclosure closed, probes connected and ratings checked, personnel clear, hardware HV lamp functional). The confirmation, user name and time shall be logged. Confirmation is valid for the run until any OPERATOR_ACTION or FAULT.
- **SAF-03 [CHG]** All inputs shall be checked against configurable hardware limits *before any instrument is configured*: max V_DC (min of PSU, fixture, DUT V_CES/V_DSS × derating, probe ratings, DC-link capacitor rating), max peak current (min of DUT, fixture, inductor, current-probe peak rating), max pulse width, max total on-time per shot, max gate-input level, shunt pulse energy (if a shunt is configured: I_pk²·R·(t1+t3) ≤ rating).
- **SAF-04 [CHG]** The DC-link voltage shown and used by the safety logic shall come from the **dedicated DC-link monitor** (Appendix F), polled ≥ 2 Hz whenever HV may be present. After PSU output-off the application shall display "HV PRESENT (software reading — hardware indicator is authoritative)" until the monitor reads below V_safe (default 50 V, configurable; must be ≤ 60 V DC). Source priority: (1) dedicated monitor (DMM/DAQ), (2) scope `VDC` channel (only valid in an acquisition taken after output-off), (3) PSU readback — sources (2) and (3) shall be shown but shall never alone clear the indication.
- **SAF-05** Controlled shutdown (abort, fault, communication loss, exit): AFG output OFF → PSU set 0 V and output OFF → wait for DC-link discharge (timeout = max(10 s, 1.5·R_bleed·C·ln(V_start/V_safe))) → IDLE or FAULT. Each step logged with timestamp and result. A failing step shall not prevent the following steps.
- **SAF-06** Unhandled exceptions, GUI close and SIGINT/console close shall run SAF-05 before exit (`sys.excepthook`, `QApplication.aboutToQuit`, `atexit`).
- **SAF-07** The software shall not be the only protection layer (see §2.4). The GUI "About/Safety" page shall state this.
- **SAF-08 [NEW]** On application start and on reconnect, the software shall force AFG output OFF and PSU output OFF before any other command.
- **SAF-09 [NEW] Discharge plausibility.** During DISCHARGING the software shall fit the measured DC-link decay to an exponential and compare τ_meas with τ_exp = R_bleed·C (from fixture config). If τ_meas > 1.5·τ_exp (configurable; 1.5 covers ±10 % C and R tolerance and still detects one open string of the two-string bleeder of Appendix E, which doubles τ), raise warning **"Bleeder may be open — verify discharge with hardware indicator before opening the enclosure"** and enter FAULT.
- **SAF-10 [NEW]** Every fired shot shall be counted and logged (run ID, point ID, attempt number, commanded t1/t2/t3, V_DC). The total pulse count per DUT shall be stored in the DUT record.
- **SAF-12 [NEW] Monitor plausibility ("live–dead–live" in software).** (a) While the PSU output is ON and settled, |V_monitor − V_PSU| shall be ≤ ΔV_allowed = V_diode + I_PSU·R_series + monitor and PSU accuracy (all from config); otherwise FAULT `MONITOR_DISAGREE`. (b) A "discharged" reading is accepted only if the same monitor showed ≥ 0.9·V_target earlier in the same HV cycle; otherwise the GUI shows "Monitor not verified — measure with a meter before opening". (c) Monitor communication loss while HV may be present → FAULT, indication stays "HV PRESENT".
- **SAF-13 [NEW] Discharge request.** If a `DISCHARGE_REQ` output is configured (DIO-03), the software shall assert it on entering DISCHARGING and before any downward V_DC step (PSU-08), and only after PSU output OFF has been confirmed. The hardware shall make the discharge relay close on loss of coil power and shall prevent the PSU from being enabled while the relay is closed (Appendix E).
- **SAF-11 [NEW]** If any HAL call raises a communication error while PSU output may be ON, the software shall execute SAF-05 using the remaining reachable instruments and enter FAULT.

---

## 6. Functional requirements

### 6.1 Network and connection (NET)

- **NET-01 [CHG]** Instrument addresses, transport type (VXI-11 / VICP / SOCKET), port and timeouts shall be stored in `config/station.yaml`. Static IPs or DHCP reservations shall be used.
- **NET-02** A connection screen shall show for each instrument: resource string, `*IDN?` response, firmware version, connection state, last error.
- **NET-03** Model and firmware shall be checked against `config/instruments/supported.yaml`; mismatch → warning (model mismatch → block).
- **NET-04** All I/O shall use timeouts. After every configuration block, the instrument error queue shall be read (`SYST:ERR?` or the instrument's equivalent) until empty; any error shall be logged and, during a run, abort the point.
- **NET-05** Communication loss during a run shall trigger SAF-11.
- **NET-06** Simulation mode (`--sim` or GUI toggle) shall run the complete application with mock instruments (§6.13).
- **NET-07 [NEW]** Commissioning documentation shall require: Archer C50 Wi-Fi disabled (or isolated SSID with no route), WAN port unconnected, laptop firewall allowing only the instrument subnet for the app. The connection screen shall warn if the laptop has a default route on the instrument interface.
- **NET-08 [NEW]** A "Ping/Identify" button per instrument shall run `*IDN?` and a harmless read to verify the link without changing outputs.
- **NET-09 [NEW, v1.3] Bench check.** A command `autodpt bench-check <station>` (and later a GUI page) shall verify connection and controllability and write a JSON + Markdown report:
  - *connect level (read-only):* laptop interface/subnet, default-route isolation (NET-07), TCP reachability of each resource's port (111 VXI-11, socket port, 1861 VICP), VISA open, `*IDN?` against the command set (NET-03), error queue, median round-trip latency, output-state report. No setting commands are sent.
  - *control level:* after operator confirmation, force AFG and PSU outputs OFF (SAF-08) and prove each instrument executes commands by write → read-back → restore on settings that are harmless with outputs off: AFG load impedance, logic levels, ARB function, burst ×1 and an ARB upload with point-count read-back; PSU voltage/current setpoints and status/interlock decoding; scope communication format, timebase, optional single-shot acquisition, full waveform transfer with throughput, and a self-check of the binary waveform-descriptor parser against the scope's `INSPECT?` text; DC-link monitor agreement with the PSU read-back.
  - It shall never enable an output or trigger the AFG; forbidden commands are blocked at transport level. Outputs are verified OFF at the end.
  - Command strings come from per-instrument command sets with a `verified` flag; the report lists unverified commands, and a passing control step confirms them on the actual firmware.

### 6.2 Configuration GUI (CFG) [NEW — from review feedback]

- **CFG-01 Probe library.** The user shall create, edit and save probe definitions with: name, type (passive, HV differential, optically isolated, Rogowski, coaxial shunt, current transformer, Hall/zero-flux), scale factor and unit (V/V, V/A or shunt Ω), bandwidth, rise time, propagation delay (for single de-skew), max voltage/current/di/dt rating, Rogowski low-frequency time constant (for droop compensation), shunt pulse-energy rating, serial number, calibration due date. Expired calibration → warning on run start.
- **CFG-02 Channel map.** The user shall assign scope channels to signals: `VGE_DUT`, `VCE_DUT`, `IC_DUT` (required); `VGE_COMP`, `VCE_COMP`, `IL` (inductor current), `VDC` (DC-link), `AUX1..n` (optional). Each assignment references a probe from CFG-01, polarity (invert), offset, bandwidth limit and de-skew value. One channel may be marked as the DC-link monitor for SAF-04.
- **CFG-03 Fixture parameters.** DC-link capacitance C (and tolerance), bleeder resistance R_bleed (and number of parallel strings), active-discharge resistance R_dis and relay present (yes/no), DC-link monitor type/scale/accuracy and series resistance R_series/diode drop between PSU and link, load inductance L (value, measurement frequency, DC resistance), fixture max V and I, PSU series resistor/diode present (yes/no). Derived values shall be shown live: stored energy ½CV², bleed-down time to V_safe, per-shot charge and droop estimate, inductor energy ½LI².
- **CFG-04 DUT record.** Part number, type (IGBT / SiC MOSFET / other), rated V_CES/V_DSS, nominal current, Tvj max, sample/serial ID, position (phase U/V/W; HS/LS), notes. Cumulative shot count (SAF-10).
- **CFG-05 Gate drive record.** V_on, V_off, R_g,on, R_g,off, gate-driver type, measured propagation delay AFG→gate (optional). These are operator-set hardware values; the software records them and prompts for changes (SEQ-05).
- **CFG-06 Limits.** Editable site limits (SAF-03) shall be protected by a separate "lab supervisor" profile (simple local password is sufficient; not a security control).
- **CFG-07** All configuration objects shall be saved/loaded as YAML or JSON (GUI-06) and a full snapshot embedded in every HDF5 file.

### 6.3 DC power supply — Magna-Power SL1000-1.5 (PSU)

- **PSU-01** Set voltage setpoint, current limit, OVP (OVT) and OCP (OCT) trip levels. Defaults: OVP = min(1.05·V_target, V_max), current limit = configurable (≤ 1.5 A).
- **PSU-02** Reject setpoints above the configured max V (≤ 1000 V and ≤ SAF-03 limit).
- **PSU-03 [CHG]** Ramp to target at a configurable rate (V/s), then wait until the *DC-link channel* (if configured; otherwise PSU readback) is within tolerance (default ±0.5 %) for a configurable settle time.
- **PSU-04** Poll output voltage, current and status/fault registers (OVP/OVT, OCP/OCT, over-temperature, interlock, other faults per manual) at a configurable interval (default 250 ms) while output may be ON.
- **PSU-05** Any PSU fault → abort test, SAF-05.
- **PSU-06** End of test or abort: setpoint 0 V, output OFF; never assume discharge (SAF-04).
- **PSU-07 [NEW]** Interlock state from the PSU status register shall be shown on the run screen and logged. An active interlock fault shall block HV_CHARGING. The interlock clear command shall only be sent after explicit operator action.
- **PSU-08 [NEW]** When the new target V is lower than the present DC-link voltage by more than the tolerance, the software shall set the new setpoint and wait for bleed-down (timeout from R_bleed·C), then settle. It shall not fire until settled.

### 6.4 Pulse generation — Tektronix AFG31022 (AFG)

- **AFG-01 [CHG]** t1 shall be computed by the pulse planner (SEQ-03). t2 and t3 are user inputs (defaults 3 µs / 3 µs). An additional configurable delay `t_pre` (default 5 µs) precedes t1 so the scope captures a baseline.
- **AFG-02 [CHG]** The double pulse shall be built as an arbitrary waveform on CH1, played in **burst mode, count 1, software trigger**. Construction rules:
  - Sample interval Δt = max(1/f_clk,max, total_duration/N_max) using limits from config; all edges quantised to Δt; quantisation error logged.
  - First and last ≥ 8 samples = LOW; the waveform shall end LOW.
  - The waveform shall be uploaded as normalised data, then amplitude/offset set so that LOW = 0 V and HIGH = 5 V (logic levels; selectable list in config, e.g. 3.3 V or 5 V; hard upper limit default 5.5 V). The AFG never drives a power-device gate: device-specific gate voltages (V_on/V_off) are produced by the gate-driver circuit and only *recorded* by the software (CFG-05).
  - After upload, the software shall read back the waveform length and the burst/trigger settings and verify them.
- **AFG-03 [CHG]** Output load impedance setting shall match the configured gate-driver input (default high-Z) and be read back together with HIGH/LOW levels. The software shall compute the level actually present at the driver input from the setting and the configured input impedance (e.g. 50 Ω setting into high-Z → 2× programmed level) and block arming if it exceeds the logic-input limit. Readback failure → block arming.
- **AFG-09 [NEW] Timing reference.** t1/t2/t3 are defined at the AFG output. Gate-driver propagation delays and device t_d(on)/t_d(off) shift the effective conduction time; this is corrected by the measured-current iteration (SEQ-03) and the learned factor k_V, not by a fixed offset. The measured AFG→V_GE delay (rising and falling) shall be reported per shot for information.
- **AFG-04** AFG output OFF on start, connect, abort, shutdown and after every shot (ACQUIRING entry); ON only in ARMED/PULSING.
- **AFG-05** Reject parameters exceeding limits: max t1, max total on-time (t1 + t3), max I_peak,est = I_0 + V_DC·(t1 + t3)/L (with I_0 = 0 for a fresh shot), and minimum t2/t3 (default 1 µs) to ensure the transient settles in the window.
- **AFG-06 [CHG]** CH2 shall output a scope trigger marker (single pulse, 1 µs, aligned to t = 0 of the CH1 waveform, same burst trigger). Fallback: scope edge trigger on `VGE_DUT`.
- **AFG-07 [NEW]** A commissioning routine (no HV) shall: (a) measure CH1 idle level before/after enabling output; (b) detect any glitch at output enable/disable (scope single-shot on CH1); (c) fire a test burst and compare measured t1/t2/t3 with commanded (±2·Δt + 10 ns). Results are stored as a station verification record; the run shall warn if the record is older than a configurable period (default 30 days).
- **AFG-08 [NEW]** The software shall not use continuous-run mode for CH1 under any state. Any readback showing CH1 not in burst mode while output ON → immediate AFG output OFF and FAULT.

### 6.5 Oscilloscope — LeCroy WaveRunner 8108HD (SCO)

- **SCO-01** Configure channels per CFG-02: scale/attenuation, units, coupling, offset, bandwidth limit, de-skew (scope-side de-skew default 0; software de-skew preferred so the raw data stay unshifted — configurable).
- **SCO-02 [CHG]** Vertical auto-scaling from test conditions: V_CE range ≥ V_DC·(1 + expected overshoot fraction, default 0.4); I_C range ≥ I_test·(1 + expected I_rrm fraction, default 0.6) and ≥ I_peak,est; V_GE range from V_off/V_on with 20 % margin. Optional "probe shot" at reduced V_DC to refine. Clipping shall be detected on every shot from raw ADC codes at or beyond the top/bottom limits (or the scope's overrange flags); clipped channel → shot invalid.
- **SCO-03 [CHG]** Timebase and record: capture from `t_pre` before t = 0 to ≥ 3 µs after the third edge; sample rate ≥ target (default 5 GS/s) on all enabled channels. If the required memory/sample-rate combination is not achievable, the software shall propose: reduce channels, reduce sample rate (warning if < 10 samples on the fastest expected edge), or use sequence/segmented capture if supported (verify).
- **SCO-04** Single-shot trigger on AFG CH2 marker (AFG-06); arm, then confirm armed state (query) before AFG trigger is sent.
- **SCO-05** Wait for acquisition complete (poll with timeout), then transfer enabled channels as binary (16-bit word) with full scaling metadata (vertical gain/offset, horizontal interval/offset, trigger offset, instrument timestamp). Metadata shall be parsed from the waveform descriptor, not reconstructed from settings.
- **SCO-06** Optional: save `.trc` and screenshot on the scope with the same base filename.
- **SCO-07 [NEW]** On connect the software shall record scope settings that affect data (interpolation, acquisition mode, HD/ERES/averaging state, channel de-skew) and refuse runs if averaging or non-real-time acquisition is enabled.

### 6.6 Test sequencer (SEQ)

- **SEQ-01** Test matrix: V_DC list × I_test list × (R_g,on/R_g,off) × (V_on/V_off) × temperature list; plus L, side (HS/LS), phase label, repetitions per point, t2, t3.
- **SEQ-02 [CHG]** Per test point: pre-checks → (V change: PSU-03/PSU-08) → scope config → pulse plan → AFG config/verify → ARM (scope armed, AFG output ON) → fire → ACQUIRING (AFG output OFF, transfer) → analyse → validate → log/store → cool-down → next shot.
- **SEQ-03 [CHG] Pulse planner.**
  - Initial t1 from the LC-aware formula, which includes DC-link droop: t1 = √(L·C)·asin(I_test·√(L/C) / V_DC). If the asin argument ≥ 1 → reject (C too small for the requested current).
  - Learned correction: t1 ← t1 · k_V, where k_V is the last measured I_target/I_meas ratio at the same V_DC (reset when V_DC, L or DUT changes).
  - Iteration: t1,k+1 = t1,k · I_target / I_meas,k, step limited to ±20 %, until |I_meas − I_target| ≤ tol (default ±2 %) or max attempts (default 4). Attempt shots are stored and flagged `tuning`.
  - The droop estimate ΔV ≈ Q/C with Q = ½·I·t1 + I·t3 shall be shown; warning if > 2 % of V_DC (configurable).
- **SEQ-04** Minimum shot interval = max(user cool-down, recharge time Q/I_lim × 3, PSU settle time). Default user cool-down 1 s.
- **SEQ-05** Non-software parameters (R_g, V_on/V_off, DUT swap, manual temperature) → OPERATOR_ACTION: HV discharged first, prompt shows exactly what to change, operator enters the actual values set, then re-confirmation (SAF-02).
- **SEQ-06** Pause/resume/abort at any time; completed points preserved (data written per shot, not at end).
- **SEQ-07** Re-run single point; resume a sweep from a selected point; append to the same run.
- **SEQ-08 [NEW] Ordering.** Default loop order (outer → inner): temperature → gate conditions → V_DC ascending → I_test ascending → repetitions. The user may reorder but a descending V_DC order shall show the bleed-down time penalty before start.
- **SEQ-09 [NEW] Stop rules** (each configurable, default ON):
  - V_CE,peak > k_ov · V_rated (default k_ov = 0.85) → stop current sweep at this V_DC, skip higher I_test, warn.
  - Gate-driver fault/DESAT (when readable, §6.11) or I_C,peak > limit → FAULT.
  - Two consecutive invalid shots at the same point → pause for operator.
  - Measured V_DC at switching deviates > 5 % from target → invalid shot.
- **SEQ-10 [NEW]** Dry-run mode: run the full sequence with HV disabled (PSU output never enabled; operator acknowledges) to verify triggering, timing and data handling.

### 6.7 Waveform analysis (ANA)

General
- **ANA-01** Compute every parameter in the result template using the definitions in §7.
- **ANA-02** Compute energies and times for de-skew methods: none, single, statistic (§7.5).
- **ANA-03** Filtering: none (default) / moving average (N samples) / zero-phase low-pass (Butterworth, `filtfilt`, cutoff). The filter type and parameters shall be stored with every result. Raw data are always stored unfiltered.
- **ANA-04** Each shot shall be flagged VALID / WARNING / INVALID with machine-readable reason codes (§7.7).
- **ANA-05** Rogowski droop compensation (optional per probe): first-order inverse, i_corr(t) = i(t) + (1/τ_L)·∫ i dt from the baseline window; baseline offset removed first.
- **ANA-06** The analysis entry point shall be a pure function `analyse_shot(waveforms, metadata, analysis_config) -> ShotResult` with no I/O.

Remaining ANA requirements are in §7 (ANA-07 … ANA-40).

### 6.8 Data management (DATA)

- **DATA-01** One HDF5 file per run (layout §8.1); every shot written and flushed immediately.
- **DATA-02 [CHG]** Result workbook (.xlsx, primary) per §8.3: one workbook per combination of DUT, phase, side, V_DC, R_g,on, R_g,off, gate voltages and temperature, with one row per current point. Implementation shall be template-driven: header row (row 1) and units row (row 2) are copied verbatim from the template file at runtime; values are filled through `template/template_map.yaml` (column header → internal result key, scale, de-skew method, decimals). Unmapped template columns → "NaN" + warning in the log; never reorder.
- **DATA-03 [CHG]** A CSV with the same header row, units row and data rows shall be written next to each workbook (UTF-8 with BOM so Excel displays "°" correctly). A combined Excel file with one sheet per workbook of the run may be generated on request.
- **DATA-04** Event log: timestamped (ISO 8601 with ms), level, source (GUI/SEQ/HAL/ANA), message; every instrument command and response at DEBUG level; rotated files; also stored in HDF5.
- **DATA-05 [CHG]** File name pattern configurable; default follows the supplied template: `<DUT>_<Config>_<Phase>phase_<HS|LS>_<V>V_<Imin>-<Imax>A_Ron<Rgon>_Roff<Rgoff>_<T>oC` (example: `FS820_6IN1_Wphase_HS_400V_50-950A_Ron3_Roff6.6_125oC`). Optional tokens `_Von<x>_Voff<y>` when gate voltages are swept. Numbers formatted without trailing zeros. If the file exists, a suffix `_r<n>` is added — never overwrite.
- **DATA-06 [NEW]** Unit tests shall assert that (a) rows 1–2 of every generated workbook are identical to the template's, (b) the CSV header/units rows are identical apart from line endings and BOM, (c) the normalisation block formulas have exactly the template's form (DATA-10) with row references shifted per point.
- **DATA-10 [NEW] Normalisation block.** Below the data rows (start row = last data row + 4; = row 25 for the template's 19 rows, configurable) the writer shall reproduce the template's block: labels Date / Temperature / "Voltage " / Resistance on (+ "Gate voltage on") / Resistance off (+ "Gate voltage off") with formulas referencing row 3; then the header Current / Turn-on energy / Turn-off energy / Reverse recovery energy and **one row per data row r** (template has only the first):
  - Current `=G{r}`
  - E_on,norm `=M{r}*(B{k}/AE{r})*(E{r}/AG{r})`  (E_on × I_set/I_on × V_set/V_on)
  - E_off,norm `=N{r}*(B{k}/AF{r})*(E{r}/AH{r})`  (E_off × I_set/I_off × V_set/V_off)
  - E_rec,norm `=W{r}*(E{r}/AG{r})*(B{k}/AE{r})^0.5`  (E_rec × V_set/V_on × √(I_set/I_on))
  where k is the block row itself. If any referenced cell is "NaN", write "NaN" instead of the formula. Option (default off): fill the empty "Gate voltage on/off" value cells with `=J3` / `=K3`. Option: use single- or statistic-de-skew energy columns instead of M/N/W (default as template). The linear I and V scaling is the template's empirical rule; with SEQ-03 holding I within ±2 % the correction is small and the exponent choice hardly matters.
- **DATA-11 [NEW]** Sheet name = file stem truncated to 31 characters with characters `[]:*?/\` replaced by `_` (Excel limits; the template's sheet name is the truncated stem). Date/time cells use number format `m/d/yy h:mm`. Values are rounded to the decimals given in `template_map.yaml` (default 2, as in the template); full precision stays in HDF5.
- **DATA-12 [NEW]** Row granularity configurable: `per_point` (default; mean over valid repetitions, Measurement Point = point index) or `per_shot` (every valid shot one row). Invalid shots never appear in the workbook; they remain in HDF5.
- **DATA-07 [NEW]** Re-analysis of an existing HDF5 file with a different analysis config shall write a new results group (versioned), never overwrite earlier results.

### 6.9 GUI (GUI)

- **GUI-01** PySide6 screens: Connection, Configuration (probes, channels, fixture, DUT, gate drive, limits), Test setup (matrix), Run/monitor, Results, Reports, Event log.
- **GUI-02** Run screen: state machine state (color-coded), PSU V/I, DC-link V, interlock state, current point/attempt/progress, ETA, last shot validity, large ABORT button (also bound to a keyboard shortcut, e.g. Esc or F12) always enabled.
- **GUI-03** Waveform view after each shot: overview + zoom tabs for turn-off, turn-on and reverse recovery; markers for threshold crossings and shaded integration windows; toggle de-skew method.
- **GUI-04** Result plots vs. sweep variable (e.g. E_on/E_off/E_rec vs I_test per V_DC), with repetition mean ± std.
- **GUI-05** Overlay waveforms/results from different runs, DUTs or conditions (load from HDF5).
- **GUI-06** Save/load test setups (YAML/JSON).
- **GUI-07 [NEW]** Pre-run summary dialog: matrix size, estimated shot count (including tuning), estimated duration, max V_DC/I_peak/energy, warnings (droop, bandwidth, bleed-down time).
- **GUI-08 [NEW]** The GUI shall remain responsive (event loop not blocked > 100 ms) during waveform transfer and analysis.

### 6.10 Reporting (REP)

- **REP-01** Report (PDF and Excel): test conditions, station/instrument IDs and firmware, probe list with calibration dates, fixture and DUT data, analysis definitions (threshold set, integration windows, filter, de-skew method), results table, characteristic plots, representative waveforms with markers, validity flags, software version and git hash.
- **REP-02** Configurable template: logo, header fields, sections and plots included.
- **REP-03 [NEW]** Reports shall state the de-skew method and the threshold preset used for every energy/time column.

### 6.11 Interlock and digital I/O — reserved (DIO) [NEW]

- **DIO-01** The HAL shall define a `DigitalIO` interface (read named inputs, optional named outputs) with a mock and a "none" implementation. No hardware is required in v1.
- **DIO-02** Named inputs reserved: `ESTOP_OK`, `DOOR_CLOSED`, `HV_PRESENT_HW`, `DCLINK_SAFE_HW` (hardware comparator, Appendix F), `GATE_DRIVER_FAULT`, `DISCHARGE_RELAY_CLOSED`. When configured, they shall be shown on the run screen, logged, and used as **permissives** (all OK → allow HV_CHARGING/ARMED; any not OK → safe-off and FAULT).
- **DIO-03** Reserved outputs: `GATE_ENABLE_REQ`, `DISCHARGE_REQ` (SAF-13). If used, the hardware shall AND it with the interlock chain; the software shall treat it as a request, never as a safety function.
- **DIO-04** Polling ≥ 10 Hz while HV may be present.
- See Appendix A for the options and recommendation.

### 6.12 Temperature — reserved (TMP) [NEW]

- **TMP-01** HAL interfaces `TemperatureSource` (read °C, channel name) and `TemperatureController` (setpoint, read, is_stable) with implementations: `Manual` (operator enters measured value at OPERATOR_ACTION), `Mock`. Future: hot-plate controller, thermocouple reader.
- **TMP-02** Sequencer step `TEMP_SOAK`: wait until |T − T_set| ≤ band (default 1 K) for t_stable (default 5 min), with timeout; HV OFF during heating unless explicitly allowed.
- **TMP-03** Every shot stores T_set, T_meas (or "manual") and source; CSV/report include them.
- **TMP-04** The GUI shall contain a disabled "Temperature control" panel showing "not configured" until a controller is selected.
- Note for the operator: at single-shot duty the junction temperature ≈ case/plate temperature after soak; the software records the measured case/plate temperature, not Tvj.

### 6.13 Simulation and mock instruments (SIM) [NEW]

- **SIM-01** Mock PSU: first-order voltage ramp limited by current limit into C, bleeder discharge, injectable faults (OVP, interlock, comm loss).
- **SIM-02** Mock AFG: stores waveform and settings, validates them against AFG limits, injectable errors.
- **SIM-03** Mock scope: returns synthetic DPT waveforms from `analysis/synth.py` consistent with the commanded t1/t2/t3, V_DC, L, C; configurable skew, noise, quantisation (12 bit), clipping, missing edge, ringing.
- **SIM-04** Mock latencies configurable (to test GUI responsiveness and abort timing).

---

## 7. Analysis specification (normative)

### 7.1 Signals and sign conventions

- **ANA-07** Inputs after scaling: v_ge(t), v_ce(t), i_c(t) (DUT); optional v_ge,c(t), v_ce,c(t) (complementary), i_L(t), v_dc(t). Positive i_c = conventional current into collector/drain of the DUT. Diode current i_D positive in the forward (freewheeling) direction; reverse recovery makes i_D negative. For a high-side DUT the channel polarities from CFG-02 make these conventions hold.
- **ANA-08** Pre-processing order: (1) scale codes → physical units; (2) baseline: subtract mean of `i_c` (and `i_L`) over the pre-pulse window [−t_pre+0.5 µs, −0.5 µs] (current known zero); (3) Rogowski droop compensation if configured; (4) de-skew time shift by interpolation onto the reference time base (`v_ce` is the reference; shifts applied to current channels; fractional-sample shifts by linear interpolation, cubic optional); (5) optional filter. Each step's parameters are recorded.

### 7.2 Edge windows and reference levels

- **ANA-09** Gate edges: locate the four DUT gate transitions by 50 % crossings of the v_ge swing, searched near the commanded times (±1 µs). Missing edge → INVALID (`EDGE_MISSING`).
- **ANA-10** Plateaus: V_GE,on = median of v_ge over the last 30 % of t1; V_GE,off = median over the pre-pulse window. Gate thresholds depend on the preset (§7.3): IEC presets reference the percentage to 0 V, V_x = x·V_GE,on (e.g. +15.5/−6.5 V: 10 % = 1.55 V, 90 % = 13.95 V); the LEGACY preset uses the swing, V_x = V_GE,off + x·(V_GE,on − V_GE,off). The reference rule is part of the preset and is recorded with the results. "%I" thresholds refer to the switching current (I_on or I_off, ANA-11) and "%V" thresholds to the measured V_DC (ANA-13), both referenced to zero.
- **ANA-11 [P1] Switching currents.**
  - I_off = DUT current at t_v90 (V_CE reaches 90 % of V_DC,off at turn-off). The conduction ramp is fitted over [0.6, 1.0]·t1 (minus 200 ns guard before the gate edge) and extrapolated only to t_v10 (start of the voltage rise); the current gained during the voltage rise is added from the measured voltage: I_off = fit(t_v10) + (1/L)·∫[t_v10, t_v90] (V_DC,off − v_CE) dt. *Reason:* during the voltage rise the inductor sees V_DC − v_CE, so a straight extrapolation over-states the current (≈ 0.4 % for an IGBT with 100 ns voltage rise).
  - I_on = inductor current at the second turn-on: from the `IL` channel if present; otherwise I_on = I_off − V_F·(t_on − t_v90)/L with V_F = freewheel diode drop (config, default 1.5 V), flag `I_ON_FROM_I_OFF` (info). *Reason:* the earlier "extrapolate the t3 ramp back" method is biased because the ramp starts only when the DUT voltage collapses. The recovery results are very sensitive to I_on: a 0.06 % error in I_on shifted Q_rr by 1.7 % on the SiC test case.
- **ANA-12 [P1] Diode signals.** i_D(t) = I_L(t) − i_c(t) during the turn-on window, with I_L(t) = I_on + (1/L)·∫ v_D dt (the load inductor is across the complementary device, so its current is tracked through the recovery tail) or the `IL` channel. v_D(t) = v_ce,c(t) if the complementary channel is present, else v_D(t) ≈ V_DC,on − v_ce(t) (flag `VD_DERIVED`; error includes L_σ·di/dt).
- **ANA-13 DC-link reference.** V_DC,off = median of v_ce over the last 50 % of t2 (DUT off, diode conducting) — represents link voltage after droop at turn-off. V_DC,on = same window (immediately before second turn-on). If a `VDC` channel exists, its median over the same window is used instead and v_ce-based values are stored for comparison.

### 7.3 Threshold crossings and integration

- **ANA-14** Crossing time = first crossing in the specified direction after the window start, by linear interpolation between samples, with a debounce: the signal must stay beyond the threshold for N_db samples (default 3) after the crossing. For integration **end** points (2 % / 10 % levels near the tail of a ringing waveform) the search direction is configurable: first crossing from the left (default) or last crossing from the right within the window; the choice is part of the preset. Search windows [P1]: turn-off [min(commanded AFG edge, gate edge) − 0.3 µs, gate edge + min(t2, 3 µs)], turn-on likewise with t3; configurable. *Reason:* with an IGBT Miller plateau of ≈ 0.5 µs the 90 % V_GE crossing lies far before the 50 % gate edge. **Hysteresis [P1]:** every crossing search uses a Schmitt trigger: a crossing is only accepted if, since the window start or the previous accepted crossing, the signal stayed on the other side of the level by ≥ k·σ (k = 4, σ = channel noise rms from the baseline window) for N_db consecutive samples. *Reason:* the turn-off window opens while the IGBT current is still ramping up through 90 % of I_off; with 1 A rms noise this produced false "falling" crossings 1 µs early.
- **ANA-15 [CHG] Threshold presets.** Presets are YAML files in `config/presets/`; each parameter is defined as *start (signal, level, edge direction) → end (signal, level, edge direction)*. The preset is **selected automatically from the DUT type** (CFG-04): IGBT → `IEC60747-9`, MOSFET (Si or SiC) → `IEC60747-8`. The user may override (e.g. `LEGACY` to compare with data from the earlier tool, or a vendor preset to compare with a datasheet). The preset name, version and `verified` flag are stored with every result and printed in reports. Shipped presets carry `verified: false` until the definitions have been checked against the standard text (open point O8); reports then show "preset not verified".

| Parameter | `IEC60747-9` (IGBT) | `IEC60747-8` (MOSFET) | `LEGACY` (draft table) |
|---|---|---|---|
| Gate % reference | % of V_GE,on, referenced to 0 V | % of V_GS,on, referenced to 0 V | % of swing V_off→V_on |
| t_d(on) | V_GE 10 % ↑ → I_C 10 % ↑ | V_GS 10 % ↑ → V_DS 90 % ↓ | V_GE 10 % ↑ → I_C 10 % ↑ |
| t_r | I_C 10 % → 90 % | V_DS 90 % → 10 % | I_C 10 % → 90 % |
| t_d(off) | V_GE 90 % ↓ → I_C 90 % ↓ | V_GS 90 % ↓ → V_DS 10 % ↑ | V_GE 90 % ↓ → I_C 90 % ↓ |
| t_f | I_C 90 % → 10 % | V_DS 10 % → 90 % | I_C 90 % → 10 % |
| E_on window | V_GE 10 % ↑ → V_CE 2 % of V_DC ↓ | I_D 10 % ↑ → V_DS 10 % of V_DC ↓ | I_C 10 % ↑ → V_CE 2 % ↓ |
| E_off window | V_GE 90 % ↓ → I_C 2 % ↓ | V_DS 10 % ↑ → I_D 10 % ↓ (*) | V_CE 10 % ↑ → I_C 2 % ↓ |
| Tail time t_z (stored, not in template) | I_C 10 % → 2 % | — | — |
| t_rr, Q_rr, I_rrm, E_rec, S | §7.4 (ANA-18), all presets | §7.4 | §7.4 |
| dv/dt, di/dt | not defined by the standard — default: average slope between 10 % and 90 % of the transition (alt. 20/80 % or maximum derivative); **signed** (falling = negative) to match the template | same | same |

Source note for the implementer: the IEC columns follow the parameter definitions in Keysight's PD1500A documentation, which cites IEC 60747-9 cl. 6.3.11/6.3.12 and IEC 60747-8 cl. 4.3.5/3.3.20–21. That is a secondary source; (*) the MOSFET E_off window is inferred by symmetry because the secondary source repeats the E_on text there. Everest verifies both columns against the standards before setting `verified: true`.

Consequences worth knowing (no action for the implementer): the IGBT IEC E_off window starts at the gate edge, so it includes I·V_CE(sat)·t_d(off) — at 50 A, t_d(off) ≈ 1.6 µs that is ≈ 0.1 mJ, a few % of E_off. The MOSFET preset reports *voltage* rise/fall times in the template columns "Rise time"/"Fall time"; MOSFET datasheets that use current-based times will not match unless a vendor preset is selected.

- **ANA-16** Energies: trapezoidal integration of p(t) = v(t)·i(t) between the interpolated window limits (partial first/last interval included). Units J (template may require mJ — handled by template_map).
- **ANA-17 [CHG]** Peaks: V_CE,peak (turn-off window max) — this absolute value is what the template column "Voltage overshoot" contains (sample: 431.98 V at V_DC ≈ 406 V); the difference ΔV = V_CE,peak − V_DC,off is also computed and stored (HDF5, report, stop rule SEQ-09). I_C,peak (turn-on window max) = I_on + I_rrm; V_GE min/max per window.
- **ANA-27 [NEW] Complementary-device monitors** (template columns 36–41), evaluated in the DUT turn-on window ("on") and turn-off window ("off"): max v_ce,c (on); max/min v_ge,c (on and off); max complementary current (off) from an optional `IC_COMP` channel (NaN if absent — definition to be confirmed, §8.4 Q4). Validation: max v_ge,c > configured V_th,min of the complementary device → WARNING `COMP_GATE_NEAR_VTH` (parasitic turn-on risk); min v_ge,c < configured V_GE,min rating → WARNING `COMP_GATE_UNDERSHOOT` (gate-oxide stress, relevant for SiC).

### 7.4 Reverse recovery and stray inductance

- **ANA-18 Reverse recovery** (from i_D of ANA-12, turn-on window):
  - t0 = zero crossing of i_D (falling).
  - I_rrm = |min(i_D)| at t_peak, refined [P1] by a least-squares parabola through the contiguous samples within the top 5 % of the peak (≥ ±2 samples): sub-sample peak without following the most negative noise sample.
  - Tail line through the points where i_D = −0.9·I_rrm and −0.25·I_rrm *after* t_peak; t_end = its zero crossing. t_rr = t_end − t0.
  - t_a = t_peak − t0, t_b = t_end − t_peak, S = t_b / t_a.
  - Q_rr = ∫ |i_D| dt over [t0, t_end] (negative part only). Option: integrate to the point where i_D returns to −2 % I_rrm (recorded).
  - E_rec = ∫ v_D·(−i_D) dt over [t0, t_end] (positive number = loss in the diode); window option as for Q_rr.
- **ANA-19 L_σ.** During the turn-on current rise, window = I_C 20 %→80 % of I_on: di/dt = slope of a linear fit of i_c; ΔV = V_DC,on − mean(v_ce); L_σ,on = ΔV / (di/dt). Also L_σ,off = ΔV_peak / |di/dt|_off (turn-off, 90→10 %) reported for comparison (labelled as indicative — includes device-related effects). Note: these are the loop inductance seen *outside* the v_ce probe points.

### 7.5 De-skew

- **ANA-20 None.** Raw time bases.
- **ANA-21 Single.** A fixed skew τ_s per current channel (from CFG-02 or a de-skew-fixture measurement) applied in ANA-08 step 4. Positive τ_s means the current channel lags and is shifted earlier.
- **ANA-22 [P1] Statistic (per shot).** Fit window: from (I_C 2 % crossing − 0.2·rise span − τ_max) to min(I_C 98 % + 0.2·span, recovery current peak − τ_max), both shifted by the search centre; the recovery peak is searched within 3 rise spans only (later in t3 the conduction ramp can exceed a small SiC recovery peak). The search is centred on the single de-skew value (τ_single ± τ_max) so that large fixed probe delays (Rogowski: tens of ns) stay in range. The same Savitzky–Golay smoothing is applied to v_CE as to the current derivative (consistent bandwidth). Details:
  - Model: v_ce(t) = V_DC,on − L_σ·di_c/dt(t + τ) (valid while the diode still blocks the DUT voltage, i.e. before v_ce collapse).
  - di_c/dt computed with a Savitzky–Golay derivative (window ≈ 5 ns, configurable).
  - For τ in [−τ_max, +τ_max] (default ±20 ns, step 0.1 ns via interpolation): least-squares fit of (V_DC,on, L_σ) and residual r(τ). τ* = argmin r.
  - Confidence: dip amplitude/noise RMS ≥ 10, a well-defined minimum (r(τ*±2 ns) ≥ 1.2·r(τ*)), minimum not at the search limit, and [P1] model fit error ≤ 0.06 (rms residual beyond the smoothed noise, relative to the dip amplitude); otherwise flag `DESKEW_ILL_CONDITIONED` and do not use τ* for that shot. *Reason:* without the fit-error test a skew outside the search range returned a confidently wrong value (−17 ns for a true +30 ns).
- **ANA-23 Statistic (per point/series).** τ_stat = median of valid τ* over repetitions (and optionally over all I_test at the same V_DC, configurable). Energies/times for the statistic method use τ_stat. Report τ_stat, its spread (IQR) and the count of valid shots.
- **ANA-24** The three methods are computed on the same raw data; τ values used are stored with the results.

### 7.6 Derived and auxiliary outputs

- **ANA-25** Per shot: I_off, I_on, V_DC,off, V_DC,on, droop (pre-pulse V − V_DC,off), all §7.3/7.4 parameters × 3 de-skew methods, τ values, L_σ, filter info, validity.
- **ANA-26** Per point: mean, std, min, max over valid repetitions.

### 7.7 Validation (reason codes)

- **ANA-30** Flags (INVALID unless noted; `COMP_GATE_NEAR_VTH`, `COMP_GATE_UNDERSHOOT` from ANA-27 are WARNING): `CLIPPED`, `EDGE_MISSING`, `CURRENT_OUT_OF_TOL` (vs SEQ-03 tol), `VDC_OUT_OF_TOL`, `NEGATIVE_ENERGY`, `THRESHOLD_NOT_REACHED`, `TRANSFER_INCOMPLETE`; WARNING: `BW_LIMITED_EDGE` (measured 10–90 % time < 3 × system rise time, system rise time = √(t_r,probe² + t_r,scope²)), `LOW_SAMPLES_ON_EDGE` (< 10 samples between 10–90 %), `I_ON_ESTIMATED`, `VD_DERIVED`, `DESKEW_ILL_CONDITIONED`, `DROOP_HIGH`, `RINGING_NOT_SETTLED` (window end before ringing decays to ±5 %).
- **ANA-31** Plausibility ranges (configurable per DUT type), e.g. E_off/E_on within 0.01–100 × expected from a user-entered datasheet reference value → WARNING.

### 7.8 Verification of the analysis

- **ANA-40** Analysis runs offline from HDF5 with any analysis config (DATA-07).
- **ANA-41 Golden synthetic waveforms** (`analysis/synth.py`): piecewise model with known ground truth — linear current rise/fall with defined t_r/t_f, voltage transitions, L_σ dip, overshoot with damped ringing, diode recovery with defined I_rrm/t_a/t_b, gate waveforms with Miller plateau, known skew, Gaussian noise, 12-bit quantisation. Analytical energies computed in high-resolution reference.
- **ANA-42 [P1] Acceptance tolerances** (each shipped preset — IEC60747-9, IEC60747-8, LEGACY — on the matching synthetic device type). *Ideal* acquisition (no noise, 16-bit, 10 GS/s; isolates algorithm error): energies ±0.5 %; crossing times ±1 sample; recovery times (t_rr, t_a, t_b) ±2 samples or ±0.5 %; I_rrm ±0.5 %; Q_rr ±1 %; softness ±2 %; L_σ ±2 %; statistic skew ±0.2 ns. *Realistic* acquisition (12-bit, noise 0.5 % of the V_CE/I_C swing, 0.1 V on gates, worst of 6 seeds): energies ±2 %, skew ±0.5 ns, other limits per `docs/analysis_accuracy.md`, which documents the definition-inherent noise sensitivity (IGBT tail-current times; SiC recovery quantities when I_rrm ≪ current range). Tests cover IGBT-like (tail current) and SiC-like (fast, ringing) shapes and HS polarity.
- **ANA-43** Cross-check against the scope's own measurement parameters on at least 3 real shots per device type during commissioning; differences documented (expected where window definitions differ).

---

## 8. Data formats

### 8.1 HDF5 layout (one file per run)

```text
/                         attrs: schema_version, software_version, git_hash, run_id, created, operator, station
/config                   attrs/JSON: full snapshot (station, probes, channel map, fixture, DUT, gate drive, limits, matrix, analysis config)
/instruments/<name>       attrs: idn, firmware, resource, settings snapshot (scope settings per SCO-07)
/events                   compound dataset: time, level, source, message
/points/<pid>             attrs: matrix coordinates (V_DC, I_test, Rg_on, Rg_off, V_on, V_off, T_set, side, phase)
/points/<pid>/shots/<sid> attrs: attempt, tuning flag, t1,t2,t3 commanded, quantised; timestamp; T_meas; validity
    /waveforms/<signal>   int16 raw codes (gzip or lzf) + attrs: vertical_gain, vertical_offset, dt, t0, units, channel, probe_id, deskew_applied_by_scope
/points/<pid>/results/v<n>  table of per-shot results + point statistics + analysis config hash
```

- **DATA-08** Raw ADC codes and scaling metadata are stored (lossless); scaled float arrays are computed on read.
- **DATA-09** `schema_version` shall be incremented on any layout change; a reader shall support all previous versions.

### 8.2 Configuration files

`station.yaml` (instruments/transport), `probes.yaml` (library), `fixtures/*.yaml`, `duts/*.yaml`, `setups/*.yaml` (matrix + channel map + analysis config), `limits.yaml` (supervisor). All validated by pydantic models; example files shipped in `config/examples/`.

### 8.3 Result template mapping (`FS820_6IN1_Wphase_HS_400V_50-950A_Ron3_Roff6.6_125oC.xlsx`)

Layout: one sheet; row 1 = 59 headers (A…BG), row 2 = units, data from row 3; normalisation block per DATA-10. Empty values are written as the text "NaN" (as in the template). Internal results are SI; the scale converts to the template unit. Ref. = requirement that defines the value.

| Col | Header | Unit | Content | Scale | Ref. |
|---|---|---|---|---|---|
| A | Measurement Point | — | point index in this workbook (1-based) | — | DATA-12 |
| B | Date Time | — | timestamp of the last valid shot of the point | Excel datetime | |
| C | Temperature | °C | T_meas if available, else T_set | 1 | TMP-03 |
| D | High-side measurement? | (bool) | side = HS | — | |
| E | Set voltage | V | V_DC target | 1 | SEQ-01 |
| F | Measured voltage offset | V | V_DC,pre − V_set (pre-pulse link voltage minus setpoint) — **to confirm, Q1** | 1 | ANA-25 |
| G | Set current | A | I_test | 1 | |
| H, I | Resistance on / off | Ohm | R_g,on / R_g,off as entered | 1 | CFG-05 |
| J, K | Gate voltage on / off | V | V_on / V_off as entered (measured plateaus in HDF5) | 1 | CFG-05 |
| L | Load inductance | uH | L | 1e6 | CFG-03 |
| M, N | Turn-on / Turn-off energy | uJ | E_on / E_off, no de-skew | 1e6 | ANA-15/16 |
| O, P, Q, R | Turn-on delay / Rise time / Turn-off delay / Fall time | ns | t_d(on), t_r, t_d(off), t_f, no de-skew | 1e9 | ANA-15 |
| S | Reverse recovery time | ns | t_rr | 1e9 | ANA-18 |
| T | Reverse recovery charge | pQ (= pC) | Q_rr | 1e12 | ANA-18 |
| U | Maximum reverse current | A | I_rrm (magnitude) | 1 | ANA-18 |
| V | Reverse recovery softness factor | unitless | S = t_b/t_a (≥ 0) — **Q3** | 1 | ANA-18 |
| W | Reverse recovery energy | uJ | E_rec, no de-skew | 1e6 | ANA-18 |
| X, Y | Dv/Dt / Di/Dt Turn-on | V/ns, A/ns | signed slopes at turn-on (dv/dt < 0, di/dt > 0) | 1e-9 | ANA-15 |
| Z, AA | Dv/Dt / Di/Dt Turn-off | V/ns, A/ns | signed slopes at turn-off (dv/dt > 0, di/dt < 0) | 1e-9 | ANA-15 |
| AB, AC, AD | Duration first pulse / Pulse break / Duration second pulse | us | t1, t2, t3 as played by the AFG (quantised) | 1e6 | AFG-02 |
| AE | Measured current (on) | A | I_on = current at the second turn-on — **Q2** | 1 | ANA-11 |
| AF | Measured current (off) | A | I_off = DUT current at turn-off (at t_v90) | 1 | ANA-11 |
| AG, AH | Measured voltage (on) / (off) | V | V_DC,on / V_DC,off | 1 | ANA-13 |
| AI | Voltage overshoot | V | V_CE,peak (absolute) | 1 | ANA-17 |
| AJ | Complementary Maximum Current (off) | A | max complementary current in DUT turn-off window — **Q4** | 1 | ANA-27 |
| AK | Complementary Maximum Voltage (on) | V | max v_ce,c in DUT turn-on window | 1 | ANA-27 |
| AL, AM | Complementary Maximum Gate Voltage (on) / (off) | V | max v_ge,c in turn-on / turn-off window | 1 | ANA-27 |
| AN, AO | Complementary Minimum Gate Voltage (on) / (off) | V | min v_ge,c in turn-on / turn-off window | 1 | ANA-27 |
| AP | Total Stray Inductance | nH | L_σ,on | 1e9 | ANA-19 |
| AQ | Single skew | ns | τ_s used | 1e9 | ANA-21 |
| AR–AX | Turn-on energy … Reverse recovery energy (single de-skew) | uJ / ns | E_on, E_off, t_d(on), t_r, t_d(off), t_f, E_rec with τ_s | as M–R, W | ANA-21 |
| AY | Statistic skew | ns | τ_stat used | 1e9 | ANA-23 |
| AZ–BF | … (statistic de-skew) | uJ / ns | same seven values with τ_stat | as above | ANA-23 |
| BG | sweep bool | — | TRUE for rows produced by a matrix sweep, FALSE for single manual shots — **Q5** | — | |

### 8.4 Findings from the template's sample row (FS820, HS, 400 V, 50 A, 125 °C)

These come from the tool that produced the template, not from AutoDPT; they matter if old data are compared with new data.

| # | Severity | Observation | Interpretation / action |
|---|---|---|---|
| T1 | Major | Measured current (on) = 68.83 A > Measured current (off) = 45.29 A. | At the second turn-on the inductor current is ≤ I_off (it decays during t2). 68.83 A is close to the current at the *end* of the second pulse, I_off + V·t3/L = 45.3 + 400·4/54 ≈ 74.9 A. If the old tool sampled I_on there, the normalised E_on in the summary block (E_on·I_set/I_on) is low by ≈ 45/69 → about −34 %. Check one raw waveform before reusing old normalised E_on/E_rec. AutoDPT uses ANA-11. |
| T2 | Major | I_off = 45.29 A for I_set = 50 A (−9.4 %), t1 = 6.79 µs ≈ L·I/V. | No current correction in the old tool. SEQ-03 brings this to ±2 %, so the normalisation becomes a small correction. |
| T3 | Minor | Softness factor = −0.55. | S = t_b/t_a cannot be negative with the ANA-18 definition — sign/definition issue in the old tool. AutoDPT reports S ≥ 0 (Q3). |
| T4 | Minor | E_rec (single de-skew) = 0 while E_rec = 3022 µJ; all statistic-de-skew cells NaN. | Not computed by the old tool. AutoDPT computes all three methods. |
| T5 | Minor | Q_rr unit "pQ", value 11 703 273 → 11.7 µC. | "pQ" means pC. Recommend changing the template unit to "pC" (or "uC" with scale 1e6); the writer follows whatever the template says. |
| T6 | Minor | Temperature unit cell contains "Â°C". | "°C" saved as UTF-8 and re-read as Latin-1. Fix the template cell; the writer copies it verbatim. |
| T7 | Question | Measured voltage 406–409 V for a 400 V setpoint (+1.5 … +2.2 %). | Either PSU offset or V_CE probe gain. The independent DC-link monitor (Appendix F) will show which; calibrate the probe factor if needed. |
| T8 | Info | "Voltage overshoot" holds the absolute peak (431.98 V), not ΔV. | Kept for compatibility (ANA-17). |

---

## 9. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-PERF-01 | Abort latency: ABORT press → AFG output-OFF command on the wire ≤ 200 ms when no waveform transfer is in progress; ≤ 1 s worst case (transfers chunked so the safe-off path can interleave or the transfer is aborted). Measured in the mock with injected latencies and on the bench. |
| NFR-PERF-02 | Analysis of one shot (8 ch × 1 Mpts) ≤ 2 s on the target laptop including three de-skew methods. |
| NFR-PERF-03 | Transfer budget: the pre-run summary (GUI-07) shall estimate transfer time from record size and a measured link throughput (measured at connect with a test transfer). Recommendation recorded in commissioning: replace the Archer C50 LAN path with a gigabit switch if shot rate is limited by transfer. |
| NFR-REL-01 | No data loss on crash: each shot flushed to HDF5 before the next shot starts. |
| NFR-REL-02 | A 500-shot unattended mock run shall complete without memory growth > 200 MB. |
| NFR-LOG-01 | All operator actions, state transitions, safety-relevant commands (output ON/OFF, setpoints) logged at INFO. |
| NFR-QUAL-01 | `analysis` and `hal` packages: type-checked (mypy strict), unit-test line coverage ≥ 85 %; GUI smoke tests with pytest-qt. |
| NFR-QUAL-02 | Requirement IDs referenced in tests (`@pytest.mark.req("ANA-18")` or docstring) — a script generates a traceability matrix (requirement → tests). |
| NFR-PORT-01 | Runs on Windows 10/11; development and CI also on Linux (simulation mode). No admin rights needed for normal use. |
| NFR-USE-01 | All numeric inputs show units; SI prefixes accepted (e.g. "20u" for 20 µH). |
| NFR-VER-01 | Software version, git hash and analysis config hash stored in every output file and report. |

---

## 10. Verification and acceptance

### 10.1 Software (no hardware)

| ID | Test | Pass criterion |
|---|---|---|
| VER-SW-01 | Golden synthetic waveforms (ANA-41/42) | Within ANA-42 tolerances |
| VER-SW-02 | State machine: every illegal output command per state | Raises, logged, outputs unchanged |
| VER-SW-03 | Fault injection in mocks: comm loss, PSU fault, interlock, scope timeout, AFG readback mismatch | SAF-05 executed, FAULT, correct log sequence |
| VER-SW-04 | Abort timing with mock latencies | NFR-PERF-01 |
| VER-SW-05 | Workbook/CSV vs template | DATA-06 (a)–(c) pass with the supplied template |
| VER-SW-06 | Re-analysis from HDF5 | Identical results for identical config; new version group for changed config |
| VER-SW-07 | Pulse planner | LC formula, asin-domain rejection, step limiting, convergence on mock with 10 % L error in ≤ 4 shots |

### 10.2 Hardware commissioning (docs/commissioning.md)

| ID | Test (HV OFF unless stated) | Pass criterion |
|---|---|---|
| VER-HW-01 | AFG idle level and enable/disable glitch (AFG-07) | Idle = LOW, no glitch above gate-driver input threshold |
| VER-HW-02 | Pulse timing accuracy | Measured t1/t2/t3 within ±2·Δt + 10 ns |
| VER-HW-03 | AFG impedance/level check with real gate-driver input | Measured HIGH within ±5 % of programmed |
| VER-HW-04 | Comm loss: unplug PSU / AFG / scope cable during HV_READY at 50 V | Remaining instruments safed, FAULT, log complete |
| VER-HW-05 | PSU interlock open during HV_READY at 50 V | PSU trips in hardware; software shows interlock, FAULT |
| VER-HW-06 | Discharge plausibility (SAF-09) incl. simulated open bleeder string (disconnect one string, test at ≤ 100 V) | Warning raised |
| VER-HW-10 | Bleeder and discharge relay (Appendix E): measure τ with bleeder only and with relay closed at ≤ 100 V; remove relay coil supply while charged | τ within ±15 % of calculation; relay closes on loss of coil power; PSU interlock opens |
| VER-HW-11 | DC-link monitor (Appendix F): compare with a reference meter at 3 voltages; disconnect the monitor lead while charged | Within accuracy after gain calibration; software FAULT `MONITOR_DISAGREE`, indication stays "HV PRESENT" |
| VER-HW-07 | First DPT at low V_DC (e.g. 50 V, low current) | Valid shot; analysis vs scope parameters documented |
| VER-HW-08 | De-skew: fixture-based single skew vs statistic method | Difference ≤ 1 ns or explained |
| VER-HW-09 | Stepwise V/I increase to the max planned point with stop rules active | No unintended stop; overshoot stop triggers when threshold lowered artificially |

---

## 11. Constraints and assumptions

- The gate-driver interface (logic input, isolation, DESAT, hardware max-on-time limiter, enable input) exists and is qualified by HW; the AFG never drives a power gate directly.
- The fixture has a fail-safe discharge (bleeder and/or normally-closed discharge relay) and an independent HV indicator.
- R_g and gate voltages are changed manually.
- Probe ratings, de-skew fixture and probe calibration are owned by the lab.

---

## 12. Implementation phases (order for Claude Code)

| Phase | Deliverable | Exit criterion |
|---|---|---|
| P0 ✔ | Repo skeleton, config models (pydantic), event log, CLI `--sim`, CI (ruff, mypy, pytest) | CI green — **done 2026-10-22** |
| P1 ✔ | `analysis` package + `synth.py` + golden tests | VER-SW-01 passes — **done 2026-10-22** |
| P2a ✔ | Transport layer (PyVISA / mock), command sets, instrument simulators, bench check (NET-09) | simulated + loopback-socket tests pass — **done 2026-10-22**; on-bench run by Everest pending |
| P2 | HAL interfaces + mocks; real drivers for PSU, AFG, scope with CLI smoke scripts (`python -m autodpt.hal.<x> --ip … --identify`) | Mock tests pass; on bench: identify + safe read-only queries |
| P3 | State machine, safety layer, pulse planner, sequencer (headless, CLI-driven) | VER-SW-02/03/04/07 pass |
| P4 | Data layer: HDF5, template workbook + CSV (§8.3, DATA-10/11/12) | VER-SW-05/06 pass |
| P5 | GUI (all screens) | GUI smoke tests; full mock run from GUI |
| P6 | Reports (PDF/Excel) | Report generated from a mock run |
| P7 | Bench commissioning support, `docs/commissioning.md` | VER-HW-01…09 executed by HW engineer |

Each phase ends with a short `CHANGELOG.md` entry and an updated traceability matrix.

---

## 13. Open points / decisions

| # | Item | Owner | Default if not decided |
|---|---|---|---|
| O1 | ~~Result template~~ — supplied (.xlsx); questions Q1–Q7 below | Everest | Defaults in §8.3 |
| O2 | Scope transport: VXI-11 (default) vs VICP | Everest | VXI-11 |
| O3 | Magna-Power +LXI socket port and exact SCPI fault/interlock register bits (from SL manual) | Implementer, verify | Configurable port; register map in config |
| O4 | Interlock readback option (Appendix A) | Everest + lab safety | Option A now, DIO interface reserved |
| O5 | Temperature hardware (next generation) | Everest | Manual entry |
| O6 | DC-link monitor hardware (Appendix F) | Everest | LAN/USB DMM; PSU readback only as fallback with warning |
| O7 | Network: keep Archer C50 or add gigabit switch | Everest | Keep; measure throughput (NFR-PERF-03) |
| O8 | Verify `IEC60747-9` and `IEC60747-8` preset definitions against the standards (§7.3) and set `verified: true`; add vendor presets if needed for datasheet comparison | Everest | Presets as in ANA-15, reported as "not verified" |
| O9 | DC-link capacitance C, max V_DC of the fixture, enclosure discharge-time rule of the lab (Appendix E) | Everest + lab safety | Appendix E table, 1 mF / 1000 V row |
| Q1 | Template "Measured voltage offset": definition? | Everest | V_DC,pre − V_set |
| Q2 | Template "Measured current (on)": confirm = current at second turn-on (see T1) | Everest | ANA-11 |
| Q3 | Softness sign: confirm S = t_b/t_a ≥ 0 (see T3) | Everest | S ≥ 0 |
| Q4 | "Complementary Maximum Current (off)": which signal, sign convention, needs `IC_COMP` channel? | Everest | `IC_COMP` channel, positive into collector/drain; NaN if absent |
| Q5 | "sweep bool": meaning | Everest | TRUE for matrix-sweep rows |
| Q6 | Workbook rows per point (mean) or per shot | Everest | per point |
| Q7 | Normalisation block energies: no de-skew (as template) or single/statistic | Everest | no de-skew |

---

## Appendix A — Interlock / E-stop readback options (for decision O4)

**Principle:** whatever is chosen, the interlock *acts* in hardware. The software only *reads* it to (a) refuse to start, (b) safe-off faster and cleaner, (c) log what happened. Losing the software reading must never weaken protection.

| Option | What it is | Software sees | Extra HW | Pros | Cons |
|---|---|---|---|---|---|
| A. PSU interlock | E-stop + enclosure door contacts in series into the Magna-Power 5 V interlock input | PSU interlock fault via SCPI status (polled, ~250 ms) | None (wiring) | Free; PSU output inhibited in hardware and latched; clear requires +5 V restored and a clear command | Only inhibits the PSU; does **not** discharge the link or block gate pulses; software reading is slow and indirect |
| B. DIO module | USB or Ethernet digital I/O module reading status contacts | E-stop, door, HV-present comparator, gate-driver fault/DESAT, discharge relay state (DIO-02) | DIO module + auxiliary contacts | Full visibility, gate-driver fault visible to the sequencer (SEQ-09), better logs | Cost, wiring, one more device to manage |
| C. Safety relay/PLC | Dedicated safety relay or safety PLC handles E-stop/door, drives discharge relay and gate-enable; exposes status contacts | Status via option B | Safety relay/PLC | Proper safety function design (risk assessment, performance level per lab rules) | Most effort; requires safety engineering sign-off |

**Recommended path:** implement A now (wiring only); design the hardware chain so that E-stop/door also (i) opens the gate-driver enable and (ii) closes a normally-closed discharge relay (designed in Appendix E) — that is hardware work independent of software. Add B when the next-generation bench (with temperature control) is built; the software interface (DIO-01…04) is already reserved, so it becomes a configuration change plus one driver. Typical device classes for B: USB DIO (e.g. LabJack- or NI-USB-class devices) or Modbus/TCP Ethernet DIO modules — evaluate isolation, input voltage and Python support; not evaluated here.

---

## Appendix B — Worked sizing example (used by tests VER-SW-07 and GUI-07)

Assumptions: V_DC = 450 V, I_test = 600 A, L = 20 µH, C = 1 mF, t3 = 3 µs, PSU limit 1.5 A, R_bleed = 44 kΩ (Appendix E), R_dis = 470 Ω.

| Quantity | Formula | Value |
|---|---|---|
| t1 ideal | L·I/V | 26.7 µs |
| t1 with droop | √(LC)·asin(I·√(L/C)/V) | 26.8 µs |
| Current at end of t3 | I + V·t3/L | 667.5 A |
| Charge per shot | ½·I·t1 + I_avg·t3 | ≈ 9.9 mC |
| DC-link droop | Q/C | ≈ 9.9 V (2.2 %) → WARNING at default 2 % |
| Inductor energy | ½·L·I² | 3.6 J |
| DC-link stored energy | ½·C·V² | 101 J |
| Recharge at 1.5 A | Q/I_lim | ≈ 6.6 ms |
| Bleed-down 450 → 50 V, bleeder only | R_bleed·C·ln(450/50) | ≈ 97 s |
| Bleed-down 450 → 50 V, discharge relay closed | (R_bleed‖R_dis)·C·ln(9) | ≈ 1.0 s |
| Record (5 µs pre + t1 + t2 + t3 + 10 µs) at 5 GS/s | — | ≈ 238 kpts/ch; 6 ch ≈ 2.9 MB |
| Low-V worst case 50 V, 600 A, 20 µH | t1 = 240 µs | 8 ch ≈ 21 MB/shot → ≈ 2–3 s over 100 Mbit/s |

---

## Appendix C — Remote-command starting points (UNVERIFIED — check against manuals)

These are commonly used command families for these instrument classes. The implementer shall verify each against the current programmer manual and firmware, mark verified commands in code, and add a driver self-test that issues only read-only queries.

| Instrument | Function | Candidate command family |
|---|---|---|
| LeCroy (MAUI) | Headers off | `COMM_HEADER OFF` |
| | Binary waveform format | `COMM_FORMAT DEF9,WORD,BIN` |
| | Trigger mode single / arm | `TRIG_MODE SINGLE`, `ARM` (or `TRMD`), then `WAIT` / `INR?` polling |
| | Waveform with descriptor | `C<n>:WAVEFORM? ALL` (parse WAVEDESC) |
| | Automation-level settings | `VBS 'app.Acquisition.C1.VerScale = …'` (MAUI automation object model) |
| Tek AFG31000 | Output | `OUTPut1:STATe ON|OFF`, `OUTPut1:IMPedance INFinity|50` |
| | Arbitrary data | `TRACe:DATA EMEMory,<block>`, `SOURce1:FUNCtion EMEMory` (or USER) |
| | Burst / trigger | `SOURce1:BURSt:STATe ON`, `SOURce1:BURSt:NCYCles 1`, `TRIGger:SEQuence:SOURce …`, `*TRG` |
| | Levels | `SOURce1:VOLTage:LEVel:IMMediate:HIGH|LOW` |
| Magna-Power SL | Setpoints/protection | `VOLT`, `CURR`, `VOLT:PROT`, `CURR:PROT` |
| | Output | `OUTP:START` / `OUTP:STOP` (verify) |
| | Measure | `MEAS:VOLT?`, `MEAS:CURR?` |
| | Status / faults / interlock | `STAT:QUES:COND?` or the SL-specific status query; clear command per manual |
| DMM (Appendix F) | DC voltage | `CONF:VOLT:DC 1000`, `MEAS:VOLT:DC?` / `READ?` (model-specific) |
| All | Errors / identity | `SYST:ERR?` (LeCroy: `CMR?`/`EXR?`/`CHL` equivalents — verify), `*IDN?`, `*OPC?` |

---

## Appendix D — Suggested first prompt to Claude Code

> Read docs/SRS.md completely. Implement Phase P0 and P1 only (§12): repository skeleton per §4.2, pydantic config models (§6.2, §8.2), event log, the `analysis` package per §7 including `synth.py` golden-waveform generator, and pytest tests implementing VER-SW-01 with the tolerances of ANA-42. No instrument code yet. Reference requirement IDs in docstrings and tests. Stop and list any requirement you find ambiguous before guessing.

---

## Appendix E — DC-link bleeder and active discharge (hardware guideline)

Context: the Magna-Power supply is a source only, so every downward voltage step and every shutdown relies on the fixture to remove the stored energy ½·C·V² (1 mF at 1000 V = 500 J). This appendix is a hardware design guideline; the software side is SAF-05, SAF-09, SAF-13, PSU-08 and CFG-03. Final values need C and the fixture's max V_DC (open point O9).

### E.1 Targets

| Function | Target | Basis |
|---|---|---|
| Active discharge (normal case: E-stop, door open, end of test) | ≤ 60 V within 5 s | Common machinery practice (my understanding of IEC 60204-1 cl. 6.2.4; lab equipment under IEC 61010-1 has its own provision) — confirm which rule your lab applies |
| Passive bleeder (fail-safe backup: relay failed, no control power) | ≤ 60 V within ≈ 2 min; warning label "Wait 3 min, verify with meter" | Engineering choice: keeps continuous dissipation moderate |
| Hardware HV indication | Independent of software | §2.4 |

Why not passive only: 5 s to 60 V from 1000 V at 1 mF needs R ≈ 1.8 kΩ → 563 W continuous and 0.56 A (37 % of the 1.5 A PSU) whenever HV is on. Hence the two-tier design.

### E.2 Topology

```text
 PSU+ ──[R_series and/or diode]──┬──────────┬──────────┬──────────────┬───── DC+ (to half-bridge)
                                 │          │          │              │
                               C_bank    R_b string  R_b string    R_dis
                                 │          A          B              │
                                 │          │          │         K_dis (NC contact)
 PSU− ───────────────────────────┴──────────┴──────────┴──────────────┴───── DC−
                                 ▲ Kelvin taps for the DC-link monitor (Appendix F) — separate from the bleeder

 K_dis coil (24 V): E-stop (NC) ─ door switch ─ [DISCHARGE_REQ, optional, software] ─ coil
   coil energised  = contact OPEN  = discharge disabled (charging allowed)
   coil unpowered  = contact CLOSED = discharge (fail-safe: loss of 24 V, E-stop, door open)
 K_dis auxiliary contact → in series with the Magna-Power interlock input → PSU inhibited whenever K_dis is closed
 Same chain → gate-driver enable (no gate pulses with the door open)
```

Rules:
1. Bleeder strings and R_dis mount **directly at the capacitor terminals** — no connector between C and its discharge path.
2. Two parallel bleeder strings: one open string doubles τ instead of removing the discharge path; SAF-09 detects it (threshold 1.5·τ_exp).
3. Re-energising K_dis during a discharge would make it break DC current (up to V/R_dis ≈ 2.1 A at 1000 V). Either use contacts with a DC break rating at that voltage/current, or gate the coil re-enable with the hardware comparator `DCLINK_SAFE_HW` (Appendix F) so the relay can only open below 60 V.
4. The PSU must be off when K_dis closes. The aux contact opening the PSU interlock does this in hardware; in the software path SAF-13 turns the PSU off first. In the short overlap the PSU is current-limited (1.5 A) into R_dis, so make sure R_dis survives ≈ 1.5 A × 700 V ≈ 1 kW for the PSU's inhibit time.

### E.3 Sizing (V_max = 1000 V — the PSU maximum, so the design holds whatever is set; V_safe = 60 V)

Formulas: τ = R·C, t(V_max → V_safe) = τ·ln(V_max/V_safe) = 2.81·τ, P = V_max²/R, E = ½·C·V_max².

| C | Bleeder R_b (2 strings × 4 resistors) | τ_b | t → 60 V | t → 60 V, one string open | P_bleed at 1000 V | Per resistor (250 V) | R_dis | t → 60 V with K_dis closed | I_peak | E per discharge |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.5 mF | 2 × (4 × 47 kΩ) = 94 kΩ | 47 s | 132 s | 264 s | 10.6 W | 1.3 W | 470 Ω | 0.7 s | 2.1 A | 250 J |
| 1 mF | 2 × (4 × 22 kΩ) = 44 kΩ | 44 s | 124 s | 248 s | 22.7 W | 2.8 W | 470 Ω | 1.3 s | 2.1 A | 500 J |
| 2 mF | 2 × (4 × 10 kΩ) = 20 kΩ | 40 s | 113 s | 225 s | 50 W | 6.3 W | 470 Ω | 2.6 s | 2.1 A | 1000 J |
| 3 mF | 2 × (4 × 6.8 kΩ) = 13.6 kΩ | 41 s | 115 s | 230 s | 74 W | 9.2 W | 470 Ω | 4.0 s | 2.1 A | 1500 J |

R_dis = 470 Ω meets the 5 s target up to C ≈ 3.5 mF. Above that, lower R_dis (higher peak current) or accept a longer time with your lab's approval.

### E.4 Component requirements

| Part | Requirement |
|---|---|
| Bleeder resistors | Element (limiting) voltage ≥ 2 × its share (≥ 500 V for 4 in series at 1000 V); power rating ≥ 3 × dissipation (e.g. 2.8 W → ≥ 10 W chassis-mount or heatsinked); wirewound or thick-film power type; creepage/clearance for the working voltage; mounted with airflow — 23–74 W of continuous heat goes into the enclosure. |
| R_dis | Single-pulse energy rating ≥ 2 × ½CV² (adiabatic, high-energy wirewound or bulk ceramic type); voltage rating ≥ 1000 V (or 2 in series); peak power ≈ 2.1 kW for < 1 s; repetitive rating ≥ E / shortest discharge interval (500 J every 60 s ≈ 8 W). |
| K_dis | Normally-closed contact, ≥ 1000 V DC rated (≥ 1.2 kV preferred), make ≥ I_peak; break rating per E.2 rule 3; HV reed/vacuum relay or DC contactor with NC contact; 24 V coil; auxiliary contact for status (`DISCHARGE_RELAY_CLOSED`) and for the PSU interlock. |
| Labels | "DC-link may be charged — wait 3 min and verify with meter" on the enclosure. |

### E.5 Effect on the test sequence

- Continuous bleeder current at 1000 V is 10–74 mA — up to 5 % of the PSU's 1.5 A, so charging and recharge are hardly affected.
- Downward V_DC step 800 → 400 V at 1 mF: bleeder only ≈ 30 s; with K_dis via `DISCHARGE_REQ` ≈ 0.3 s. Without a DIO module the sequencer orders V_DC ascending (SEQ-08) and waits (PSU-08).
- End of run without `DISCHARGE_REQ`: the link bleeds passively (≈ 2 min) unless the operator opens the door / presses E-stop, which closes K_dis.

---

## Appendix F — DC-link voltage monitor setup (guideline)

Purpose: give the software (SAF-04, SAF-09, SAF-12) a DC-link reading that is independent of the PSU and of the bleeder, and give the operator a hardware indication that does not depend on the software.

### F.1 Options

| Option | Implementation | Continuous monitoring? | Pros | Cons |
|---|---|---|---|---|
| **F-A (recommended)** | Bench DMM with LAN or USB, 1000 V DC range, wired to the capacitor terminals | Yes (polled ≥ 2 Hz) | Simple, accurate (≈ 0.1 %), independent of scope and PSU, standard SCPI | Needs a network port — the Archer C50's 4 LAN ports are taken (laptop + 3 instruments): add a switch or use USB |
| F-B | Resistive divider + isolation amplifier → DAQ analog input | Yes | Can also feed the hardware comparator and lamp | Custom hardware; isolation design needed if DC− is not earthed |
| F-C | Scope channel `VDC` with HV differential probe | **No** — the scope is armed or stopped between shots | Good per-shot V_DC for analysis (ANA-13) | Not usable for SAF-04 monitoring; keep as an additional analysis channel |

### F.2 Wiring rules

1. **Tap point:** Kelvin connection on the capacitor-bank terminals (or the DC busbar at the DUT), downstream of the PSU series diode/resistor — never at the PSU output terminals.
2. **Separate from the bleeder:** do not use the bleeder string as a divider. If it opens, a divider tap reads 0 V on a charged link — the dangerous failure direction.
3. **Protective series resistors** at the tap (e.g. 2 × 47 kΩ HV-rated, one per lead) limit current if a lead is damaged. Into a 10 MΩ DMM input they cause a ≈ 0.9 % low reading — enter the gain correction in CFG-03 and verify at commissioning (VER-HW-11).
4. **Wire:** insulation rated ≥ 1.5–2 × V_max, twisted pair, routed away from the power loop; guarded terminals; HV labels.
5. **Earthing:** decide and document where DC− is earthed. Passive low-side probes on the scope already tie the DUT emitter/source (often DC−) to protective earth. DMM inputs float, so F-A works either way; F-B needs isolation if DC− is not earthed.
6. **DMM settings:** fixed 1000 V DC range (no autorange), fast integration (≈ 1 PLC), remote mode locked during a run.

### F.3 Hardware indication (authoritative, no software)

- "HV PRESENT" lamp lit above ≈ 50 V, powered from the link itself (self-powered indicator), and a separate green "SAFE" lamp lit only when a comparator sees < 50 V **and** its own supply is healthy. A dark HV lamp alone is not proof (lamp failure); a lit SAFE lamp is positive confirmation.
- The comparator output `DCLINK_SAFE_HW` can gate the re-enable of K_dis (Appendix E rule 3) and later feed the DIO module (DIO-02).
- Before touching the fixture: live–dead–live test with a handheld meter (check meter on a known source, measure the link, re-check the meter).

### F.4 Software side (cross-reference)

- HAL interface `DcLinkMonitor` with implementations `ScpiDmm`, `ScopeChannel`, `PsuReadback` (fallback, warning "not independent"), `Mock`.
- Plausibility while charging and "monitor verified" logic: SAF-12. Decay τ check: SAF-09. Polling ≥ 2 Hz, comm loss → FAULT (SAF-12c).
