import pytest

from koshguard.guardrails.pii import (
    PIIVault,
    detect_pii,
    is_valid_aadhaar,
    luhn_valid,
    verhoeff_checksum,
    verhoeff_valid,
)

# 234123412346 is a synthetic number built with verhoeff_checksum (not a real Aadhaar).
VALID_AADHAAR = "234123412346"
VALID_CARD = "4111111111111111"  # standard Visa test number


class TestVerhoeff:
    def test_known_check_digit(self):
        # Published Verhoeff example: 236 -> check digit 3
        assert verhoeff_checksum("236") == 3
        assert verhoeff_valid("2363")

    def test_generated_number_validates(self):
        assert verhoeff_valid("23412341234" + str(verhoeff_checksum("23412341234")))

    @pytest.mark.parametrize("bad", ["234123412345", "234123412347", "abc", ""])
    def test_rejects_wrong_check_digit(self, bad):
        assert not verhoeff_valid(bad)

    def test_single_digit_transposition_is_caught(self):
        assert not verhoeff_valid("324123412346")

    def test_aadhaar_cannot_start_with_0_or_1(self):
        body = "13412341234"
        assert not is_valid_aadhaar(body + str(verhoeff_checksum(body)))
        assert is_valid_aadhaar("2341 2341 2346")


class TestLuhn:
    @pytest.mark.parametrize("ok", [VALID_CARD, "5555555555554444", "378282246310005"])
    def test_valid(self, ok):
        assert luhn_valid(ok)

    @pytest.mark.parametrize("bad", ["4111111111111112", "1234567812345678", "x"])
    def test_invalid(self, bad):
        assert not luhn_valid(bad)


class TestDetection:
    @pytest.mark.parametrize(
        "text,pii_type",
        [
            (f"aadhaar {VALID_AADHAAR}", "AADHAAR"),
            ("aadhaar 2341 2341 2346", "AADHAAR"),
            ("PAN ABCPE1234F", "PAN"),
            ("card 4111 1111 1111 1111", "CARD"),
            ("card 4111-1111-1111-1111", "CARD"),
            ("ifsc KOSH0001234", "IFSC"),
            ("call me on 9876543210", "MOBILE"),
            ("call me on +91 98765 43210", "MOBILE"),
            ("mail ravi.k@example.com", "EMAIL"),
            ("pay ravi@okkosh", "UPI"),
            ("account 123456789012345", "ACCOUNT"),
        ],
    )
    def test_detects_each_type(self, text, pii_type):
        assert [m.pii_type for m in detect_pii(text)] == [pii_type]

    def test_checksum_failure_is_still_protected_as_account(self):
        # Fails Verhoeff, so not an Aadhaar, but still masked as a possible account number.
        assert [m.pii_type for m in detect_pii("234123412345")] == ["ACCOUNT"]

    def test_amounts_and_helpline_are_not_pii(self):
        assert detect_pii("FD of 50000 rupees, call 1800-000-0000, rate 7.10 percent") == []

    def test_email_wins_over_upi(self):
        assert [m.pii_type for m in detect_pii("a.b@mail.co.in")] == ["EMAIL"]


class TestVault:
    def test_round_trip_and_masking(self):
        vault = PIIVault()
        text = f"My Aadhaar {VALID_AADHAAR} and email ravi@example.com"
        sanitized, _ = vault.tokenize(text)
        assert VALID_AADHAAR not in sanitized and "ravi@example.com" not in sanitized
        assert "<AADHAAR_1>" in sanitized and "<EMAIL_1>" in sanitized
        restored = vault.restore(sanitized)
        assert "ravi@example.com" in restored  # safe type restored verbatim
        assert VALID_AADHAAR not in restored and "XXXX2346" in restored  # sensitive type masked

    def test_full_restore_when_allowed(self):
        vault = PIIVault()
        sanitized, _ = vault.tokenize(f"card {VALID_CARD}")
        assert vault.restore(sanitized, restorable=frozenset({"CARD"})) == f"card {VALID_CARD}"

    def test_same_value_same_token(self):
        vault = PIIVault()
        sanitized, _ = vault.tokenize("9876543210 or +91 98765 43210, and 9123456780")
        assert sanitized.count("<MOBILE_1>") == 2 and "<MOBILE_2>" in sanitized

    def test_unknown_placeholders_left_alone(self):
        assert PIIVault().restore("see <AADHAAR_7>") == "see <AADHAAR_7>"

    def test_repr_never_shows_values(self):
        vault = PIIVault()
        vault.tokenize(f"aadhaar {VALID_AADHAAR}")
        assert VALID_AADHAAR not in repr(vault)
