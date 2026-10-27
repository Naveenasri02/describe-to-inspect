import json
from datetime import datetime

import pytest

from d2i.checklist import approve
from d2i.meta_api import MetaError
from d2i.audit import AuditLog, config_hashes
from d2i.record import ask, shift_record
from tests.test_station import CHECKLIST

T0 = 1_800_000_000.0
R2 = {"rule": 2, "check": "empty_pocket", "pocket": 7, "text": "Rule 2 failed: pocket 7 is empty"}
R1 = {"rule": 1, "check": "count", "pocket": None, "text": "Rule 1 failed: found 9 pockets, expected 10"}


def _at(t):
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _verdict(i, status, reasons=()):
    return {"status": status, "reasons": list(reasons), "pocket_scores": [], "strip_id": i,
            "image_name": f"s{i}.png", "at": _at(T0 + i), "checklist_version": 1}


@pytest.fixture
def product(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    rows = [_verdict(i, "PASS") for i in range(3)] + [_verdict(3, "REJECT", [R2]), _verdict(4, "REJECT", [R2]),
                                                      _verdict(5, "HOLD", [R1])]
    (tmp_path / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    test = {"passed": True, "results": [{"kind": "empty_pocket", "pocket": 3, "caught": True, "named_right": True}],
            "good_passed": True, "failed": [], "time": T0 + 1, "at": _at(T0 + 1), "checklist_version": 1, "taught_at": T0}
    (tmp_path / "selftests.jsonl").write_text(json.dumps(test) + "\n", encoding="utf-8")
    return tmp_path


class Fake:
    def __init__(self, out=None, err=None):
        self.out, self.err, self.calls = out, err, []

    def spark_json(self, system, user, schema, **kw):
        self.calls.append((system, user, schema, kw))
        if self.err:
            raise self.err
        return self.out


def test_record_contents(product):
    html = shift_record(product, T0 - 10, T0 + 100, "B-42", "Asha")
    for s in ["DRAFT — needs QA signature", "Rule 2", "<td>2</td>", "vitamin C strip", "B-42", "Asha",
              "signature", "Priya", "Ten tablets in two rows of five.", "s5.png", "Rule 1 failed",
              "This bench adds to the physical challenge test; it does not replace it."]:
        assert s in html
    assert "Summary not available" in html


def test_record_filters_by_time(product):
    html = shift_record(product, T0 + 3.5, T0 + 100, "B", "A")
    assert "Rule 1 failed" in html  # held strip 5 is in range
    assert "<td>0</td><td>1</td><td>1</td><td>0</td><td>0</td>" in html  # strips 4 (REJECT) and 5 (HOLD) only


def test_ask_sends_records_and_returns_answer(product):
    fake = Fake({"answer": "Rule 2, pocket 7."})
    assert ask("Why was strip 3 rejected?", product, fake) == "Rule 2, pocket 7."
    system, user, schema, kw = fake.calls[0]
    assert "Rule 2 failed: pocket 7 is empty" in user and "Why was strip 3 rejected?" in user
    assert "never change a verdict" in system and "max_out" not in kw


def test_ask_propagates_errors(product):
    with pytest.raises(MetaError):
        ask("q", product, Fake(err=MetaError("down")))


def test_record_without_client_says_not_available(product):
    assert "Summary not available" in shift_record(product, 0, T0 + 100, "B", "A")


def test_record_with_summary_and_escaping(product):
    fake = Fake({"summary": "Three passed. <b>x</b>"})
    html = shift_record(product, 0, T0 + 100, "B<1>", "A", client=fake)
    assert "Shift summary (written by Muse Spark — check before signing)" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "B&lt;1&gt;" in html and "B<1>" not in html


def test_record_summary_error_falls_back(product):
    html = shift_record(product, 0, T0 + 100, "B", "A", client=Fake(err=MetaError("down")))
    assert "Summary not available" in html


def test_record_shows_chain_and_hashes(product):
    (product / "model.pt").write_bytes(b"weights")
    audit = AuditLog(product / "audit.jsonl")
    audit.append("teach", "Asha", {})
    audit.append("sign", "Priya", {})
    sha = config_hashes(product)["model_sha256"]
    html = shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    assert "Audit chain: intact (2 entries)" in html and sha[:12] in html and "tamper-evident" in html
    (product / "audit.jsonl").write_text((product / "audit.jsonl").read_text().replace("Asha", "Mallory"))
    assert "Audit chain: BROKEN at entry 1" in shift_record(product, 0, T0 + 100, "B", "A", audit=audit)


def test_record_summary_missing_key_falls_back(product):
    assert "Summary not available" in shift_record(product, 0, T0 + 100, "B", "A", client=Fake({"other": "x"}))


def test_rejects_sorted_by_rule_number(product):
    r10 = dict(R2, rule=10)
    rows = [_verdict(9, "REJECT", [R2]), _verdict(10, "REJECT", [r10])]
    (product / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    html = shift_record(product, 0, T0 + 100, "B", "A")
    assert html.index("Rule 2<") < html.index("Rule 10<")


def test_record_renders_broken_chain_with_stripped_keys(product):
    audit = AuditLog(product / "audit.jsonl")
    for i in range(3):
        audit.append("act", "Asha", {"i": i})
    rows = [json.loads(x) for x in (product / "audit.jsonl").read_text().splitlines()]
    del rows[1]["user"]
    rows[2]["at"] = "garbage"
    (product / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    html = shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    assert "Audit chain: BROKEN at entry 2" in html
