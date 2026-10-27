from d2i.commands import match


def test_match_english():
    assert match("please start the self test") == "self_test"
    assert match("Inspect this strip") == "inspect"
    assert match("hold the line") == "hold"


def test_match_tamil():
    assert match("சுய சோதனை தொடங்கு") == "self_test"
    assert match("நிறுத்து") == "hold"


def test_no_match_returns_none():
    assert match("good morning") is None and match("") is None
