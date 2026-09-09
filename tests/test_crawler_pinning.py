"""Verification that crawler's DNS-rebinding defence actually intercepts sockets.

crawler.py monkeypatches urllib3.util.connection.create_connection so the TCP
dial goes to an IP address it has already validated as public, closing the gap
between "we checked the hostname" and "requests looked the hostname up again".
That patch only works if the module which opens sockets, urllib3.connection,
reaches create_connection through a module attribute lookup at call time. If a
future urllib3 switches to binding the name directly at import, the patch
becomes a silent no-op: the protection would look present in the source while
doing nothing. These tests exist to make that failure loud.

Unlike the rest of the suite this needs the third-party dependencies installed,
so it skips cleanly on a bare clone.
"""
import http.server
import socket
import threading
import unittest

try:
    import requests
    import urllib3.connection
    import urllib3.util.connection

    import crawler
    DEPS_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the environment, not the code
    DEPS_AVAILABLE = False

# RFC 2606 reserves .invalid, so this name must not resolve. That is the whole
# point: if a request to it succeeds, the only way it got there was the pin.
UNRESOLVABLE_HOST = "pin-target.invalid"


def _hostname_resolves(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


class _QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"pinned-server")

    def log_message(self, *args):
        pass


@unittest.skipUnless(DEPS_AVAILABLE, "requires requests and urllib3 (pip install -r requirements.txt)")
class PinnedConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _QuietHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://{UNRESOLVABLE_HOST}:{cls.port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self):
        crawler._pin_state.host = None
        crawler._pin_state.ip = None

    def test_socket_call_site_sees_the_patch(self):
        # The cheap, fast version of this whole file. urllib3.connection does
        # "from .util import connection", so this attribute lookup happens at
        # call time against the very module object crawler patched. If an
        # upgrade changes that to "from .util.connection import create_connection",
        # this assertion fails and the pin is known to be dead.
        self.assertIs(urllib3.connection.connection, urllib3.util.connection)
        self.assertIs(
            urllib3.connection.connection.create_connection,
            crawler._pinned_create_connection,
        )

    def test_pinned_request_reaches_the_pinned_address(self):
        crawler._pin_state.host = UNRESOLVABLE_HOST
        crawler._pin_state.ip = "127.0.0.1"
        response = requests.get(self.url, timeout=5)
        self.assertEqual(response.text, "pinned-server")

    def test_without_a_pin_the_same_request_cannot_connect(self):
        # Control for the test above: proves the success there came from the pin
        # and not from the hostname resolving on its own.
        if _hostname_resolves(UNRESOLVABLE_HOST):
            self.skipTest("this resolver answers for reserved .invalid names")
        with self.assertRaises(requests.RequestException):
            requests.get(self.url, timeout=5)

    def test_pin_is_scoped_to_its_hostname(self):
        # A pin set for one host must not redirect traffic for a different host,
        # or one crawl could silently hijack another's destination.
        if _hostname_resolves(UNRESOLVABLE_HOST):
            self.skipTest("this resolver answers for reserved .invalid names")
        crawler._pin_state.host = "somewhere-else.invalid"
        crawler._pin_state.ip = "127.0.0.1"
        with self.assertRaises(requests.RequestException):
            requests.get(self.url, timeout=5)


if __name__ == "__main__":
    unittest.main()
