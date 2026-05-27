# Raypak Crosswind Local Control — Project Context

## Goal

Programmatically connect to a **Raypak Crosswind 65-I** pool heat pump over Wi-Fi to:

1. **Poll telemetry** (water temperature, ambient temp, compressor state, faults, etc.) and store it in **InfluxDB** for **Grafana** dashboards.
2. **Control the device** locally — power on/off, change setpoint, change mode (warm/cool/smart).
3. Eventually expose control as a capability to **OpenClaw** (likely via a thin FastAPI endpoint or a skill that wraps the Python module).

## Architecture decision

- The heater is a **Tuya / Smart Life** device. Confirmed via the Smart Life app pairing and the Tuya IoT portal.
- Using **`tinytuya`** (Python, local LAN control over port 6668) — *not* Home Assistant, *not* LocalTuya, *not* the Tuya Cloud API for polling.
  - Rationale: Kevin's stack is already Python + InfluxDB + Grafana with a custom `proxy.js`. Polling daemon drops straight into that pipeline.
  - The Tuya **Cloud API** is only used **once** during setup to retrieve the device's `local_key`. After that, all polling and control is local LAN.
- The Tuya cloud subscription has a free tier that expires (typically annually). Local control keeps working after expiration; cloud only needs re-subscription if the heater is re-paired and `local_key` rotates.

## Repository

- GitHub: https://github.com/khaney64/tuya
- Heat pump dashboard: https://github.com/khaney64/tuya/blob/main/RaypakHeatPump.json
- Pool power/cost dashboard: https://github.com/khaney64/tuya/blob/main/Pool.json

## Operating context (user habits)

- **Solar cover** (bubble blanket) on the pool at night. Effective heat loss factor: ~15% of input rather than 40%.
- **Silent mode is rarely used.** Spec-sheet silent-mode derate is informational only; default behavior assumes full-power capacity.
- **Mode can be either `warm` or `cool`.** In late summer Kevin switches to cool mode to drop pool temp on hot days. Thermal model must handle both directions:
  - **Warm mode:** target > current → heater adds heat → temperature rises toward target.
  - **Cool mode:** target < current → heater removes heat → temperature falls toward target.
- **Pool circulation pump runs on a schedule** (off overnight typically). When pump is off, `water_in_f` reads stagnant water inside the heater housing, not pool average.
- **Pool volume model:** 15×30 oval above-ground pool, 52-inch wall, typically filled to 46 inches. Use **10,100 gallons** as the working volume for dashboard ETA/BTU/COP math.

## Current implementation state

✅ **Working as of session 2:**

- Tuya IoT portal project created in correct data center (Western America for US account)
- `local_key` retrieved via `python -m tinytuya wizard`
- Polling daemon running, writing to InfluxDB bucket `heater`, measurement `pool_heater`
- Grafana dashboard `RaypakHeatPump.json` live with panels: Load, Fault Status, Water Temp, Setpoint, Weather Temp, Mode, Run State, time series (water/setpoint/outside), compressor/fan load time series, fault history table, latest telemetry table
- Weather temperature being pulled from external source and written to same measurement as `weather_temp_f` (allowing single-query overlays)
- Fault decoding implemented (`fault_codes` string field, empty when no faults)
- Polling presumed to be at ~10s interval given the dashboard's 30s refresh
- **iotaWatt-based power monitoring** already running independently — measures wall-side power draw of the heater unit and circulation pump. Stored in bucket `iotawatt`. This is the *authoritative power source* for kWh/cost calculations; the heater's reported `comp_amps` is just compressor current and misses fan, control board, and inverter losses.

## InfluxDB schemas in play

### Heater telemetry (from tinytuya polling daemon)

| Property      | Value |
|---------------|-------|
| Bucket        | `heater` |
| Measurement   | `pool_heater` |
| Datasource UID| `efn38cqkeercwa` |

**Fields confirmed in use:**

| Field            | Type   | Source DP | Notes |
|------------------|--------|-----------|-------|
| `water_in_f`     | int    | DP 102    | Water inlet temperature |
| `setpoint_f`     | int    | DP 106    | Target setpoint |
| `weather_temp_f` | float  | external  | Outside temperature (external API, not from heater) |
| `power`          | bool   | DP 1      | Main on/off |
| `mode`           | string | DP 105    | "warm" / "cool" / "smart" |
| `speed_pct`      | int    | DP 104    | Compressor speed % |
| `dc_fan_rpm`     | int    | DP 129    | Fan RPM (also displayed as load % = rpm/100) |
| `comp_freq_hz`   | int    | DP 125    | Compressor frequency |
| `comp_amps`      | int    | DP 126    | Compressor current (use iotawatt for whole-unit power) |
| `fault_codes`    | string | DP 115/116 derived | Empty string = no fault |

**Likely additional fields (presumed being written but not yet on dashboard):**
`ambient_f`, `outpipe_f`, `exhaust_f`, `silent_mode`, `comp_relay`, `pump_relay`, `defrost`, `ac_fan_speed`, `warm_or_cool`

### iotaWatt power data (from separate iotaWatt monitoring)

| Property      | Value |
|---------------|-------|
| Bucket        | `iotawatt` |
| Datasource UID| `kA0zmMz4z` |

**Confirmed measurements:**

| Measurement   | Unit  | Notes |
|---------------|-------|-------|
| `pool_heater` | watts | Whole-unit heater wall power (collides with heater bucket's measurement name — different bucket, no actual conflict) |
| `pump`        | watts | Pool circulation pump power |
| `pf_pump`     | unitless 0-1 | Pump power factor — gauge with thresholds at 0.66/0.67/0.68 |

**Variables defined in Pool.json:**
- `${kWhPrice}` — cost per kWh, used in all cost calculations

The Pool dashboard already does:
- Cost today / past 7 days / current billing cycle for both heater and pump
- Cost-by-hour and kWh-by-hour time series
- Pump power factor gauge
- 7-day bar chart of daily cost

## Heater specifications (Crosswind 65-I)

From the manufacturer spec sheet, "Inverter Models Only":

### Heating capacity by operating condition

| Conditions                          | BTU/hr full | COP  |
|-------------------------------------|-------------|------|
| Air 80°F, Water 80°F, 80% humidity  | **61,000**  | 5.74 |
| Air 80°F, Water 80°F, 63% humidity  | **57,650**  | 5.30 |
| Air 50°F, Water 80°F, 63% humidity  | **24,500**  | 4.10 |

Capacity falls roughly linearly with ambient — at 50°F you get ~40% of the 80°F output.

### Cooling capacity (not on spec sheet but inferable)

The unit is a reversible heat pump (DP 105 supports `cool` mode, DP 136 is a 4-way reversing valve). The spec sheet doesn't publish cooling capacity, but for reversible air-source heat pumps in chiller mode the typical relationship is:

- **Cooling capacity ≈ Heating capacity − Compressor input power** (energy balance: same compressor, but in cooling mode the heat we want is what came from the water, not what we added)
- **Cooling efficiency curve is inverted vs heating:** in cooling mode, **hotter ambient = harder to cool** (worse COP). The unit dumps heat to outdoor air, so if outdoor air is hot, the temp delta is smaller and the compressor works harder.
- **Practical estimate:** cooling BTU/hr at 90°F ambient ≈ 30,000-40,000 BTU/hr for the 65-I. This is a rough estimate without manufacturer data — should be calibrated against observation.

For thermal modeling, the cooling-mode capacity curve assumes a *peak* at moderate ambient (~75°F) and degrades on both sides — a different shape than heating mode's monotonically-rising curve.

### Other specs

- Rated input: 1.55 kW / 6.8A @ 208-230VAC 1Ph 60Hz
- Refrigerant: R410A, 45.9 oz
- Heat exchanger: Titanium in PVC
- Water pipe: 1-1/2" PVC (50mm)
- **Advised water flow: 28.5–37.5 GPM (108–142 L/min)** — important for the flow switch and efficiency
- Sound: 38.2–49.3 dBA at 3m

### Faults (from DPS bitmap labels)

- `fault1` (DP 115): E1, E2, E3, E4, E5, E6, E7, E8, E9, EA, EB, ED, P0, P1, P2, P3, P4, P5, P6, P7, P8, P9, PA, F1, F2, F3, F4, F5, F6, F7
- `fault2` (DP 116): F8, F9, Fb, Fa

## Thermal model — rate of pool temperature change

### The math (direction-agnostic)

For an open body of water:

```
ΔT_°F/hr = ±BTU/hr ÷ (gallons × 8.33)
```

Sign convention: positive ΔT in warm mode (water rising), negative in cool mode (water falling). `8.33` = lb/gal of water.

### Warm mode capacity curve

Linear interpolation between the two spec data points (50°F → 24,500 BTU, 80°F → 57,650 BTU):

| Pool size    | At 80°F air | At 65°F air (~42k est.) | At 50°F air |
|--------------|-------------|-------------------------|-------------|
| 10,000 gal   | 0.73°F/hr   | 0.50°F/hr               | 0.29°F/hr   |
| 15,000 gal   | 0.49°F/hr   | 0.34°F/hr               | 0.20°F/hr   |
| 20,000 gal   | 0.37°F/hr   | 0.25°F/hr               | 0.15°F/hr   |
| 25,000 gal   | 0.29°F/hr   | 0.20°F/hr               | 0.12°F/hr   |

### Cool mode capacity curve (estimated, requires calibration)

Cooling capacity peaks at moderate ambient and degrades on hot days. Starting parametric model:

| Ambient | Est. cooling BTU/hr |
|---------|---------------------|
| 70°F    | ~45,000             |
| 80°F    | ~40,000             |
| 90°F    | ~32,000             |
| 100°F   | ~25,000             |

These are placeholders. Real cool-mode data should be captured during cool-mode operation and fit empirically.

### Heat loss model

Solar cover is **standing assumption** for this user (always covered at night). Adjustments:

- **Covered + warm mode:** ~15% loss factor. Cover suppresses evaporation, the dominant loss term.
- **Covered + cool mode:** loss is now a *gain* (heat coming in from sun, ambient, etc.) and works against the cooling. ~10-15% of cooling capacity added back as ambient gain.
- **Uncovered + warm mode:** ~40% loss factor.
- **Uncovered + cool mode:** ambient gain ~25-35% of cooling capacity (sun, evaporation works for you, but the system has more total heat load).

The loss factor scales with Δtemp (water vs ambient): bigger Δtemp = bigger loss/gain.

### Observed behavior from 24h dashboard

Real overnight cycle showed characteristic patterns:

- **Pump-off overnight = stratified reading:** `water_in_f` drifts from ~74°F at 17:00 down to ~58°F by 06:00. This is the *heater housing* cooling, not the pool.
- **Pump-on convergence:** ~09:00 pump starts → `water_in_f` jumps from ~58°F back up to ~65°F within minutes as real pool water flows through. First valid pool reading of the morning.
- **Steady state:** once pump is running consistently, `water_in_f` tracks pool average well.

**Implication:** any rate calc or "time to target" must filter to pump-on samples — gating on `pump_relay` (DP 135), falling back to iotaWatt `pump` watts, or using active compressor/fan load only as a heater-running signal.

## Device DPS map (Raypak Crosswind — already reverse-engineered)

Pulled from [localtuya issue #2066](https://github.com/rospogrigio/localtuya/issues/2066).

### Read/write controls

| DP ID | Code         | Type | Description |
|-------|--------------|------|-------------|
| 1     | `Power`      | bool | Main power on/off |
| 103   | `change_tem` | bool | Units: 0=Celsius, 1=Fahrenheit |
| 105   | `SetMode`    | enum | `smart` / `warm` / `cool` |
| 106   | `SetTemp`    | int  | Target setpoint (in current units) |
| 117   | `SilentMdoe` | bool | Silent / quiet mode (NOTE: misspelled in firmware — use verbatim; Kevin rarely uses) |

### Read-only telemetry

| DP ID | Code                | Type   | Description |
|-------|---------------------|--------|-------------|
| 102   | `WInTemp`           | int    | **Water inlet temp** (main one for Grafana) |
| 104   | `SpeedPercentage`   | int    | Overall speed % (0-150) |
| 107   | `SetDnLimit`        | int    | Setpoint lower limit |
| 108   | `SetUpLimit`        | int    | Setpoint upper limit |
| 115   | `fault1`            | bitmap | E1, E2, E3, E4, E5, E6, E7, E8, E9, EA, EB, ED, P0-P9, PA, F1-F7 |
| 116   | `fault2`            | bitmap | F8, F9, Fb, Fa |
| 118   | `WarmOrCool`        | bool   | Current operating direction (heat vs cool). Boolean-to-direction mapping not yet confirmed — observe the field while running in known modes (`SetMode=warm` vs `SetMode=cool`) to establish which boolean value corresponds to which direction. |
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
| 136   | `ReserveValve`      | bool   | 4-way reversing valve (heat vs cool refrigerant direction) |
| 139   | `ChargeRly`         | bool   | Current-limit charge relay |
| 140   | `ACFanSpeed`        | enum   | `LowSpeed` / `MidSpeed` / `HighSpeed` |

### Important: no flow rate sensor

The Crosswind exposes pump on/off (`CyclePump`) but **no actual flow rate**. The unit uses a binary pressure/flow switch as a safety interlock.

### Sentinel values

The `-22` value on AmbTemp/OutPipeTemp/ExhaustTemp is the firmware's "sensor offline / not running" sentinel. Filter these out — they aren't real readings.

## Polling loop (reference)

```python
import tinytuya
import time

heater = tinytuya.Device(
    'DEVICE_ID_FROM_DEVICES_JSON',
    'HEATER_LAN_IP',
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

POLL_INTERVAL = 10
HEARTBEAT_INTERVAL = 9

last_heartbeat = 0
while True:
    raw = heater.status()
    dps = raw.get('dps', {})
    fields = {}
    for dp_id, (name, cast) in DPS_MAP.items():
        if dp_id in dps:
            val = dps[dp_id]
            if isinstance(val, int) and val == -22 and name in ('outpipe_f', 'exhaust_f', 'ambient_f'):
                continue
            fields[name] = cast(val)
    # write `fields` to InfluxDB line protocol with measurement=pool_heater
    now = time.time()
    if now - last_heartbeat > HEARTBEAT_INTERVAL:
        heater.heartbeat()
        last_heartbeat = now
    time.sleep(POLL_INTERVAL)
```

## Control surface

```python
heater.set_status(True,  switch=1)   # power on
heater.set_status(False, switch=1)   # power off
heater.set_value(106, 85)            # setpoint 85°F
heater.set_value(105, 'warm')        # 'smart' / 'warm' / 'cool'
heater.set_value(117, True)          # silent mode (not commonly used)
heater.set_value(103, 1)             # 1=F, 0=C
```

## Constraints / gotchas

- **One local TCP connection per device.** Tuya devices accept exactly one local socket at a time. Polling daemon owns the socket; everything else (FastAPI controller, OpenClaw skill) must funnel commands through it.
- **Don't poll faster than 1Hz.** 10s is fine for a heat pump.
- **Heartbeat every ~9s** with a persistent socket. Device closes idle connections around 28s.
- **`local_key` rotates on re-pair.** Re-run wizard if device is removed and re-added in Smart Life. Don't commit `devices.json` to git.
- **Tuya free tier expiration** (1 year). Local polling keeps working — only cloud re-fetch of `local_key` requires active subscription.
- **Firmware typo `SilentMdoe`** — use verbatim.
- **Pump-off readings are misleading.** Gate thermal calcs on `pump_relay = true`.
- **Two ambient sources:** `weather_temp_f` (external API, always valid) and `ambient_f` (DP 124, only valid when heater running). Use external for predictions, heater's for sanity checks.
- **Use iotaWatt for power, not `comp_amps`.** Compressor amps miss fan, controls, inverter losses. iotaWatt measures whole-unit wall draw and is already integrated.
- **`pool_heater` measurement name collision.** Same name in two buckets (`heater` for telemetry, `iotawatt` for power). Disambiguate by bucket in queries; never assume which one.

## Next steps (priority order)

### Already done

- ✅ Wizard, local_key retrieval, polling daemon, basic dashboard, iotaWatt power monitoring, Pool cost dashboard

### In progress

1. **Dashboard enhancements** — see `DASHBOARD_ENHANCEMENTS.md` for detailed task list. Top items:
   - Pump-on filter (`pump_relay`) verification and propagation through thermal queries
   - Time-to-target panel — handles both warm and cool modes
   - Δtemp (water vs ambient) panel
   - Observed COP using iotaWatt input power as denominator (more accurate than comp_amps)
   - Pump-on water_in_f only / "valid pool reading" indicator
   - Available capacity panel showing spec-sheet expected BTU/hr at current ambient and current mode

### Pending

2. **Heater capacity model** now lives inside `raypak_poller.py` as derived metrics written to InfluxDB (`available_capacity_btu_hr`, `capacity_vs_rated_pct`, `eta_seconds`, `cost_to_target_usd`, `observed_btu_hr`, `observed_cop`, `pool_reading_valid`, `mode_sanity`). ETA/cost use heater `setpoint_f` by default; `RAYPAK_TARGET_TEMP_F` is only an optional override. When circulation is off, derived state is written as idle (`eta_seconds=-3`, `cost_to_target_usd=-3`, `mode_sanity=4`). Extract to `raypak/thermal.py` later if OpenClaw needs to reuse the same math.
3. **Cool-mode capacity calibration** — capture cool-mode operation data over a hot week to fit the curve empirically. Current cool-mode numbers are estimates.
4. **Pool volume calibration** — working estimate is 10,100 gallons. Calibration approach: known-state run (record water temp, run heater for 2 hours under steady conditions, measure rise, back-solve for effective gallons). Until then, ETA/COP are estimates.
5. **Control endpoint** — FastAPI service exposing `/heater/power`, `/heater/setpoint`, `/heater/mode`. Single socket to device, multiplexed across HTTP callers.
6. **OpenClaw integration** — wrap FastAPI as a skill. Example automations:
   - "Warm pool to 88°F by 4pm Saturday if forecast ambient ≥ 65°F"
   - "If forecast high > 95°F tomorrow, switch to cool mode at 6am, target 82°F"
   - "Estimate cost to reach setpoint" using iotaWatt power × time-to-target × kWhPrice
7. **Alerting** — Grafana alerts:
   - Heater running but `water_in_f` slope ~0 over 30 min (with pump on) → mechanical issue
   - Non-empty `fault_codes` → notification
   - Whole-unit power (iotaWatt) significantly higher than expected for current operating condition → degraded performance
   - Mode = `cool` but ambient < 75°F (probably a forgotten setting)
   - Mode = `warm` but ambient > 90°F (questionable — waste of energy)
8. **Cross-dashboard linking** — add a dashboard link from RaypakHeatPump.json to Pool.json and vice versa.

## Reference links

- tinytuya repo: https://github.com/jasonacox/tinytuya
- tinytuya examples: https://github.com/jasonacox/tinytuya/tree/master/examples
- Raypak Crosswind DPS dump: https://github.com/rospogrigio/localtuya/issues/2066
- HA community thread: https://community.home-assistant.io/t/raypak-crosswinds-inverter-pool-heater-tuya-tywe1s/912840
- Tuya IoT portal: https://iot.tuya.com
- Project repo: https://github.com/khaney64/tuya
