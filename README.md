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
| `devices.json` | Tuya device metadata including `local_key`. Secret file; ignored by git. |
| `tinytuya.json`, `tuya-raw.json`, `snapshot.json` | `tinytuya wizard` output. Secret/local setup files; ignored by git. |
| `AGENTS.md` | Project context, DPS map, architecture notes, and next steps. |

## Run Manually

Poll continuously with the default non-persistent Tuya socket:

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

Opt into a persistent Tuya socket:

```powershell
python .\raypak_poller.py --persistent
```

Default is non-persistent. Use `--persistent` only when polling often enough to keep the Tuya socket healthy, or after adding heartbeat logic.

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
- Persistent socket: off
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
- The poller filters `-22` sentinel values for heater ambient/outpipe/exhaust readings.
- If logs show only a few fields instead of the full telemetry set, prefer non-persistent mode at a 30 second interval.
- The `RequestsDependencyWarning` from `requests` is harmless for this poller. The scheduled runner suppresses Python warnings to keep logs readable.
