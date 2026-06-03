# Raypak Crosswind Tuya Poller

Local polling and control support for a Raypak Crosswind pool heat pump exposed as a Tuya / Smart Life device.

The poller uses `tinytuya` against the heater's LAN IP and writes telemetry to InfluxDB for Grafana. Tuya cloud output files are kept only for setup and local-key recovery.

## Files

| Path | Purpose |
| --- | --- |
| `raypak_poller.py` | Main poller. Reads Tuya DPS, normalizes fields, computes derived thermal metrics, writes InfluxDB line protocol. |
| `install-raypak-service.ps1` | Installs and starts the Windows service via WinSW. Defaults to `LocalSystem` at boot. |
| `uninstall-raypak-service.ps1` | Stops and removes the Windows service. |
| `run-raypak-poller.ps1` | Manual Windows runner. Writes logs to `logs\raypak-poller.log`. |
| `install-raypak-task.ps1` | Legacy Scheduled Task installer. Prefer the Windows service. |
| `influxdb-env.ps1` | Local InfluxDB configuration. Secret file; ignored by git. |
| `RaypakHeatPump.json` | Grafana dashboard JSON for visualizing the InfluxDB telemetry written by the poller. |
| `devices.json` | Tuya device metadata including `local_key`. Secret file; ignored by git. |
| `tinytuya.json`, `tuya-raw.json`, `snapshot.json` | `tinytuya wizard` output. Secret/local setup files; ignored by git. |
| `AGENTS.md` | Project context, DPS map, architecture notes, and next steps. |
| `DASHBOARD_ENHANCEMENTS.md` | Companion: Grafana panel queries and migration notes to consume poller-derived fields. |
| `SHELLY_INTEGRATION.md` | Implementation spec for adding the Shelly Plus 1 + DS18B20 pool probe(s). |

## Run Manually

Poll continuously with the default 30 second telemetry interval and fault merge sampler:

```powershell
python .\raypak_poller.py
```

Poll once, write once, then exit:

```powershell
python .\raypak_poller.py --once
```

Print line protocol instead of writing to InfluxDB:

```powershell
python .\raypak_poller.py --once --dry-run
```

Weather data is disabled unless a location is configured. Keep coordinates in ignored local config:

```powershell
$env:RAYPAK_WEATHER_LATITUDE = "12.3456"
$env:RAYPAK_WEATHER_LONGITUDE = "-12.3456"
```

Or pass them explicitly for a one-off run:

```powershell
python .\raypak_poller.py --weather-latitude 12.3456 --weather-longitude -12.3456
```

Disable fast fault sampling for a one-off diagnostic run:

```powershell
python .\raypak_poller.py --fault-sample-seconds 0
```

Fault sampling is generic for all fault codes. If a full telemetry poll does not see a fault, the poller tries up to 5 extra fault bitmap reads 2 seconds apart before writing the full telemetry point. If a fault appears, those fault fields are merged into the full telemetry record; no sparse fault-only points are written. E3 is one example: it is `fault1_raw` bit 2, so the raw value is `4`. Persistent sockets are not enabled automatically because this heater can return partial DPS responses on long-lived sockets.

## Derived Metrics

The poller writes derived dashboard fields once per minute by default. Defaults assume a 15x30 oval pool filled to 46 inches:

```powershell
$env:RAYPAK_POOL_GALLONS = "10100"
$env:RAYPAK_KWH_PRICE = "0.15828"
$env:RAYPAK_PUMP_WATT_THRESHOLD = "50"
```

By default, ETA and cost use the heater's reported `setpoint_f`. Set `RAYPAK_TARGET_TEMP_F` only when you want an override target that differs from the heater setpoint.

If the iotaWatt data is in a different InfluxDB datasource or requires a different token than the heater bucket, add:

```powershell
$env:IOTAWATT_INFLUXDB_URL = "http://..."
$env:IOTAWATT_INFLUXDB_ORG = "..."
$env:IOTAWATT_INFLUXDB_TOKEN = "..."
$env:IOTAWATT_INFLUXDB_BUCKET = "iotawatt"
```

Derived fields include `available_capacity_btu_hr`, `capacity_vs_rated_pct`, `eta_seconds`, `cost_to_target_usd`, `observed_btu_hr`, `observed_cop`, `observed_cop_status`, `pool_reading_valid`, and `mode_sanity`. When circulation is off, the poller still writes state fields with idle sentinels: `pool_reading_valid=false`, `eta_seconds=-3`, `cost_to_target_usd=-3`, and `mode_sanity=4`. Observed COP is calculated from a 3-hour stable full-load regression window and is suppressed when the result is unstable or physically implausible.

## Shelly Pool Probe (optional)

When a Shelly Plus 1 + Plus Add-On + DS18B20 probe is deployed for true pool-body temperature, configure:

```powershell
$env:RAYPAK_SHELLY_IP = "192.168.86.71"
$env:RAYPAK_POOL_PROBE_ID = "100"
# Optional: second probe (e.g. heater return line)
$env:RAYPAK_HEATER_RETURN_PROBE_ID = "101"
# Optional: plausibility window (defaults shown)
$env:RAYPAK_SHELLY_TIMEOUT_S = "5"
$env:RAYPAK_SHELLY_MIN_PLAUSIBLE_F = "32"
$env:RAYPAK_SHELLY_MAX_PLAUSIBLE_F = "110"
```

If `RAYPAK_SHELLY_IP` is unset, the Shelly integration is disabled and the poller runs heater-only. When enabled, the poller writes `shelly_probe_NNN_f` fields (one per detected probe — auto-detected, no per-probe config), a canonical `pool_temp_f` field promoted from the best available sensor, and a `pool_temp_source` provenance tag (`"shelly_100"`, `"heater_water_in"`, `"heater_water_in_stale"`). Derived metrics like `eta_seconds` and `observed_btu_hr` automatically consume the canonical reading. See `SHELLY_INTEGRATION.md` for the full spec.

## Windows Service

The poller should run as a Windows service. This survives reboot and avoids Scheduled Task runtime limits.

Download the WinSW x64 executable from the official releases page (`https://github.com/winsw/winsw/releases`), then install or update the service from an elevated PowerShell session:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1 -WinSWExe C:\path\to\WinSW-x64.exe
```

Default install mode:

- Service name: `RaypakPoller`
- Display name: `Raypak Poller`
- Account: `LocalSystem`
- Startup: automatic
- Poll interval: `30` seconds
- Fault sample interval: `2` seconds
- Fault sample attempts: `5`
- Persistent socket: off by default
- WinSW config: `C:\development\home\tuya\tools\RaypakPoller.xml`
- WinSW logs: `C:\development\home\tuya\logs\RaypakPoller.out.log`, `RaypakPoller.err.log`, and `RaypakPoller.wrapper.log`

The installer starts the service and verifies it reaches `Running`. After that succeeds, it removes the legacy Scheduled Task named `Raypak Poller`. Keep the old task for comparison with `-KeepScheduledTask`.

Install with persistent socket enabled:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1 -WinSWExe C:\path\to\WinSW-x64.exe -Persistent
```

Install with a custom interval:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1 -WinSWExe C:\path\to\WinSW-x64.exe -IntervalSeconds 10
```

If `tools\RaypakPoller.exe` already exists, omit `-WinSWExe`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1
```

Install with a custom fault sample interval:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1 -WinSWExe C:\path\to\WinSW-x64.exe -FaultSampleSeconds 2 -FaultSampleAttempts 3
```

Install with a custom weather refresh interval:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-service.ps1 -WinSWExe C:\path\to\WinSW-x64.exe -WeatherRefreshSeconds 300
```

## Useful Commands

Check service status:

```powershell
Get-Service RaypakPoller
```

Start or stop the service:

```powershell
Start-Service RaypakPoller
Stop-Service RaypakPoller
```

Restart the service:

```powershell
Restart-Service RaypakPoller
```

Tail the service logs:

```powershell
Get-Content C:\development\home\tuya\logs\RaypakPoller.out.log -Tail 50
```

Follow the service log:

```powershell
Get-Content C:\development\home\tuya\logs\RaypakPoller.out.log -Wait
```

Uninstall the service:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\uninstall-raypak-service.ps1
```

Legacy Scheduled Task commands, if needed:

```powershell
Get-ScheduledTask -TaskName "Raypak Poller"
Unregister-ScheduledTask -TaskName "Raypak Poller" -Confirm:$false
```

## Notes

- The heater is controlled locally over Tuya LAN protocol, not Tuya cloud.
- `devices.json` contains the Tuya `local_key`. Do not commit it.
- Import `RaypakHeatPump.json` into Grafana to create the dashboard for the InfluxDB telemetry.
- Weather coordinates are optional. Store `RAYPAK_WEATHER_LATITUDE` and `RAYPAK_WEATHER_LONGITUDE` in ignored local config, not tracked files.
- The poller filters `-22` sentinel values for heater ambient/outpipe/exhaust readings.
- Do not run another local Tuya client at the same time; the heater only accepts one local socket.
- If logs show only a few fields instead of the full telemetry set, disable fault sampling with `--fault-sample-seconds 0` for comparison.
- The `RequestsDependencyWarning` from `requests` is harmless for this poller. The scheduled runner suppresses Python warnings to keep logs readable.
