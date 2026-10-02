"""Keymaster.reset: reset a held lease's chips and keep the lease.

It borrows the revalidation pattern of Keymaster.release. The gate is re-read
under the mutex before the reset and again after it, and the mutex is never held
across the reset itself (a reset takes tens of seconds).
"""
import json
import os

import pytest

from conftest import QUIETBOX, fake_proc_tree
from gozer import history, keymaster as keymaster_mod
from gozer.gatekeeper import Gatekeeper
from gozer.keymaster import Keymaster

RECORD_DISAGREES = "lease record disagrees with its units"
OWNER = 5_000_100
CHILD = 5_000_200


class _Ok:
    returncode, stdout, stderr = 0, "reset ok", ""


class _Bad:
    returncode, stdout, stderr = 1, "", "tt-smi: link training failed"


def make(tmp_path, sysfs):
    """A gatekeeper over a fake /proc holding only the owner process.

    Tests that need a device holder add it after the lease is taken: a process
    that already has a device open makes its board look busy, and acquire would
    put the lease on the other board.
    """
    gk = Gatekeeper(root=str(tmp_path / "state"), sysfs_root=sysfs(QUIETBOX),
                    proc_root=fake_proc_tree(tmp_path, {OWNER: {"ppid": 1}}))
    km = Keymaster(gk)
    calls = []
    km.reset_runner = lambda argv, **kw: calls.append(argv) or _Ok()
    return km, gk, calls


def events(gk, kind):
    path = os.path.join(gk.history_root, "history.jsonl")
    with open(path) as f:
        return [r for r in map(json.loads, f) if r["event"] == kind]


def test_reset_runs_tt_smi_on_exactly_the_leased_chips_and_keeps_the_lease(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    result = km.reset(grant.lease_id)
    assert result.ok is True and result.status == "reset"
    assert len(calls) == 1 and calls[0][1] == "-r"
    assert set(calls[0][2].split(",")) == set(grant.bdfs)
    assert gk.read_lease(grant.lease_id) is not None
    assert gk.unit_lease(grant.units[0])["lease_id"] == grant.lease_id


def test_reset_does_not_mark_the_unit_clean(tmp_path, sysfs):
    # The lease continues, and release resets again; marking clean now would
    # let a later --fresh request trust silicon the workload may dirty first.
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    km.reset(grant.lease_id)
    assert gk.is_clean(grant.units[0]) is False


def test_reset_leaves_the_queue_and_claim_window_alone(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    held = km.acquire("1", who="orchard:test", pid=OWNER)
    km.acquire("1", who="claude:other", pid=OWNER)          # takes the other board
    waiting = km.acquire("1", who="claude:waiting", pid=OWNER)   # queues
    assert isinstance(waiting, dict)
    km.reset(held.lease_id)
    assert [e["ticket"] for e in gk.queue_entries()] == [waiting["ticket"]]
    assert gk.expire_and_get_claim_holder() is None


def test_reset_logs_a_reset_event(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    km.reset(grant.lease_id)
    (rec,) = events(gk, "reset")
    assert rec["lease_id"] == grant.lease_id and rec["who"] == "orchard:test"
    assert rec["chips"] == grant.bdfs and rec["reset_ok"] is True


def test_reset_of_an_unknown_lease_is_not_found(tmp_path, sysfs):
    km, _, calls = make(tmp_path, sysfs)
    result = km.reset("nope")
    assert (result.ok, result.status) == (False, "not-found")
    assert "nope" in result.message and calls == []


def test_reset_refuses_while_a_device_is_open_and_runs_nothing(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    assert 0 in grant.dev_indices
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0]}})
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "still open" in result.message and str(CHILD) in result.message
    assert calls == []
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == "device still open"


def test_reset_force_overrides_an_open_device(tmp_path, sysfs):
    km, _, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    assert 0 in grant.dev_indices
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0]}})
    result = km.reset(grant.lease_id, force=True)
    assert result.ok is True and len(calls) == 1


def test_failed_reset_keeps_the_lease_and_reports_the_output(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    km.reset_runner = lambda argv, **kw: _Bad()
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "failed")
    assert "link training failed" in result.message
    assert gk.read_lease(grant.lease_id) is not None
    assert events(gk, "reset")[0]["reset_ok"] is False


def test_reset_never_touches_chips_that_now_belong_to_another_lease(tmp_path, sysfs, monkeypatch):
    """The release race, for reset: the lease is reaped and the unit re-claimed
    while the open-device scan runs. A reset here would hit the new tenant."""
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:stale", pid=OWNER)
    unit = grant.units[0]

    def stealing_holders(proc_root, **kw):
        gk.release_unit(unit)
        gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
        return {}

    monkeypatch.setattr(keymaster_mod.procfd, "holders", stealing_holders)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "newtenant" in result.message
    assert calls == [], "reset ran against another lease's chips"
    assert gk.unit_lease(unit)["lease_id"] == "newtenant"


def test_reset_refuses_when_its_own_lock_is_already_gone(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    gk.release_unit(grant.units[0])          # reaped out from under the lease
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "no longer locked" in result.message and calls == []


def test_reset_reports_changed_hands_when_the_lease_is_taken_during_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    unit = grant.units[0]

    def runner(argv, **kw):
        gk.release_unit(unit)
        gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
        return _Ok()

    km.reset_runner = runner
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "changed-hands")
    assert "newtenant" in result.message
    assert gk.unit_lease(unit)["lease_id"] == "newtenant"     # lock left alone


def test_reset_reports_changed_hands_when_the_lease_is_reaped_during_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)

    def runner(argv, **kw):
        gk.release_unit(grant.units[0])
        return _Ok()

    km.reset_runner = runner
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "changed-hands")
    assert "reaped" in result.message


def test_reset_does_not_hold_the_mutex_across_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    held_during_reset = []

    def watching_runner(argv, **kw):
        held_during_reset.append(os.path.isdir(os.path.join(gk.root, "mutex", ".gatekeeper.lock")))
        return _Ok()

    km.reset_runner = watching_runner
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    km.reset(grant.lease_id)
    assert held_during_reset == [False]


# ---- messages, history fields and failure paths beyond the main behaviours ----

def test_a_successful_reset_with_no_output_says_reset_ok(tmp_path, sysfs):
    km, _, _ = make(tmp_path, sysfs)

    class Silent:
        returncode, stdout, stderr = 0, "", ""

    km.reset_runner = lambda argv, **kw: Silent()
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    assert km.reset(grant.lease_id).message == "reset ok"


def test_a_failed_reset_with_no_output_says_reset_failed(tmp_path, sysfs):
    km, _, _ = make(tmp_path, sysfs)

    class Silent:
        returncode, stdout, stderr = 2, "", ""

    km.reset_runner = lambda argv, **kw: Silent()
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status, result.message) == (False, "failed", "reset failed")


def test_unknown_lease_writes_no_history(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    km.reset("nope")
    assert not os.path.exists(os.path.join(gk.history_root, "history.jsonl"))


def test_a_runner_that_raises_is_a_failed_reset_and_keeps_the_lease(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)

    def boom(argv, **kw):
        raise OSError("tt-smi: not found")

    km.reset_runner = boom
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "failed")
    assert "not found" in result.message
    assert gk.read_lease(grant.lease_id) is not None
    assert events(gk, "reset")[0]["reset_ok"] is False


def test_a_history_write_failure_does_not_fail_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    gk.history_root = str(tmp_path / "no" / "such" / "dir")
    result = km.reset(grant.lease_id)
    assert result.ok is True and result.status == "reset"


def _damage(gk, lease_id, **changes):
    """Rewrite the lease record with fields replaced, or removed when None."""
    lease = gk.read_lease(lease_id)
    for key, value in changes.items():
        if value is None:
            lease.pop(key, None)
        else:
            lease[key] = value
    gk.write_lease(lease)


@pytest.mark.parametrize("field", ["units", "chips"])
def test_a_lease_record_missing_units_or_chips_is_refused(tmp_path, sysfs, field):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, **{field: None})
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "no units or chips" in result.message and calls == []
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == "lease record has no units or chips"


@pytest.mark.parametrize("field", ["units", "chips"])
def test_a_lease_record_with_empty_units_or_chips_is_refused(tmp_path, sysfs, field):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, **{field: []})
    result = km.reset(grant.lease_id)
    assert result.status == "refused" and calls == []
    assert "no units or chips" in result.message
    assert events(gk, "refused")[0]["reason"] == "lease record has no units or chips"


def test_a_malformed_chip_id_is_refused_and_runs_nothing(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, chips=["not-a-bdf"])
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "not-a-bdf" in result.message and calls == []
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == RECORD_DISAGREES
    assert events(gk, "reset") == []


def test_the_foreign_refusal_logs_the_foreign_unit(tmp_path, sysfs, monkeypatch):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:stale", pid=OWNER)
    unit = grant.units[0]
    gk.release_unit(unit)
    gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
    result = km.reset(grant.lease_id)
    assert result.status == "refused" and "refusing to reset" in result.message
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["lease_id"] == grant.lease_id
    assert rec["who"] == "orchard:stale"
    assert rec["reason"] == "units now belong to another lease"
    assert rec["foreign"] == {unit: "newtenant"}


def test_the_unheld_refusal_logs_the_unit(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    gk.release_unit(grant.units[0])
    km.reset(grant.lease_id)
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["lease_id"] == grant.lease_id
    assert rec["who"] == "orchard:test"
    assert rec["reason"] == "unit no longer locked by this lease"
    assert rec["unheld"] == list(grant.units)


def test_the_open_device_refusal_logs_the_devices(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": [0]}})
    km.reset(grant.lease_id)
    (rec,) = events(gk, "refused")
    assert rec["lease_id"] == grant.lease_id and rec["who"] == "orchard:test"
    assert rec["dev_indices"] == [0]


def test_changed_hands_keeps_the_reset_output_and_still_logs_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    unit = grant.units[0]

    def runner(argv, **kw):
        gk.release_unit(unit)
        gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
        return _Ok()

    km.reset_runner = runner
    result = km.reset(grant.lease_id)
    assert result.message.startswith("reset ok; ")
    assert "leaving the new tenant's lock alone" in result.message
    (rec,) = events(gk, "reset")
    assert rec["reset_ok"] is True and rec["lease_id"] == grant.lease_id


def test_reaped_during_reset_keeps_the_reset_output_and_logs_the_reset(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    def runner(argv, **kw):
        gk.release_unit(grant.units[0])
        return _Ok()

    km.reset_runner = runner
    result = km.reset(grant.lease_id)
    assert result.message.startswith("reset ok; ")
    assert len(events(gk, "reset")) == 1


def test_a_failed_reset_that_also_changed_hands_reports_changed_hands(tmp_path, sysfs):
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    def runner(argv, **kw):
        gk.release_unit(grant.units[0])
        return _Bad()

    km.reset_runner = runner
    result = km.reset(grant.lease_id)
    assert result.status == "changed-hands" and result.ok is False
    assert "link training failed" in result.message
    assert events(gk, "reset")[0]["reset_ok"] is False


def test_the_first_reset_check_runs_before_the_reset_not_only_after(tmp_path, sysfs):
    # Guards the pre-reset revalidation against the post-reset one alone: when
    # the lock is gone before the call, the runner must not be invoked at all.
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    gk.release_unit(grant.units[0])
    gk.claim_unit(grant.units[0], {"lease_id": "newtenant", "who": "claude:b"})
    km.reset(grant.lease_id)
    assert calls == []


def test_force_with_no_device_open_still_resets_once(tmp_path, sysfs):
    km, _, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    assert km.reset(grant.lease_id, force=True).status == "reset" and len(calls) == 1


def test_force_does_not_override_the_other_tenant_check(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    unit = grant.units[0]
    gk.release_unit(unit)
    gk.claim_unit(unit, {"lease_id": "newtenant", "who": "claude:b"})
    result = km.reset(grant.lease_id, force=True)
    assert result.status == "refused" and calls == []
    assert "now belongs to lease newtenant" in result.message


def test_both_gate_checks_run_under_the_mutex(tmp_path, sysfs):
    # Pins exactly two critical sections by design: the check before the reset
    # and the check after it. A third would mean the mutex is held for longer
    # than a gate read needs; one would mean a check ran without the mutex and
    # could race another agent's claim. Change this number only on purpose.
    km, gk, _ = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    entered = []
    real = gk.critical_section

    def counting(*a, **kw):
        entered.append(1)
        return real(*a, **kw)

    gk.critical_section = counting
    km.reset(grant.lease_id)
    assert len(entered) == 2


# ---- the lease record must agree with the units it claims ----

def two_leases(km):
    a = km.acquire("1", who="orchard:a", pid=OWNER)
    b = km.acquire("1", who="claude:b", pid=OWNER)
    assert set(a.units).isdisjoint(b.units)
    return a, b


def test_a_record_whose_chips_reach_another_leases_chips_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    _damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs))
    result = km.reset(a.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "chips" in result.message and b.bdfs[0] in result.message
    assert calls == []
    assert f"the units have {sorted(a.bdfs)}" in result.message
    assert gk.unit_lease(b.units[0])["lease_id"] == b.lease_id
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == RECORD_DISAGREES
    assert rec["mismatched"] == ["chips"]


def test_a_record_whose_dev_indices_reach_another_board_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    _damage(gk, a.lease_id, dev_indices=list(a.dev_indices) + list(b.dev_indices))
    result = km.reset(a.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "dev_indices" in result.message and calls == []
    assert events(gk, "refused")[0]["mismatched"] == ["dev_indices"]


def test_a_record_with_no_dev_indices_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, dev_indices=None)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert "dev_indices" in result.message and calls == []


def test_a_record_that_misses_one_of_its_chips_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, chips=list(grant.bdfs)[:1])
    result = km.reset(grant.lease_id)
    assert result.status == "refused" and calls == []
    assert "disagrees with its units" in result.message
    assert events(gk, "refused")[0]["reason"] == RECORD_DISAGREES


def test_a_unit_the_topology_does_not_know_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    _damage(gk, grant.lease_id, units=["ghost-board"])
    result = km.reset(grant.lease_id)
    assert result.status == "refused" and calls == []
    assert "disagrees with its units" in result.message
    (rec,) = events(gk, "refused")
    assert rec["reason"] == RECORD_DISAGREES


def test_force_does_not_skip_the_record_check(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    _damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs))
    result = km.reset(a.lease_id, force=True)
    assert result.status == "refused" and calls == []
    assert "disagrees with its units" in result.message
    assert events(gk, "refused")[0]["reason"] == RECORD_DISAGREES



def test_force_does_not_override_the_unheld_check(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    gk.release_unit(grant.units[0])
    result = km.reset(grant.lease_id, force=True)
    assert result.status == "refused" and calls == []
    assert "no longer locked" in result.message
    assert events(gk, "refused")[0]["reason"] == "unit no longer locked by this lease"


def test_a_bad_record_is_refused_for_the_record_even_with_a_device_open(tmp_path, sysfs):
    # The record check comes before the open-device scan, so the scan never
    # reads indices that were not verified against the units.
    km, gk, calls = make(tmp_path, sysfs)
    a, b = two_leases(km)
    _damage(gk, a.lease_id, chips=list(a.bdfs) + list(b.bdfs))
    fake_proc_tree(tmp_path, {CHILD: {"ppid": OWNER, "devs": list(a.dev_indices)}})
    result = km.reset(a.lease_id)
    assert result.status == "refused" and "disagrees" in result.message and calls == []


def test_a_lease_over_two_units_resets_all_of_its_chips(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("4", who="orchard:test", pid=OWNER)
    assert len(grant.units) == 2
    result = km.reset(grant.lease_id)
    assert result.status == "reset"
    assert set(calls[0][2].split(",")) == set(grant.bdfs) and len(grant.bdfs) == 4


def test_a_record_with_a_duplicated_chip_is_refused(tmp_path, sysfs):
    # A set comparison alone lets [a, b, a] through and tt-smi would be asked
    # to reset "a,b,a".
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    dup = list(grant.bdfs) + [grant.bdfs[0]]
    _damage(gk, grant.lease_id, chips=dup)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert calls == []
    assert "duplicate" in result.message and grant.bdfs[0] in result.message
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == RECORD_DISAGREES
    assert rec["mismatched"] == ["chips"]


def test_a_record_with_a_duplicated_device_index_is_refused(tmp_path, sysfs):
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    dup = list(grant.dev_indices) + [grant.dev_indices[0]]
    _damage(gk, grant.lease_id, dev_indices=dup)
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert calls == []
    assert "duplicate" in result.message and "dev_indices" in result.message
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and rec["reason"] == RECORD_DISAGREES
    assert rec["mismatched"] == ["dev_indices"]


def test_a_chip_id_that_is_not_a_pci_address_is_refused_not_raised(tmp_path, sysfs, monkeypatch):
    # The record agrees with the topology here, so the record check passes.
    # The only way to reach a non-BDF is topology itself returning one (a
    # dangling sysfs device link does that), so make no id a valid BDF.
    import re
    from gozer import reset as reset_mod
    km, gk, calls = make(tmp_path, sysfs)
    grant = km.acquire("1", who="orchard:test", pid=OWNER)
    monkeypatch.setattr(reset_mod, "BDF_RE", re.compile(r"^never-matches$"))
    result = km.reset(grant.lease_id)
    assert (result.ok, result.status) == (False, "refused")
    assert calls == []
    assert grant.bdfs[0] in result.message and "nothing was reset" in result.message
    (rec,) = events(gk, "refused")
    assert rec["action"] == "reset" and "chip id" in rec["reason"]
    assert events(gk, "reset") == []
