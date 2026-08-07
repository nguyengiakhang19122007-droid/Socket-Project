import unittest
from types import SimpleNamespace
from unittest import mock

from pynput import keyboard

import agent_main
from desktop_capture_utils import RemoteKeylogger


class KeyloggerFilteringTests(unittest.TestCase):
    def test_num_lock_is_not_added_to_buffer(self):
        keylogger = RemoteKeylogger()

        keylogger._on_press(keyboard.KeyCode.from_vk(0x90))

        self.assertEqual(keylogger.get_and_clear(), "")

    def test_named_num_lock_is_not_added_to_buffer(self):
        keylogger = RemoteKeylogger()

        keylogger._on_press(SimpleNamespace(name="num_lock", vk=None))  # type: ignore[arg-type]

        self.assertEqual(keylogger.get_and_clear(), "")

    def test_other_special_key_is_still_recorded(self):
        keylogger = RemoteKeylogger()

        keylogger._on_press(keyboard.Key.shift)

        self.assertEqual(keylogger.get_and_clear(), "[SHIFT]")


class KeyloggerIndicatorTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = agent_main.AgentOrchestrator(allowed_applications={})
        self.orchestrator.keylogger = mock.Mock()
        self.orchestrator.keylogger.is_running = False
        self.orchestrator.keylogger_indicator = mock.Mock()

    def test_start_turns_on_keylogger_and_visible_indicator(self):
        result = self.orchestrator._execute_module("keylogger", {"action": "start"})

        self.orchestrator.keylogger.start.assert_called_once()
        self.orchestrator.keylogger_indicator.start.assert_called_once()
        self.assertEqual(result, {"keylogger_status": "running"})

    def test_stop_turns_off_keylogger_and_visible_indicator(self):
        result = self.orchestrator._execute_module("keylogger", {"action": "stop"})

        self.orchestrator.keylogger.stop.assert_called_once()
        self.orchestrator.keylogger_indicator.stop.assert_called_once()
        self.assertEqual(result, {"keylogger_status": "stopped"})

    def test_permission_cleanup_stops_indicator(self):
        self.orchestrator.stop_active_feature("keylogger")

        self.orchestrator.keylogger.stop.assert_called_once()
        self.orchestrator.keylogger_indicator.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
