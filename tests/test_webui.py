"""控制台端口：被占用时顺延，不让驱动跟着挂掉。"""
import os
import sys
import socket
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui


class PortTest(unittest.TestCase):
    def test_busy_port_falls_back(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        try:
            srv, got = webui.bind_server(port)
            self.assertIsNotNone(srv)
            self.assertGreater(got, port)
            srv.server_close()
        finally:
            busy.close()


if __name__ == "__main__":
    unittest.main()
