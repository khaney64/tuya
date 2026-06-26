# Shelly DS18B20 Integration

Companion to `AGENTS.md` and `DASHBOARD_ENHANCEMENTS.md`. This file documents the implemented **Shelly Plus 1 + Plus Add-On + DS18B20 probe** integration and remaining follow-up work.

## Goal

Add a Shelly-based pool water temperature reading to the project, with these properties:

1. **Auto-detect probes.** Start with one probe (`temperature:100`). The code must automatically pick up additional probes (`temperature:101`, etc.) when added, with no code change.
2. **Reuse the existing polling daemon (`raypak_poller.py`).** The Shelly polling lives inline in the same loop that handles the heater. One process, one service, two devices.
3. **Write to InfluxDB.** New fields land in the existing `pool_heater` measurement in the `heater` bucket — same measurement on purpose, so Grafana queries can overlay heater telemetry, canonical temperatures, and raw Shelly values without a join.
4. **Promote the better reading.** When a pool-body probe is present and plausible, canonical `pool_temp_f` is populated from the Shelly. `compute_derived_fields()` and observed COP history now read `pool_temp_f`, so every derived metric (`eta_seconds`, `observed_btu_hr`, `observed_cop`, `available_capacity_btu_hr`, `mode_sanity`, `cost_to_target_usd`) benefits without Grafana implementing sensor precedence.

## What's implemented

- **`pump_relay` and `comp_relay` are already in `DPS_MAP`** and being written to InfluxDB. No Phase-Zero prerequisite step is needed.
- **`compute_derived_fields()` uses the union pattern** `pump_relay OR pump_watts >= threshold` to determine `pump_valid`. The canonical `pool_temp_f` is selected before derived metrics run.
- **iotaWatt cross-bucket queries are already implemented** in the poller (`query_measurement_mean` against `iotawatt` bucket for `pool_heater` and `pump` measurements). No new cross-bucket plumbing.
- **Derived metrics are computed in-process and written as fields**, not via Flux. The Shelly integration improves the input (`pool_temp_f` instead of direct `water_in_f`) rather than duplicating math in Grafana.
- **Polling cadence is controlled by the `RaypakPoller` Windows service** through the poller interval args; default 30s. Shelly poll piggybacks on this — same cadence, no separate timer.

## Hardware context

Confirmed working as of this writing:

- Device: **Shelly Plus 1 UL** (model `SNSW-001X15UL`, MAC `B8D61A886348`, firmware updated to latest stable Plus line)
- Add-On: **Shelly Plus Add-on** (sensor type set to `sensor` via `Sys.SetConfig` → `device.addon_type`)
- Probe: one DS18B20, wired red→VCC, yellow→DATA, black→GND on the Add-On
- Pinned IP via DHCP reservation: **192.168.86.71** on the user's `HaneyNet` network
- Verified live reading: `temperature:100.tF ≈ 71.4°F` at room temp

The Shelly RPC endpoint that returns everything in one call:

```
GET http://192.168.86.71/rpc/Shelly.GetStatus
```

Response excerpt:

```json
{
  "switch:0": {"id": 0, ..., "temperature": {"tC": 53.0, "tF": 127.4}},
  "temperature:100": {"id": 100, "tC": 21.9, "tF": 71.4},
  "sys": {"uptime": 297, ...},
  "wifi": {"rssi": -52, ...}
}
```

`temperature:N` is a top-level key — one per detected DS18B20. `switch:0.temperature` is the *internal CPU temperature* of the Shelly, not a probe. When `tC`/`tF` is `null`, the probe is enrolled but not currently reading (rare, treat as missing data).

## InfluxDB schema additions

All new fields write to:

- **Bucket:** `heater` (existing)
- **Measurement:** `pool_heater` (existing — same measurement as heater telemetry on purpose)

### New fields

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `shelly_probe_100_f` | float | Shelly `temperature:100.tF` | Generic per-probe field, always written when probe present |
| `shelly_probe_101_f` | float | Shelly `temperature:101.tF` | Outside-air probe currently installed |
| `shelly_probe_102_f` | float | Shelly `temperature:102.tF` | Only written if third probe detected (Add-On supports 3 max) |
| `pool_temp_f` | float | promoted from whichever sensor is configured/best | Single canonical "best pool temperature" field — consumed by every downstream calc |
| `pool_temp_source` | string | `"shelly_100"` / `"heater_water_in"` / `"heater_water_in_stale"` | Provenance tag — which sensor produced the current `pool_temp_f` |
| `outside_temp_f` | float | promoted from Shelly outside probe or weather API | Single canonical "best outside temperature" field — consumed by dashboard and derived capacity math |
| `outside_temp_source` | string | `"shelly_101"` / `"weather_api"` | Provenance tag — which sensor produced the current `outside_temp_f` |
| `shelly_cpu_c` | float | Shelly `switch:0.temperature.tC` | Enclosure health — flag if it climbs past ~70°C |
| `shelly_uptime_s` | int | Shelly `sys.uptime` | Reboot detection (drops to low value when device restarts) |
| `shelly_rssi` | int | Shelly `wifi.rssi` | WiFi signal strength; degrades over distance / through walls |

### Rationale for the schema choices

- **Per-probe raw fields stay forever.** `shelly_probe_100_f`, `shelly_probe_101_f`, etc. are the unprocessed truth — they always exist for the probes physically present. Dashboards can always go back to them for diagnostics.
- **`pool_temp_f` is the canonical field for everything downstream.** `compute_derived_fields()`, time-to-target, BTU rate, COP — all consume `pool_temp_f`, not the per-probe fields directly. That keeps every panel and the existing derived-metric code using one consistent input that automatically upgrades to the Shelly when available.
- **`pool_temp_source` makes promotion visible.** If a panel ever looks wrong, this field tells you which sensor produced the value. Also a useful Grafana annotation source (when the source flips, draw a vertical line).
- **`outside_temp_f` is the canonical outside-air field.** It uses Shelly probe `101` first and only fetches API weather when the Shelly outside probe is missing or implausible.
- **Probe `101` is not the Raypak output/return probe.** No Raypak-output probe is installed yet. Future heater-output or return-line work should use a separate probe ID and field.
- **`shelly_cpu_c`, `shelly_uptime_s`, `shelly_rssi` are operational.** Same role as fault tracking on the heater — let the dashboard show device health, not just data.

## Configuration additions

Current config lives in `influxdb-env.ps1` with the rest of the poller env:

```ini
# Shelly Plus 1 with Add-On
RAYPAK_SHELLY_IP=192.168.86.71
RAYPAK_SHELLY_TIMEOUT_S=5

# Which probe is the canonical pool-body sensor.
# Set to the integer id that Shelly assigns to that probe (typically 100 for
# the first probe added, 101 for the second). If unset or set to a probe id
# that isn't currently present, the daemon falls back to the heater's
# water_in_f for `pool_temp_f`.
RAYPAK_POOL_PROBE_ID=100

# Optional: outside-air probe. Defaults to 101 when Shelly is enabled.
# This is not the future heater return/output probe.
RAYPAK_SHELLY_OUTSIDE_PROBE_ID=101

# Optional: plausibility window for Shelly readings (Fahrenheit)
RAYPAK_SHELLY_MIN_PLAUSIBLE_F=32     # below this = treat as bad data (ice or sensor fault)
RAYPAK_SHELLY_MAX_PLAUSIBLE_F=110    # above this = treat as bad data (sensor fault)
```

The probe IDs (100, 101, 102) come from the Shelly firmware — `temperature:100` is the first auto-discovered probe, `:101` is the second, in physical-detection order. Once a probe is enrolled the id is stable across reboots.

**Naming convention:** prefix env vars with `RAYPAK_` to match existing patterns (`RAYPAK_POOL_GALLONS`, `RAYPAK_KWH_PRICE`, etc.) — this keeps all project-related env vars discoverable as `Get-ChildItem env: | Where-Object Name -Like "RAYPAK_*"`.

Also add CLI args mirroring the env vars for one-off runs:

```
--shelly-ip 192.168.86.71
--pool-probe-id 100
--outside-probe-id 101
```

Match the existing `--once`, `--dry-run`, `--weather-latitude` style.

## Module: `shelly_client.py`

New file in the same directory as `raypak_poller.py` (the project is flat-layout, not a package). Single responsibility: poll one Shelly Plus 1 over HTTP, parse the response, return a normalized dict of fields ready to be merged into the InfluxDB write batch.

### Public interface

```python
# shelly_client.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import logging

import requests

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellyConfig:
    ip: str
    timeout_s: float = 5.0
    min_plausible_f: float = 32.0
    max_plausible_f: float = 110.0


class ShellyClient:
    """
    Thin client around the Shelly Gen2 Shelly.GetStatus RPC.
    Auto-detects however many temperature:N probes are present.
    """

    def __init__(self, config: ShellyConfig):
        self.cfg = config
        self._session = requests.Session()  # connection reuse

    def poll(self) -> dict:
        """
        Fetch current status, return a flat dict of InfluxDB-ready fields.

        Returns an empty dict on error rather than raising — the heater
        polling loop should keep running even if the Shelly is unreachable.
        Logs the error.

        Output shape (only keys with valid data are present):
            {
                "shelly_probe_100_f": 71.4,
                "shelly_probe_101_f": 73.8,      # only if probe present
                "shelly_cpu_c": 53.0,
                "shelly_uptime_s": 1234,
                "shelly_rssi": -54,
            }
        """
        try:
            r = self._session.get(
                f"http://{self.cfg.ip}/rpc/Shelly.GetStatus",
                timeout=self.cfg.timeout_s,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Shelly poll failed (%s): %s", self.cfg.ip, exc)
            return {}

        fields: dict = {}

        # Auto-detect every temperature:N probe in the response.
        # Firmware assigns ids 100/101/102 for Add-On probes; the loop is
        # generic in case future firmware uses different ids.
        for key, block in data.items():
            if not key.startswith("temperature:"):
                continue
            if not isinstance(block, dict):
                continue
            probe_id = block.get("id")
            tF = block.get("tF")
            if probe_id is None or tF is None:
                continue
            if not self._plausible(tF):
                log.warning(
                    "Shelly probe %s reading %s°F outside plausible range %s-%s; skipping",
                    probe_id, tF,
                    self.cfg.min_plausible_f, self.cfg.max_plausible_f,
                )
                continue
            fields[f"shelly_probe_{probe_id}_f"] = float(tF)

        # Operational health fields.
        switch0 = data.get("switch:0", {}) or {}
        cpu_temp = (switch0.get("temperature") or {}).get("tC")
        if cpu_temp is not None:
            fields["shelly_cpu_c"] = float(cpu_temp)

        sys_block = data.get("sys", {}) or {}
        if "uptime" in sys_block:
            fields["shelly_uptime_s"] = int(sys_block["uptime"])

        wifi_block = data.get("wifi", {}) or {}
        if "rssi" in wifi_block:
            fields["shelly_rssi"] = int(wifi_block["rssi"])

        return fields

    def _plausible(self, temp_f: float) -> bool:
        return self.cfg.min_plausible_f <= temp_f <= self.cfg.max_plausible_f


def detect_probes(client: ShellyClient) -> list[int]:
    """
    Utility for setup / debugging — return list of probe ids currently
    reporting from this Shelly. Useful for confirming the second probe
    was wired correctly without restarting the daemon.

    Run from a Python shell:
        from shelly_client import ShellyClient, ShellyConfig, detect_probes
        c = ShellyClient(ShellyConfig(ip="192.168.86.71"))
        print(detect_probes(c))
    """
    fields = client.poll()
    return sorted(
        int(k.removeprefix("shelly_probe_").removesuffix("_f"))
        for k in fields
        if k.startswith("shelly_probe_") and k.endswith("_f")
    )
```

### Notes on the module

- **Pure function feel.** `poll()` takes no arguments, returns a dict. Easy to test, easy to compose.
- **Never raises on network errors.** A failed Shelly poll never crashes the heater poller. The function logs and returns `{}`; the caller's `update()` on the field accumulator is a no-op.
- **Plausibility check.** The DS18B20 has known failure modes that produce 85°C (185°F) or 0°C (32°F) — sensor-init garbage values. The 32-110°F window catches both. Adjustable via env if a user has a hot tub above 110°F at the same probe.
- **Auto-detection is the loop over `temperature:*` keys.** No hardcoded list of probe ids. Adding a second probe to the Add-On is a wiring change with zero code change.
- **`detect_probes()` is a debug helper.** Useful from a Python shell to confirm Shelly wiring before committing to a daemon restart.
- **Connection reuse via `Session`.** The Shelly is friendly to keep-alive; reusing the TCP connection across polls cuts latency.

## Integration in `raypak_poller.py`

The current implementation is inline in `raypak_poller.py`. The historical snippets below remain useful as reference for the data flow, but the landed code uses `ShellyConfig`, `fetch_shelly_status()`, `shelly_temperature_fields()`, and `promote_pool_temperature()` directly in the poller.

### 1. Initialization (alongside the existing Tuya device construction)

```python
from shelly_client import ShellyClient, ShellyConfig

shelly_ip = args.shelly_ip or os.getenv("RAYPAK_SHELLY_IP")
shelly = None
if shelly_ip:
    shelly = ShellyClient(ShellyConfig(
        ip=shelly_ip,
        timeout_s=float(os.getenv("RAYPAK_SHELLY_TIMEOUT_S", "5")),
        min_plausible_f=float(os.getenv("RAYPAK_SHELLY_MIN_PLAUSIBLE_F", "32")),
        max_plausible_f=float(os.getenv("RAYPAK_SHELLY_MAX_PLAUSIBLE_F", "110")),
    ))
    log.info("Shelly client enabled: %s", shelly_ip)
else:
    log.info("Shelly client disabled (RAYPAK_SHELLY_IP not set)")

pool_probe_id = int(args.pool_probe_id or os.getenv("RAYPAK_POOL_PROBE_ID", "100"))
heater_return_probe_id = None
hr_id = args.heater_return_probe_id or os.getenv("RAYPAK_HEATER_RETURN_PROBE_ID")
if hr_id:
    heater_return_probe_id = int(hr_id)
```

If `RAYPAK_SHELLY_IP` is unset, skip Shelly entirely — the daemon keeps working as a heater-only poller. Shelly is opt-in via config presence.

### 2. Per-cycle: poll the Shelly and merge

After the existing heater status fetch and field-normalization step, before `compute_derived_fields()`:

```python
shelly_fields = shelly.poll() if shelly else {}

# Promote canonical pool temperature.
# Important: this MUST run before compute_derived_fields() so that
# pool_temp_f is present in `fields` for the derived math to consume.
promotion = promote_pool_temp(
    shelly_fields=shelly_fields,
    heater_fields=fields,
    pool_probe_id=pool_probe_id,
    pump_relay=fields.get("pump_relay"),
    pump_watts=pump_watts,
    pump_watt_threshold=derived_config.pump_watt_threshold,
)

# Merge into the same field accumulator that gets written to InfluxDB.
fields.update(shelly_fields)
fields.update(promotion)
```

### 3. The promotion function

Lives in `raypak_poller.py` (or a small `temperature_fusion.py` module if the agent prefers separation):

```python
def promote_pool_temp(
    shelly_fields: dict,
    heater_fields: dict,
    pool_probe_id: int,
    pump_relay: bool | None,
    pump_watts: float | None,
    pump_watt_threshold: float,
) -> dict:
    """
    Decide which sensor's reading becomes the canonical pool_temp_f.

    Preference order:
      1. The Shelly probe configured as pool_probe_id, if present in this poll.
      2. The heater's water_in_f, but only if circulation is confirmed
         (pump_relay True OR pump_watts above threshold).
      3. The heater's water_in_f marked as stale if circulation can't be
         confirmed — still emit it so the dashboard has a value, but tag
         the source so panels can show degraded confidence.

    Returns a dict with `pool_temp_f` (float) and `pool_temp_source` (string),
    or an empty dict if nothing usable is available.
    """
    shelly_key = f"shelly_probe_{pool_probe_id}_f"
    shelly_val = shelly_fields.get(shelly_key)
    if shelly_val is not None:
        return {
            "pool_temp_f": float(shelly_val),
            "pool_temp_source": f"shelly_{pool_probe_id}",
        }

    heater_val = heater_fields.get("water_in_f")
    if heater_val is None:
        return {}

    circulation_confirmed = bool(pump_relay) or (
        pump_watts is not None and pump_watts >= pump_watt_threshold
    )

    if circulation_confirmed:
        return {
            "pool_temp_f": float(heater_val),
            "pool_temp_source": "heater_water_in",
        }

    # Circulation off or unknown — heater reading reflects housing temp,
    # not pool. Emit anyway but mark stale.
    return {
        "pool_temp_f": float(heater_val),
        "pool_temp_source": "heater_water_in_stale",
    }
```

**Note** the union pattern (`pump_relay OR pump_watts`) is the same logic `compute_derived_fields()` uses for `pump_valid`. Consider extracting it into a shared helper like `_circulation_confirmed(fields, pump_watts, threshold)` and using it from both places — but that's a refactor, not a requirement.

### 4. Make `compute_derived_fields()` consume `pool_temp_f`

This is the one-line change that propagates the Shelly benefit to every derived metric. In `compute_derived_fields()`:

```python
# Before:
water_f = numeric_field(fields, "water_in_f")

# After:
water_f = numeric_field(fields, "pool_temp_f")
# Fall back to water_in_f if pool_temp_f isn't populated yet
# (e.g. very first poll cycle, or both sensors absent)
if water_f is None:
    water_f = numeric_field(fields, "water_in_f")
```

Everything downstream — `eta_seconds`, `available_capacity_btu_hr`, `observed_btu_hr`, `observed_cop`, `mode_sanity`, `cost_to_target_usd` — automatically benefits. No other changes to the math.

### 5. `observed_btu_hr` window query

The poller's existing observed-BTU calculation queries past `water_in_f` samples from InfluxDB to compute a regression slope. Update that query to read `pool_temp_f` instead:

```python
# In the query around line 624:
'  |> filter(fn: (r) => r._field == "pool_temp_f" or r._field == "speed_pct" or r._field == "pool_reading_valid" or r._field == "active_load")',
```

And the corresponding row filter around line 638:

```python
if row.get("_field") != "pool_temp_f":
```

For backfill of historical data that pre-dates `pool_temp_f`, the poller can gracefully fall back: query `pool_temp_f` first, if no rows are returned within the window, query `water_in_f` and use those rows instead. Adds maybe 10 lines and one extra query in the cold-start case.

### Why this design is robust

- **One InfluxDB write per cycle.** Don't write Shelly separately — merging into the same point keeps timestamps aligned, halves network chatter, avoids needing explicit `time=` anywhere.
- **Heater poll cadence wins.** The Shelly handles much higher polling rates trivially (it's a tiny webserver, not a sleepy IoT device), so it rides along on whatever the heater's interval is. Don't introduce a separate cadence.
- **Promotion is centralized.** One function decides the canonical value. Every downstream consumer reads `pool_temp_f`. Adding more sensors later (a different pool probe, a thermowell, anything else) means changing only the promotion function.
- **The Shelly being offline is a non-event.** `shelly.poll()` returns `{}`, the promotion falls back to `water_in_f`, the daemon keeps running.

## Dashboard logic updates

See `DASHBOARD_ENHANCEMENTS.md` for the full panel-by-panel migration. The high-level shape:

- Panels showing **pool water temperature** switch from `water_in_f` to `pool_temp_f`.
- Panels showing **heater inlet specifically** keep `water_in_f`.
- New panels enabled by Shelly: "Pool ↔ Heater Inlet Drift", "Pool Temp Source" indicator, optional Shelly health row, and (when second probe added) Heat Exchanger ΔT.

## Step-by-step agent task list

The order matters — each step builds on the previous.

### Phase 1: New module + tests

1. Add `shelly_client.py` per the spec above.
2. Add unit tests with a mocked `requests.Session` — at minimum: happy path with one probe, happy path with two probes, malformed JSON, network timeout, probe with `tF: null`, probe outside plausible range.
3. Add the new env vars (`RAYPAK_SHELLY_IP`, `RAYPAK_POOL_PROBE_ID`, etc.) to the README env var documentation block.

### Phase 2: Wire into the poller

4. Add `--shelly-ip`, `--pool-probe-id`, `--heater-return-probe-id` CLI args matching the env-var pattern in `raypak_poller.py`.
5. Initialize the `ShellyClient` in `main()` alongside the existing Tuya device.
6. Add `promote_pool_temp()` as a top-level function in `raypak_poller.py`.
7. In the polling loop, call `shelly.poll()`, then `promote_pool_temp(...)`, then merge both into `fields` before the `compute_derived_fields()` call.
8. Update `compute_derived_fields()` to read `pool_temp_f` (falling back to `water_in_f`).
9. Update the `observed_btu_hr` historical query to use `pool_temp_f` (with `water_in_f` fallback for backfill).

### Phase 3: Smoke test

10. Run `python raypak_poller.py --once` and verify InfluxDB now contains `pool_temp_f`, `pool_temp_source`, `shelly_probe_100_f`, `shelly_cpu_c`, `shelly_rssi`, `shelly_uptime_s`.
11. Verify `pool_temp_source` shows `"shelly_100"` when the Shelly is reachable.
12. Unplug Shelly briefly, run `--once` again, verify `pool_temp_source` falls back to `"heater_water_in"` (pump on) or `"heater_water_in_stale"` (pump off).
13. Verify derived metrics (`eta_seconds`, `observed_btu_hr`, etc.) still compute correctly — they should be slightly different because they're now using the (more accurate) Shelly reading.

### Phase 4: Dashboard

14. Apply the field substitutions from `DASHBOARD_ENHANCEMENTS.md` — primarily switch panel queries from `water_in_f` → `pool_temp_f` for pool-temperature panels.
15. Add the new panels: "Pool ↔ Heater Inlet Drift" (section 13), "Pool Temp Source" indicator (section 14), optional Shelly Health row (section 15).
16. Re-import to Grafana, verify rendering, commit updated `RaypakHeatPump.json` to the repo.

### Phase 5: Documentation

17. Update `AGENTS.md` "Current implementation state" — move Shelly from 🚧 to ✅.
18. Update `README.md` env var block — document the new `RAYPAK_SHELLY_*` variables.
19. Cross-link from `AGENTS.md` to this file.

### Phase 6: When the heater return/output probe is installed (no agent action needed)

20. User wires the additional DS18B20 to any unused VCC/DATA/GND triplet on the Shelly Add-On.
21. User records the firmware-assigned ID. Do not reuse `101`; that probe is outside air.
22. New field `shelly_probe_<id>_f` automatically starts being written. No code change required for the raw field itself.
23. At user's request, agent adds the "Heat Exchanger ΔT" panel from `DASHBOARD_ENHANCEMENTS.md` section 16.

## Testing checklist

Before marking the integration done:

- [ ] `curl http://<shelly-ip>/rpc/Shelly.GetStatus` returns a `temperature:100` block with a valid `tF`
- [ ] `ShellyClient.poll()` returns `{"shelly_probe_100_f": <float>, ...}` when invoked from a Python REPL
- [ ] `python raypak_poller.py --once --dry-run` line protocol includes `pool_temp_f=`, `pool_temp_source=`, and `shelly_probe_100_f=`
- [ ] InfluxDB query for `pool_temp_f` in the `heater` bucket returns recent points
- [ ] When the Shelly is unplugged, `pool_temp_source` switches to `heater_water_in` (pump on) or `heater_water_in_stale` (pump off) within one poll cycle
- [ ] When the Shelly is back online, `pool_temp_source` switches back to `shelly_100` within one poll cycle
- [ ] Dashboard's main water temp stat panel shows the Shelly reading when present
- [ ] Time series panel shows both `pool_temp_f` and `water_in_f` as distinct lines during pump-on operation; they should track closely
- [ ] During pump-off period (e.g. overnight), the two lines should *diverge* — Shelly stays steady, heater inlet drifts toward ambient. This is the proof the integration is doing what it's supposed to.
- [ ] Derived fields (`eta_seconds`, `observed_btu_hr`, `observed_cop`) continue to compute and stay within reasonable ranges after the switch

## Gotchas / things to remember

- **Probe id `100` vs `0`.** Some older Shelly Add-On firmware versions used `temperature:0`, `temperature:1` instead of `temperature:100`. The auto-detection loop handles both because it doesn't hardcode ids — but if `RAYPAK_POOL_PROBE_ID=100` is set and the firmware writes id `0`, the promotion will fall back to the heater. After Shelly firmware updates, the user may need to update `RAYPAK_POOL_PROBE_ID` to match what the firmware actually reports. `detect_probes()` is the easiest way to see what ids are in use.
- **Plausibility window applies to the canonical field too.** If a Shelly probe returns 200°F (sensor fault), it's filtered out, `shelly_probe_100_f` isn't written, and promotion falls back to the heater. The dashboard should never see a bogus pool reading.
- **DHCP reservation is non-negotiable.** The Shelly's IP must be pinned. Without it, an IP change silently breaks polling and the dashboard quietly degrades to heater-only. Add a comment in the config: `# Set DHCP reservation on router for MAC B8D61A886348 → SHELLY_IP`.
- **Don't poll Shelly faster than the heater.** The Shelly can handle 1Hz easily, but InfluxDB write cadence is set by the heater poll. Keep them aligned.
- **Calibration opportunity.** Once both sensors have been running side-by-side for a day during pump-on operation, query the mean ΔT between `pool_temp_f` (Shelly) and `water_in_f` (heater) over a steady-state window. A consistent offset is either plumbing heat loss or sensor offset — worth recording in `AGENTS.md` as a known constant after observation.
- **The promotion happens BEFORE `compute_derived_fields()`.** Order matters: `compute_derived_fields()` reads `pool_temp_f`, which only exists after `promote_pool_temp()` runs. Don't reorder.
- **`switch:0.temperature` is the Shelly's CPU, not a probe.** Don't confuse the two. The Shelly's CPU runs warm (~50°C) under normal operation; that's `shelly_cpu_c`, not pool temperature.

## Reference

- Shelly Plus 1 RPC docs: https://shelly-api-docs.shelly.cloud/gen2/Devices/ShellyPlus1
- Shelly Sys.SetConfig API: https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Sys#sysconfiguration
- Add-On sensor docs: https://shelly-api-docs.shelly.cloud/gen2/Devices/Addons/ShellyPlusAddOn
- DS18B20 datasheet (1-Wire bus length & pull-up): https://www.analog.com/media/en/technical-documentation/data-sheets/DS18B20.pdf
- Project repo: https://github.com/khaney64/tuya
- Companion docs: `AGENTS.md`, `DASHBOARD_ENHANCEMENTS.md`, `README.md`
