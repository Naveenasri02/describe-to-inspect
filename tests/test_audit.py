import json

import pytest

from d2i.audit import (AuditLog, Users, log_physical_challenge, quarantine_list, release_quarantine, sign_record)
from tests.test_record import T0, _at, _verdict


def test_chain_detects_edit(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    assert log.verify() == (True, None)
    for i in range(3):
        log.append("act", "u", {"i": i}, reason=f"r{i}")
    assert log.verify() == (True, None)
    rows = [json.loads(x) for x in (tmp_path / "a.jsonl").read_text().splitlines()]
    rows[1]["reason"] = "changed"
    (tmp_path / "a.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert log.verify() == (False, 2)


def test_concurrent_appends_keep_the_chain(tmp_path):
    import threading
    log = AuditLog(tmp_path / "a.jsonl")

    def write(who):
        for i in range(40):
            log.append("act", who, {"i": i})
    threads = [threading.Thread(target=write, args=(w,)) for w in ("agent", "qa")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log.verify() == (True, None) and len(log.entries()) == 80


def test_users_never_store_plain_password(tmp_path):
    u = Users(tmp_path / "users.json")
    u.add("priya", "qa", "s3cret-pw")
    assert "s3cret-pw" not in (tmp_path / "users.json").read_text()
    assert u.check("priya", "s3cret-pw") == "qa" and u.check("priya", "bad") is None
    with pytest.raises(ValueError):
        u.add("x", "boss", "p")


def _selftest(t, passed):
    return {"passed": passed, "results": [], "time": t, "at": _at(t), "checklist_version": 1, "taught_at": T0}


def _setup(tmp_path):
    rows = [_verdict(i, "PASS") for i in range(3)]
    (tmp_path / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    tests = [_selftest(T0 - 1, True), _selftest(T0 + 10, False)]
    (tmp_path / "selftests.jsonl").write_text("\n".join(json.dumps(r) for r in tests) + "\n")
    return tmp_path


def test_quarantine_lists_strips_since_last_pass(tmp_path):
    q = quarantine_list(_setup(tmp_path))
    assert len(q["strips"]) == 3 and q["since"] == _at(T0 - 1) and q["failed_at"] == _at(T0 + 10)
    (tmp_path / "selftests.jsonl").write_text(json.dumps(_selftest(T0, True)) + "\n")
    assert quarantine_list(tmp_path) == {"since": None, "failed_at": None, "strips": []}


def test_release_needs_qa_and_reason(tmp_path):
    _setup(tmp_path)
    audit, users = AuditLog(tmp_path / "audit.jsonl"), Users(tmp_path / "users.json")
    users.add("op", "operator", "pw1")
    users.add("qa1", "qa", "pw2")
    with pytest.raises(PermissionError):
        release_quarantine(tmp_path, audit, users, "op", "pw1", "ok")
    with pytest.raises(PermissionError):
        release_quarantine(tmp_path, audit, users, "qa1", "wrong", "ok")
    with pytest.raises(ValueError):
        release_quarantine(tmp_path, audit, users, "qa1", "pw2", " ")
    e = release_quarantine(tmp_path, audit, users, "qa1", "pw2", "re-checked by hand")
    assert e["action"] == "release_quarantine" and e["details"]["strips"] == 3 and audit.verify() == (True, None)


def test_physical_challenge_logged(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    e = log_physical_challenge(audit, "op", "CH-1", True)
    assert e["action"] == "physical_challenge" and e["details"] == {"strip_label": "CH-1", "ejected": True}


def test_sign_requires_password(tmp_path):
    audit, users = AuditLog(tmp_path / "audit.jsonl"), Users(tmp_path / "users.json")
    users.add("op", "operator", "pw1")
    users.add("qa1", "qa", "pw2")
    for who, pw in [("qa1", "wrong"), ("op", "pw1")]:
        with pytest.raises(PermissionError):
            sign_record("<html>", audit, users, who, pw)
    assert audit.entries() == []
    s = sign_record("<html>", audit, users, "qa1", "pw2")
    assert s["signed_by"] == "qa1" and len(s["record_sha256"]) == 64
    assert audit.entries()[0]["action"] == "sign"


def _three(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.append("act", "u", {"i": i})
    return log


def _garble(tmp_path):
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    lines[1] = "{not json"
    (tmp_path / "a.jsonl").write_text("\n".join(lines) + "\n")


def test_verify_garbled_line_reports_break(tmp_path):
    log = _three(tmp_path)
    _garble(tmp_path)
    assert log.verify() == (False, 2)
    assert len(log.entries()) == 2 and len(log.entries(since="2000-01-01T00:00:00+00:00")) == 2


def test_append_refuses_on_damaged_log(tmp_path):
    log = _three(tmp_path)
    _garble(tmp_path)
    with pytest.raises(ValueError, match="damaged at entry 2"):
        log.append("act", "u", {})


def test_verify_survives_cut_multibyte_char(tmp_path):
    from d2i.record import shift_record
    log = AuditLog(tmp_path / "a.jsonl")
    log.append("act", "u", {})
    log.append("act", "u", {}, reason="சோதனை")
    log.append("act", "u", {})
    lines = (tmp_path / "a.jsonl").read_bytes().split(b"\n")
    lines[1] = lines[1][:lines[1].index("சோ".encode()) + 1]  # cut inside the first Tamil character
    (tmp_path / "a.jsonl").write_bytes(b"\n".join(lines))
    assert log.verify() == (False, 2)
    assert "BROKEN" in shift_record(tmp_path, 0, 4_000_000_000, "B", "A", audit=log)


def test_quarantine_stays_open_until_qa_release(tmp_path):
    import time
    now = time.time()
    tests = [_selftest(now - 100, True), _selftest(now - 50, False)]
    strips = [dict(_verdict(i, "PASS"), at=_at(now - 40 + i)) for i in range(3)]
    (tmp_path / "log.jsonl").write_text("\n".join(json.dumps(r) for r in strips) + "\n")
    tests.append(_selftest(now - 10, True))  # a later passing self-test
    (tmp_path / "selftests.jsonl").write_text("\n".join(json.dumps(r) for r in tests) + "\n")
    audit, users = AuditLog(tmp_path / "audit.jsonl"), Users(tmp_path / "users.json")
    users.add("qa1", "qa", "pw2")
    assert quarantine_list(tmp_path)["strips"] == []  # without the audit: old rule, the passing self-test closes it
    q = quarantine_list(tmp_path, audit)
    assert len(q["strips"]) == 3 and q["since"] == _at(now - 100) and q["failed_at"] == _at(now - 50)
    assert release_quarantine(tmp_path, audit, users, "qa1", "pw2", "re-checked by hand")["details"]["strips"] == 3
    assert quarantine_list(tmp_path, audit) == {"since": None, "failed_at": None, "strips": []}
