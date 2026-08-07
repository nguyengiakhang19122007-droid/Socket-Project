"""FFmpeg-backed low-latency H.264/fMP4 streaming helpers."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path


SCREEN_FMP4_PACKET_TYPE = 0x01
SCREEN_FMP4_MIME = 'video/mp4; codecs="avc1.42C02A"'


def build_screen_ffmpeg_command(
    *,
    ffmpeg_path: str,
    width: int,
    height: int,
    fps: float,
    bitrate: str,
    fragment_ms: int,
) -> list[str]:
    """Build an FFmpeg command that turns BGR frames into fragmented MP4."""
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("H.264 frame dimensions must be positive even numbers")

    safe_fps = max(1.0, float(fps))
    gop_size = max(1, round(safe_fps / 2.0))
    fragment_us = max(20, int(fragment_ms)) * 1000
    return [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "rawvideo",
        "-pixel_format", "bgr24",
        "-video_size", f"{width}x{height}",
        "-framerate", f"{safe_fps:g}",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-profile:v", "baseline",
        "-level:v", "4.2",
        "-pix_fmt", "yuv420p",
        "-b:v", bitrate,
        "-maxrate", bitrate,
        "-bufsize", bitrate,
        "-g", str(gop_size),
        "-keyint_min", str(gop_size),
        "-sc_threshold", "0",
        "-bf", "0",
        "-f", "mp4",
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-frag_duration", str(fragment_us),
        "-flush_packets", "1",
        "pipe:1",
    ]


def pack_screen_fmp4(payload: bytes) -> bytes:
    """Prefix an fMP4 chunk with the one-byte application packet type."""
    if not payload:
        raise ValueError("Cannot pack an empty fMP4 payload")
    return bytes((SCREEN_FMP4_PACKET_TYPE,)) + payload


class FfmpegFmp4Encoder:
    """Async FFmpeg subprocess with bounded diagnostic stderr collection."""

    def __init__(
        self,
        *,
        ffmpeg_path: str,
        width: int,
        height: int,
        fps: float,
        bitrate: str,
        fragment_ms: int,
    ) -> None:
        self.command = build_screen_ffmpeg_command(
            ffmpeg_path=ffmpeg_path,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            fragment_ms=fragment_ms,
        )
        self.expected_frame_bytes = width * height * 3
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def diagnostics(self) -> str:
        return " | ".join(self._stderr_lines)

    async def start(self) -> None:
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            executable = Path(self.command[0]).name
            raise RuntimeError(
                f"FFmpeg executable '{executable}' was not found. "
                "Install FFmpeg and add it to PATH, or set FFMPEG_PATH."
            ) from exc
        self._stderr_task = asyncio.create_task(
            self._collect_stderr(), name="screen-ffmpeg-stderr"
        )

    async def _collect_stderr(self) -> None:
        process = self._require_process()
        assert process.stderr is not None
        while line := await process.stderr.readline():
            decoded = line.decode("utf-8", errors="replace").strip()
            if decoded:
                self._stderr_lines.append(decoded)

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("FFmpeg encoder has not been started")
        return self._process

    async def write_frame(self, frame: bytes) -> None:
        if len(frame) != self.expected_frame_bytes:
            raise ValueError(
                f"Unexpected raw frame size: got {len(frame)}, "
                f"expected {self.expected_frame_bytes}"
            )
        process = self._require_process()
        if process.stdin is None or process.returncode is not None:
            raise RuntimeError(self._exit_message())
        try:
            process.stdin.write(frame)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RuntimeError(self._exit_message()) from exc

    async def read_chunk(self, size: int = 64 * 1024) -> bytes:
        process = self._require_process()
        assert process.stdout is not None
        chunk = await process.stdout.read(max(1024, int(size)))
        if not chunk:
            await process.wait()
            raise RuntimeError(self._exit_message())
        return chunk

    def _exit_message(self) -> str:
        process = self._process
        code = process.returncode if process is not None else "not started"
        detail = self.diagnostics or "no FFmpeg diagnostic output"
        return f"FFmpeg H.264 encoder stopped (exit={code}): {detail}"

    async def close(self) -> None:
        process = self._process
        if process is None:
            return

        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

        if self._stderr_task is not None:
            if not self._stderr_task.done():
                self._stderr_task.cancel()
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        self._process = None
