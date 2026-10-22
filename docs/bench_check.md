# Bench check — connection and controllability (SRS NET-09)

Run this first on a new laptop, after any network change, after instrument firmware updates, and
before a measurement campaign.

## 1. One-time setup

**Laptop**

```bat
py -3.11 -m venv .venv && .venv\Scripts\activate
pip install -e ".[hw]"            :: pyvisa + pyvisa-py (VXI-11 and raw socket, no NI driver needed)
```

Only if the scope must use VICP instead of LXI: install NI-VISA and the Teledyne LeCroy *VICP Passport*,
set `visa_library: ""` and the scope resource to `VICP::<ip>` in the station file.

**Network** (all instruments + laptop on the Archer C50 LAN ports)

| Item | Setting |
|---|---|
| Addresses | Static IPs or DHCP reservations in one /24, e.g. scope .10, AFG .11, PSU .12, DMM .13, laptop .100 |
| Isolation (NET-07) | Wi-Fi off on the router, WAN port unplugged (or the laptop's other NIC carries the office network) |
| Ports | 4 LAN ports = laptop + 3 instruments; a DC-link DMM needs an extra switch (or USB) |
| Laptop firewall | allow outbound TCP 111 (VXI-11 portmapper) + dynamic RPC ports, 50505 (Magna socket), 1861 only for VICP |

**Instruments**

| Instrument | Remote setting | Resource string |
|---|---|---|
| WaveRunner 8108HD | Utilities > Utilities Setup > Remote > **LXI (VXI11)** | `TCPIP0::<ip>::inst0::INSTR` |
| AFG31022 | LAN enabled, IP set (AFG31000 user manual, I/O settings) | `TCPIP0::<ip>::inst0::INSTR` |
| Magna-Power SL +LXI | LAN IP set; socket port = factory default **50505** (`SYST:COMM:NET:PORT?`) | `TCPIP0::<ip>::50505::SOCKET` |

Enter the addresses in `config/examples/station.yaml` (copy it to your own station file).

## 2. Run

```bat
python -m autodpt discover                                              :: optional: list VISA resources
python -m autodpt bench-check my_station.yaml                           :: level "connect": read-only
python -m autodpt bench-check my_station.yaml --level control           :: + controllability (asks YES)
python -m autodpt bench-check my_station.yaml --level control --acquire :: + scope single-shot acquisition
python -m autodpt bench-check my_station.yaml --sim --level control     :: try it without hardware
```

Each run writes `reports/bench_check_<time>.json` and `.md` and logs every command and response to the
event log (`--log-dir`).

## 3. What is checked

| Level | Instrument | Check | Pass criterion |
|---|---|---|---|
| connect | laptop | interface / subnet | laptop and all instruments in one /24 |
| connect | laptop | network isolation | instrument LAN is not the default route (else WARN, NET-07) |
| connect | each | TCP reachability | port open (111 VXI-11, 50505 socket, 1861 VICP) |
| connect | each | VISA open, `*IDN?` | vendor/model match the command set (NET-03) |
| connect | each | error queue, latency | queue empty; median round trip < 100 ms |
| connect | AFG, PSU | output state | reported only (read-only level) |
| control | AFG | outputs OFF | CH1/CH2 forced and read back OFF (SAF-08) |
| control | AFG | load, levels | load = station `afg_load`; HIGH 5 V / LOW 0 V read back (AFG-03) |
| control | AFG | ARB + burst | FUNC EMEM, burst ×1 read back; 1000-point test pulse uploaded, point count read back |
| control | PSU | output OFF, status | output forced OFF; STAT:QUES bits decoded (interlock = WARN) |
| control | PSU | setpoints | VOLT 10 V / CURR 0.1 A written, read back, originals restored; output never enabled |
| control | PSU | measure | MEAS:VOLT; > 50 V with output OFF → WARN "DC link charged?" |
| control | scope | format, timebase | CHDR/CFMT/CORD accepted; TDIV changed, read back, restored |
| control | scope | transfer | full waveform read; MB/s and an 8 ch × 1 Mpts estimate (< 8 MB/s → WARN: 100 Mbit link) |
| control | scope | descriptor self-check | binary WAVEDESC gain/interval = scope's `INSPECT?` text (proves the parser) |
| control | DMM | DC link | reading agrees with PSU read-back (SAF-12 prerequisite) |

**Never sent:** PSU `OUTP:START`, AFG output ON, AFG trigger. A transport-level guard rejects them even
if code tried (`forbidden` list in each command set).

## 4. Reading the result

* **PASS / INFO** — fine. INFO lists commands that are not yet confirmed in the manual; a PASS on the
  control step that uses them confirms them for your firmware — then set `verified: true` in
  `autodpt/hal/commandsets/*.yaml`.
* **WARN** — works, but look at it (outputs were ON, interlock open, slow link, isolation).
* **FAIL** — fix before using the bench.

| Symptom | Likely cause |
|---|---|
| TCP: timeout | wrong IP, cable, instrument off, laptop in another subnet |
| TCP: connection refused | host is up but the remote interface/port is disabled (scope not in LXI mode; PSU port changed) |
| VISA open fails, TCP ok | wrong resource string type (INSTR vs SOCKET), VICP without NI-VISA + Passport |
| identify FAIL | another device at that IP, or the model differs from the command set |
| control step FAIL with "instrument reported: …" | the instrument rejected the command — check the syntax in the manual and fix the command set |
| WAVEDESC self-check FAIL | descriptor layout differs on this firmware — do not trust waveform scaling until fixed |
