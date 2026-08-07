from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from websockets.exceptions import ConnectionClosedError

from gateway_server import _handle_client


def _closed_without_handshake() -> ConnectionClosedError:
    return ConnectionClosedError(None, None)


class GatewayAuthenticationDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_closing_during_auth_does_not_trigger_a_send(self) -> None:
        websocket = mock.AsyncMock()
        websocket.remote_address = ("127.0.0.1", 50000)
        websocket.recv.side_effect = _closed_without_handshake()
        state = mock.Mock()
        state.authenticate = mock.AsyncMock()

        await _handle_client(websocket, state)

        websocket.send.assert_not_awaited()
        state.authenticate.assert_not_awaited()

    async def test_closed_client_does_not_crash_invalid_auth_response(self) -> None:
        websocket = mock.AsyncMock()
        websocket.remote_address = ("127.0.0.1", 50001)
        websocket.recv.return_value = "not valid JSON"
        websocket.send.side_effect = _closed_without_handshake()
        state = mock.Mock()
        state.authenticate = mock.AsyncMock()

        await _handle_client(websocket, state)

        websocket.send.assert_awaited_once()
        state.authenticate.assert_not_awaited()

    async def test_authenticated_client_is_removed_if_first_response_cannot_send(self) -> None:
        websocket = mock.AsyncMock()
        websocket.remote_address = ("127.0.0.1", 50002)
        websocket.recv.return_value = '{"type":"auth","token":"correct"}'
        websocket.send.side_effect = _closed_without_handshake()
        state = SimpleNamespace(
            authenticate=mock.AsyncMock(return_value=True),
            client_id=mock.Mock(return_value="client-1"),
            remove_client=mock.AsyncMock(),
            orchestrator=SimpleNamespace(
                security=SimpleNamespace(permission_snapshot=mock.Mock(return_value={}))
            ),
        )

        await _handle_client(websocket, state)

        state.authenticate.assert_awaited_once_with(websocket, "correct")
        state.remove_client.assert_awaited_once_with(websocket)


if __name__ == "__main__":
    unittest.main()
