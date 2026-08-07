from __future__ import annotations

import errno
import unittest

from gateway_server import _is_address_in_use_error


class GatewayStartupTests(unittest.TestCase):
    def test_windows_port_in_use_is_recognized(self) -> None:
        self.assertTrue(_is_address_in_use_error(OSError(10048, "in use")))

    def test_posix_port_in_use_is_recognized(self) -> None:
        self.assertTrue(_is_address_in_use_error(OSError(errno.EADDRINUSE, "in use")))

    def test_unrelated_os_error_is_not_hidden(self) -> None:
        self.assertFalse(_is_address_in_use_error(OSError(errno.EACCES, "denied")))


if __name__ == "__main__":
    unittest.main()
