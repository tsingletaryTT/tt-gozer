# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Review findings for `gozer services` (2026-11-11, fourth review). Each test
asserts the corrected behaviour."""
import http.server
import json
import threading

import pytest

from gozer import cli
from test_services import box, services  # noqa: F401


def test_G1_service_name_with_trailing_newline_is_refused(box, capsys):
    km, gk = box
    rc = cli.main(["acquire", "--chips", "1", "--who", "me:x", "--service", "evil\n"])
    capsys.readouterr()
    assert rc == cli.EXIT_UNAVAILABLE, "regex `$` matches before a trailing newline"


def test_G2_a_stale_lease_is_listed_as_not_live_and_left_out_by_live(box, capsys):
    km, gk = box
    # pid 4242 does not exist in the fake /proc: owner dead, lease STALE, unreaped
    km.acquire("1", who="me:dead", pid=4242, service={"name": "d", "port": 8009, "model": "d"})
    [s] = services(capsys)
    assert s["state"] == "STALE" and s["live"] is False
    assert services(capsys, "--live") == []


def test_G3_check_probe_ignores_http_proxy(box, capsys, monkeypatch):
    km, gk = box

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    km.acquire("1", who="me:up", pid=1,
               service={"name": "up", "port": srv.server_address[1], "model": "up"})
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.delenv("no_proxy", raising=False); monkeypatch.delenv("NO_PROXY", raising=False)
    try:
        [s] = services(capsys, "--check")
    finally:
        srv.shutdown()
    assert s["up"] is True, "--check went through $http_proxy for a 127.0.0.1 URL"


def test_G4_check_probe_follows_redirect_off_localhost(box, capsys):
    km, gk = box
    hits = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    tgt = http.server.HTTPServer(("127.0.0.2", 0), Target)

    class Redir(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.2:{tgt.server_address[1]}/elsewhere")
            self.send_header("Content-Length", "0"); self.end_headers()

        def log_message(self, *a):
            pass
    red = http.server.HTTPServer(("127.0.0.1", 0), Redir)
    for s in (tgt, red):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    km.acquire("1", who="me:r", pid=1,
               service={"name": "r", "port": red.server_address[1], "model": "r"})
    try:
        [s] = services(capsys, "--check")
    finally:
        tgt.shutdown(); red.shutdown()
    assert hits == [], f"probe followed a redirect to another host: {hits}, up={s['up']}"


def test_G5_renew_with_service_only_keeps_port(box, capsys):
    km, gk = box
    g = km.acquire("1", who="sup", pid=1, service={"name": "s", "port": 9000, "model": "org/m"})
    assert cli.main(["renew", g.lease_id, "--service", "s"]) == 0
    capsys.readouterr()
    [s] = services(capsys)
    assert s["port"] == 9000 and s["model"] == "org/m", f"renew --service wiped fields: {s}"


def test_G6_duplicate_port_or_name_is_refused(box, capsys):
    km, gk = box
    km.acquire("1", who="a", pid=1, service={"name": "s", "port": 9000, "model": "s"})
    rc = cli.main(["acquire", "--chips", "1", "--who", "b", "--service", "s", "--port", "9000"])
    capsys.readouterr()
    assert rc != 0, "two live leases advertise the same name and port"


def test_G7_state_comes_from_the_lease_not_a_borrowed_lock(box, capsys):
    km, gk = box
    # chip grain? the QUIETBOX fixture is board grain; borrow the other board's unit with this lease id
    g = km.acquire("1", who="me:s", pid=1, exact="0000:03:00.0",
                   service={"name": "s", "port": 8004, "model": "s"})
    lease = gk.read_lease(g.lease_id)
    other = sorted(u for u in gk.held_units() if u not in g.units)
    free = [u for u in sorted({gk.unit_key_for(c) for b in gk.boards for c in b.chips})
            if u not in g.units]
    # put a borrowed lock (owner pid 4242 dead -> STALE) on a unit that sorts before ours
    first = min(free)
    gk.claim_unit(first, {**lease, "pid": 4242, "detached": False, "borrowed_for_reset": g.lease_id})
    [s] = services(capsys)
    assert s["state"] != "STALE", f"state taken from the borrowed lock: {s['state']} (units {g.units}, borrowed {first})"


def test_renew_can_move_only_the_port(box, capsys):
    km, gk = box
    g = km.acquire("1", who="sup", pid=1, service={"name": "s", "port": 9000, "model": "org/m"})
    assert cli.main(["renew", g.lease_id, "--port", "9001"]) == 0
    capsys.readouterr()
    [s] = services(capsys)
    assert (s["service"], s["port"], s["model"]) == ("s", 9001, "org/m")


def test_a_name_with_a_slash_is_refused(box, capsys):
    rc = cli.main(["acquire", "--chips", "1", "--who", "me:x", "--service", "team/a"])
    capsys.readouterr()
    assert rc == cli.EXIT_UNAVAILABLE


def test_a_queued_run_does_not_put_its_service_on_the_ticket(box):
    import sys as _sys
    km, gk = box
    km.acquire("all", who="busy", pid=1)
    rc = km.run([_sys.executable, "-c", "pass"], chips_spec="1", who="run:q", pid=1,
                service={"name": "q", "port": 8010, "model": "q"})
    assert rc == 10
    [ticket] = gk.queue_entries()
    assert ticket.get("service") is None


def test_services_survives_a_record_without_name_or_model(box, capsys):
    km, gk = box
    km.acquire("1", who="prog", pid=1, service={"port": 8011})
    assert cli.main(["services"]) == 0
    assert "8011" in capsys.readouterr().out
