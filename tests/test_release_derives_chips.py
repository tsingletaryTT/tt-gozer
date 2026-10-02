"""Keymaster.release takes its chips and device indices from the lease's units.

The record's own `chips` and `dev_indices` lists are never used to decide what
to reset or which devices to scan. A hand-edited or corrupt record therefore
cannot make release reset another tenant's chips, and cannot make it crash
before the lease is cleaned up.
"""
import json
import os
import re

from conftest import QUIETBOX, fake_proc_tree
from gozer import reset as reset_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster

OWNER = 5_000_100
HOLDER = 5_000_200


class _Ok:
    returncode, stdout, stderr = 0, "reset ok", ""


def make(tmp_path, sysfs):
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}}))
    km = Keymaster(gk)
    calls = []
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    return km, gk, calls


def two_leases(km):
    a = km.acquire("1", who="orchard:a", pid=OWNER)
    b = km.acquire("1", who="claude:b", pid=OWNER)
    assert set(a.units).isdisjoint(b.units)
    return a, b


def damage(gk, lease_id, **changes):
    """Rewrite the lease record with fields replaced, or removed when None."""
    lease = gk.read_lease(lease_id)
    for key, value in changes.items():
        if value is None:
            lease.pop(key, None)
        else:
            lease[key] = value
    gk.write_lease(lease)


def events(gk, kind):
    with open(os.path.join(gk.history_root, "history.jsonl")) as f:
        return [r for r in map(json.loads, f) if r["event"] == kind]


def dev_indices_of(gk, unit):
    return sorted(c.dev_index for c in gk.chips_in_unit(unit))


def bdfs_in_call(call):
    return set(call[2].split(","))


def hold_open(tmp_path, gk, devs):
    """Point the gatekeeper at a /proc where one process holds `devs` open."""
    gk.proc_root = fake_proc_tree(
        tmp_path, {OWNER: {"ppid": 1}, HOLDER: {"ppid": 1, "devs": devs}})


# ---- 1. the attack ----

def test_a_record_naming_another_tenants_chips_resets_only_its_own(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs),
           dev_indices=dev_indices_of(gk, a.units[0]) + dev_indices_of(gk, b.units[0]))
    ok, msg = km.release(a.lease_id)
    assert ok is True
    assert len(calls) == 1 and bdfs_in_call(calls[0]) == set(a.bdfs)
    assert gk.read_lease(a.lease_id) is None
    assert gk.read_lease(b.lease_id) is not None
    assert gk.unit_lease(b.units[0])["lease_id"] == b.lease_id
    assert "using the units" in msg


def test_a_record_mismatch_is_logged(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs))
    km.release(a.lease_id)
    (rec,) = events(gk, "release_record_mismatch")
    assert rec["lease_id"] == a.lease_id


# ---- 2. missing keys ----

def test_a_record_with_no_chips_key_still_releases_and_resets_derived_chips(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    damage(gk, grant.lease_id, chips=None)
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert len(calls) == 1 and bdfs_in_call(calls[0]) == set(grant.bdfs)
    assert gk.read_lease(grant.lease_id) is None
    assert gk.unit_lease(grant.units[0]) is None
    assert gk.is_clean(grant.units[0]) is True


def test_a_record_with_no_dev_indices_key_still_releases_and_resets(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    damage(gk, grant.lease_id, dev_indices=None)
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert len(calls) == 1 and bdfs_in_call(calls[0]) == set(grant.bdfs)
    assert gk.read_lease(grant.lease_id) is None


# ---- 3. the open-device scan uses derived indices ----

def test_an_empty_dev_indices_record_does_not_hide_an_open_device(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    mine = dev_indices_of(gk, grant.units[0])
    damage(gk, grant.lease_id, dev_indices=[])
    hold_open(tmp_path, gk, [mine[0]])
    ok, msg = km.release(grant.lease_id)
    assert ok is False and "still open" in msg
    assert calls == []
    assert gk.read_lease(grant.lease_id) is not None


def test_a_record_naming_another_boards_open_device_does_not_refuse(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    theirs = dev_indices_of(gk, b.units[0])
    damage(gk, a.lease_id, dev_indices=theirs)
    hold_open(tmp_path, gk, [theirs[0]])
    ok, msg = km.release(a.lease_id)
    assert ok is True
    assert len(calls) == 1 and bdfs_in_call(calls[0]) == set(a.bdfs)


# ---- 4. a chip id that is not a PCI address ----

def test_a_non_bdf_chip_id_is_a_failed_reset_not_a_traceback(tmp_path, sysfs, monkeypatch):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    # Topology can return a non-BDF id (a dangling sysfs device link does);
    # make no id a valid BDF to reach the same path.
    monkeypatch.setattr(reset_mod, "BDF_RE", re.compile(r"^never-matches$"))
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert calls == []
    assert "refused" in msg and "not a PCI BDF" in msg
    assert "NOT marked clean" in msg
    assert gk.read_lease(grant.lease_id) is None
    assert gk.unit_lease(grant.units[0]) is None
    assert gk.is_clean(grant.units[0]) is False
    (rec,) = events(gk, "released")
    assert rec["reset_ran"] is True and rec["reset_ok"] is False


# ---- 5. a unit the topology does not know ----

def test_a_unit_unknown_to_the_topology_skips_the_reset_and_cleans_up(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    real_unit = grant.units[0]
    damage(gk, grant.lease_id, units=[real_unit])
    gk.chips_in_unit = lambda unit: []
    ok, msg = km.release(grant.lease_id)
    assert ok is True
    assert calls == []
    assert "no chips" in msg and "not resetting" in msg
    assert gk.read_lease(grant.lease_id) is None
    assert gk.unit_lease(real_unit) is None
    assert gk.is_clean(real_unit) is False
    (rec,) = events(gk, "released")
    assert rec["reset_ran"] is False


def test_a_record_with_no_units_is_cleaned_up_without_a_reset(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    damage(gk, grant.lease_id, units=[])
    ok, msg = km.release(grant.lease_id)
    assert ok is True and calls == []
    assert gk.read_lease(grant.lease_id) is None


# ---- 6. history lists the derived chips ----

def test_the_released_event_lists_the_derived_chips(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs))
    km.release(a.lease_id)
    (rec,) = events(gk, "released")
    assert sorted(rec["chips"]) == sorted(a.bdfs)


# ---- 7. an ordinary release is unchanged ----

def test_a_consistent_record_releases_without_a_mismatch_note(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:a", pid=OWNER)
    ok, msg = km.release(grant.lease_id)
    assert ok is True and "using the units" not in msg
    assert bdfs_in_call(calls[0]) == set(grant.bdfs)
    assert gk.is_clean(grant.units[0]) is True
    assert events(gk, "release_record_mismatch") == []
