"""Topic scope check: keep the assistant on retail-banking support.

A support bot that will chat about anything widens the attack surface and
creates regulatory risk. In India, personalised investment advice is a
SEBI-regulated activity, so the assistant must not recommend stocks, funds or
crypto, and it must not give legal advice. Product facts (e.g. "what is the FD
rate?") are fine; recommendations ("should I put my savings in X?") are not.

The check is deliberately conservative about blocking: a message is off-topic
only when it matches an explicit off-topic pattern, or when it is a longer
message with no banking vocabulary at all. Short messages and greetings pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BANKING_TERMS = re.compile(
    r"\b(bank|banking|account|a/c|savings?|deposit|fd|rd|interest|rate|loan|emi|card|debit|credit|atm|upi|"
    r"neft|rtgs|imps|ifsc|kyc|aadhaar|pan|cheque|check ?book|balance|transfer|transaction|payment|pin|otp|"
    r"branch|charges?|fees?|fine|penalty|limit|statement|nominee|nomination|passbook|complaint|grievance|"
    r"ombudsman|refund|reversal|tds|15g|15h|netbanking|net banking|mobile banking|app|money|rupees?|lakh|"
    r"crore|salary|withdraw(al)?|closure|close|open|block|freeze|replace(ment)?|dispute|fraud|scam|"
    r"minimum|amb|cibil|credit score|prepay(ment)?|tenure|senior citizen|koshbank|kosh)\b",
    re.IGNORECASE,
)

_GREETING = re.compile(
    r"^\s*(hi|hello|hey|namaste|good (morning|afternoon|evening)|thanks|thank you|ok(ay)?|bye)\b",
    re.IGNORECASE,
)

_INVESTMENT_ADVICE = re.compile(
    r"\b(should i|recommend|suggest|advise|best|good time to|worth)\b.{0,50}"
    r"\b(invest(ing)?|buy|sell|stocks?|shares?|equity|mutual funds?|sip|crypto(currency)?|bitcoin|ipo|gold|trading)\b"
    r"|\b(which|what)\b.{0,30}\b(stocks?|shares?|mutual funds?|crypto(currency)?|bitcoin|ipo|coins?)\b.{0,30}\b(buy|pick|choose|invest|go up|returns?)\b"
    r"|\b(stock|share|crypto|bitcoin|ipo|trading) (tips?|picks?|recommendations?)\b"
    r"|\bwill\b.{0,30}\b(stock|share|nifty|sensex|bitcoin|market)\b.{0,20}\b(go up|rise|fall|crash)\b",
    re.IGNORECASE,
)

_LEGAL_ADVICE = re.compile(
    r"\b(sue|lawsuit|legal (advice|action|notice)|file a case|court case|lawyer|advocate|"
    r"(avoid|evade|dodge) (paying )?(any )?tax(es)?|tax evasion|loopholes?)\b",
    re.IGNORECASE,
)

# Creative or general-purpose tasks are refused even if they mention banking
# ("write a poem about my savings account" is still not a support request).
_GENERAL_TASK = re.compile(
    r"\b(poem|poetry|song|lyrics|joke|essay|recipe|limerick|rap|haiku|"
    r"write (me )?(a|an|some)( \w+)? (code|program|script|function|app|bot)|python code|homework)\b",
    re.IGNORECASE,
)

_OFF_TOPIC = re.compile(
    r"\b(story|weather|movie|film|cricket|football|horoscope|translate this|capital of|who won|"
    r"celebrity|dating|girlfriend|boyfriend|politics|election|religion)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TopicVerdict:
    allowed: bool
    category: str  # banking | greeting | investment_advice | legal_advice | off_topic
    reason: str = ""


def check_topic(text: str) -> TopicVerdict:
    if _INVESTMENT_ADVICE.search(text):
        return TopicVerdict(False, "investment_advice", "Personalised investment advice is out of scope.")
    if _LEGAL_ADVICE.search(text):
        return TopicVerdict(False, "legal_advice", "Legal advice is out of scope.")

    if _GENERAL_TASK.search(text):
        return TopicVerdict(False, "off_topic", "General-purpose tasks are out of scope.")

    has_banking = bool(_BANKING_TERMS.search(text))
    if _OFF_TOPIC.search(text) and not has_banking:
        return TopicVerdict(False, "off_topic", "Request is unrelated to KoshBank banking support.")
    if has_banking:
        return TopicVerdict(True, "banking")
    if _GREETING.search(text):
        return TopicVerdict(True, "greeting")
    if len(text.split()) >= 6:
        return TopicVerdict(False, "off_topic", "No banking topic detected.")
    return TopicVerdict(True, "banking")
