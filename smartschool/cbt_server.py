"""Browser CBT server: students sit exams from any browser on the school network (standard library only).

Security model (see docs/CBT.md):
* A student signs in with admission number + the exam's access code; the server then issues a random,
  HttpOnly, SameSite=Strict session cookie bound to that student. A new sign-in ends their older session.
* Failed sign-ins are rate limited per address and per admission number.
* Every state-changing request must be JSON with an ``X-Requested-With`` header (a cross-site page cannot send it).
* The server owns the clock; correct answers never leave the server; request bodies are size-limited.
* Strict security headers and a nonce-based Content-Security-Policy; no cookies are readable by scripts.
* Traffic is plain HTTP unless a certificate is supplied: use an isolated lab network / Wi-Fi.
"""

import json
import logging
import secrets
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import cbt, cbt_page, cbt_runtime
from . import database as db
from .cbt_runtime import CBTError

logger = logging.getLogger(__name__)

MAX_BODY = 16 * 1024
SESSION_IDLE_SECONDS = 4 * 3600
MAX_SESSIONS = 3000
LOGIN_FAILURES = 8
LOGIN_WINDOW = 300
COOKIE = "cbt_session"


class RateLimiter:
    """Sliding-window failure counter keyed by any string."""

    def __init__(self, limit=LOGIN_FAILURES, window=LOGIN_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key):
        cutoff = self.clock() - self.window
        hits = [t for t in self._hits.get(key, []) if t > cutoff]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key) -> int:
        """Seconds to wait, or 0 when allowed."""
        with self._lock:
            hits = self._prune(key)
            if len(hits) >= self.limit:
                return max(1, int(hits[0] + self.window - self.clock()))
            return 0

    def fail(self, key):
        with self._lock:
            self._prune(key)
            self._hits.setdefault(key, []).append(self.clock())
            if len(self._hits) > 20000:  # bound memory under a flood of distinct keys
                self._hits.pop(next(iter(self._hits)))

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


class _State:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.by_ip = RateLimiter()
        self.by_admission = RateLimiter()
        self.tls = False

    def create(self, info: dict, ip: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            for key in [k for k, v in self.sessions.items() if now - v["seen"] > SESSION_IDLE_SECONDS
                        or v["student_id"] == info["student_id"]]:
                del self.sessions[key]               # one live session per student
            if len(self.sessions) >= MAX_SESSIONS:
                raise CBTError("busy", "The exam server is busy. Try again in a minute.")
            self.sessions[token] = {"student_id": info["student_id"], "exam_id": info["exam_id"], "attempt_id": None,
                                    "ip": ip, "seen": now}
        return token

    def get(self, token: str | None):
        if not token:
            return None
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return None
            if self.clock() - session["seen"] > SESSION_IDLE_SECONDS:
                del self.sessions[token]
                return None
            session["seen"] = self.clock()
            return session

    def drop(self, token):
        with self.lock:
            self.sessions.pop(token, None)

    def active(self) -> int:
        with self.lock:
            return len(self.sessions)


def lan_addresses() -> list[str]:
    """This computer's likely LAN addresses (for telling students where to go)."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))          # no packet is sent
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found and not info[4][0].startswith("127."):
                found.append(info[4][0])
    except OSError:
        pass
    return found


def _school_name() -> str:
    try:
        with db.connection() as conn:
            row = conn.execute("SELECT school_name FROM school_profile WHERE id=1").fetchone()
        return (row["school_name"] if row else "") or "School"
    except Exception:  # noqa: BLE001
        return "School"


class Handler(BaseHTTPRequestHandler):
    server_version = "SmartSchoolCBT"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 20

    def log_message(self, fmt, *args):  # keep student traffic out of the console; errors are logged explicitly
        logger.debug("cbt %s %s", self.address_string(), fmt % args)

    @property
    def state(self) -> _State:
        return self.server.cbt_state

    # ---------------------------------------------------------------- plumbing
    def _headers(self, nonce: str | None = None):
        csp = ("default-src 'none'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; "
               "frame-ancestors 'none'")
        if nonce:
            csp += f"; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if self.state.tls:
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _send(self, status: int, body: bytes, ctype: str, extra=None, nonce=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._headers(nonce)
        if status >= 400:
            self.send_header("Connection", "close")   # the request body may be unread; never reuse the connection
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra=None):
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", extra)

    def _error(self, status: int, code: str, message: str, extra=None):
        self._json(status, {"error": message, "code": code}, extra)

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and 20 <= len(value) <= 80:
                return value
        return None

    def _ip(self) -> str:
        return self.client_address[0]

    def _body(self) -> dict | None:
        if (self.headers.get("X-Requested-With") or "") != "cbt" or \
                not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            self._error(400, "bad_request", "Invalid request.")
            return None
        try:
            length = int(self.headers.get("Content-Length") or "-1")
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._error(413 if length > MAX_BODY else 400, "bad_request", "Invalid request.")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._error(400, "bad_request", "Invalid request.")
            return None
        if not isinstance(data, dict):
            self._error(400, "bad_request", "Invalid request.")
            return None
        return data

    # ------------------------------------------------------------------ routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                self._send(200, cbt_page.render(_school_name(), nonce), "text/html; charset=utf-8", nonce=nonce)
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain; charset=utf-8")
            elif path == "/api/state":
                self._state()
            elif path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                self._error(404, "not_found", "Not found.")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("CBT request failed")
            self._error(500, "server_error", "The exam server had a problem. Tell your teacher.")

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            routes = {"/api/login": self._login, "/api/start": self._start, "/api/answer": self._answer,
                      "/api/submit": self._submit, "/api/focus": self._focus, "/api/logout": self._logout}
            handler = routes.get(path)
            if handler is None:
                self._error(404, "not_found", "Not found.")
                return
            data = self._body()
            if data is not None:
                handler(data)
        except CBTError as exc:
            self._error(409 if exc.code in ("time_up", "already_submitted") else 400, exc.code, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("CBT request failed")
            self._error(500, "server_error", "The exam server had a problem. Tell your teacher.")

    def _session(self):
        session = self.state.get(self._token())
        if session is None:
            self._error(401, "no_session", "Please sign in again.")
        return session

    def _login(self, data):
        ip, adm = self._ip(), str(data.get("admission_no", "")).strip().lower()[:40]
        wait = max(self.state.by_ip.blocked(ip), self.state.by_admission.blocked(adm))
        if wait:
            self._error(429, "rate_limited", f"Too many attempts. Wait {wait} seconds and try again.",
                        [("Retry-After", str(wait))])
            return
        try:
            info = cbt_runtime.identify(str(data.get("admission_no", "")), str(data.get("code", "")))
        except CBTError as exc:
            self.state.by_ip.fail(ip)
            self.state.by_admission.fail(adm)
            self._error(400, exc.code, str(exc))
            return
        self.state.by_admission.clear(adm)
        token = self.state.create(info, ip)
        cookie = f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_IDLE_SECONDS}"
        if self.state.tls:
            cookie += "; Secure"
        public = {k: info[k] for k in ("student_name", "title", "duration_minutes", "instructions", "question_count",
                                       "total_marks", "attempt_status")}
        self._json(200, public, [("Set-Cookie", cookie)])

    def _start(self, data):
        session = self._session()
        if session is None:
            return
        attempt_id = cbt_runtime.start_attempt(session["exam_id"], session["student_id"], client_ip=self._ip())
        session["attempt_id"] = attempt_id
        self._json(200, cbt_runtime.attempt_state(attempt_id, session["student_id"]))

    def _state(self):
        session = self._session()
        if session is None:
            return
        if not session["attempt_id"]:
            self._error(409, "not_started", "Start the exam first.")
            return
        self._json(200, cbt_runtime.attempt_state(session["attempt_id"], session["student_id"]))

    def _answer(self, data):
        session = self._session()
        if session is None or not self._started(session):
            return
        left = cbt_runtime.save_answer(session["attempt_id"], session["student_id"], data.get("question_id"),
                                       data.get("answer", ""))
        self._json(200, {"remaining_seconds": max(0, left)})

    def _started(self, session) -> bool:
        if not session["attempt_id"]:
            self._error(409, "not_started", "Start the exam first.")
            return False
        return True

    def _submit(self, data):
        session = self._session()
        if session is None or not self._started(session):
            return
        self._json(200, cbt_runtime.submit(session["attempt_id"], session["student_id"]))

    def _focus(self, data):
        session = self._session()
        if session is None or not self._started(session):
            return
        cbt_runtime.record_focus_loss(session["attempt_id"], session["student_id"])
        self._json(200, {"ok": True})

    def _logout(self, data):
        token = self._token()
        if token:
            self.state.drop(token)
        self._json(200, {"ok": True}, [("Set-Cookie", f"{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")])


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


class CBTServer:
    """Start/stop wrapper used by the desktop app and by tests."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, certfile: str | None = None, keyfile: str | None = None):
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("The port must be a number from 0 to 65535.")
        self.host, self.port, self.certfile, self.keyfile = host, port, certfile, keyfile
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self._sweeper: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = _State()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> int:
        if self._httpd is not None:
            return self.port
        try:
            httpd = _Server((self.host, self.port), Handler)
        except OSError as exc:
            raise ValueError(f"The server could not start on port {self.port}: it may already be in use. ({exc.strerror})") from None
        if self.certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.certfile, self.keyfile)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            self.state.tls = True
        httpd.cbt_state = self.state
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._stop.clear()
        self._thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25}, name="cbt-server", daemon=True)
        self._thread.start()
        self._sweeper = threading.Thread(target=self._sweep, name="cbt-sweeper", daemon=True)
        self._sweeper.start()
        return self.port

    def _sweep(self):
        while not self._stop.wait(15):                       # submit attempts whose browser went away
            try:
                cbt_runtime.finalize_expired()
            except Exception:  # noqa: BLE001
                logger.exception("CBT sweep failed")

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._stop.set()
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self._httpd = None
        self.state = _State()

    def urls(self) -> list[str]:
        scheme = "https" if self.certfile else "http"
        hosts = ["localhost"] if self.host in ("127.0.0.1", "localhost") else lan_addresses() or ["localhost"]
        return [f"{scheme}://{h}:{self.port}/" for h in hosts]

    @property
    def active_sessions(self) -> int:
        return self.state.active()
