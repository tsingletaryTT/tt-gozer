# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.
"""Leases that say what they serve, and `gozer services` for a router.

gozer still does not start models or route requests; it only records, on the
lease, the name, port and model id a tenant serves, so something like
serve-gateway's router can find every model on the box without a config file.
"""
import http.server
import json
import subprocess
import sys
import threading

import pytest

from gozer import cli
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Grant, Keymaster
from conftest import QUIETBOX, fake_proc_tree


@pytest.fixture
def box(tmp_path, sysfs, monkeypatch):
    proc = fake_proc_tree(tmp_path, {1: {}})
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=proc)
    km = Keymaster(gk)
    km.reset_runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setenv("GOZER_ROOT", gk.root)
    monkeypatch.setenv("GOZER_SYSFS_ROOT", gk.sysfs_root)
    monkeypatch.setenv("GOZER_PROC_ROOT", gk.proc_root)
    monkeypatch.setenv("GOZER_RESET_CMD", "/bin/true")
    return km, gk


def services(capsys, *extra):
    assert cli.main(["services", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)["services"]


def test_acquire_records_the_service_and_services_lists_it(box, capsys):
    km, gk = box
    assert cli.main(["acquire", "--chips", "1", "--who", "me:llama",
                     "--service", "llama-3b", "--port", "8001",
                     "--model", "meta-llama/Llama-3.2-3B-Instruct", "--json"]) == 0
    lease_id = json.loads(capsys.readouterr().out)["lease_id"]
    assert gk.read_lease(lease_id)["service"] == {
        "name": "llama-3b", "port": 8001,
        "model": "meta-llama/Llama-3.2-3B-Instruct"}
    [s] = services(capsys)
    assert s["service"] == "llama-3b" and s["port"] == 8001
    assert s["url"] == "http://127.0.0.1:8001"
    assert s["lease_id"] == lease_id and len(s["chips"]) == 2


def test_model_defaults_to_the_service_name(box, capsys):
    assert cli.main(["acquire", "--chips", "1", "--who", "me:q",
                     "--service", "qwen-7b"]) == 0
    capsys.readouterr()
    [s] = services(capsys)
    assert s["model"] == "qwen-7b" and s["port"] is None and s["url"] is None


@pytest.mark.parametrize("argv,needle", [
    (["--port", "8001"], "name it"),
    (["--service", "bad name"], "letters, digits"),
    (["--service", "x", "--port", "70000"], "not a TCP port"),
    (["--service", "x", "--model", " "], "must not be empty"),
])
def test_bad_service_fields_are_refused_before_anything_is_leased(box, capsys, argv, needle):
    km, gk = box
    rc = cli.main(["acquire", "--chips", "1", "--who", "me:x", *argv])
    assert rc == cli.EXIT_UNAVAILABLE
    assert needle in capsys.readouterr().out
    assert gk.held_units() == {}


def test_a_queued_service_keeps_its_fields_through_wait(box, capsys):
    km, gk = box
    km.acquire("all", who="busy", pid=1)
    rc = cli.main(["acquire", "--chips", "1", "--who", "me:later",
                   "--service", "later", "--port", "8002", "--json"])
    assert rc == cli.EXIT_QUEUED
    ticket = json.loads(capsys.readouterr().out)["ticket"]
    for lid in [l["lease_id"] for l in gk.all_leases()]:
        km.release(lid)
    assert cli.main(["wait", ticket, "--timeout", "5s", "--json"]) == 0
    lease_id = json.loads(capsys.readouterr().out)["lease_id"]
    assert gk.read_lease(lease_id)["service"]["port"] == 8002


def test_renew_can_name_the_service_later(box, capsys):
    km, gk = box
    g = km.acquire("1", who="supervisor", pid=1)
    assert services(capsys) == []
    assert cli.main(["renew", g.lease_id, "--service", "late", "--port", "9000"]) == 0
    capsys.readouterr()
    [s] = services(capsys)
    assert s["service"] == "late" and s["port"] == 9000
    assert gk.unit_lease(g.units[0])["service"]["name"] == "late"   # gate copy too


def test_status_shows_what_a_chip_serves(box, capsys):
    km, gk = box
    km.acquire("1", who="me:s", pid=1, service={"name": "svc", "port": 8003,
                                                "model": "svc"})
    assert cli.main(["status"]) == 0
    assert "serving svc on :8003" in capsys.readouterr().out


def test_services_check_probes_v1_models(box, capsys):
    km, gk = box

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/v1/models" else 404)
            self.end_headers()
            self.wfile.write(b'{"data": []}')

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        km.acquire("1", who="me:up", pid=1,
                   service={"name": "up", "port": port, "model": "up"})
        km.acquire("1", who="me:down", pid=1,
                   service={"name": "down", "port": 1, "model": "down"})
        got = {s["service"]: s["up"] for s in services(capsys, "--check")}
        assert got == {"up": True, "down": False}
    finally:
        srv.shutdown()


def test_services_is_read_only_and_skips_borrowed_partner_locks(box, capsys):
    km, gk = box
    g = km.acquire("1", who="me:s", pid=1,
                   service={"name": "s", "port": 8004, "model": "s"})
    lease = gk.read_lease(g.lease_id)
    # A borrowed lock (die-grain release) carries a copy of the record.
    gk.claim_unit("0000000000000001", {**lease, "who": "releaser",
                                       "borrowed_for_reset": g.lease_id})
    before = sorted(gk.held_units())
    assert [(s["service"], s["who"]) for s in services(capsys)] == [("s", "me:s")]
    assert sorted(gk.held_units()) == before


def test_run_records_the_service(box, monkeypatch):
    km, gk = box
    seen = {}
    real = km.acquire

    def spy(*a, **kw):
        g = real(*a, **kw)
        if isinstance(g, Grant):
            seen.update(gk.read_lease(g.lease_id).get("service") or {})
        return g
    monkeypatch.setattr(km, "acquire", spy)
    rc = km.run([sys.executable, "-c", "pass"], chips_spec="1", who="run:s",
                pid=1, service={"name": "r", "port": 8005, "model": "r"})
    assert rc == 0 and seen["port"] == 8005
