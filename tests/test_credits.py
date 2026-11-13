from src.main_tab.helpers import credits_left


def test_credits_left_reads_the_credits_limit():
    response = {
        "limits": [
            {"name": "agent_analysis", "left_amount": 3, "total_amount": 5},
            {"name": "credits", "left_amount": 0, "total_amount": 0},
        ]
    }
    assert credits_left(response) == 0


def test_credits_left_is_unknown_without_a_credits_limit():
    assert credits_left(None) is None
    assert credits_left({"limits": "invalid"}) is None
    assert credits_left({"limits": [{"name": "agent_analysis", "left_amount": 3}]}) is None
    assert credits_left({"limits": [{"name": "credits", "left_amount": None}]}) is None
