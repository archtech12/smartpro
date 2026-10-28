"""LAN FastAPI host. The first slice intentionally exposes health only."""

from __future__ import annotations

import ipaddress
import socket
import threading

def create_app():
    from fastapi import FastAPI

    app = FastAPI(title="SmartSchool CBT Server", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "SmartSchool CBT Server"}

    return app


class CbtServerController:
    """Manage one local Uvicorn thread without blocking the Qt event loop."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host, self.port = host, int(port)
        self._lock = threading.RLock()
        self._thread = None
        self._server = None
        self._state = "stopped"
        self._error = ""

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._state, self._error = "starting", ""
            self._thread = threading.Thread(target=self._run, name="smartschool-cbt-api", daemon=True)
            self._thread.start()

    def _run(self):
        try:
            import uvicorn

            server = uvicorn.Server(uvicorn.Config(
                create_app(), host=self.host, port=self.port, log_level="warning",
                access_log=False, server_header=False,
            ))
            with self._lock:
                self._server = server
            server.run()
            with self._lock:
                self._state = "stopped" if self._state == "stopping" or server.started else "failed"
                if self._state == "failed":
                    self._error = f"Could not start on port {self.port}; it may already be in use."
        except Exception as exc:
            with self._lock:
                self._state, self._error = "failed", str(exc)
        finally:
            with self._lock:
                self._server = None

    def stop(self):
        with self._lock:
            if self._server is not None and self._thread and self._thread.is_alive():
                self._state = "stopping"
                self._server.should_exit = True
            elif not self._thread or not self._thread.is_alive():
                self._state = "stopped"

    def status(self):
        with self._lock:
            if self._thread and self._thread.is_alive() and self._server and self._server.started:
                if self._state == "starting":
                    self._state = "running"
            return {"state": self._state, "error": self._error, "urls": self._lan_urls() if self._state == "running" else []}

    def _lan_urls(self):
        addresses = set()
        try:
            for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
                address = result[4][0]
                parsed = ipaddress.ip_address(address)
                if parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
                    addresses.add(address)
        except OSError:
            pass
        return [f"http://{address}:{self.port}/health" for address in sorted(addresses)]
