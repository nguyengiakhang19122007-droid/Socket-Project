from __future__ import annotations

import unittest
from unittest import mock

import windows_system_control
from windows_system_control import WindowsSystemError


class SleepSystemTests(unittest.TestCase):
    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control, "_request_windows_suspend")
    @mock.patch.object(windows_system_control, "_set_hybrid_sleep_values")
    @mock.patch.object(windows_system_control, "_read_hybrid_sleep_values")
    def test_sleep_disables_hybrid_then_restores_power_plan(
        self, read_values, set_values, request_suspend, _require_windows
    ):
        read_values.side_effect = [(1, 0), (0, 0)]

        windows_system_control.sleep_system(confirm=True)

        self.assertEqual(set_values.call_args_list, [mock.call(0, 0), mock.call(1, 0)])
        request_suspend.assert_called_once_with()

    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control, "_request_windows_suspend")
    @mock.patch.object(windows_system_control, "_set_hybrid_sleep_values")
    @mock.patch.object(windows_system_control, "_read_hybrid_sleep_values")
    def test_sleep_restores_power_plan_when_suspend_fails(
        self, read_values, set_values, request_suspend, _require_windows
    ):
        read_values.side_effect = [(1, 1), (0, 0)]
        request_suspend.side_effect = WindowsSystemError("Suspend failed")

        with self.assertRaisesRegex(WindowsSystemError, "Suspend failed"):
            windows_system_control.sleep_system(confirm=True)

        self.assertEqual(set_values.call_args_list, [mock.call(0, 0), mock.call(1, 1)])

    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control, "_read_hybrid_sleep_values")
    def test_sleep_still_requires_confirmation(self, read_values, _require_windows):
        with self.assertRaises(PermissionError):
            windows_system_control.sleep_system(confirm=False)

        read_values.assert_not_called()

    @mock.patch.object(windows_system_control, "_run_power_command")
    def test_hybrid_values_are_read_without_localized_labels(self, run_command):
        run_command.return_value = mock.Mock(
            stdout="AC value: 0x00000001\nDC value: 0x00000000\n"
        )

        self.assertEqual(windows_system_control._read_hybrid_sleep_values(), (1, 0))

    @mock.patch.object(windows_system_control, "_run_power_command")
    def test_suspend_command_never_requests_hibernate(self, run_command):
        windows_system_control._request_windows_suspend()

        script = run_command.call_args.args[0][-1]
        self.assertIn("[System.Windows.Forms.PowerState]::Suspend", script)
        self.assertNotIn("[System.Windows.Forms.PowerState]::Hibernate", script)


if __name__ == "__main__":
    unittest.main()
