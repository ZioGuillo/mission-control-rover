"""Tiny local HTTP servers for tests (no network, no getfqdn stall)."""
import socketserver
import threading
from http.server import HTTPServer


class Server(HTTPServer):
    def server_bind(self):  # HTTPServer's getfqdn() reverse lookup stalls ~35 s here
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def start(handler) -> tuple[Server, str]:
    srv = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def stop(srv: Server) -> None:
    srv.shutdown()
    srv.server_close()
