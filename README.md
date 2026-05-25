# Raypak Crosswind Tuya Poller

Local polling and control support for a Raypak Crosswind pool heat pump exposed as a Tuya / Smart Life device.

The poller uses `tinytuya` against the heater's LAN IP and writes telemetry to InfluxDB for Grafana. Tuya cloud output files are kept only for setup and local-key recovery.

## Files

| Path | Purpose |
| --- | --- |
| `raypak_poller.py` | Main poller. Reads Tuya DPS, normalizes fields, writes InfluxDB line protocol. |
| `run-raypak-poller.ps1` | Windows runner used by Task Scheduler. Writes logs to `logs\raypak-poller.log`. |
| `install-raypak-task.ps1` | Installs and starts the Windows Scheduled Task. Defaults to `SYSTEM` at boot. |
| `influxdb-env.ps1` | Local InfluxDB configuration. Secret file; ignored by git. |
| `RaypakHeatPump.json` | Grafana dashboard JSON for visualizing the InfluxDB telemetry written by the poller. |
| `devices.json` | Tuya device metadata including `local_key`. Secret file; ignored by git. |
| `tinytuya.json`, `tuya-raw.json`, `snapshot.json` | `tinytuya wizard` output. Secret/local setup files; ignored by git. |
| `AGENTS.md` | Project context, DPS map, architecture notes, and next steps. |

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

## Windows Scheduled Task

Install or update the service-like scheduled task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-task.ps1
```

Default install mode:

- Task name: `Raypak Poller`
- Principal: `SYSTEM`
- Trigger: system startup
- Poll interval: `30` seconds
- Fault sample interval: `2` seconds
- Fault sample attempts: `5`
- Persistent socket: off by default
- Log file: `C:\development\home\tuya\logs\raypak-poller.log`

Install as current user at logon instead of `SYSTEM` at boot:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-task.ps1 -CurrentUser
```

Install with persistent socket enabled:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-task.ps1 -Persistent
```

Install with a custom interval:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-task.ps1 -IntervalSeconds 10
```

Install with a custom fault sample interval:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\development\home\tuya\install-raypak-task.ps1 -FaultSampleSeconds 2 -FaultSampleAttempts 3
```

## Useful Commands

Check task status:

```powershell
Get-ScheduledTask -TaskName "Raypak Poller"
Get-ScheduledTaskInfo -TaskName "Raypak Poller"
```

Start or stop the task:

```powershell
Start-ScheduledTask -TaskName "Raypak Poller"
Stop-ScheduledTask -TaskName "Raypak Poller"
```

Restart the task:

```powershell
Stop-ScheduledTask -TaskName "Raypak Poller"
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "Raypak Poller"
```

Tail the poller log:

```powershell
Get-Content C:\development\home\tuya\logs\raypak-poller.log -Tail 50
```

Follow the log:

```powershell
Get-Content C:\development\home\tuya\logs\raypak-poller.log -Wait
```

Uninstall the task:

```powershell
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
