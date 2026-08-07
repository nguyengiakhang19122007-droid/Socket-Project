from __future__ import annotations

import unittest
from unittest import mock

import windows_system_control
from windows_system_control import WindowsSystemError


class SleepSystemTests(unittest.TestCase):
    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control.subprocess, "run")
    def test_sleep_uses_suspend_instead_of_hibernate(self, run, _require_windows):
        windows_system_control.sleep_system(confirm=True)

        command = run.call_args.args[0]
        script = command[-1]
        self.assertEqual(command[:4], [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
        ])
        self.assertIn("[System.Windows.Forms.PowerState]::Suspend", script)
        self.assertNotIn("[System.Windows.Forms.PowerState]::Hibernate", script)
        run.assert_called_once_with(
            command,
            check=True,
            shell=False,
            capture_output=True,
            text=True,
        )

    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control.subprocess, "run")
    def test_sleep_reports_powershell_failure(self, run, _require_windows):
        run.side_effect = OSError("PowerShell unavailable")

        with self.assertRaisesRegex(WindowsSystemError, "Windows Sleep command failed"):
            windows_system_control.sleep_system(confirm=True)

    @mock.patch.object(windows_system_control, "_require_windows")
    @mock.patch.object(windows_system_control.subprocess, "run")
    def test_sleep_still_requires_confirmation(self, run, _require_windows):
        with self.assertRaises(PermissionError):
            windows_system_control.sleep_system(confirm=False)

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
