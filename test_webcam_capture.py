import asyncio
import unittest
from unittest import mock

import numpy as np

import agent_main
import desktop_capture_utils as capture_utils
import gateway_server


class CameraOpenTests(unittest.TestCase):
    def test_windows_falls_back_when_first_backend_has_no_frames(self):
        first_camera = mock.Mock()
        first_camera.isOpened.return_value = True
        second_camera = mock.Mock()
        second_camera.isOpened.return_value = True
        expected_frame = np.zeros((2, 3, 3), dtype=np.uint8)

        with (
            mock.patch.object(capture_utils.os, "name", "nt"),
            mock.patch.object(
                capture_utils.cv2,
                "VideoCapture",
                side_effect=[first_camera, second_camera],
            ) as video_capture,
            mock.patch.object(
                capture_utils,
                "_read_camera_frame",
                side_effect=[None, expected_frame],
            ),
        ):
            camera, frame = capture_utils._open_working_camera(0, fps=30)

        self.assertIs(camera, second_camera)
        self.assertIs(frame, expected_frame)
        first_camera.release.assert_called_once()
        second_camera.release.assert_not_called()
        self.assertEqual(video_capture.call_count, 2)

    def test_stream_uses_warmed_up_frame_before_reading_again(self):
        camera = mock.Mock()
        frame = np.zeros((2, 3, 3), dtype=np.uint8)
        encoded = np.array([1, 2, 3], dtype=np.uint8)

        with (
            mock.patch.object(
                capture_utils, "_open_working_camera", return_value=(camera, frame)
            ),
            mock.patch.object(
                capture_utils.cv2, "imencode", return_value=(True, encoded)
            ),
        ):
            stream = capture_utils.CameraStreamCapture(fps=30)
            result = stream.read_jpeg()

        self.assertEqual(result, b"\x01\x02\x03")
        camera.read.assert_not_called()


class WebcamLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_is_set_only_after_first_frame_is_sent(self):
        websocket = mock.AsyncMock()
        stream = mock.Mock()
        stream.read_jpeg.return_value = b"jpeg"
        ready = asyncio.get_running_loop().create_future()

        with mock.patch.object(gateway_server, "CameraStreamCapture", return_value=stream):
            task = asyncio.create_task(
                gateway_server._webcam_stream_loop(websocket, 0, 30, ready)
            )
            await asyncio.wait_for(ready, timeout=1)
            task.cancel()
            await task

        websocket.send.assert_awaited()
        stream.close.assert_called_once()


class SnapshotPayloadTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = agent_main.AgentOrchestrator(allowed_applications={})

    def test_screen_snapshot_is_bounded_jpeg_with_mime(self):
        with mock.patch.object(
            agent_main, "capture_primary_screen_jpeg", return_value=b"screen-jpeg"
        ):
            result = self.orchestrator._execute_module("screen_capture", {})

        self.assertEqual(result["mime"], "image/jpeg")
        self.assertEqual(result["image_base64"], "c2NyZWVuLWpwZWc=")

    def test_webcam_snapshot_is_bounded_jpeg_with_mime(self):
        with mock.patch.object(
            agent_main, "capture_camera_jpeg", return_value=b"camera-jpeg"
        ) as capture:
            result = self.orchestrator._execute_module(
                "camera_capture", {"camera_index": 2}
            )

        capture.assert_called_once_with(2)
        self.assertEqual(result["mime"], "image/jpeg")
        self.assertEqual(result["image_base64"], "Y2FtZXJhLWpwZWc=")


if __name__ == "__main__":
    unittest.main()
