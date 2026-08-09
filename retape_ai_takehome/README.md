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
3. Try payment counts from `1` to the maximum allowed count.
4. Construct payments according to the creditor rules.
5. Simulate the complete ledger date by date.
6. During simulation, collect the program fee as early as safely possible.
7. Keep the best feasible schedule according to the fee-front-loading objective.
8. If no schedule is feasible, find the minimum lump sum and monthly increment.

Existing ledger debits are fixed and are never modified.

---

### 2. Payment shapes

#### Even

When `even_pays` is true, payments are made as equally as possible.

For example:

```text
$10001 over 4 payments
→ $2500, $2500, $2500, $2501
```

Any remainder cents are placed on the latest payments so that the sequence remains non-decreasing.

#### Staircase

When both `even_pays` and `is_ballooning_allowed` are false, I use a non-decreasing staircase.

Earlier payments are kept as small as possible and the remaining amount is pushed towards later payments while respecting:

* minimum payment;
* token-pay limit;
* tier floors;
* exact settlement total;
* `max_segments`.

A staircase does not require a constant common difference. `max_segments` means the number of distinct payment levels.

For example:

```text
$25, $25, $25, $50, $50, $50
```

uses two payment levels.

#### Balloon

When `is_ballooning_allowed` is true, earlier payments are kept as small as the applicable rules allow and the final payment absorbs the remaining amount.

For example:

```text
$25, $25, $25, $25, $25, $175
```

The final payment must still satisfy the applicable non-decreasing, tier and minimum-payment constraints.

---

### 3. Front-loading the program fee

The main objective is to collect the program fee as early as possible.

The fee cannot be collected before the first creditor payment date.

However, simply taking all currently available money as fee could make a later mandatory ledger debit or creditor payment fail. Therefore, before collecting the fee, the simulation reserves enough cash for future mandatory transactions.

The order is:

```text
Current balance
      ↓
Ledger credits
      ↓
Ledger debits
      ↓
Creditor payment + bank fee
      ↓
Reserve cash required for future mandatory obligations
      ↓
Collect as much program fee as safely possible
```

The future reserve considers:

* future fixed ledger debits;
* future creditor payments;
* future bank fees;
* future ledger credits.

The program fee itself is not included in the reserve because its timing is flexible.

This means the fee is collected greedily, but without putting future mandatory payments at risk.

For example, if `$100` remains after today's mandatory payments but `$60` is required for future mandatory obligations, at most `$40` can be collected as program fee today.

A fee-only cadence date is also allowed:

```text
creditor payment = $0
bank fee          = $0
program fee       > $0
```

Such a date does not incur a bank fee.

---

### 4. Payment construction

For each possible number of payments, I construct the minimum valid non-decreasing payment requirements first.

The exact settlement amount is then distributed while preserving the creditor constraints.

For non-even, non-balloon schedules, the payment structure is limited to `max_segments` distinct levels.

For balloon schedules, the final payment absorbs the remaining amount after satisfying the earlier-payment requirements.

Every candidate is checked using the complete ledger simulation rather than assuming that satisfying the payment constraints alone makes it feasible.

---

### 5. Ledger simulation

The simulation processes all relevant dates chronologically.

It includes:

* existing ledger credits;
* existing ledger debits;
* creditor payments;
* bank fees;
* program fees;
* additional funding when evaluating Part 2.

Credits are processed before debits on the same date.

A schedule is feasible only if the balance never becomes negative.

A balance of exactly zero is valid.

No payment or fee is scheduled past `last_draft_date`.

The simulation also visits cadence dates on which there is no creditor payment so that the remaining program fee can be collected on a fee-only date when appropriate.

---

### 6. Part 2 — additional funding

If no schedule is feasible with the existing funds, I independently calculate:

**Lump sum**

Find the smallest additional credit that can be placed on or before the horizon and makes a valid schedule possible.

**Monthly increment**

Find the smallest uniform amount added to every future draft that makes a valid schedule possible.

The two values are independent and can result in different total additional funding.

The funding searches use the same feasibility logic as the original schedule.

After finding the minimum amounts, the assignment guardrails are applied:

```text
lump sum <= round(65% of offer total)

monthly increment <=
max(10000, round(40% of draft amount))
```

The minimum is calculated first; the guardrail only determines whether that minimum passes.

---

### 7. Schedule selection

I considered two possible policies.

**Path 1 — used**

The primary objective is to collect the program fee as early as possible.

For each feasible candidate, the amounts of program fee collected on successive cadence dates form a fee vector.

The vectors are compared from the earliest date onwards.

For example:

```text
Schedule A: [$70, $20, $10]
Schedule B: [$70, $25, $5]
```

The first date is tied, so the second date is compared:

```text
$25 > $20
```

Therefore Schedule B is preferred.

If the fee vectors are identical, no additional optimization is required and the first valid schedule found is returned.

**Path 2 — alternative**

A possible secondary policy would be:

1. Collect the program fee as early as possible.
2. Among equally good schedules, prefer earlier creditor recovery.
3. If still tied, prefer the schedule that finishes sooner.
4. Otherwise return any valid schedule.

Path 2 is kept as an alternative rather than part of the main objective because the assignment explicitly focuses on collecting the program fee as early as possible.

---

### 8. Assumptions

* Staircase payments do not need a constant common difference.
* `max_segments` counts distinct payment levels.
* Token and tier floors also apply when ballooning is enabled.
* The final balloon must still satisfy the applicable payment constraints.
* Existing future ledger debits are fixed.
* Credits and debits on the same date follow the required credit-before-debit ordering.
* Earlier placement of an additional lump sum is preferred when the amount is otherwise equivalent.
* Multiple schedules can be valid; equally good fee vectors do not need an additional ranking.
* All money calculations use integer cents.
* Required monetary rounding uses round-half-up.

---

### 9. Complexity

Let:

* `K` = maximum number of usable creditor payments;
* `D` = number of relevant dates in the simulation.

For each possible payment count, payment construction and ledger simulation are linear in the number of relevant dates.

The normal feasibility check is therefore approximately:

```text
O(K × D)
```

The Part 2 minimum-funding searches reuse the same feasibility check and use binary search over the funding amount, giving approximately:

```text
O(K × D × log F)
```

for each funding type, where `F` is the searched funding range in cents.

Space usage is:

```text
O(K + D)
```

This is practical for the intended use case because the schedules are monthly and the number of payment terms and relevant dates is relatively small. Even when the number of terms is increased beyond the supplied examples, the search remains small compared with problems involving large transaction streams.

---

### 10. Edge cases tested

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
* mid-month cadence and month-length clamping;
* horizon limits;
* same-day credit-before-debit ordering;
* balances reaching exactly zero;
* fee-only dates;
* fee-only dates without bank fees;
* program-fee timing;
* future mandatory debits affecting early fee collection;
* future credits offsetting future mandatory debits;
* split program-fee collection across cadence dates;
* minimum lump-sum funding;
* minimum monthly funding;
* Part 2 guardrails.
