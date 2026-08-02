# Role 2 – Network & Gateway Architect: Hướng Dẫn Hoàn Chỉnh

## Những file bạn (Huy) phụ trách

| File | Vai trò |
|------|---------|
| [gateway_server.py](file:///Users/giahuy/PycharmProjects/Socket-Project/gateway_server.py) | **File chính của bạn** – WebSocket Gateway Server |
| [test_gateway_client.py](file:///Users/giahuy/PycharmProjects/Socket-Project/test_gateway_client.py) | Test client để kiểm tra gateway |
| [.env.example](file:///Users/giahuy/PycharmProjects/Socket-Project/.env.example) | Template cấu hình môi trường |

---

## Kiến Trúc Tổng Quan

```
┌─────────────────────────────────────┐        LAN (WebSocket)
│        MÁY AGENT (Windows)          │◄──────────────────────────►  Web App (Máy Khang)
│                                     │  ws://192.168.x.x:8765
│  ┌─────────────────────────────┐    │
│  │     gateway_server.py       │    │  Protocol:
│  │  (WebSocket Server - Huy)   │    │  1. {"type":"auth","token":"..."}
│  └──────────────┬──────────────┘    │  2. {"type":"command","module":"..."}
│                 │ internal call     │  3. {"type":"stream_control",...}
│  ┌──────────────▼──────────────┐    │
│  │     agent_main.py           │    │
│  │  (AgentOrchestrator - Nghĩa)│    │
│  └──────────────┬──────────────┘    │
│                 │                   │
│  ┌──────────────▼──────────────┐    │
│  │  Module OS (Nghĩa)          │    │
│  │  psutil, opencv, pynput...  │    │
│  └─────────────────────────────┘    │
└─────────────────────────────────────┘
```

---

## Giao Thức JSON (Protocol)

### Bước 1 – Xác thực (Authentication)

Web App gửi đầu tiên:
```json
{"type": "auth", "token": "secret_token_here"}
```

Gateway trả về:
```json
{"type": "auth_result", "status": "ok", "message": "Authenticated. Client ID: a1b2c3d4"}
```
Hoặc nếu sai token:
```json
{"type": "auth_result", "status": "error", "message": "Invalid token"}
```

### Bước 2 – Gửi Lệnh (Command)

```json
{
  "type": "command",
  "request_id": "abc123",
  "module": "list_processes",
  "parameters": {}
}
```

Gateway trả về:
```json
{
  "type": "command_result",
  "request_id": "abc123",
  "status": "ok",
  "data": [
    {"pid": 1234, "name": "chrome.exe", "cpu": 2.5, "ram": 1.2, "gui": true}
  ]
}
```

### Bảng Module Commands

| Module | parameters | Mô tả |
|--------|-----------|-------|
| `list_processes` | `{}` | Liệt kê tiến trình (PID, CPU%, RAM%, tên) |
| `process_terminate` | `{"pid": 1234}` | Kill process theo PID |
| `application_launch` | `{"application": "notepad"}` | Mở app trong whitelist |
| `screen_capture` | `{}` | Chụp 1 ảnh màn hình (base64 PNG) |
| `system_metrics` | `{}` | RAM%, uptime |
| `power_control` | `{"action": "lock/sleep/restart/shutdown"}` | Điều khiển nguồn |
| `file_list` | `{"path": "C:\\RemoteWorkspace"}` | Liệt kê file |
| `file_read` | `{"path": "C:\\RemoteWorkspace\\test.txt"}` | Đọc file (base64) |
| `keylogger` | `{"action": "start/stop/get"}` | Keylogger |
| `camera_capture` | `{"camera_index": 0}` | Chụp 1 frame webcam (base64 JPEG) |

### Bước 3 – Streaming (Screen / Webcam)

**Bắt đầu stream:**
```json
{"type": "stream_control", "module": "screen", "action": "start"}
```

Gateway liên tục gửi về:
```json
{
  "type": "stream_frame",
  "module": "screen",
  "data": {"image_base64": "...base64 PNG..."}
}
```

**Dừng stream:**
```json
{"type": "stream_control", "module": "screen", "action": "stop"}
```

**Webcam stream:**
```json
{"type": "stream_control", "module": "webcam", "action": "start", "camera_index": 0}
```

### Ping / Pong (giữ kết nối sống)
```json
{"type": "ping"} → {"type": "pong"}
```

---

## Cách Chạy

### Trên máy Agent (Windows)

```bash
# 1. Cài dependencies
pip install websockets psutil mss opencv-python pynput plyer

# 2. Tạo token bảo mật
python gateway_server.py --generate-token

# 3. Chạy Gateway (dùng token vừa tạo)
set GATEWAY_TOKEN=<token_vừa_tạo>
python gateway_server.py --host 0.0.0.0 --port 8765

# 4. Mở Firewall (nếu cần)
netsh advfirewall firewall add rule name="RAT Gateway" dir=in action=allow protocol=TCP localport=8765
```

### Test Gateway (terminal khác)

```bash
python test_gateway_client.py
```

### Lấy IP LAN để Khang kết nối

```bash
ipconfig | findstr "IPv4"
```

Khang sẽ kết nối đến: `ws://192.168.x.x:8765`

---

## Giải Thích Code Quan Trọng

### `GatewayState` – Quản lý trạng thái

```python
class GatewayState:
    _authenticated_clients: dict  # WebSocket → client_id
    _screen_tasks: dict           # WebSocket → asyncio.Task (streaming)
    _webcam_tasks: dict           # WebSocket → asyncio.Task (streaming)
```

Mỗi client có task streaming **riêng biệt** → nhiều Web App có thể kết nối đồng thời.

### Tại sao dùng `asyncio.create_task`?

Stream màn hình chạy trong **background task** → Gateway có thể **đồng thời**:
- Nhận lệnh `list_processes` từ Web App
- Stream màn hình liên tục
- Không bị block lẫn nhau

### `run_in_executor` – Không block event loop

```python
png_bytes = await asyncio.get_event_loop().run_in_executor(
    None, capture_primary_screen_png  # Hàm đồng bộ của Nghĩa
)
```

`capture_primary_screen_png` là code đồng bộ (blocking), nếu gọi trực tiếp sẽ block toàn bộ server. `run_in_executor` chạy nó trong thread pool riêng.

---

## Lỗi Thường Gặp

| Lỗi | Nguyên nhân | Giải pháp |
|-----|------------|-----------|
| `ConnectionRefusedError` | Gateway chưa chạy | Chạy `gateway_server.py` trước |
| `Auth failed: Invalid token` | Token không khớp | Kiểm tra `GATEWAY_TOKEN` giống nhau trên 2 máy |
| `max_size` error | Ảnh quá lớn | Tăng `max_size` hoặc giảm JPEG quality |
| Module `denied` | User bấm "No" dialog | Bình thường, Agent từ chối lệnh |
| `screen capture only on Windows` | Chạy test trên macOS | Module này chỉ chạy được trên Windows |

---

## Điểm Tích Hợp Với Nghĩa (Role 1)

Bạn chỉ cần gọi `orchestrator.dispatch_command(dict)` – hàm của Nghĩa xử lý phần còn lại:
- Hiển thị dialog xin quyền trên Windows
- Thực thi module OS
- Trả về kết quả dạng dict

**Bạn không cần sửa code của Nghĩa**, chỉ cần đảm bảo gateway chạy đúng cổng.

## Điểm Tích Hợp Với Khang (Role 3)

Khang cần biết:
1. URL kết nối: `ws://<IP_máy_agent>:8765`
2. Bước auth đầu tiên bắt buộc
3. Định dạng JSON command ở bảng trên
4. Stream frame gửi về dạng `image_base64` → dùng `<img src="data:image/png;base64,...">` trong HTML

> [!TIP]
> Khang có thể dùng `test_gateway_client.py` làm reference để hiểu protocol.

> [!IMPORTANT]
> **Token bảo mật**: Đừng hardcode token vào code. Dùng biến môi trường `GATEWAY_TOKEN`. Khi demo, tạo token mới bằng `--generate-token`.
