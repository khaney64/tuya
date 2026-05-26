# Grafana Dashboard Enhancements — RaypakHeatPump.json

Companion to `AGENTS.md`. Concrete instructions for enhancing https://github.com/khaney64/tuya/blob/main/RaypakHeatPump.json.

## Conventions

| Item | Value |
|------|-------|
| **Heater bucket** | `heater` |
| **Heater measurement** | `pool_heater` |
| **Heater datasource UID** | `efn38cqkeercwa` |
| **Power bucket** (iotaWatt) | `iotawatt` |
| **Power measurement** | `pool_heater` (whole-unit watts) — same name, different bucket |
| **Power datasource UID** | `kA0zmMz4z` |
| **Timezone** | `America/New_York` |
| **Refresh** | 30s |
| **Existing variable** (in Pool.json) | `${kWhPrice}` — for cost calcs |

Cross-bucket queries are allowed in Flux — `from(bucket: "iotawatt")` and `from(bucket: "heater")` work in the same query and can be joined.

## Configuration variables to add

Add to Settings → Variables for the RaypakHeatPump.json dashboard:

| Variable          | Type     | Default | Purpose |
|-------------------|----------|---------|---------|
| `pool_gallons`    | constant | `10100` | Total pool water volume (15×30 oval, 46" water depth) |
| `has_cover`       | constant | `1`     | 0=uncovered, 1=cover present (Kevin: always 1 except mid-day) |
| `kWhPrice`        | constant | `0.15828` | Cost per kWh; matches Pool.json variable for consistency |

`raypak_poller.py` also reads matching environment variables (`RAYPAK_POOL_GALLONS`, `RAYPAK_KWH_PRICE`) when computing derived metrics. ETA and cost use the heater's `setpoint_f` by default; set `RAYPAK_TARGET_TEMP_F` only to override the target. If the iotaWatt bucket needs a different Influx token/datasource, configure `IOTAWATT_INFLUXDB_URL`, `IOTAWATT_INFLUXDB_ORG`, `IOTAWATT_INFLUXDB_TOKEN`, and `IOTAWATT_INFLUXDB_BUCKET`.

## Enhancement tasks

### 1. Verify `pump_relay` is being written

Before any thermal calc, confirm:

```flux
from(bucket: "heater")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "pool_heater")
  |> filter(fn: (r) => r._field == "pump_relay")
  |> count()
```

If the count is zero, update the polling daemon to write DP 135 (`pump_relay`) and DP 134 (`comp_relay`). The polling-daemon fix is the right answer; until that lands, use **iotaWatt pump watts as the fallback proxy for valid pool-flow gating** — Pool.json already pulls `pump` watts from the `iotawatt` bucket, and a pump above its minimum running threshold (say `>50W`) is a near-perfect signal that pool water is actually flowing.

**Why iotaWatt pump watts and not `comp_amps > 0`:** the compressor running tells you the heater wants to heat, but not that water is moving through it. If the pump is off and the flow switch hasn't tripped yet (or is failing), you can get compressor + no-flow for short windows. iotaWatt pump watts is direct evidence of pool circulation regardless of what the heater's internal flow logic thinks.

Reusable Flux pattern to use throughout this doc — a stream of timestamps when pool flow is valid:

```flux
// Primary: pump_relay from heater telemetry
pump_relay_stream = from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "pump_relay" and r._value == true)
  |> aggregateWindow(every: 5m, fn: last, createEmpty: false)
  |> keep(columns: ["_time"])

// Fallback: iotaWatt pump watts above running threshold
pump_watts_stream = from(bucket: "iotawatt")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pump" and r._value > 50.0)
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time"])

// Union — either signal counts as "pool flow valid"
flow_valid = union(tables: [pump_relay_stream, pump_watts_stream])
  |> group(columns: [])
  |> distinct(column: "_time")
  |> keep(columns: ["_time"])
```

This `flow_valid` stream is referenced in the BTU rate and COP panels below.

### 2. Δtemp (water vs ambient) panel

**Title:** Temperature Differential
**Type:** Time series
**Unit:** `fahrenheit`
**Position:** y=14, full width

```flux
water = from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "water_in_f")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)

weather = from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "weather_temp_f")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)

join(tables: {w: water, a: weather}, on: ["_time"])
  |> map(fn: (r) => ({ _time: r._time, _value: r._value_w - r._value_a, _field: "delta_t" }))
```

In warm mode, large positive Δtemp = significant heat loss to environment. In cool mode, large negative Δtemp = significant heat gain working against the cool. Either way, magnitude indicates "how hard is the heater fighting the environment."

### 3. Live power draw from iotaWatt

**Title:** Power Draw (live)
**Type:** Stat or gauge
**Unit:** `kwatt`
**Decimals:** 2

```flux
from(bucket: "iotawatt")
  |> range(start: -2m)
  |> filter(fn: (r) => r._measurement == "pool_heater")
  |> last()
  |> map(fn: (r) => ({ r with _value: float(v: r._value) / 1000.0 }))
```

This is the **authoritative** power reading — whole-unit watts, including fan, controls, and inverter losses. Use this rather than `comp_amps × 230V` everywhere.

### 4. Daily kWh — heater only

**Title:** Heater kWh Today
**Type:** Stat
**Unit:** `kwatth`
**Decimals:** 1

```flux
import "date"
import "timezone"

option location = timezone.location(name: "America/New_York")

start_of_day = date.truncate(t: today(), unit: 1d)

from(bucket: "iotawatt")
  |> range(start: start_of_day)
  |> filter(fn: (r) => r._measurement == "pool_heater")
  |> map(fn: (r) => ({ r with _value: float(v: r._value) / 1000.0 }))
  |> integral(unit: 1h)
```

`integral(unit: 1h)` turns kW samples into kWh. Same approach the Pool.json dashboard uses, just on the heater dashboard for direct visibility.

### 5. Observed BTU rate panel

**Title:** Observed BTU Rate
**Type:** Time series
**Unit:** custom label "BTU/hr"
**Note:** sign is *positive* in warm mode (water rising), *negative* in cool mode (water falling). Y-axis should allow negatives.

```flux
// Heater iotaWatt watts above 200 = meaningful operation
heater_on_stream = from(bucket: "iotawatt")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._value > 200.0)
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time"])

// Pool flow valid (uses the `flow_valid` pattern defined in section 1)
pump_relay_stream = from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "pump_relay" and r._value == true)
  |> aggregateWindow(every: 5m, fn: last, createEmpty: false)
  |> keep(columns: ["_time"])

pump_watts_stream = from(bucket: "iotawatt")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pump" and r._value > 50.0)
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time"])

flow_valid = union(tables: [pump_relay_stream, pump_watts_stream])
  |> group(columns: [])
  |> distinct(column: "_time")
  |> keep(columns: ["_time"])

// Compute the BTU rate *before* the join, so _value is the rate we want.
// difference() on water_in_f gives us °F per 5-min; multiply out to BTU/hr.
btu_rate = from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "water_in_f")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> difference(columns: ["_value"], nonNegative: false)
  |> map(fn: (r) => ({
      _time: r._time,
      _value: r._value * 12.0 * ${pool_gallons} * 8.33,  // 12 × 5-min = 1 hour
      _field: "btu_per_hr"
    }))

// Now inner-join with the gate streams. After join, the gate columns are
// suffixed (_value_b, _time_b, etc.) but our rate is _value_a — extract it cleanly.
joined1 = join(tables: {b: btu_rate, h: heater_on_stream}, on: ["_time"])
joined2 = join(tables: {b: joined1, f: flow_valid}, on: ["_time"])

joined2
  |> map(fn: (r) => ({
      _time: r._time,
      _value: r._value_b,
      _field: "btu_per_hr"
    }))
```

**Why this version works:** the original tried to call `difference()` *after* joining three tables that all had a `_value` column. Flux suffixes collided columns (`_value_w`, `_value_p`, `_value_pp`), so `difference(columns: ["_value"])` had nothing to operate on. Computing the rate *before* the join keeps `_value` unambiguous in the rate stream, and the join just acts as a gate (only emit timestamps where heater and flow are both valid).

Compare visually against the Available Capacity panel (#7 below) — if observed is consistently 60% of expected, something's off (refrigerant, fouled coil, low flow). Note that 5-min samples will be noisy because water_in_f only changes by tenths of a degree; consider a 15-min smoothing in a follow-up query.

### 6. Observed COP gauge — using iotaWatt power

**Title:** Observed COP
**Type:** Gauge
**Min:** 0, **Max:** 7
**Thresholds:** red <2, yellow 2-4, green >4 (heating); for cool mode, expect lower COP, adjust mentally
**Decimals:** 2

```flux
import "experimental"

// Use a 60-minute window — long enough for measurable water ΔT
// Sample windows every 5 min = 12 samples in the window
WINDOW_MINUTES = 60.0
EXPECTED_SAMPLES = 12  // 60 min / 5 min

water = from(bucket: "heater")
  |> range(start: -60m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "water_in_f")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)

power = from(bucket: "iotawatt")
  |> range(start: -60m)
  |> filter(fn: (r) => r._measurement == "pool_heater")
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)

// CONTINUITY GATE: require heater power > 200W in every 5-min sample of the window.
// If any sample is below threshold (heater cycled off, was paused, etc.), abort.
power_samples_above_threshold = power
  |> filter(fn: (r) => r._value > 200.0)
  |> count()
  |> findRecord(fn: (key) => true, idx: 0)

heater_continuous = power_samples_above_threshold._value >= EXPECTED_SAMPLES

// CONTINUITY GATE: require pump_relay == true OR iotaWatt pump > 50W in every sample.
pump_relay = from(bucket: "heater")
  |> range(start: -60m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "pump_relay" and r._value == true)
  |> aggregateWindow(every: 5m, fn: last, createEmpty: false)
  |> count()
  |> findRecord(fn: (key) => true, idx: 0)

pump_watts_ok = from(bucket: "iotawatt")
  |> range(start: -60m)
  |> filter(fn: (r) => r._measurement == "pump" and r._value > 50.0)
  |> aggregateWindow(every: 5m, fn: mean, createEmpty: false)
  |> count()
  |> findRecord(fn: (key) => true, idx: 0)

pump_continuous = pump_relay._value >= EXPECTED_SAMPLES or pump_watts_ok._value >= EXPECTED_SAMPLES

// Take ΔT over the full window — only meaningful if both gates passed
water_last = water |> last() |> findRecord(fn: (key) => true, idx: 0)
water_first = water |> first() |> findRecord(fn: (key) => true, idx: 0)
delta_f = water_last._value - water_first._value

// Power mean — already computed above, just need a scalar
power_mean = power
  |> mean(column: "_value")
  |> findRecord(fn: (key) => true, idx: 0)
mean_w = float(v: power_mean._value)

// Output BTU/hr = ΔT × (60/WINDOW_MINUTES) × gallons × 8.33
// Input BTU/hr = (mean_w / 1000) × 3412
// COP = abs(output / input) — abs() so cool mode shows positive efficiency
cop_value =
  if heater_continuous and pump_continuous and mean_w > 0.0 then
    math.abs(x: (delta_f * (60.0 / WINDOW_MINUTES) * ${pool_gallons} * 8.33) / ((mean_w / 1000.0) * 3412.0))
  else
    float(v: -1)  // sentinel: gate failed, panel should show "No Data"

array.from(rows: [{ _time: now(), _value: cop_value }])
```

**Panel value mapping:** map `_value: -1` to display text "No data (insufficient continuous operation)" or similar. In Grafana panel options, use a Value Mapping: special value `-1` → text "Cycling / pump off". Otherwise the gauge shows the computed COP.

**Why continuity matters:** a 30-min average power and start/end ΔT can produce a plausible-looking COP even if the heater was off for part of the window (the average pulls toward "low input"), or if the pump cycled (some samples reflect stagnant heater housing rather than pool). Requiring *every* sample in the window to pass the threshold eliminates both cases.

**Cool-mode interpretation:** in cool mode, `delta_f` is negative. `math.abs()` gives efficiency magnitude. Add a separate small panel showing current mode so the gauge reading is contextualized.

### 7. Available Capacity (spec-sheet expected BTU/hr)

**Title:** Available Capacity
**Type:** Stat (or bar gauge)
**Unit:** `none` (label "BTU/hr")
**Mode-aware:** different curves for warm vs cool

```flux
// Spec-sheet derived heating capacity:
// Linear between (50°F, 24500 BTU) and (80°F, 57650 BTU)
// slope = (57650 - 24500) / (80 - 50) = 1105
// intercept = 24500 - 1105*50 = -30750
heatCapacity = (ambient_f) => {
  raw = 1105.0 * ambient_f - 30750.0
  return if ambient_f > 80.0 then 57650.0
         else if raw < 5000.0 then 5000.0
         else raw
}

// Estimated cooling capacity — piecewise linear from documented table:
//   70°F → 45000 BTU/hr
//   80°F → 40000 BTU/hr
//   90°F → 32000 BTU/hr
//   100°F → 25000 BTU/hr
// Extrapolation outside [70, 100] uses the nearest segment's slope; clamp below 10k.
coolCapacity = (ambient_f) => {
  raw =
    if ambient_f <= 70.0 then
      // Extrapolate using 70→80 slope: -500/°F, so capacity *rises* as ambient cools
      // (until the unit physically can't dump heat). Cap at 50000.
      // slope = (40000 - 45000) / (80 - 70) = -500
      45000.0 + (-500.0) * (ambient_f - 70.0)
    else if ambient_f <= 80.0 then
      // slope = (40000 - 45000) / 10 = -500
      45000.0 + (-500.0) * (ambient_f - 70.0)
    else if ambient_f <= 90.0 then
      // slope = (32000 - 40000) / 10 = -800
      40000.0 + (-800.0) * (ambient_f - 80.0)
    else
      // 90+ : slope = (25000 - 32000) / 10 = -700
      32000.0 + (-700.0) * (ambient_f - 90.0)
  capped_low = if raw < 10000.0 then 10000.0 else raw
  capped_high = if capped_low > 50000.0 then 50000.0 else capped_low
  return capped_high
}
// IMPORTANT: these cooling numbers are estimates pending empirical calibration —
// see AGENTS.md "Cool-mode capacity calibration" pending task.

ambient = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "weather_temp_f")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

mode_now = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "mode")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

ambient_f = float(v: ambient._value)
is_cool = mode_now._value == "cool"
// Note: in smart mode, this defaults to heating capacity. Smart mode can do either
// direction depending on water vs setpoint; panel #8 (time-to-target) picks the
// appropriate curve dynamically. For this panel, heating capacity is the safer
// default since it's also the more common operating mode.

capacity = if is_cool then coolCapacity(ambient_f: ambient_f) else heatCapacity(ambient_f: ambient_f)

array.from(rows: [{ _time: now(), _value: capacity, _field: "btu_per_hr" }])
```

Display tip: add a panel field override for the label to include the mode — e.g. "{mode_now} capacity at {ambient_f}°F".

### 8. Time to Setpoint — handles warm and cool modes

**Title:** Time to Setpoint
**Type:** Stat
**Unit:** `s`

The trick: warm mode subtracts heat loss; cool mode subtracts heat gain (loss becomes gain that fights cooling). Both directions converge toward target, but at different rates and with different sign of the imbalance.

```flux
import "math"
import "date"

heatCapacity = (ambient_f) => {
  raw = 1105.0 * ambient_f - 30750.0
  return if ambient_f > 80.0 then 57650.0
         else if raw < 5000.0 then 5000.0
         else raw
}

// Piecewise linear cool capacity from documented table (70/80/90/100°F).
// MUST MATCH the curve in panel #7 above — if you adjust one, adjust both.
coolCapacity = (ambient_f) => {
  raw =
    if ambient_f <= 80.0 then
      45000.0 + (-500.0) * (ambient_f - 70.0)
    else if ambient_f <= 90.0 then
      40000.0 + (-800.0) * (ambient_f - 80.0)
    else
      32000.0 + (-700.0) * (ambient_f - 90.0)
  capped_low = if raw < 10000.0 then 10000.0 else raw
  capped_high = if capped_low > 50000.0 then 50000.0 else capped_low
  return capped_high
}

// --- gather current state ---
water = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "water_in_f")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

weather = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "weather_temp_f")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

mode_now = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "mode")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

setpoint = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "setpoint_f")
  |> last()
  |> findRecord(fn: (key) => true, idx: 0)

water_f = float(v: water._value)
ambient_f = float(v: weather._value)
target_f = float(v: setpoint._value)
is_cool = mode_now._value == "cool"
is_warm = mode_now._value == "warm"
is_smart = mode_now._value == "smart"
covered = ${has_cover} == 1

// --- direction the pool needs to move ---
needs_cooling = water_f > target_f + 0.5   // small dead-band
needs_heating = water_f < target_f - 0.5
already_there = not needs_cooling and not needs_heating

// --- MODE-DIRECTION MATCH CHECK ---
// The heater can only push the pool in the direction its mode allows.
// Warm mode can heat but won't actively cool. Cool mode can cool but won't heat.
// Smart mode is assumed bidirectional (manages both within the SetDnLimit/SetUpLimit band).
// If mode doesn't match direction, ETA is undefined — emit sentinel.
mode_matches_direction =
  already_there or
  is_smart or
  (is_warm and needs_heating) or
  (is_cool and needs_cooling)

// --- heater capacity in the chosen mode ---
// In smart mode, pick the curve matching the direction needed.
gross_btu =
  if is_cool then coolCapacity(ambient_f: ambient_f)
  else if is_warm then heatCapacity(ambient_f: ambient_f)
  else if is_smart and needs_heating then heatCapacity(ambient_f: ambient_f)
  else if is_smart and needs_cooling then coolCapacity(ambient_f: ambient_f)
  else 0.0

// --- environmental gain/loss ---
delta_t = water_f - ambient_f
loss_factor_warm = if covered then 0.15 else 0.40
gain_factor_cool = if covered then 0.12 else 0.30

// Scale by Δt magnitude (bigger Δt = bigger thermal exchange with environment)
delta_scale = math.abs(x: delta_t) / 20.0

// Net BTU/hr toward target
net_btu =
  if needs_heating then
    gross_btu * (1.0 - loss_factor_warm * delta_scale)
  else if needs_cooling then
    gross_btu * (1.0 - gain_factor_cool * delta_scale)
  else 0.0

gallons = float(v: ${pool_gallons})
rate_per_hr = net_btu / (gallons * 8.33)

degrees_needed = math.abs(x: target_f - water_f)
hours_needed = if rate_per_hr <= 0.0 then 999.0
               else degrees_needed / rate_per_hr

// Output sentinels:
//   already at target → 0
//   mode doesn't match direction → -1 (UI maps to "Mode mismatch")
//   net rate too low / runaway → -2 (UI maps to "Cannot reach target")
//   normal → seconds_needed clamped at 999h
final_seconds =
  if already_there then 0.0
  else if not mode_matches_direction then -1.0
  else if rate_per_hr <= 0.0 then -2.0
  else if hours_needed * 3600.0 > 999.0 * 3600.0 then 999.0 * 3600.0
  else hours_needed * 3600.0

array.from(rows: [{ _time: now(), _value: final_seconds, _field: "eta_seconds" }])
```

**Panel value mappings** (Panel Options → Value Mappings):

| Value | Display | Color |
|-------|---------|-------|
| `0` | "At target" | green |
| `-1` | "Mode mismatch" | orange |
| `-2` | "Cannot reach target" | red |
| Range > `0` | (default — seconds, Grafana formats as h/m) | blue |

The mode mismatch case is the most important addition. Example: pool is at 78°F, target is 82°F (needs heating), but mode = `cool`. Previously the panel would happily multiply by the (wrong) cool capacity and report some plausible-looking time. Now it surfaces the misconfiguration directly.

The dashboard now reads `eta_seconds` from `raypak_poller.py` derived metrics. Keep the Flux version above as a reference model only; the shipped panel should use the simple derived-field query.

### 9. Cost-to-Target — poller-derived

**Title:** Estimated Cost to Reach Target
**Type:** Stat
**Unit:** `currencyUSD`
**Decimals:** 2

This panel reads the derived field written by `raypak_poller.py`. The reason: implementing this purely in Flux would require either calling the time-to-target query as a subquery (Flux doesn't compose function-style across `from()` boundaries cleanly) or duplicating the entire mode-aware capacity/loss/direction logic from panel #8. Both options are brittle and would drift out of sync with #8 every time the model is refined.

**Poller-derived contract:**

The poller computes derived metrics on a 1-min cadence, reads recent mean heater/pump power from iotaWatt, and writes back derived fields to `pool_heater` in the `heater` bucket:

| Field | Type | Meaning |
|-------|------|---------|
| `eta_seconds` | float | Same sentinels as panel #8 (`0` = at target, `-1` = mode mismatch, `-2` = unreachable, `>0` = seconds to target) |
| `available_capacity_btu_hr` | float | Expected gross capacity for current ambient and mode |
| `capacity_vs_rated_pct` | float | `available_capacity_btu_hr / 57650 * 100`, used by the dashboard gauge |
| `heater_watts` | float | Mean iotaWatt heater power over the recent query window |
| `pump_watts` | float | Mean iotaWatt pump power over the recent query window |
| `cost_to_target_usd` | float | `(eta_seconds / 3600) × (heater_watts / 1000) × kWhPrice`; mirrors the eta sentinels |

Once the poller is writing derived fields, this panel's query is trivial:

```flux
from(bucket: "heater")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "cost_to_target_usd")
  |> last()
```

**Panel value mappings** (mirroring panel #8):

| Value | Display | Color |
|-------|---------|-------|
| `0` | "At target — $0" | green |
| `-1` | "Mode mismatch" | orange |
| `-2` | "Cannot reach target" | red |
| Range > `0` | (default — formatted as USD) | blue |

**Why this is poller-derived and not best-effort Flux:**

A partial Flux implementation would either (a) silently use the wrong eta when mode is mismatched, or (b) reimplement the panel #8 logic verbatim — at which point fixing a bug in #8 means fixing it in two places. `raypak_poller.py` is the right boundary for now: one Python function computes eta and cost together, writes both, and every panel that needs them reads the field.

**Poller implementation notes:**

- Reuse the `heat_capacity_btu()` and `cool_capacity_btu()` Python functions for available-capacity and ETA computation.
- Read `RAYPAK_POOL_GALLONS`, `RAYPAK_KWH_PRICE`, and threshold env vars in `raypak_poller.py`; use `RAYPAK_TARGET_TEMP_F` only as an optional override.
- Query InfluxDB for recent iotaWatt heater/pump power; use current poll fields for heater state; write derived fields back.
- Run derived writes on a 1-min cadence inside the polling daemon.
- Mode mismatch detection lives in Python where it's straightforward (no Flux `findRecord` gymnastics).

### 10. Modify existing "Pool, Setpoint, and Weather" — add heater's own ambient sensor

The heater has its own ambient sensor (DP 124, written as `ambient_f`). When it diverges from the external `weather_temp_f`, it tells you something useful (sun on the unit case, microclimate, sensor drift). Update the query:

```flux
from(bucket: "heater")
  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
  |> filter(fn: (r) => r._measurement == "pool_heater")
  |> filter(fn: (r) =>
       r._field == "water_in_f" or
       r._field == "setpoint_f" or
       r._field == "weather_temp_f" or
       r._field == "ambient_f")
  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)
  |> yield(name: "mean")
```

Override `ambient_f` → displayName "Heater Sensor", color gray, dashed line. Keep `weather_temp_f` as solid "Outside".

### 11. Modify existing "Water Temp" stat — add stale indicator

Add a thin sub-stat or color override when pump has been off recently:

```flux
water = from(bucket: "heater")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "water_in_f")
  |> last()

pump_recently_on = from(bucket: "heater")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "pump_relay")
  |> filter(fn: (r) => r._value == true)
  |> count()

join(tables: {w: water, p: pump_recently_on}, on: [])
  |> map(fn: (r) => ({
      r with
      _value: r._value_w,
      _field: if r._value_p > 0 then "Pool Water (valid)" else "Pool Water (STALE — pump off)"
    }))
```

Field override on title for color: green when valid, orange when stale.

### 12. Mode-aware operating sanity panel

**Title:** Mode Sanity
**Type:** Stat with value mappings
**Purpose:** flag obvious misconfigurations (cool mode on a cold day, warm mode on a hot day)

```flux
mode = from(bucket: "heater") |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "mode")
  |> last() |> findRecord(fn: (key) => true, idx: 0)

ambient = from(bucket: "heater") |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "pool_heater" and r._field == "weather_temp_f")
  |> last() |> findRecord(fn: (key) => true, idx: 0)

ambient_f = float(v: ambient._value)
m = mode._value

state = if m == "warm" and ambient_f > 90.0 then 2
        else if m == "cool" and ambient_f < 70.0 then 2
        else if m == "warm" and ambient_f < 50.0 then 1
        else 0

array.from(rows: [{ _time: now(), _value: state, _field: "sanity" }])
```

Value mappings: 0 = "Normal" green, 1 = "Marginal" yellow, 2 = "Mode mismatch" red.

## Suggested layout (post-enhancement)

```
Row 1 (existing stat row, y=0, h=5):
  Load | Fault Status | Water Temp | Setpoint | Weather Temp | Mode | Run State

Row 2 (new, y=5, h=5):
  Power Draw (kW) | kWh Today | Capacity vs Rated | Observed COP | Time to Setpoint | Cost to Setpoint | Mode Sanity

Row 3 (existing time series, y=10, h=9):
  Pool, Setpoint, and Weather (+ ambient_f overlay)

Row 4 (new, y=19, h=6):
  Temperature Differential (water - ambient)
  Observed BTU Rate

Row 5 (existing, y=25, h=8):
  Compressor and Fan Load

Row 6 (existing, y=33, h=7):
  Fault History | Latest Telemetry
```

## How to apply these changes

**Option A — JSON edit:**
1. Pull `RaypakHeatPump.json` from the repo
2. Insert new panel objects into the `panels` array
3. Re-import to Grafana with "replace existing"
4. Commit updated JSON

**Option B — UI then export (recommended for queries with `findRecord` that may need tweaking):**
1. Open existing dashboard → add panel → test query iteratively
2. Settings → JSON Model → copy → commit

The Flux reference queries should be treated as math documentation. The shipped dashboard should prefer poller-derived fields for capacity, COP, ETA, cost, and mode sanity.

## Calibration plan

Once the panels are wired up, accuracy depends on calibrating constants. Do this in order:

1. **`pool_gallons`** — working value is `10100` from 15×30 oval and 46" water depth. Refinement methods:
   - Pool builder's spec (if known)
   - Calibration run against observed heating rate
   - **Calibration run:** record water temp, run heater for 2 hours under steady conditions (warm mode, full power, pump on the whole time, no swimmers). Use the **observed power draw** from iotaWatt and **observed temp rise** to back-solve gallons:
     ```
     gallons = (kWh_consumed × COP_from_spec × 3412) / (ΔT × 8.33 × hours × loss_factor_warm)
     ```
   - Refine over multiple runs at different ambient temps.

2. **`loss_factor_warm` / `gain_factor_cool`** — fit empirically once gallons is set. Comparing observed BTU rate against spec capacity gives you the loss/gain factor directly.

3. **Cool-mode capacity curve** — capture a full week of cool-mode operation in summer, fit a curve. The current quadratic is a placeholder.

## Stretch goals

- Forecast-based "predicted pool temp at sunrise tomorrow" using NWS/Open-Meteo forecast + current state + thermal model
- Defrost cycle annotations (DP 130) on the temperature chart — cold-weather BTU dips will correlate
- Manual cover toggle in Grafana (button or variable) for real-time loss adjustment
- Cross-dashboard link button: from RaypakHeatPump.json link to Pool.json (and vice versa)
- Long-term: physical flow meter via ESP32 + MQTT to replace parametric gallons × rate with measured ΔT × GPM. Eliminates the gallons unknown entirely.
