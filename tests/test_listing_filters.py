import pytest

from bot.nlp import is_not_listing, looks_like_listing


@pytest.mark.parametrize("text", [
    "Продаю колонку JBL, 5000 руб",
    "Продам телевизор Samsung, цена 30000",
    "Отдам за 100€",
])
def test_listings_pass(text):
    assert looks_like_listing(text) is True
    assert is_not_listing(text) is False


@pytest.mark.parametrize("text", [
    "Кто может перевезти диван?",
    "Ищу мастера по ремонту",
    "Вакансия: набираем курьеров, заработок 3000 в день",
    "Сдаётся квартира на месяц",
])
def test_non_listings_filtered(text):
    assert is_not_listing(text) is True
    assert looks_like_listing(text) is False
