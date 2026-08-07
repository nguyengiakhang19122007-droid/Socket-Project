"""Test client đơn giản để kiểm tra Gateway từ dòng lệnh.
Chạy: python test_gateway_client.py

Đảm bảo gateway_server.py đang chạy trước.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import websockets


GATEWAY_URL = "ws://127.0.0.1:8765"
# Phải khớp với GATEWAY_TOKEN trong gateway_server.py
TOKEN = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def new_id() -> str:
    return str(uuid.uuid4())[:8]


async def run_tests() -> None:
    print(f"Connecting to {GATEWAY_URL} ...")
    async with websockets.connect(GATEWAY_URL, max_size=10 * 1024 * 1024) as ws:

        # ── Test 1: Xác thực ────────────────────────────────────────────────
        print("\n[TEST 1] Authentication")
        await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
        resp = json.loads(await ws.recv())
        assert resp["status"] == "ok", f"Auth failed: {resp}"
        print(f"  ✓ Auth success: {resp['message']}")

        # ── Test 2: Ping / Pong ─────────────────────────────────────────────
        print("\n[TEST 2] Ping/Pong")
        rid = new_id()
        await ws.send(json.dumps({"type": "ping", "request_id": rid}))
        resp = json.loads(await ws.recv())
        assert resp["type"] == "pong", f"Expected pong, got: {resp}"
        print(f"  ✓ Pong received (request_id={resp.get('request_id')})")

        # ── Test 3: list_processes ──────────────────────────────────────────
        print("\n[TEST 3] list_processes command")
        rid = new_id()
        await ws.send(json.dumps({
            "type": "command",
            "request_id": rid,
            "module": "list_processes",
            "parameters": {},
        }))
        resp = json.loads(await ws.recv())
        print(f"  Status: {resp['status']}")
        if resp["status"] == "ok" and resp.get("data"):
            procs = resp["data"]
            print(f"  ✓ Got {len(procs)} processes. First 3:")
            for p in procs[:3]:
                print(f"      PID={p['pid']:6d}  CPU={p.get('cpu', 0):5.1f}%  {p['name']}")
        else:
            print(f"  ✗ Failed or empty: {resp.get('message')}")

        # ── Test 4: system_metrics ──────────────────────────────────────────
        print("\n[TEST 4] system_metrics command")
        rid = new_id()
        await ws.send(json.dumps({
            "type": "command",
            "request_id": rid,
            "module": "system_metrics",
            "parameters": {},
        }))
        resp = json.loads(await ws.recv())
        print(f"  Status: {resp['status']}")
        if resp["status"] == "ok":
            data = resp.get("data", {})
            uptime_h = data.get("uptime_seconds", 0) / 3600
            print(f"  ✓ RAM usage: {data.get('ram_usage_percent', '?')}%  |  Uptime: {uptime_h:.1f} hours")
        else:
            print(f"  Note: {resp.get('message')} (bình thường nếu không chạy trên Windows)")

        # ── Test 5: screen_capture (chụp 1 ảnh) ────────────────────────────
        print("\n[TEST 5] screen_capture command")
        rid = new_id()
        await ws.send(json.dumps({
            "type": "command",
            "request_id": rid,
            "module": "screen_capture",
            "parameters": {},
        }))
        resp = json.loads(await ws.recv())
        print(f"  Status: {resp['status']}")
        if resp["status"] == "ok":
            img_b64 = resp["data"].get("image_base64", "")
            size_kb = len(img_b64) * 3 // 4 // 1024
            print(f"  ✓ Screenshot received: ~{size_kb} KB")
        else:
            print(f"  Note: {resp.get('message')}")

        # ── Test 6: stream_control screen start/stop ────────────────────────
        print("\n[TEST 6] Screen livestream (3 frames)")
        rid = new_id()
        await ws.send(json.dumps({
            "type": "stream_control",
            "request_id": rid,
            "module": "screen",
            "action": "start",
        }))
        ctrl_resp = None
        segment_count = 0
        deadline = asyncio.get_event_loop().time() + 5  # chờ tối đa 5 giây
        while segment_count < 3 or ctrl_resp is None:
            if asyncio.get_event_loop().time() > deadline:
                print("  Timeout waiting for H.264 segments")
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                if isinstance(raw, bytes):
                    assert raw[0] == 0x01, f"Unknown binary packet type: {raw[0]}"
                    segment_count += 1
                    print(f"  ✓ fMP4 chunk {segment_count}: {len(raw) - 1} bytes")
                else:
                    message = json.loads(raw)
                    if message.get("type") == "stream_control_result":
                        ctrl_resp = message
                        print(f"  Stream start: {ctrl_resp['status']}")
            except asyncio.TimeoutError:
                print("  Timeout on fMP4 chunk")
                break

        # Stop stream
        await ws.send(json.dumps({
            "type": "stream_control",
            "module": "screen",
            "action": "stop",
        }))
        while True:
            raw = await ws.recv()
            if isinstance(raw, str):
                stop_resp = json.loads(raw)
                if (
                    stop_resp.get("type") == "stream_control_result"
                    and stop_resp.get("data", {}).get("status") == "stopped"
                ):
                    break
        print(f"  Stream stop: {stop_resp['status']}")

        # ── Test 7: Invalid token ────────────────────────────────────────────
        print("\n[TEST 7] Reject invalid token (new connection)")
        async with websockets.connect(GATEWAY_URL) as ws2:
            await ws2.send(json.dumps({"type": "auth", "token": "wrong_token"}))
            resp2 = json.loads(await ws2.recv())
            assert resp2["status"] == "error", f"Should have rejected bad token: {resp2}"
            print(f"  ✓ Bad token correctly rejected: {resp2['message']}")

        print("\n" + "=" * 50)
        print("All tests passed! Gateway is working correctly.")


def main() -> None:
    try:
        asyncio.run(run_tests())
    except ConnectionRefusedError:
        print(f"\n❌ Cannot connect to {GATEWAY_URL}")
        print("   Hãy chắc chắn gateway_server.py đang chạy trước!")
        sys.exit(1)
    except AssertionError as exc:
        print(f"\n❌ Test failed: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nTest interrupted.")


if __name__ == "__main__":
    main()
