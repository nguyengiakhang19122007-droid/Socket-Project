import asyncio
import os
import unittest
from unittest import mock

import numpy as np

import desktop_capture_utils
import gateway_server
from video_stream_utils import (
    FfmpegFmp4Encoder,
    SCREEN_FMP4_MIME,
    SCREEN_FMP4_PACKET_TYPE,
    build_screen_ffmpeg_command,
    pack_screen_fmp4,
)


class FfmpegCommandTests(unittest.TestCase):
    def test_command_uses_low_latency_fragmented_mp4(self):
        command = build_screen_ffmpeg_command(
            ffmpeg_path="ffmpeg",
            width=1280,
            height=720,
            fps=60,
            bitrate="4M",
            fragment_ms=100,
        )

        self.assertIn("libx264", command)
        self.assertIn("zerolatency", command)
        self.assertIn("+frag_keyframe+empty_moov+default_base_moof", command)
        self.assertEqual(command[command.index("-g") + 1], "30")
        self.assertEqual(command[command.index("-frag_duration") + 1], "100000")
        self.assertEqual(command[-1], "pipe:1")

    def test_h264_dimensions_must_be_even(self):
        with self.assertRaises(ValueError):
            build_screen_ffmpeg_command(
                ffmpeg_path="ffmpeg",
                width=1279,
                height=720,
                fps=60,
                bitrate="4M",
                fragment_ms=100,
            )

    def test_binary_packet_has_application_type_prefix(self):
        packet = pack_screen_fmp4(b"ftyp")
        self.assertEqual(packet[0], SCREEN_FMP4_PACKET_TYPE)
        self.assertEqual(packet[1:], b"ftyp")


class RawScreenCaptureTests(unittest.TestCase):
    def test_capture_produces_fixed_even_bgr24_frame(self):
        fake_mss = mock.Mock()
        fake_mss.monitors = [{}, {"width": 7, "height": 5}]
        fake_mss.grab.return_value = np.zeros((5, 7, 4), dtype=np.uint8)

        with mock.patch.object(desktop_capture_utils.mss, "mss", return_value=fake_mss):
            capture = desktop_capture_utils.ScreenRawCapture(max_width=0)
            frame = capture.read_bgr24()
            capture.close()

        self.assertEqual((capture.width, capture.height), (6, 4))
        self.assertEqual(len(frame), 6 * 4 * 3)
        fake_mss.close.assert_called_once()


class EncoderFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_ffmpeg_has_actionable_error(self):
        encoder = FfmpegFmp4Encoder(
            ffmpeg_path="missing-ffmpeg",
            width=1280,
            height=720,
            fps=60,
            bitrate="4M",
            fragment_ms=100,
        )
        with (
            mock.patch.object(
                asyncio,
                "create_subprocess_exec",
                side_effect=FileNotFoundError,
            ),
            self.assertRaisesRegex(RuntimeError, "Install FFmpeg"),
        ):
            await encoder.start()

    @unittest.skipUnless(
        os.getenv("FFMPEG_INTEGRATION_PATH"),
        "Set FFMPEG_INTEGRATION_PATH to run the real encoder test",
    )
    async def test_real_ffmpeg_outputs_fragmented_mp4(self):
        encoder = FfmpegFmp4Encoder(
            ffmpeg_path=os.environ["FFMPEG_INTEGRATION_PATH"],
            width=64,
            height=48,
            fps=30,
            bitrate="500k",
            fragment_ms=20,
        )
        await encoder.start()
        writer = asyncio.create_task(self._write_test_frames(encoder))
        output = bytearray()
        try:
            while b"moof" not in output or b"mdat" not in output:
                output.extend(await asyncio.wait_for(encoder.read_chunk(), timeout=5))
            await writer
        finally:
            if not writer.done():
                writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            await encoder.close()

        self.assertIn(b"ftyp", output)
        self.assertIn(b"moov", output)
        self.assertIn(b"moof", output)
        self.assertIn(b"mdat", output)
        avcc = output.index(b"avcC") + 4
        codec = "avc1." + "".join(f"{value:02X}" for value in output[avcc + 1:avcc + 4])
        self.assertIn(codec, SCREEN_FMP4_MIME)

    async def _write_test_frames(self, encoder: FfmpegFmp4Encoder) -> None:
        frame = bytes(64 * 48 * 3)
        for _ in range(15):
            await encoder.write_frame(frame)


class ScreenStreamLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_stream_sends_binary_fmp4(self):
        websocket = mock.AsyncMock()
        capture = mock.Mock(width=4, height=2)
        capture.read_bgr24.return_value = bytes(4 * 2 * 3)
        encoder = mock.Mock()
        encoder.start = mock.AsyncMock()
        encoder.write_frame = mock.AsyncMock()
        output_gate = asyncio.Event()
        chunk_count = 0

        async def read_chunk():
            nonlocal chunk_count
            chunk_count += 1
            if chunk_count == 1:
                return b"ftyp"
            await output_gate.wait()
            return b"moof"

        encoder.read_chunk = mock.AsyncMock(side_effect=read_chunk)
        encoder.close = mock.AsyncMock()
        ready = asyncio.get_running_loop().create_future()

        with (
            mock.patch.object(gateway_server, "ScreenRawCapture", return_value=capture),
            mock.patch.object(gateway_server, "FfmpegFmp4Encoder", return_value=encoder),
        ):
            task = asyncio.create_task(
                gateway_server._screen_stream_loop(websocket, 60, ready)
            )
            await asyncio.wait_for(ready, timeout=1)
            task.cancel()
            await task

        first_packet = websocket.send.await_args_list[0].args[0]
        self.assertIsInstance(first_packet, bytes)
        self.assertEqual(first_packet[0], SCREEN_FMP4_PACKET_TYPE)
        encoder.close.assert_awaited_once()
        capture.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
