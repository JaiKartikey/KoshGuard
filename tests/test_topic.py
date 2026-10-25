import pytest

from koshguard.guardrails.topic import check_topic


@pytest.mark.parametrize(
    "text,category",
    [
        ("Which stocks should I buy this year?", "investment_advice"),
        ("Should I invest my savings in bitcoin?", "investment_advice"),
        ("Can I sue my landlord?", "legal_advice"),
        ("Write a poem about savings accounts", "off_topic"),
        ("What is the weather in Mumbai today?", "off_topic"),
    ],
)
def test_refuses_out_of_scope(text, category):
    verdict = check_topic(text)
    assert not verdict.allowed and verdict.category == category


@pytest.mark.parametrize(
    "text",
    ["What is the FD rate?", "hi", "Thanks!", "What are the options to open an account?", "UPI limit?"],
)
def test_allows_banking_and_small_talk(text):
    assert check_topic(text).allowed
