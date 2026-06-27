from app.guardrails import detect_available_numbers_inquiry
from app.repository import (
    collect_taken_numbers,
    compute_available_numbers,
    expand_payment_number_list,
    parse_draw_number_pool,
)


def test_detect_available_numbers_inquiry():
    assert detect_available_numbers_inquiry("quais numeros estao disponiveis nesse sorteio?") is True
    assert detect_available_numbers_inquiry("qual meu saldo") is False


def test_compute_available_numbers():
    pool = ["1", "2", "3", "4", "5"]
    taken = {"2", "4"}
    assert compute_available_numbers(pool, taken) == ["1", "3", "5"]


def test_collect_taken_numbers_only_approved():
    rows = [
        {"numbers": ["1", "2"], "status": "approved"},
        {"numbers": ["3"], "status": "pending"},
    ]
    assert collect_taken_numbers(rows) == {"1", "2"}


def test_parse_draw_number_pool_from_total():
    draw = {"total_numbers": 5, "min_number": 1}
    assert parse_draw_number_pool(draw) == ["1", "2", "3", "4", "5"]


def test_detect_current_raffle_inquiry_open_draw():
    from app.guardrails import detect_current_raffle_inquiry

    assert detect_current_raffle_inquiry("qual sorteio esta aberto?") is True
