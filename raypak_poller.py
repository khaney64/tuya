#!/usr/bin/env python3
"""Poll Raypak Crosswind Tuya telemetry and write it to InfluxDB."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tinytuya


POLL_INTERVAL_SECONDS = 30
WEATHER_REFRESH_SECONDS = 300
MEASUREMENT = "pool_heater"
DEVICE_FILE = "devices.json"
DEFAULT_ENV_FILE = "influxdb-env.ps1"

HEATER_TAGS = {
    "host": "heater_pool",
    "device": "raypak_crosswind",
    "source": "tuya_local",
}

DPS_MAP: dict[str, tuple[str, type]] = {
    "1": ("power", bool),
    "102": ("water_in_raw", int),
    "103": ("heater_units_raw", int),
    "104": ("speed_pct", int),
    "105": ("mode", str),
    "106": ("setpoint_raw", int),
    "115": ("fault1_raw", int),
    "116": ("fault2_raw", int),
    "117": ("silent_mode", bool),
    "118": ("warm_or_cool", bool),
    "120": ("outpipe_raw", int),
    "122": ("exhaust_raw", int),
    "124": ("heater_ambient_raw", int),
    "125": ("comp_freq_hz", int),
    "126": ("comp_amps", int),
    "127": ("radiator_raw", int),
    "128": ("exv_position", int),
    "129": ("dc_fan_rpm", int),
    "130": ("defrost", bool),
    "134": ("comp_relay", bool),
    "135": ("pump_relay", bool),
    "136": ("reserve_valve", bool),
    "139": ("charge_relay", bool),
    "140": ("ac_fan_speed", str),
}

SENTINEL_OFFLINE_FIELDS = {"outpipe_raw", "exhaust_raw", "heater_ambient_raw"}

TEMP_RAW_FIELDS = {
    "water_in_raw": "water_in",
    "setpoint_raw": "setpoint",
    "outpipe_raw": "outpipe",
    "exhaust_raw": "exhaust",
    "heater_ambient_raw": "heater_ambient",
    "radiator_raw": "radiator",
}

FAULT1_LABELS = [
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
    "E6",
    "E7",
    "E8",
    "E9",
    "EA",
    "EB",
    "ED",
    "P0",
    "P1",
    "P2",
    "P3",
    "P4",
    "P5",
    "P6",
    "P7",
    "P8",
    "P9",
    "PA",
    "F1",
    "F2",
    "F3",
    "F4",
    "F5",
    "F6",
    "F7",
]

FAULT2_LABELS = ["F8", "F9", "FB", "FA"]


@dataclass(frozen=True)
class DeviceConfig:
    device_id: str
    address: str
    local_key: str
    version: float


@dataclass(frozen=True)
class InfluxConfig:
    url: str
    org: str
    bucket: str
    token: str


class InfluxWriteError(RuntimeError):
    pass


def log(message: str) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    print(f"{timestamp} {message}", flush=True)


def load_powershell_env_file(path: Path) -> None:
    if not path.exists():
        return

    pattern = re.compile(r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(['\"])(.*?)\2\s*$")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = pattern.match(line)
        if match and match.group(1) not in os.environ:
            os.environ[match.group(1)] = match.group(3)


def load_device_config(path: Path) -> DeviceConfig:
    devices = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(devices, list) or not devices:
        raise ValueError(f"{path} must contain at least one device")

    device = devices[0]
    device_id = device.get("id")
    address = device.get("ip")
    local_key = device.get("key") or device.get("local_key")
    version = float(device.get("version") or 3.3)

    missing = [
        name
        for name, value in {
            "id": device_id,
            "ip": address,
            "key/local_key": local_key,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(f"{path} missing required device fields: {', '.join(missing)}")

    return DeviceConfig(
        device_id=str(device_id),
        address=str(address),
        local_key=str(local_key),
        version=version,
    )


def load_influx_config(env_file: Path | None) -> InfluxConfig:
    if env_file:
        load_powershell_env_file(env_file)

    values = {
        "url": os.getenv("INFLUXDB_URL"),
        "org": os.getenv("INFLUXDB_ORG"),
        "bucket": os.getenv("INFLUXDB_BUCKET"),
        "token": os.getenv("INFLUXDB_TOKEN"),
    }
    missing = [f"INFLUXDB_{name.upper()}" for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"missing InfluxDB config: {', '.join(missing)}")

    return InfluxConfig(
        url=str(values["url"]).rstrip("/"),
        org=str(values["org"]),
        bucket=str(values["bucket"]),
        token=str(values["token"]),
    )


def env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def resolve_weather_location(args: argparse.Namespace) -> tuple[float, float] | None:
    latitude = args.weather_latitude
    longitude = args.weather_longitude

    if latitude is None:
        latitude = env_float("RAYPAK_WEATHER_LATITUDE")
    if longitude is None:
        longitude = env_float("RAYPAK_WEATHER_LONGITUDE")

    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        raise ValueError("weather location requires both latitude and longitude")

    return float(latitude), float(longitude)


def create_heater(config: DeviceConfig, persistent: bool = False) -> tinytuya.Device:
    heater = tinytuya.Device(
        config.device_id,
        config.address,
        config.local_key,
        version=config.version,
    )
    heater.set_socketPersistent(persistent)
    return heater


def cast_dps_value(value: Any, target_type: type) -> Any:
    if target_type is bool:
        return bool(value)
    if target_type is int:
        return int(value)
    if target_type is str:
        return str(value)
    return value


def celsius_to_fahrenheit(value: int | float) -> float:
    return round((float(value) * 9 / 5) + 32, 1)


def fahrenheit_to_celsius(value: int | float) -> float:
    return round((float(value) - 32) * 5 / 9, 1)


def add_temperature_fields(fields: dict[str, Any]) -> None:
    units_raw = fields.get("heater_units_raw")
    if units_raw not in (0, 1):
        return

    for raw_field, base_name in TEMP_RAW_FIELDS.items():
        raw_value = fields.get(raw_field)
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            continue

        if units_raw == 1:
            fields[f"{base_name}_c"] = raw_value
            fields[f"{base_name}_f"] = celsius_to_fahrenheit(raw_value)
        else:
            fields[f"{base_name}_f"] = raw_value
            fields[f"{base_name}_c"] = fahrenheit_to_celsius(raw_value)


def decode_fault_bitmap(value: Any, labels: list[str]) -> list[str]:
    if not isinstance(value, int) or isinstance(value, bool):
        return []
    return [label for bit, label in enumerate(labels) if value & (1 << bit)]


def add_fault_fields(fields: dict[str, Any]) -> None:
    fault_codes = [
        *decode_fault_bitmap(fields.get("fault1_raw"), FAULT1_LABELS),
        *decode_fault_bitmap(fields.get("fault2_raw"), FAULT2_LABELS),
    ]
    fields["fault_active"] = bool(fault_codes)
    fields["fault_codes"] = ",".join(fault_codes)


def map_dps_fields(dps: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for dp_id, (field_name, target_type) in DPS_MAP.items():
        if dp_id not in dps:
            continue

        raw_value = dps[dp_id]
        if isinstance(raw_value, int) and raw_value == -22 and field_name in SENTINEL_OFFLINE_FIELDS:
            continue

        fields[field_name] = cast_dps_value(raw_value, target_type)

    add_temperature_fields(fields)
    add_fault_fields(fields)
    return fields


def fetch_weather_temp_f(latitude: float, longitude: float) -> float:
    query = urllib.parse.urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": "raypak-poller/1.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))

    current = payload.get("current") or {}
    temp = current.get("temperature_2m")
    if temp is None:
        raise ValueError("Open-Meteo response missing current.temperature_2m")
    return float(temp)


def escape_measurement(value: str) -> str:
    return str(value).replace(",", r"\,").replace(" ", r"\ ")


def escape_tag(value: Any) -> str:
    return str(value).replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def escape_field_string(value: Any) -> str:
    return str(value).replace("\\", r"\\").replace('"', r"\"")


def format_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value}i"
    if isinstance(value, float):
        return str(value)
    return f'"{escape_field_string(value)}"'


def format_point(
    measurement: str,
    tags: dict[str, Any],
    fields: dict[str, Any],
    timestamp_ns: int | None = None,
) -> str | None:
    clean_fields = {key: value for key, value in fields.items() if value is not None}
    if not clean_fields:
        return None

    tag_text = "".join(
        f",{escape_tag(key)}={escape_tag(value)}"
        for key, value in sorted(tags.items())
        if value is not None and value != ""
    )
    field_text = ",".join(
        f"{escape_tag(key)}={format_field_value(value)}"
        for key, value in sorted(clean_fields.items())
    )
    timestamp = timestamp_ns or time.time_ns()
    return f"{escape_measurement(measurement)}{tag_text} {field_text} {timestamp}"


def write_influx_line(config: InfluxConfig, line: str) -> None:
    query = urllib.parse.urlencode(
        {
            "org": config.org,
            "bucket": config.bucket,
            "precision": "ns",
        }
    )
    url = f"{config.url}/api/v2/write?{query}"
    body = line.encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {config.token}",
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Length": str(len(body)),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status != 204:
                detail = response.read().decode("utf-8", errors="replace").strip()
                raise InfluxWriteError(f"InfluxDB HTTP {response.status}: {detail}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise InfluxWriteError(f"InfluxDB HTTP {exc.code}: {detail}") from exc


def poll_heater(heater: tinytuya.Device) -> dict[str, Any]:
    raw = heater.status()
    dps = raw.get("dps") if isinstance(raw, dict) else None
    if not isinstance(dps, dict):
        raise ValueError(f"unexpected tinytuya response: {raw!r}")
    return map_dps_fields(dps)


def run(args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent
    device_file = Path(args.device_file)
    if not device_file.is_absolute():
        device_file = base_dir / device_file

    env_file = Path(args.env_file) if args.env_file else base_dir / DEFAULT_ENV_FILE
    if env_file and not env_file.is_absolute():
        env_file = base_dir / env_file

    device_config = load_device_config(device_file)
    influx_config = load_influx_config(env_file)
    weather_location = resolve_weather_location(args)
    heater = create_heater(device_config, persistent=args.persistent)

    weather_temp_f: float | None = None
    last_weather_fetch = 0.0

    log(
        "starting poller "
        f"device_ip={device_config.address} interval={args.interval_seconds}s "
        f"persistent={str(args.persistent).lower()} "
        f"weather_enabled={str(weather_location is not None).lower()}"
    )

    while True:
        started = time.monotonic()

        if weather_location is not None:
            try:
                if started - last_weather_fetch >= args.weather_refresh_seconds or weather_temp_f is None:
                    weather_temp_f = fetch_weather_temp_f(*weather_location)
                    last_weather_fetch = started
            except Exception as exc:
                log(f"weather_fetch_failed error={exc}")

        try:
            fields = poll_heater(heater)
            if weather_temp_f is not None:
                fields["weather_temp_f"] = weather_temp_f

            line = format_point(args.measurement, HEATER_TAGS, fields)
            if not line:
                log("poll_skipped reason=no_fields")
            elif args.dry_run:
                log(f"dry_run line={line}")
            else:
                write_influx_line(influx_config, line)
                log(f"write_ok fields={len(fields)}")
        except Exception as exc:
            log(f"poll_or_write_failed error={exc}")
            try:
                heater = create_heater(device_config, persistent=args.persistent)
                log("heater_reconnected")
            except Exception as reconnect_exc:
                log(f"heater_reconnect_failed error={reconnect_exc}")

        if args.once:
            return 0

        elapsed = time.monotonic() - started
        time.sleep(max(0.0, args.interval_seconds - elapsed))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=POLL_INTERVAL_SECONDS)
    parser.add_argument("--weather-refresh-seconds", type=float, default=WEATHER_REFRESH_SECONDS)
    parser.add_argument("--weather-latitude", type=float, default=None)
    parser.add_argument("--weather-longitude", type=float, default=None)
    parser.add_argument("--measurement", default=MEASUREMENT)
    parser.add_argument("--device-file", default=DEVICE_FILE)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--persistent", action="store_true", help="keep a persistent Tuya socket open")
    parser.add_argument("--once", action="store_true", help="poll once, write once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print line protocol instead of writing")
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(run(parse_args(sys.argv[1:])))
    except KeyboardInterrupt:
        log("stopped")
        raise SystemExit(130)
