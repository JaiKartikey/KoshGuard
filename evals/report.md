# KoshGuard eval report

Generated 2026-10-25 12:20 UTC with LLM mode **mock** on 16 golden questions.
Regenerate with `python evals/run_evals.py` (add `--live` for the real model and LLM-as-judge).

| Metric | Value |
|---|---|
| Retrieval hit@1 | 100.0% (16/16) |
| Retrieval hit@3 | 100.0% (16/16) |
| Answered (not refused) | 100.0% (16/16) |
| Fact accuracy | 93.8% (15/16) |
| Faithfulness (LLM judge) | not run (offline) |

| Question | Expected source | hit@1 | hit@3 | Outcome | Facts OK |
|---|---|---|---|---|---|
| What is the additional FD interest for senior citizens? | fixed_deposits.md | yes | yes | answered | yes |
| What is the minimum amount for a fixed deposit? | fixed_deposits.md | yes | yes | answered | yes |
| What is the penalty for premature withdrawal of an FD? | fixed_deposits.md | yes | yes | answered | no |
| What is the average monthly balance for Kosh Premium? | savings_account.md | yes | yes | answered | yes |
| What interest does a savings account earn on balances above 1 lakh? | savings_account.md | yes | yes | answered | yes |
| What is the default daily ATM withdrawal limit on the debit card? | debit_card.md | yes | yes | answered | yes |
| Within how many days should I report an unauthorised transaction for zero liability? | debit_card.md | yes | yes | answered | yes |
| What is the UPI limit for a newly registered device? | upi.md | yes | yes | answered | yes |
| How long does a failed UPI transaction take to be reversed? | upi.md | yes | yes | answered | yes |
| How often must low-risk customers update KYC? | kyc.md | yes | yes | answered | yes |
| What are the video KYC timings? | kyc.md | yes | yes | answered | yes |
| What is the starting interest rate for home loans? | loans_faq.md | yes | yes | answered | yes |
| What is the bounce charge for a failed EMI? | loans_faq.md | yes | yes | answered | yes |
| How long does the Nodal Officer take to respond to a complaint? | grievance_redressal.md | yes | yes | answered | yes |
| What is the charge per ATM transaction after the free limit? | fees_and_charges.md | yes | yes | answered | yes |
| What is the annual fee for a Premium debit card? | fees_and_charges.md | yes | yes | answered | yes |
