import pytest

from d2i.checklist import CHECKS, approve, current, make_checklist, rule_number


class FakeSpark:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def spark_json(self, system, user, schema, name="result", max_out=2000):
        self.calls.append((system, user, schema))
        return dict(self.answer)


def spark(rows=2, per_row=5, item="tablet", reject_if=("empty_pocket", "broken_tablet"), unclear=()):
    return FakeSpark({"product": "vitamin C strip", "rows": rows, "per_row": per_row, "item": item,
                      "reject_if": list(reject_if), "unclear": list(unclear)})


def test_checklist_from_fake_spark():
    client = spark()
    cl = make_checklist("Ten tablets in two rows of five; reject empty or broken ones.", client)
    assert cl["count"] == 10
    assert cl["rules"][0].startswith("Rule 1: exactly 10 tablets")
    assert cl["rules"][1] == "Rule 2: no pocket may be empty"
    assert cl["rules"][2] == "Rule 3: no tablet may be broken or chipped"
    assert rule_number(cl, "broken_tablet") == 3 and rule_number(cl, "count") == 1
    assert cl["sentence"].startswith("Ten tablets") and cl["version"] is None
    system, user, schema = client.calls[0]
    assert "Ten tablets" in user and set(schema["properties"]["reject_if"]["items"]["enum"]) == set(CHECKS)


def test_layout_out_of_range_raises():
    with pytest.raises(ValueError, match="numbers"):
        make_checklist("Some tablets.", spark(rows=0, unclear=["How many rows?"]))


def test_standard_checks_are_always_on():
    cl = make_checklist("Ten tablets in two rows.", spark(reject_if=()))
    assert cl["reject_if"] == CHECKS and len(cl["rules"]) == len(CHECKS) + 1
    assert cl["rules"][3] == "Rule 4: one tablet per pocket, never two"
    assert rule_number(cl, "tablet_spot") == 6


def test_approve_versions(tmp_path):
    path = tmp_path / "checklists.jsonl"
    assert current(path) is None
    cl = make_checklist("Ten tablets in two rows of five.", spark())
    assert approve(cl, "Priya", path)["version"] == 1
    assert approve(cl, "Ravi", path)["version"] == 2
    now = current(path)
    assert now["version"] == 2 and now["approved_by"] == "Ravi" and now["approved_at"]


def test_make_checklist_uses_full_answer_budget(tmp_path):
    from d2i.meta_api import Budget, CREDIT_END_UTC, MetaClient
    from tests.conftest import FakeHTTP
    answer = {"product": "p", "rows": 2, "per_row": 5, "item": "tablet", "reject_if": ["empty_pocket"], "unclear": []}
    http = FakeHTTP([{"choices": [{"message": {"content": __import__("json").dumps(answer)}}], "usage": {}}])
    client = MetaClient(Budget(tmp_path / "s.json", clock=lambda: CREDIT_END_UTC - 3600), key="k", http=http)
    make_checklist("Ten tablets in two rows of five.", client)
    assert http.calls[0]["json"]["max_tokens"] == 4000 and http.calls[0]["json"]["reasoning_effort"] == "low"
