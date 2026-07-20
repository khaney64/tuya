import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import raypak_poller


class PartialTelemetryRecoveryTests(unittest.TestCase):
    def test_recreates_heater_after_partial_poll_threshold(self) -> None:
        args = raypak_poller.parse_args(
            ["--once", "--partial-reconnect-polls", "1"]
        )
        device_config = SimpleNamespace(address="192.0.2.1")
        derived_config = SimpleNamespace(
            iotawatt_bucket="iotawatt",
            pool_gallons=10100.0,
            target_temp_f=None,
        )
        first_heater = object()
        replacement_heater = object()
        create_heater = Mock(side_effect=[first_heater, replacement_heater])
        messages: list[str] = []

        with (
            patch.object(raypak_poller, "load_device_config", return_value=device_config),
            patch.object(raypak_poller, "load_influx_config", return_value=object()),
            patch.object(raypak_poller, "load_derived_config", return_value=derived_config),
            patch.object(raypak_poller, "load_shelly_config", return_value=None),
            patch.object(raypak_poller, "load_iotawatt_influx_config", return_value=object()),
            patch.object(raypak_poller, "resolve_weather_location", return_value=None),
            patch.object(raypak_poller, "create_heater", create_heater),
            patch.object(
                raypak_poller,
                "poll_heater",
                return_value={"power": True, "mode": "warm", "setpoint_f": 75},
            ),
            patch.object(raypak_poller, "log", side_effect=messages.append),
        ):
            result = raypak_poller.run(args)

        self.assertEqual(result, 0)
        self.assertEqual(create_heater.call_count, 2)
        self.assertTrue(
            any(
                "heater_reconnected reason=partial_telemetry consecutive=1" in message
                for message in messages
            )
        )
        self.assertTrue(
            any(
                "names=mode,power,setpoint_f consecutive=1" in message
                for message in messages
            )
        )

    def test_reconnect_threshold_must_be_positive(self) -> None:
        args = raypak_poller.parse_args(["--partial-reconnect-polls", "0"])

        with self.assertRaisesRegex(ValueError, "must be at least 1"):
            raypak_poller.run(args)


if __name__ == "__main__":
    unittest.main()
