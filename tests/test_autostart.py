import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import saymore.win.autostart as autostart


class AutostartTest(unittest.TestCase):
    def test_run_entry_disabled_by_windows_is_not_enabled(self):
        winreg = Mock()
        run_key = object()
        winreg.QueryValueEx.return_value = ('"C:\\Saymore\\Saymore.exe"', 1)

        with (
            patch.object(autostart, "_open_run", return_value=(winreg, run_key)),
            patch.object(autostart, "_is_startup_approved", return_value=False),
        ):
            self.assertFalse(autostart.is_enabled())

    def test_startup_approved_03_is_disabled(self):
        winreg = Mock()
        approval_key = object()
        winreg.QueryValueEx.return_value = (b"\x03" + b"\x00" * 11, 3)

        with patch.object(
            autostart,
            "_open_startup_approved",
            return_value=(winreg, approval_key),
        ):
            self.assertFalse(autostart._is_startup_approved())

    def test_enable_clears_stale_windows_disabled_marker(self):
        winreg = Mock()
        run_key = object()

        with (
            patch.object(autostart, "_open_run", return_value=(winreg, run_key)),
            patch.object(autostart, "_clear_startup_approved") as clear,
        ):
            ok, _ = autostart.enable()

        self.assertTrue(ok)
        clear.assert_called_once_with()

    def test_clear_startup_approved_deletes_saymore_state(self):
        winreg = Mock()
        approval_key = object()

        with patch.object(
            autostart,
            "_open_startup_approved",
            return_value=(winreg, approval_key),
        ):
            autostart._clear_startup_approved()

        winreg.DeleteValue.assert_called_once_with(approval_key, autostart.VALUE_NAME)
        winreg.CloseKey.assert_called_once_with(approval_key)

    def test_installer_clears_stale_windows_disabled_marker(self):
        setup = (Path(__file__).parents[1] / "installer/setup.iss").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\Run"',
            setup,
        )
        self.assertIn('ValueName: "Saymore"; Tasks: autostart; Flags: deletevalue', setup)


if __name__ == "__main__":
    unittest.main()
