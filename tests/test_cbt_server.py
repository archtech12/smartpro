"""CBT browser server over real HTTP on localhost (synthetic data only)."""

import http.client
import json
from datetime import datetime, timedelta

import pytest

from smartschool import cbt, cbt_server
from tests.test_cbt import T0, _open_exam, _student


class Browser:
    """A tiny cookie-keeping client."""

    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, body=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
            hdrs.setdefault("X-Requested-With", "cbt")
        conn.request(method, path, body=data, headers=hdrs)
        response = conn.getresponse()
        payload = response.read()
        set_cookie = response.getheader("Set-Cookie")
        if set_cookie and "=" in set_cookie:
            self.cookie = set_cookie.split(";")[0] if not set_cookie.split(";")[0].endswith("=") else None
        conn.close()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload
        return response.status, parsed, response


@pytest.fixture()
def server(school):
    srv = cbt_server.CBTServer("127.0.0.1", 0)
    srv.start()
    yield srv
    srv.stop()


def test_page_has_security_headers_and_escapes_school_name(school, server):
    from smartschool import database as db
    db.save_school_profile({"school_name": "<b>Evil</b> & Co"}, "admin")
    status, body, resp = Browser(server.port).call("GET", "/")
    assert status == 200 and b"&lt;b&gt;Evil&lt;/b&gt; &amp; Co" in body and b"<b>Evil</b>" not in body
    csp = resp.getheader("Content-Security-Policy")
    assert "script-src 'nonce-" in csp and "unsafe-inline" not in csp and "default-src 'none'" in csp
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"'.encode() in body
    assert resp.getheader("X-Content-Type-Options") == "nosniff" and resp.getheader("X-Frame-Options") == "DENY"
    assert resp.getheader("Cache-Control") == "no-store"
    assert Browser(server.port).call("GET", "/etc/passwd")[0] == 404
    assert Browser(server.port).call("GET", "/healthz")[0] == 200


def test_complete_exam_over_http(school, server, monkeypatch):
    student = _student(school, 1)
    exam, code = _open_exam(school, show_score=True)
    clock = {"now": T0}
    monkeypatch.setattr(cbt, "utc_now", lambda: clock["now"])
    b = Browser(server.port)
    assert b.call("POST", "/api/start", {})[0] == 401                                  # no session yet
    status, info, resp = b.call("POST", "/api/login", {"admission_no": "adm-1", "code": code.lower()})
    cookie = resp.getheader("Set-Cookie")
    assert status == 200 and info["question_count"] == 3 and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "token" not in json.dumps(info)
    status, state, _ = b.call("POST", "/api/start", {})
    assert status == 200 and not state["finished"] and state["remaining_seconds"] == 1800
    dumped = json.dumps(state)
    assert "Abuja" not in dumped and '"answer"' not in dumped and "correct" not in dumped
    by_prompt = {q["prompt"]: q for q in state["questions"]}
    mars = next(o["key"] for o in by_prompt["Red planet?"]["options"] if o["text"] == "Mars")
    clock["now"] = T0 + timedelta(minutes=10)
    status, saved, _ = b.call("POST", "/api/answer", {"question_id": by_prompt["Red planet?"]["id"], "answer": mars})
    assert status == 200 and saved["remaining_seconds"] == 1200
    b.call("POST", "/api/answer", {"question_id": by_prompt["Sun rises in the west."]["id"], "answer": "false"})
    b.call("POST", "/api/answer", {"question_id": by_prompt["Capital of Nigeria?"]["id"], "answer": "ABUJA"})
    assert b.call("POST", "/api/focus", {})[0] == 200
    status, again, _ = b.call("GET", "/api/state")                                      # a page refresh resumes
    assert status == 200 and len(again["answers"]) == 3 and again["remaining_seconds"] == 1200
    status, result, _ = b.call("POST", "/api/submit", {})
    assert status == 200 and result["score"] == 5 and result["max_score"] == 5
    assert b.call("POST", "/api/answer", {"question_id": 1, "answer": "A"})[0] == 409
    assert cbt.monitor("admin", exam, clock["now"])["students"][0]["focus_losses"] == 1
    assert b.call("POST", "/api/logout", {})[0] == 200 and b.call("GET", "/api/state")[0] == 401


def test_requests_without_csrf_header_or_json_are_rejected(school, server):
    b = Browser(server.port)
    assert b.call("POST", "/api/login", raw=b'{"admission_no":"a","code":"b"}', headers={"Content-Type": "application/json"})[0] == 400
    assert b.call("POST", "/api/login", raw=b'a=b', headers={"Content-Type": "text/plain", "X-Requested-With": "cbt"})[0] == 400
    assert b.call("POST", "/api/login", raw=b'[1]', headers={"Content-Type": "application/json", "X-Requested-With": "cbt"})[0] == 400
    assert b.call("POST", "/api/login", raw=b"x" * 40000, headers={"Content-Type": "application/json", "X-Requested-With": "cbt"})[0] == 413
    assert b.call("POST", "/api/login", raw=b"{broken", headers={"Content-Type": "application/json", "X-Requested-With": "cbt"})[0] == 400


def test_bad_logins_are_rate_limited_and_messages_do_not_leak(school, server):
    _student(school, 2)
    _, code = _open_exam(school)
    b = Browser(server.port)
    messages = set()
    for _ in range(cbt_server.LOGIN_FAILURES):
        status, body, _ = b.call("POST", "/api/login", {"admission_no": "ADM-2", "code": "WRONG1"})
        assert status == 400
        messages.add(body["error"])
    status, body, resp = b.call("POST", "/api/login", {"admission_no": "ADM-NOPE", "code": "WRONG1"})
    assert len(messages) == 1                                                            # same text for any bad detail
    assert status == 429 and int(resp.getheader("Retry-After")) > 0
    assert b.call("POST", "/api/login", {"admission_no": "ADM-2", "code": code})[0] == 429   # blocked even with the right code


def test_new_sign_in_ends_the_old_session_and_sessions_are_isolated(school, server):
    _student(school, 3)
    _student(school, 4)
    _, code = _open_exam(school)
    first, second, other = Browser(server.port), Browser(server.port), Browser(server.port)
    assert first.call("POST", "/api/login", {"admission_no": "ADM-3", "code": code})[0] == 200
    assert first.call("POST", "/api/start", {})[0] == 200
    assert second.call("POST", "/api/login", {"admission_no": "ADM-3", "code": code})[0] == 200
    assert first.call("GET", "/api/state")[0] == 401                                    # the earlier browser was signed out
    assert second.call("POST", "/api/start", {})[0] == 200                              # same attempt, resumed
    assert other.call("POST", "/api/login", {"admission_no": "ADM-4", "code": code})[0] == 200
    assert other.call("GET", "/api/state")[0] == 409                                     # signed in, exam not started
    assert server.active_sessions >= 1


def test_closed_exam_and_stop_release_the_port(school):
    _student(school, 5)
    exam, code = _open_exam(school)
    srv = cbt_server.CBTServer("127.0.0.1", 0)
    port = srv.start()
    b = Browser(port)
    assert b.call("POST", "/api/login", {"admission_no": "ADM-5", "code": code})[0] == 200
    cbt.close_exam("admin", exam)
    assert b.call("POST", "/api/start", {})[0] == 400
    assert Browser(port).call("POST", "/api/login", {"admission_no": "ADM-5", "code": code})[0] == 400
    srv.stop()
    assert not srv.running
    clash = cbt_server.CBTServer("127.0.0.1", port)
    clash.start()
    other = cbt_server.CBTServer("127.0.0.1", port)
    with pytest.raises(ValueError, match="port"):
        other.start()
    clash.stop()


def test_rate_limiter_window():
    now = {"t": 0.0}
    limiter = cbt_server.RateLimiter(limit=2, window=10, clock=lambda: now["t"])
    limiter.fail("k"); limiter.fail("k")
    assert limiter.blocked("k") > 0
    now["t"] = 11
    assert limiter.blocked("k") == 0
