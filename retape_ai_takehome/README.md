# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests (pass out of the box)
│   └── test_cases.py        # example expectations — make these pass, then add your own
├── run.py                   # python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
python run.py cases/case1_feasible_even

# tests
pytest -q
```

Out of the box, `tests/test_smoke.py` passes and `tests/test_cases.py` fails —
the latter is your target. Go beyond those four cases with your own tests.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.



## Implementation Notes

### 1. Approach

I treat the problem as a constrained payment-scheduling problem.

The main flow is:

1. Calculate the settlement amount and total program fee.
2. Generate the creditor payment dates from `first_payment_date`.
3. Try valid payment counts from `1` to `min(max_terms, max_payments)`.
4. Construct the payment amounts according to the creditor flags.
5. Allocate the program fee as early as possible.
6. Simulate the complete ledger date by date.
7. Return the first valid schedule.
8. If no schedule is feasible, find the minimum lump sum and minimum monthly increment.

The ledger simulation is the final feasibility check. Existing ledger debits are treated as fixed and are never modified.

---

### 2. Payment shapes

#### Even

When `even_pays` is true, payments are made as equally as possible.

For example:

```text
$10001 over 4 payments
→ $2500, $2500, $2500, $2501
```

Any remainder cents are placed on the latest payments so that payments remain non-decreasing.

#### Staircase

When both `even_pays` and `is_ballooning_allowed` are false, I use a non-decreasing staircase.

Earlier payments are kept as small as possible and the remaining amount is pushed towards later payments, while respecting:

* minimum payment;
* token-pay limit;
* tier floors;
* exact settlement total;
* `max_segments`.

A staircase does not require a constant common difference. `max_segments` refers to the number of distinct payment levels.

For example:

```text
$25, $25, $25, $50, $50, $50
```

has two payment levels.

#### Balloon

When `is_ballooning_allowed` is true, the final payment absorbs the remaining amount.

For example:

```text
$25, $25, $25, $25, $25, $175
```

The earlier payments are kept as small as the applicable rules allow and the final payment contains the remainder.

Token and tier restrictions still apply to the earlier payments.

---

### 3. Front-loading the program fee

The main objective is to collect Retape's program fee as early as possible.

The program fee cannot be collected before the first creditor payment date.

For each eligible date, the available money is used in this order:

```text
available balance
        ↓
creditor payment + bank fee
        ↓
program fee
        ↓
remaining escrow balance
```

Therefore, keeping early creditor payments low naturally leaves more money available for the program fee.

The fee is collected greedily from the earliest eligible cadence dates and must be completely collected by the horizon.

A date containing only a program-fee payment does not incur a bank fee.

---

### 4. Payment construction

For each possible number of payments, I first build the minimum valid non-decreasing payment requirements.

The exact settlement amount is then distributed while preserving those requirements.

For non-even, non-balloon schedules, the construction is limited to `max_segments` distinct payment levels.

For balloon schedules, the final payment is allowed to absorb the remaining amount.

Every candidate is subsequently checked by the full ledger simulation rather than assuming that satisfying the payment constraints alone makes it feasible.

---

### 5. Ledger simulation

The simulation processes all relevant dates chronologically.

It includes:

* existing ledger credits;
* existing ledger debits;
* creditor payments;
* bank fees;
* program fees.

Credits are processed before debits on the same date.

A schedule is feasible only if the balance never becomes negative.

A balance of exactly zero is valid.

No payment or fee is allowed after `last_draft_date`.

---

### 6. Part 2 — additional funding

If no schedule is feasible with the existing funds, I independently calculate:

**Lump sum**

Find the smallest additional credit that can be placed on or before the horizon and makes a valid schedule possible.

**Monthly increment**

Find the smallest amount added to every future draft that makes a valid schedule possible.

The two values are independent and can result in different total additional funding.

The required guardrails are then applied:

```text
lump sum <= 65% of offer total

monthly increment <=
max($100, 40% of original draft)
```

The funding searches use the same feasibility engine as the original problem.

---

### 7. Schedule selection

I considered two possible policies.

**Path 1 — used**

The primary objective is to collect the program fee as early as possible.

If multiple schedules satisfy the same objective, the first valid schedule found is returned.

This is the simpler interpretation and stays closest to the explicit objective in the assignment.

**Path 2 — alternative**

A possible secondary policy would be:

1. Collect the program fee as early as possible.
2. Among equally good schedules, prefer earlier creditor recovery.
3. If still tied, prefer the schedule that finishes sooner.
4. Otherwise return any valid schedule.

I kept Path 2 as an alternative rather than making it part of the main objective because the assignment does not explicitly require creditor recovery to be optimized after the program-fee objective.

---

### 8. Assumptions

* Staircase payments do not need a constant common difference.
* The final balloon is simply the remaining creditor amount after satisfying earlier-payment constraints.
* Token and tier floors apply to balloon schedules as well.
* Existing future ledger debits are fixed.
* Earlier placement of an additional lump sum is preferred when the amount is otherwise equivalent.
* Multiple schedules can be valid; no attempt is made to impose an unnecessary total ordering on equally good schedules.
* All money calculations are performed in integer cents using round-half-up where rounding is required.

---

### 9. Edge cases tested

The test suite covers the provided cases as well as:

* exact settlement sums;
* even-payment remainders;
* non-decreasing payments;
* token-pay limits;
* tier floors;
* `max_segments`;
* balloon payments;
* payment-count limits;
* end-of-month cadence;
* horizon limits;
* same-day credit-before-debit ordering;
* balances reaching exactly zero;
* fee-only dates;
* bank-fee handling;
* program-fee timing;
* minimum lump-sum funding;
* minimum monthly funding;
* Part 2 guardrails.
