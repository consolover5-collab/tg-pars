import pytest

from bot.price import extract_price


@pytest.mark.parametrize("text,expected", [
    ("Продам за 15 000 руб", 15000),
    ("цена 15000р", 15000),
    ("15к", 15000),
    ("15.5к", 15500),
    ("2 тыс", 2000),
    ("100€", 100),
    ("50 евро", 50),
    ("$120", 120),
    ("отдам даром", None),
    ("", None),
])
def test_extract_price(text, expected):
    assert extract_price(text) == expected
