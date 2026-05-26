#!/usr/bin/env python3
"""Poll Raypak Crosswind Tuya telemetry and write it to InfluxDB."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
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
FAULT_SAMPLE_SECONDS = 2.0
FAULT_SAMPLE_ATTEMPTS = 5
WEATHER_REFRESH_SECONDS = 300
MEASUREMENT = "pool_heater"
DEVICE_FILE = "devices.json"
DEFAULT_ENV_FILE = "influxdb-env.ps1"
POOL_GALLONS = 10100.0
KWH_PRICE = 0.15828
PUMP_WATT_THRESHOLD = 50.0
HEATER_WATT_THRESHOLD = 200.0
DERIVED_INTERVAL_SECONDS = 60.0
OBSERVED_WINDOW_SECONDS = 3600.0
OBSERVED_MIN_WINDOW_SECONDS = 1800.0
IOTAWATT_BUCKET = "iotawatt"

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
FAULT_FIELD_NAMES = ("fault1_raw", "fault2_raw", "fault_active", "fault_codes")
MIN_FULL_TELEMETRY_FIELDS = 10


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


@dataclass(frozen=True)
class DerivedConfig:
    pool_gallons: float
    target_temp_f: float | None
    kwh_price: float
    pump_watt_threshold: float
    heater_watt_threshold: float
    interval_seconds: float
    iotawatt_bucket: str
    has_cover: bool


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


def load_iotawatt_influx_config(default_config: InfluxConfig, bucket_override: str) -> InfluxConfig:
    url = os.getenv("IOTAWATT_INFLUXDB_URL") or os.getenv("RAYPAK_IOTAWATT_INFLUXDB_URL")
    org = os.getenv("IOTAWATT_INFLUXDB_ORG") or os.getenv("RAYPAK_IOTAWATT_INFLUXDB_ORG")
    token = os.getenv("IOTAWATT_INFLUXDB_TOKEN") or os.getenv("RAYPAK_IOTAWATT_INFLUXDB_TOKEN")
    bucket = os.getenv("IOTAWATT_INFLUXDB_BUCKET") or bucket_override

    return InfluxConfig(
        url=(url or default_config.url).rstrip("/"),
        org=org or default_config.org,
        bucket=bucket,
        token=token or default_config.token,
    )


def env_float(name: str) -> float | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None

    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def arg_or_env_float(arg_value: float | None, env_name: str, default: float) -> float:
    if arg_value is not None:
        return float(arg_value)
    env_value = env_float(env_name)
    return default if env_value is None else env_value


def load_derived_config(args: argparse.Namespace) -> DerivedConfig:
    pool_gallons = arg_or_env_float(args.pool_gallons, "RAYPAK_POOL_GALLONS", POOL_GALLONS)
    target_temp_f = args.target_temp_f
    if target_temp_f is None:
        target_temp_f = env_float("RAYPAK_TARGET_TEMP_F")
    kwh_price = arg_or_env_float(args.kwh_price, "RAYPAK_KWH_PRICE", KWH_PRICE)
    pump_watt_threshold = arg_or_env_float(
        args.pump_watt_threshold,
        "RAYPAK_PUMP_WATT_THRESHOLD",
        PUMP_WATT_THRESHOLD,
    )
    heater_watt_threshold = arg_or_env_float(
        args.heater_watt_threshold,
        "RAYPAK_HEATER_WATT_THRESHOLD",
        HEATER_WATT_THRESHOLD,
    )
    interval_seconds = arg_or_env_float(
        args.derived_interval_seconds,
        "RAYPAK_DERIVED_INTERVAL_SECONDS",
        DERIVED_INTERVAL_SECONDS,
    )
    iotawatt_bucket = args.iotawatt_bucket or os.getenv("RAYPAK_IOTAWATT_BUCKET") or IOTAWATT_BUCKET
    has_cover = env_bool("RAYPAK_HAS_COVER", True)

    if pool_gallons <= 0:
        raise ValueError("pool gallons must be greater than 0")
    if interval_seconds <= 0:
        raise ValueError("derived interval seconds must be greater than 0")

    return DerivedConfig(
        pool_gallons=pool_gallons,
        target_temp_f=target_temp_f,
        kwh_price=kwh_price,
        pump_watt_threshold=pump_watt_threshold,
        heater_watt_threshold=heater_watt_threshold,
        interval_seconds=interval_seconds,
        iotawatt_bucket=iotawatt_bucket,
        has_cover=has_cover,
    )


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


def flux_string(value: str) -> str:
    return json.dumps(value)


def query_influx_csv(config: InfluxConfig, flux_query: str) -> list[dict[str, str]]:
    url = f"{config.url}/api/v2/query?{urllib.parse.urlencode({'org': config.org})}"
    body = json.dumps({"query": flux_query, "type": "flux"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {config.token}",
            "Accept": "application/csv",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise InfluxWriteError(f"InfluxDB query HTTP {exc.code}: {detail}") from exc

    csv_text = "\n".join(line for line in text.splitlines() if line and not line.startswith("#"))
    if not csv_text:
        return []
    return list(csv.DictReader(io.StringIO(csv_text)))


def query_measurement_mean(
    config: InfluxConfig,
    bucket: str,
    measurement: str,
    start: str,
) -> float | None:
    flux_query = "\n".join(
        [
            f"from(bucket: {flux_string(bucket)})",
            f"  |> range(start: {start})",
            f"  |> filter(fn: (r) => r._measurement == {flux_string(measurement)})",
            "  |> mean()",
        ]
    )
    rows = query_influx_csv(config, flux_query)
    for row in rows:
        value = row.get("_value")
        if value not in (None, ""):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def read_heater_dps(heater: tinytuya.Device) -> dict[str, Any]:
    raw = heater.status()
    dps = raw.get("dps") if isinstance(raw, dict) else None
    if not isinstance(dps, dict):
        raise ValueError(f"unexpected tinytuya response: {raw!r}")
    return dps


def poll_heater(heater: tinytuya.Device) -> dict[str, Any]:
    return map_dps_fields(read_heater_dps(heater))


def poll_fault_fields(heater: tinytuya.Device) -> dict[str, Any]:
    fields = map_dps_fields(read_heater_dps(heater))
    return {name: fields[name] for name in FAULT_FIELD_NAMES if name in fields}


def is_full_telemetry(fields: dict[str, Any]) -> bool:
    return "water_in_f" in fields and "setpoint_f" in fields and len(fields) >= MIN_FULL_TELEMETRY_FIELDS


def numeric_field(fields: dict[str, Any], name: str) -> float | None:
    value = fields.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return float(value)


def heat_capacity_btu(ambient_f: float) -> float:
    raw = (1105.0 * ambient_f) - 30750.0
    if ambient_f > 80.0:
        return 57650.0
    return max(5000.0, raw)


def interpolate(points: list[tuple[float, float]], x_value: float) -> float:
    if x_value <= points[0][0]:
        x0, y0 = points[0]
        x1, y1 = points[1]
    elif x_value >= points[-1][0]:
        x0, y0 = points[-2]
        x1, y1 = points[-1]
    else:
        for index in range(len(points) - 1):
            x0, y0 = points[index]
            x1, y1 = points[index + 1]
            if x0 <= x_value <= x1:
                break
    slope = (y1 - y0) / (x1 - x0)
    return y0 + ((x_value - x0) * slope)


def cool_capacity_btu(ambient_f: float) -> float:
    points = [(70.0, 45000.0), (80.0, 40000.0), (90.0, 32000.0), (100.0, 25000.0)]
    return max(10000.0, interpolate(points, ambient_f))


def expected_capacity_btu(ambient_f: float, mode: str, needs_cooling: bool) -> float:
    if mode == "cool" or (mode == "smart" and needs_cooling):
        return cool_capacity_btu(ambient_f)
    return heat_capacity_btu(ambient_f)


def active_load(fields: dict[str, Any]) -> bool:
    speed_pct = numeric_field(fields, "speed_pct") or 0.0
    dc_fan_rpm = numeric_field(fields, "dc_fan_rpm") or 0.0
    comp_freq_hz = numeric_field(fields, "comp_freq_hz") or 0.0
    return bool(fields.get("comp_relay")) or speed_pct > 0.0 or dc_fan_rpm > 0.0 or comp_freq_hz > 0.0


def net_capacity_btu(
    gross_btu: float,
    water_f: float,
    ambient_f: float,
    needs_cooling: bool,
    has_cover: bool,
) -> float:
    delta_scale = min(2.0, abs(water_f - ambient_f) / 20.0)
    factor = 0.12 if has_cover and needs_cooling else 0.30 if needs_cooling else 0.15 if has_cover else 0.40
    return max(0.0, gross_btu * (1.0 - (factor * delta_scale)))


def append_water_sample(
    samples: list[tuple[float, float]],
    now: float,
    water_f: float | None,
    valid: bool,
) -> None:
    if water_f is not None and valid:
        samples.append((now, water_f))
    cutoff = now - (OBSERVED_WINDOW_SECONDS * 2.0)
    del samples[: len([sample for sample in samples if sample[0] < cutoff])]


def observed_btu_per_hr(
    samples: list[tuple[float, float]],
    pool_gallons: float,
) -> float | None:
    if len(samples) < 2:
        return None
    latest_time, latest_temp = samples[-1]
    earliest_time, earliest_temp = samples[0]
    for sample_time, sample_temp in samples:
        if latest_time - sample_time <= OBSERVED_WINDOW_SECONDS:
            earliest_time, earliest_temp = sample_time, sample_temp
            break
    elapsed = latest_time - earliest_time
    if elapsed < OBSERVED_MIN_WINDOW_SECONDS:
        return None
    delta_f = latest_temp - earliest_temp
    return (delta_f / elapsed) * 3600.0 * pool_gallons * 8.33


def compute_derived_fields(
    fields: dict[str, Any],
    config: DerivedConfig,
    now: float,
    water_samples: list[tuple[float, float]],
    heater_watts: float | None,
    pump_watts: float | None,
) -> dict[str, Any]:
    water_f = numeric_field(fields, "water_in_f")
    weather_f = numeric_field(fields, "weather_temp_f")
    ambient_f = weather_f if weather_f is not None else numeric_field(fields, "heater_ambient_f")
    mode = str(fields.get("mode") or "unknown").lower()

    pump_valid = bool(fields.get("pump_relay")) or (
        pump_watts is not None and pump_watts >= config.pump_watt_threshold
    )
    reading_valid = pump_valid or active_load(fields)
    append_water_sample(water_samples, now, water_f, reading_valid)

    derived: dict[str, Any] = {
        "target_temp_f": config.target_temp_f,
        "pool_gallons": config.pool_gallons,
        "pump_watts": pump_watts,
        "heater_watts": heater_watts,
        "pool_reading_valid": reading_valid,
        "active_load": active_load(fields),
    }

    if water_f is None or ambient_f is None:
        derived.update({"eta_seconds": -2.0, "cost_to_target_usd": -2.0, "mode_sanity": 2, "mode_sanity_text": "Invalid reading"})
        return derived

    target_f = config.target_temp_f
    if target_f is None:
        target_f = numeric_field(fields, "setpoint_f")
    if target_f is None:
        derived.update({"eta_seconds": -2.0, "cost_to_target_usd": -2.0, "mode_sanity": 2, "mode_sanity_text": "Invalid setpoint"})
        return derived

    derived["target_temp_f"] = target_f
    needs_heating = water_f < target_f
    needs_cooling = water_f > target_f
    mode_mismatch = (mode == "warm" and needs_cooling) or (mode == "cool" and needs_heating)
    gross_btu = expected_capacity_btu(ambient_f, mode, needs_cooling)
    observed_btu = observed_btu_per_hr(water_samples, config.pool_gallons)

    derived["available_capacity_btu_hr"] = gross_btu
    derived["capacity_vs_rated_pct"] = (gross_btu / 57650.0) * 100.0
    derived["observed_btu_hr"] = observed_btu
    if observed_btu is not None and heater_watts is not None and heater_watts >= config.heater_watt_threshold:
        derived["observed_cop"] = abs(observed_btu) / ((heater_watts / 1000.0) * 3412.0)

    if not reading_valid:
        derived.update({"eta_seconds": -2.0, "cost_to_target_usd": -2.0, "mode_sanity": 2, "mode_sanity_text": "Invalid reading"})
        return derived
    if not needs_heating and not needs_cooling:
        derived.update({"eta_seconds": 0.0, "cost_to_target_usd": 0.0, "mode_sanity": 0, "mode_sanity_text": "At target"})
        return derived
    if mode_mismatch:
        derived.update({"eta_seconds": -1.0, "cost_to_target_usd": -1.0, "mode_sanity": 1, "mode_sanity_text": "Mode mismatch"})
        return derived

    net_btu = net_capacity_btu(gross_btu, water_f, ambient_f, needs_cooling, config.has_cover)
    rate_f_per_hr = net_btu / (config.pool_gallons * 8.33)
    if rate_f_per_hr <= 0.0:
        derived.update({"eta_seconds": -2.0, "cost_to_target_usd": -2.0, "mode_sanity": 3, "mode_sanity_text": "Cannot reach target"})
        return derived

    eta_seconds = min((abs(target_f - water_f) / rate_f_per_hr) * 3600.0, 999.0 * 3600.0)
    derived["eta_seconds"] = eta_seconds
    derived["mode_sanity"] = 0
    derived["mode_sanity_text"] = "OK"
    if heater_watts is not None and heater_watts > 0.0:
        derived["cost_to_target_usd"] = (eta_seconds / 3600.0) * (heater_watts / 1000.0) * config.kwh_price
    return derived


def write_fields(
    args: argparse.Namespace,
    influx_config: InfluxConfig,
    fields: dict[str, Any],
    kind: str,
) -> None:
    line = format_point(args.measurement, HEATER_TAGS, fields)
    if not line:
        log(f"{kind}_skipped reason=no_fields")
    elif args.dry_run:
        log(f"dry_run kind={kind} line={line}")
    else:
        write_influx_line(influx_config, line)
        log(f"write_ok kind={kind} fields={len(fields)}")


def merge_fault_sample(
    heater: tinytuya.Device,
    fields: dict[str, Any],
    sample_seconds: float,
    sample_attempts: int,
) -> None:
    if fields.get("fault_active") or sample_seconds <= 0 or sample_attempts <= 0:
        return

    for attempt in range(1, sample_attempts + 1):
        time.sleep(sample_seconds)
        fault_fields = poll_fault_fields(heater)
        if fault_fields.get("fault_active"):
            fields.update(fault_fields)
            log(f"fault_sample_captured attempt={attempt} fault_codes={fault_fields.get('fault_codes')}")
            return


def run(args: argparse.Namespace) -> int:
    if args.interval_seconds <= 0:
        raise ValueError("--interval-seconds must be greater than 0")
    if args.fault_sample_seconds < 0:
        raise ValueError("--fault-sample-seconds must be 0 or greater")
    if 0 < args.fault_sample_seconds < 1:
        raise ValueError("--fault-sample-seconds must be at least 1 second")
    if args.fault_sample_attempts < 0:
        raise ValueError("--fault-sample-attempts must be 0 or greater")

    base_dir = Path(__file__).resolve().parent
    device_file = Path(args.device_file)
    if not device_file.is_absolute():
        device_file = base_dir / device_file

    env_file = Path(args.env_file) if args.env_file else base_dir / DEFAULT_ENV_FILE
    if env_file and not env_file.is_absolute():
        env_file = base_dir / env_file

    device_config = load_device_config(device_file)
    influx_config = load_influx_config(env_file)
    derived_config = load_derived_config(args)
    iotawatt_influx_config = load_iotawatt_influx_config(influx_config, derived_config.iotawatt_bucket)
    weather_location = resolve_weather_location(args)
    effective_persistent = args.persistent
    heater = create_heater(device_config, persistent=effective_persistent)

    weather_temp_f: float | None = None
    last_weather_fetch = 0.0
    last_derived_write = 0.0
    water_samples: list[tuple[float, float]] = []

    log(
        "starting poller "
        f"device_ip={device_config.address} interval={args.interval_seconds}s "
        f"fault_sample={args.fault_sample_seconds}s "
        f"fault_attempts={args.fault_sample_attempts} "
        f"persistent={str(effective_persistent).lower()} "
        f"pool_gallons={derived_config.pool_gallons:g} "
        f"target_temp_f={derived_config.target_temp_f if derived_config.target_temp_f is not None else 'setpoint'} "
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
            if not is_full_telemetry(fields):
                log(f"poll_skipped reason=partial_telemetry fields={len(fields)}")
                if args.once:
                    return 0
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, args.interval_seconds - elapsed))
                continue

            merge_fault_sample(
                heater,
                fields,
                args.fault_sample_seconds,
                args.fault_sample_attempts,
            )
            if weather_temp_f is not None:
                fields["weather_temp_f"] = weather_temp_f

            write_fields(args, influx_config, fields, "telemetry")
            if started - last_derived_write >= derived_config.interval_seconds or args.once:
                heater_watts: float | None = None
                pump_watts: float | None = None
                try:
                    heater_watts = query_measurement_mean(
                        iotawatt_influx_config,
                        iotawatt_influx_config.bucket,
                        "pool_heater",
                        "-10m",
                    )
                    pump_watts = query_measurement_mean(
                        iotawatt_influx_config,
                        iotawatt_influx_config.bucket,
                        "pump",
                        "-10m",
                    )
                except Exception as exc:
                    log(f"derived_power_query_failed error={exc}")

                derived_fields = compute_derived_fields(
                    fields,
                    derived_config,
                    started,
                    water_samples,
                    heater_watts,
                    pump_watts,
                )
                write_fields(args, influx_config, derived_fields, "derived")
                last_derived_write = started
        except Exception as exc:
            log(f"poll_or_write_failed kind=telemetry error={exc}")
            try:
                heater = create_heater(device_config, persistent=effective_persistent)
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
    parser.add_argument("--fault-sample-seconds", type=float, default=FAULT_SAMPLE_SECONDS)
    parser.add_argument("--fault-sample-attempts", type=int, default=FAULT_SAMPLE_ATTEMPTS)
    parser.add_argument("--weather-refresh-seconds", type=float, default=WEATHER_REFRESH_SECONDS)
    parser.add_argument("--weather-latitude", type=float, default=None)
    parser.add_argument("--weather-longitude", type=float, default=None)
    parser.add_argument("--pool-gallons", type=float, default=None)
    parser.add_argument("--target-temp-f", type=float, default=None)
    parser.add_argument("--kwh-price", type=float, default=None)
    parser.add_argument("--pump-watt-threshold", type=float, default=None)
    parser.add_argument("--heater-watt-threshold", type=float, default=None)
    parser.add_argument("--derived-interval-seconds", type=float, default=None)
    parser.add_argument("--iotawatt-bucket", default=None)
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
