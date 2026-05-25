# Raypak Crosswind Local Control — Project Context

## Goal

Programmatically connect to a **Raypak Crosswind** pool heat pump over Wi-Fi to:

1. **Poll telemetry** (water temperature, ambient temp, compressor state, faults, etc.) and store it in **InfluxDB** for **Grafana** dashboards.
2. **Control the device** locally — power on/off, change setpoint, change mode (warm/cool/smart), toggle silent mode.
3. Eventually expose control as a capability to **OpenClaw** (likely via a thin FastAPI endpoint or a skill that wraps the Python module).

## Architecture decision

- The heater is a **Tuya / Smart Life** device. Confirmed via the Smart Life app pairing and the Tuya IoT portal.
- We are using **`tinytuya`** (Python, local LAN control over port 6668) — *not* Home Assistant, *not* LocalTuya, *not* the Tuya Cloud API.
  - Rationale: Kevin's stack is already Python + InfluxDB + Grafana with a custom `proxy.js`. Dropping a Python polling daemon straight into that pipeline is the lowest-friction path. Home Assistant would be a heavier dependency for a single device.
  - The Tuya **Cloud API** is only used **once** during setup to retrieve the device's `local_key`. After that, all polling and control is local LAN.
- The Tuya cloud subscription has a free tier that expires (typically annually). Local control keeps working after expiration; cloud only needs re-subscription if the heater is re-paired and `local_key` rotates.

## Why not Wireshark / packet sniffing

Tuya traffic on port 6668 is AES-encrypted with the device's `local_key`. You'd need the key to decrypt anyway, and the Tuya developer portal hands it to you for free via the cloud API. Skip the Tomdein Wireshark dissector unless you need to discover DPS on an undocumented device — ours is already fully mapped (see below).

## Current state

1. ✅ Registered on Tuya IoT developer platform (`iot.tuya.com`).
2. ✅ Created a Cloud Project named "Raypak Crosswinds".
3. ✅ Authorized 5 API services on the project:
   - IoT Core
   - Authorization Token Management
   - Industry Basic Service
   - Identity and Access Management
   - **Smart Home Basic Service** (this one is critical for device queries)
4. ✅ Resolved the Tuya data-center mismatch by using the matching project region.
5. ✅ Installed `tinytuya` on DEVBOX (`pip install tinytuya`).
6. ✅ Ran `python -m tinytuya wizard`; local setup files now exist in this folder:
   - `devices.json`
   - `tinytuya.json`
   - `tuya-raw.json`
   - `snapshot.json`
7. ✅ Retrieved the heater `local_key` into `devices.json`.
8. ✅ Verified local polling from `raypak_poller.py`; current successful writes show `write_ok fields=16`.
9. ✅ Added `--persistent` flag to `raypak_poller.py`. Default is non-persistent sockets.
10. ✅ Added Windows scheduled-task support:
   - `run-raypak-poller.ps1`
   - `install-raypak-task.ps1`
   - Installed task name: `Raypak Poller`
   - Principal: `SYSTEM`
   - Trigger: system startup
   - Log: `logs\raypak-poller.log`
11. ✅ Added `README.md` with operator commands for manual runs, task install/update, task status, restart, log tail, and uninstall.

### Historical setup note

- The Tuya project's **data center** must match where the Smart Life account is registered, or the Link App Account step fails with "Data centers inconsistency".
- Project data center is **not editable** — to switch, create a new project. They are free.
- This project already has working local credentials. Re-run the wizard only if the heater is re-paired or `local_key` rotates.

## Device DPS map (Raypak Crosswind — already reverse-engineered)

Pulled from [localtuya issue #2066](https://github.com/rospogrigio/localtuya/issues/2066) — a user dumped the full Things Data Model via Tuya's developer portal. This is the same data we'd get by calling the `QueryThingsDataModel` API ourselves.

### Read/write controls

| DP ID | Code         | Type | Description |
|-------|--------------|------|-------------|
| 1     | `Power`      | bool | Main power on/off |
| 103   | `change_tem` | bool | Units: 0=Celsius, 1=Fahrenheit |
| 105   | `SetMode`    | enum | `smart` / `warm` / `cool` |
| 106   | `SetTemp`    | int  | Target setpoint (in current units) |
| 117   | `SilentMdoe` | bool | Silent / quiet mode (NOTE: misspelled in firmware — use verbatim) |

### Read-only telemetry

| DP ID | Code                | Type   | Description |
|-------|---------------------|--------|-------------|
| 102   | `WInTemp`           | int    | **Water inlet temp** (main one for Grafana) |
| 104   | `SpeedPercentage`   | int    | Overall speed % (0-150) |
| 107   | `SetDnLimit`        | int    | Setpoint lower limit |
| 108   | `SetUpLimit`        | int    | Setpoint upper limit |
| 115   | `fault1`            | bitmap | E1, E2, E3, E4, E5, E6, E7, E8, E9, EA, EB, ED, P0-P9, PA, F1-F7 |
| 116   | `fault2`            | bitmap | F8, F9, Fb, Fa |
| 118   | `WarmOrCool`        | bool   | Current operating mode (heat vs cool) |
| 120   | `OutPipeTemp`       | int    | Outside coil temp (AIN3) — reads -22 when off |
| 122   | `ExhaustTemp`       | int    | Compressor exhaust temp (AIN5) — reads -22 when off |
| 124   | `AmbTemp`           | int    | Ambient outdoor temp (AIN7) — reads -22 when off |
| 125   | `CompFreAct`        | int    | Compressor running frequency (Hz) |
| 126   | `CompressorCurrent` | int    | Compressor current draw (amps) |
| 127   | `RadTemp`           | int    | Radiator / heatsink temp |
| 128   | `EXVPosition`       | int    | Electronic expansion valve position (0-10000) |
| 129   | `DCFanSpeed`        | int    | DC fan RPM (0-10000) |
| 130   | `Defrost`           | bool   | Defrost cycle active |
| 134   | `CompRly`           | bool   | Compressor contactor / OUT1 |
| 135   | `CyclePump`         | bool   | Circulation pump relay / OUT2 |
| 136   | `ReserveValve`      | bool   | 4-way valve |
| 139   | `ChargeRly`         | bool   | Current-limit charge relay |
| 140   | `ACFanSpeed`        | enum   | `LowSpeed` / `MidSpeed` / `HighSpeed` |

### Notes

- The `-22` value on AmbTemp/OutPipeTemp/ExhaustTemp is the firmware's "sensor offline / not running" sentinel. Filter these out in InfluxDB writes — they aren't real readings.
- The supported temp range is **-22 to 104** (matches the value range for `change_tem=1` Fahrenheit). Default appears to be Fahrenheit.
- Fault DPs are bitmaps — extract individual error codes by bit position against the label list. Most production heaters will report 0/0 here.
- Compressor metrics (`CompFreAct`, `CompressorCurrent`, `DCFanSpeed`) are the best proxies for "how hard is it working" — useful for efficiency dashboards and load tracking.

## Minimum viable polling loop

```python
import tinytuya
import time

heater = tinytuya.Device(
    'DEVICE_ID_FROM_DEVICES_JSON',
    'HEATER_LAN_IP',   # use a DHCP reservation
    'LOCAL_KEY_FROM_DEVICES_JSON',
    version=3.3,
)
heater.set_socketPersistent(True)

DPS_MAP = {
    '1':   ('power',        bool),
    '102': ('water_in_f',   int),
    '104': ('speed_pct',    int),
    '105': ('mode',         str),
    '106': ('setpoint_f',   int),
    '115': ('fault1',       int),
    '116': ('fault2',       int),
    '117': ('silent_mode',  bool),
    '118': ('warm_or_cool', bool),
    '120': ('outpipe_f',    int),
    '122': ('exhaust_f',    int),
    '124': ('ambient_f',    int),
    '125': ('comp_freq_hz', int),
    '126': ('comp_amps',    int),
    '129': ('dc_fan_rpm',   int),
    '130': ('defrost',      bool),
    '134': ('comp_relay',   bool),
    '135': ('pump_relay',   bool),
    '140': ('ac_fan_speed', str),
}

POLL_INTERVAL = 10  # seconds; don't go below 1s
HEARTBEAT_INTERVAL = 9  # seconds; Tuya closes sockets at ~28s idle

last_heartbeat = 0
while True:
    raw = heater.status()
    dps = raw.get('dps', {})
    fields = {}
    for dp_id, (name, cast) in DPS_MAP.items():
        if dp_id in dps:
            val = dps[dp_id]
            # Sentinel filter: -22 means sensor offline / heater off
            if isinstance(val, int) and val == -22 and name in ('outpipe_f', 'exhaust_f', 'ambient_f'):
                continue
            fields[name] = cast(val)
    # TODO: write `fields` to InfluxDB as line protocol with tag host="heater_pool"
    now = time.time()
    if now - last_heartbeat > HEARTBEAT_INTERVAL:
        heater.heartbeat()
        last_heartbeat = now
    time.sleep(POLL_INTERVAL)
```

## Control surface

```python
# Power
heater.set_status(True,  switch=1)   # on
heater.set_status(False, switch=1)   # off

# Setpoint
heater.set_value(106, 85)            # 85°F

# Mode
heater.set_value(105, 'warm')        # 'smart' / 'warm' / 'cool'

# Silent mode (note the typo — required verbatim)
heater.set_value(117, True)

# Units toggle (rarely needed)
heater.set_value(103, 1)             # 1=F, 0=C
```

## Constraints / gotchas

- **One local TCP connection per device.** Tuya devices accept exactly one local socket at a time. Running tinytuya polling and Home Assistant's tuya-local simultaneously will fight. Pick one for the local connection; everything else reads from InfluxDB.
- **Don't poll faster than 1Hz.** Recommended is 5-10s for non-trivial devices. 10s is fine for a heat pump.
- **Default to non-persistent sockets at 30s polling.** Persistent sockets at a 30s interval can produce partial DPS responses, seen as `write_ok fields=4` instead of the full telemetry set.
- **Heartbeat every ~9s** when using a persistent socket. The heater closes idle connections around 28s. Only use `--persistent` with shorter polling or explicit heartbeat logic.
- **`local_key` rotates on re-pair.** If the heater is removed and re-added in Smart Life, run the wizard again. Don't commit `devices.json` to git.
- **DHCP reservation for the heater.** Pin its LAN IP on the UDM/router so we don't have to chase it.
- **Tuya free tier expiration.** IoT Core subscription is time-limited (1 year). Set a calendar reminder. Local polling keeps working after expiration — only cloud re-fetch of `local_key` requires an active subscription.
- **Firmware typo.** `SilentMdoe` (not `SilentMode`) — use as-is, don't "fix" it.
- **`requests` dependency warning** when running tinytuya CLI is harmless; `pip install --upgrade urllib3 chardet` quiets it if needed.
- **Scheduled runner suppresses Python warnings** with `PYTHONWARNINGS=ignore` to keep logs readable.
- **Windows scheduled task uses `C:\Python312\python.exe`.** Update `run-raypak-poller.ps1` if Python moves.

## Next steps (priority order)

1. **DHCP-reserve** the heater's IP on the router if not already done. Current script reads the IP from `devices.json`; stable DHCP prevents silent breakage.
2. **Watch the Windows scheduled task for a day**:
   ```powershell
   Get-ScheduledTask -TaskName "Raypak Poller"
   Get-Content C:\development\home\tuya\logs\raypak-poller.log -Tail 50
   ```
   Confirm it keeps writing `fields=16` and restarts cleanly after reboot.
3. **Improve log hygiene** — current log is append-only. Add rotation if this stays on Windows long-term.
4. **Grafana dashboard** —
   - Time series: `water_in_f`, `setpoint_f`, `ambient_f` overlaid
   - State row: `power`, `comp_relay`, `pump_relay`, `defrost` as binary indicators
   - Compressor panel: `comp_freq_hz`, `comp_amps`, `dc_fan_rpm`
   - Delta-T panel: `water_in_f - ambient_f` (efficiency proxy)
   - Fault annotations from `fault1` / `fault2` bitmap changes
5. **Control endpoint** — a small FastAPI service exposing `/heater/power`, `/heater/setpoint`, `/heater/mode`. Lets OpenClaw or other systems issue commands without duplicating the tinytuya connection. Must serialize commands through a single connection since the device only accepts one socket.
6. **OpenClaw integration** — wrap the FastAPI endpoint as an OpenClaw skill so the heater can participate in automations (e.g., "warm the pool to 88°F by 4pm if ambient ≥ 65°F"). Pattern matches the existing market_events / FMP skill architecture.
7. **Optional package cleanup** — if this grows beyond one script, move toward the package layout below (`raypak/`, `systemd/`, `grafana/`). Current working implementation is intentionally flat.

## Reference links

- tinytuya repo: https://github.com/jasonacox/tinytuya
- tinytuya examples: https://github.com/jasonacox/tinytuya/tree/master/examples
- Raypak Crosswind DPS dump (full Things Data Model): https://github.com/rospogrigio/localtuya/issues/2066
- HA community thread on the same heater: https://community.home-assistant.io/t/raypak-crosswinds-inverter-pool-heater-tuya-tywe1s/912840
- Tuya IoT portal: https://iot.tuya.com
- tuya-local (alternative HA integration if we ever want it): https://github.com/make-all/tuya-local
- Windows Scheduled Tasks docs: https://learn.microsoft.com/windows/win32/taskschd/task-scheduler-start-page

## Files in this project (current)

```
C:\development\home\tuya\
├── AGENTS.md                # this file
├── README.md                # operator commands and folder overview
├── .gitignore               # excludes secrets, wizard output, logs, caches
├── raypak_poller.py         # current working poller
├── run-raypak-poller.ps1    # scheduled-task runner
├── install-raypak-task.ps1  # scheduled-task installer/updater
├── influxdb-env.ps1         # InfluxDB env vars; secret, ignored
├── devices.json             # Tuya device metadata/local_key; secret, ignored
├── tinytuya.json            # tinytuya wizard config; ignored
├── tuya-raw.json            # tinytuya wizard raw output; ignored
├── snapshot.json            # tinytuya wizard snapshot; ignored
└── logs/
    └── raypak-poller.log    # runtime log; ignored
```

## Possible future package layout

```
C:\development\home\tuya\
├── pyproject.toml
├── raypak\
│   ├── __init__.py
│   ├── dps.py
│   ├── client.py
│   ├── poller.py
│   └── api.py
├── systemd\
│   └── raypak-poller.service
└── grafana\
    └── dashboard.json
```
