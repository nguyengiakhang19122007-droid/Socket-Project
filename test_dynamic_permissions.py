from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_security
from agent_security import SecurityController
from gateway_server import GatewayState


class DynamicPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        directory = Path(self.temp_dir.name)
        self.path_patches = [
            patch.object(agent_security, "APP_DIRECTORY", directory),
            patch.object(agent_security, "CONSENT_FILE", directory / "authorization.json"),
            patch.object(agent_security, "AUDIT_LOG", directory / "agent_audit.log"),
            patch.object(agent_security, "PERMISSIONS_FILE", directory / "feature_permissions.json"),
        ]
        for item in self.path_patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.path_patches):
            item.stop()
        self.temp_dir.cleanup()

    def test_revoked_feature_is_denied_before_sensitive_consent(self) -> None:
        controller = SecurityController()
        controller.ensure_initial_consent = lambda: True
        controller.set_feature_permission("screen", False)

        with patch.object(agent_security, "_message_box", side_effect=AssertionError("must not ask consent")):
            result = controller.authorize_request({
                "request_id": "screen-1",
                "module": "screen_capture",
                "parameters": {},
            })

        self.assertIsNone(result)

    def test_policy_is_persisted_and_restored(self) -> None:
        first = SecurityController()
        first.set_feature_permission("files", False)
        second = SecurityController()

        self.assertFalse(second.permission_snapshot()["files"]["enabled"])
        stored = json.loads(agent_security.PERMISSIONS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], 1)

    def test_stop_action_remains_available_for_cleanup(self) -> None:
        controller = SecurityController()
        controller.ensure_initial_consent = lambda: True
        controller.set_feature_permission("keylogger", False)

        with patch.object(agent_security, "_message_box", return_value=True):
            result = controller.authorize_request({
                "request_id": "stop-1",
                "module": "keylogger",
                "parameters": {"action": "stop"},
            })

        self.assertIsNotNone(result)

    def test_batch_revoke_is_written_as_one_policy_snapshot(self) -> None:
        controller = SecurityController()
        snapshot = controller.set_feature_permissions({"screen": False, "webcam": False})

        self.assertFalse(snapshot["screen"]["enabled"])
        self.assertFalse(snapshot["webcam"]["enabled"])


class _FakeSecurity:
    def __init__(self) -> None:
        self.enabled = True

    def set_feature_permissions(self, updates: dict[str, bool]):
        self.enabled = all(updates.values())
        return {feature: {"enabled": enabled} for feature, enabled in updates.items()}


class _FakeOrchestrator:
    def __init__(self) -> None:
        self.security = _FakeSecurity()
        self.stopped: list[str] = []

    def stop_active_feature(self, feature: str) -> None:
        self.stopped.append(feature)


class ActiveCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_revoking_screen_cancels_all_active_streams(self) -> None:
        orchestrator = _FakeOrchestrator()
        state = GatewayState("token", orchestrator)  # type: ignore[arg-type]
        stream = asyncio.create_task(asyncio.sleep(60))
        state._screen_tasks[object()] = stream  # type: ignore[index]

        await state.set_feature_permissions({"screen": False})

        self.assertTrue(stream.cancelled())
        self.assertEqual(state._screen_tasks, {})
        self.assertIn("screen", orchestrator.stopped)


if __name__ == "__main__":
    unittest.main()
