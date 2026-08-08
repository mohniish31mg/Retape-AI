"""Candidate implementation goes here.

Implement `evaluate_offer` so that it satisfies the rules in ASSIGNMENT.md and
the example expectations in tests/test_cases.py. The dataclasses below define the
required OUTPUT shape (see ASSIGNMENT.md "Output"). You may add helpers, modules,
or rewrite internals freely, but keep `evaluate_offer`'s signature and the
serialized shape of `Result` (so the runner and tests work).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from itertools import combinations

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
)


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )

        if self.additional_funds is None:
            out["additional_funds"] = None
        else:

            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }

                if o.date is not None:
                    d["date"] = o.date.isoformat()

                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts

                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }

        return out


# ---------------------------------------------------------------------------
# Basic calculations
# ---------------------------------------------------------------------------


def _round_half_up(value: Decimal | float | int) -> int:
    """Use the round-half-up rule required by the assignment."""
    return int(
        Decimal(str(value)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _offer_total(offer: Offer) -> int:
    return _round_half_up(
        Decimal(str(offer.settlement_pct))
        * Decimal(offer.current_balance_cents)
    )


def _program_fee(offer: Offer, rules: CreditorRules) -> int:
    return _round_half_up(
        Decimal(str(rules.program_fee_pct))
        * Decimal(offer.original_balance_cents)
    )


# ---------------------------------------------------------------------------
# Payment constraints
# ---------------------------------------------------------------------------


def _effective_floors(k: int, rules: CreditorRules) -> list[int]:
    """Build the minimum valid payment at every payment position."""
    tiers = sorted(rules.min_payment_tiers)

    floors: list[int] = []
    token_pays = 0
    previous = 0

    for position in range(1, k + 1):
        floor = rules.min_payment_cents

        # Apply every tier which has started by this payment position.
        for from_payment, minimum in tiers:
            if from_payment <= position:
                floor = max(floor, minimum)
            else:
                break

        # Once the token-pay allowance is exhausted, a payment which would
        # otherwise sit at the base minimum must be strictly greater.
        if (
            floor == rules.min_payment_cents
            and token_pays >= rules.max_token_pays
        ):
            floor = rules.min_payment_cents + 1

        # Payments must be non-decreasing.
        floor = max(floor, previous)

        floors.append(floor)

        if floor == rules.min_payment_cents:
            token_pays += 1

        previous = floor

    return floors


def _valid_payments(
    payments: list[int],
    total: int,
    rules: CreditorRules,
    k: int,
) -> bool:
    """Final validation for a proposed creditor-payment sequence."""

    if len(payments) != k:
        return False

    # Creditor payments must exactly equal the settlement amount.
    if sum(payments) != total:
        return False

    # Base minimum.
    if any(payment < rules.min_payment_cents for payment in payments):
        return False

    # Payments must never decrease.
    if any(payments[i] > payments[i + 1] for i in range(k - 1)):
        return False

    # Only a limited number may equal the base minimum.
    if (
        sum(payment == rules.min_payment_cents for payment in payments)
        > rules.max_token_pays
    ):
        return False

    # Check all explicit tier floors.
    for position, payment in enumerate(payments, start=1):
        floor = rules.min_payment_cents

        for from_payment, minimum in rules.min_payment_tiers:
            if from_payment <= position:
                floor = max(floor, minimum)

        if payment < floor:
            return False

    return True


# ---------------------------------------------------------------------------
# Payment shapes
# ---------------------------------------------------------------------------


def _build_even_payments(
    total: int,
    k: int,
    rules: CreditorRules,
) -> list[int] | None:
    """Build the required as-even-as-possible payment sequence."""

    quotient, remainder = divmod(total, k)

    # Put the extra cents on the latest payments so the sequence remains
    # non-decreasing.
    payments = [quotient] * (k - remainder)
    payments.extend([quotient + 1] * remainder)

    if _valid_payments(payments, total, rules, k):
        return payments

    return None


def _build_balloon_payments(
    total: int,
    k: int,
    rules: CreditorRules,
) -> list[int] | None:
    """Keep early payments at their minimum and put the remainder at the end."""

    floors = _effective_floors(k, rules)

    if sum(floors) > total:
        return None

    # Everything except the final payment stays at its minimum.
    payments = floors[:-1]

    # The final payment absorbs everything that remains.
    payments.append(total - sum(payments))

    if _valid_payments(payments, total, rules, k):
        return payments

    return None


def _build_staircase_payments(
    total: int,
    k: int,
    rules: CreditorRules,
) -> list[int] | None:
    """Build a front-loaded staircase using at most max_segments levels.

    The exact step locations are intentionally not prescribed by the
    assignment. We try possible contiguous segment layouts and select the
    lexicographically smallest valid payment sequence we can construct.
    """

    floors = _effective_floors(k, rules)

    if sum(floors) > total or rules.max_segments <= 0:
        return None

    best: tuple[int, ...] | None = None
    max_groups = min(rules.max_segments, k)

    # Each group represents a consecutive set of payments having the same
    # payment level.
    for groups in range(1, max_groups + 1):

        for cuts in combinations(range(1, k), groups - 1):
            bounds = (0,) + cuts + (k,)

            lengths = [
                end - start
                for start, end in zip(bounds, bounds[1:])
            ]

            # The first valid level for each segment is the highest floor
            # occurring inside that segment.
            levels = [
                max(floors[start:end])
                for start, end in zip(bounds, bounds[1:])
            ]

            base_sum = sum(
                level * length
                for level, length in zip(levels, lengths)
            )

            if base_sum > total:
                continue

            remaining = total - base_sum
            last_len = lengths[-1]

            # We first try to use earlier levels to fix the remainder.
            # This keeps earlier creditor payments as small as possible.
            variable_count = groups - 1
            choices: list[int] = []

            for index in range(variable_count):
                gap = levels[index + 1] - levels[index]

                # Crossing the next level would effectively change the
                # segment structure, which is represented by another
                # partition.
                choices.append(min(gap, last_len - 1))

            # DP by remainder modulo the size of the final segment.
            #
            # min_cost[i][r] = smallest amount which the remaining earlier
            # segments can contribute while producing remainder r.
            min_cost = [{0: 0} for _ in range(variable_count + 1)]

            for index in range(variable_count - 1, -1, -1):
                table: dict[int, int] = {}
                length = lengths[index]

                for residue, cost in min_cost[index + 1].items():
                    for increase in range(choices[index] + 1):
                        added = increase * length
                        new_residue = (residue + added) % last_len
                        new_cost = cost + added

                        old_cost = table.get(new_residue)

                        if old_cost is None or new_cost < old_cost:
                            table[new_residue] = new_cost

                min_cost[index] = table

            increases: list[int] = []
            used = 0
            possible = True

            # Choose the smallest increase for every earlier segment which
            # still allows the remaining amount to be represented.
            for index in range(variable_count):
                chosen: int | None = None
                length = lengths[index]

                for increase in range(choices[index] + 1):
                    added = increase * length

                    if used + added > remaining:
                        break

                    needed = (
                        remaining - used - added
                    ) % last_len

                    if needed in min_cost[index + 1]:
                        min_remaining = min_cost[index + 1][needed]

                        if used + added + min_remaining <= remaining:
                            chosen = increase
                            break

                if chosen is None:
                    possible = False
                    break

                increases.append(chosen)
                used += chosen * length

            if not possible:
                continue

            final_extra = remaining - used

            if final_extra < 0 or final_extra % last_len != 0:
                continue

            for index, increase in enumerate(increases):
                levels[index] += increase

            levels[-1] += final_extra // last_len

            payments: list[int] = []

            for level, length in zip(levels, lengths):
                payments.extend([level] * length)

            if len(set(payments)) > rules.max_segments:
                continue

            if not _valid_payments(payments, total, rules, k):
                continue

            candidate = tuple(payments)

            if best is None or candidate < best:
                best = candidate

    return list(best) if best is not None else None


def _build_payment_candidate(
    total: int,
    k: int,
    rules: CreditorRules,
) -> tuple[list[int] | None, str]:

    # even_pays has priority because the assignment explicitly requires
    # ballooning to be irrelevant when even payments are enabled.
    if rules.even_pays:
        return _build_even_payments(total, k, rules), "even"

    if rules.is_ballooning_allowed:
        return _build_balloon_payments(total, k, rules), "balloon"

    return _build_staircase_payments(total, k, rules), "staircase"


# ---------------------------------------------------------------------------
# Candidate schedules
# ---------------------------------------------------------------------------


def _candidate_schedules(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
):
    """Generate structurally valid payment plans."""

    total = _offer_total(offer)

    first_payment_date = (
        offer.first_payment_date
        or default_first_payment_date(client)
    )

    max_payments = min(
        rules.max_terms,
        rules.max_payments,
    )

    for k in range(1, max_payments + 1):

        payment_dates = monthly_payment_dates(
            first_payment_date,
            k,
        )

        if not payment_dates:
            continue

        # A creditor payment cannot be scheduled before the current
        # simulation date.
        if payment_dates[0] <= client.as_of_date:
            continue

        # The complete payment plan must fit inside the horizon.
        if payment_dates[-1] > client.last_draft_date:
            continue

        payments, shape = _build_payment_candidate(
            total,
            k,
            rules,
        )

        if payments is not None:
            yield k, payment_dates, payments, shape


# ---------------------------------------------------------------------------
# Ledger simulation
# ---------------------------------------------------------------------------


def _simulate(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    payment_dates: list[date],
    payments: list[int],
    extra_ledger: tuple[LedgerEntry, ...] = (),
):
    """Simulate the complete account and collect the fee as early as possible."""

    fee_total = _program_fee(offer, rules)

    # Only future ledger entries are simulated. Anything on or before
    # as_of_date is already included in current_balance_cents.
    future_entries = [
        entry
        for entry in client.ledger
        if (
            entry.date > client.as_of_date
            and entry.date <= client.last_draft_date
        )
    ]

    future_entries.extend(extra_ledger)

    entries_by_date: dict[date, list[LedgerEntry]] = {}

    for entry in future_entries:
        entries_by_date.setdefault(
            entry.date,
            [],
        ).append(entry)

    payments_by_date = dict(
        zip(payment_dates, payments)
    )

    # Fee can be collected on any cadence date from the first creditor
    # payment through the horizon, including fee-only dates.
    cadence_dates = monthly_payment_dates(
        payment_dates[0],
        (
            (client.last_draft_date.year - payment_dates[0].year) * 12
            + client.last_draft_date.month
            - payment_dates[0].month
            + 1
        ),
    )

    cadence_dates = [
        payment_date
        for payment_date in cadence_dates
        if payment_date <= client.last_draft_date
    ]

    cadence_set = set(cadence_dates)

    # We need to visit every date which can affect the balance or collect fee.
    event_dates = (
        set(entries_by_date)
        | set(payment_dates)
        | cadence_set
    )

    balance = client.current_balance_cents
    remaining_fee = fee_total

    rows: list[ScheduleRow] = []

    # Amount of program fee collected on each cadence date.
    # This is used for the front-loading comparison between candidates.
    fee_vector: list[int] = []

    for current_date in sorted(event_dates):

        # Assignment requirement:
        # all ledger credits happen before all ledger debits on a date.
        credits = sum(
            entry.amount_cents
            for entry in entries_by_date.get(current_date, [])
            if entry.type == "credit"
        )

        fixed_debits = sum(
            entry.amount_cents
            for entry in entries_by_date.get(current_date, [])
            if entry.type == "debit"
        )

        balance += credits
        balance -= fixed_debits

        if balance < 0:
            return None

        creditor_payment = payments_by_date.get(
            current_date,
            0,
        )

        # Bank fee is charged only when a creditor payment is made.
        bank_fee = (
            rules.bank_fee_cents
            if creditor_payment > 0
            else 0
        )

        required_payment = (
            creditor_payment
            + bank_fee
        )

        # Creditor payment and bank fee are mandatory.
        if balance < required_payment:
            return None

        balance -= required_payment

        program_fee = 0

        # Program fee is optional in timing but mandatory in total.
        # Collect as much as possible at the earliest eligible cadence date.
        if (
            current_date in cadence_set
            and remaining_fee > 0
        ):
            program_fee = min(
                remaining_fee,
                balance,
            )

            balance -= program_fee
            remaining_fee -= program_fee

        if balance < 0:
            return None

        # Output only dates which actually carry something relevant to the
        # settlement schedule.
        if (
            creditor_payment > 0
            or program_fee > 0
        ):
            rows.append(
                ScheduleRow(
                    date=current_date,
                    creditor_payment_cents=creditor_payment,
                    program_fee_cents=program_fee,
                    bank_fee_cents=bank_fee,
                    balance_cents=balance,
                )
            )

        if current_date in cadence_set:
            fee_vector.append(program_fee)

    # The complete program fee must be collected by the horizon.
    if remaining_fee != 0:
        return None

    return rows, tuple(fee_vector)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def _find_best(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
):
    best = None

    for (
        k,
        dates,
        payments,
        shape,
    ) in _candidate_schedules(
        client,
        offer,
        rules,
    ):

        simulated = _simulate(
            client,
            offer,
            rules,
            dates,
            payments,
        )

        if simulated is None:
            continue

        rows, fee_vector = simulated

        candidate = (
            rows,
            fee_vector,
            shape,
            dates,
            payments,
        )

        if best is None:
            best = candidate
            continue

        # ---------------------------------------------------------------
        # Primary objective:
        #
        # Collect Retape's program fee as early as possible.
        #
        # Comparing the vectors lexicographically means that collecting
        # more fee on an earlier cadence date always wins over collecting
        # the same amount later.
        # ---------------------------------------------------------------

        best_fee = list(best[1])
        current_fee = list(fee_vector)

        length = max(
            len(best_fee),
            len(current_fee),
        )

        best_fee.extend(
            [0] * (length - len(best_fee))
        )

        current_fee.extend(
            [0] * (length - len(current_fee))
        )

        if tuple(current_fee) > tuple(best_fee):
            best = candidate
            continue

        if tuple(current_fee) < tuple(best_fee):
            continue

        # ---------------------------------------------------------------
        # Path 1 tie-breaker:
        #
        # If Retape receives its fee equally early, prefer the schedule
        # which gives the creditor more money earlier.
        # ---------------------------------------------------------------

        best_payments = list(best[4])
        current_payments = list(payments)

        length = max(
            len(best_payments),
            len(current_payments),
        )

        best_payments.extend(
            [0] * (length - len(best_payments))
        )

        current_payments.extend(
            [0] * (length - len(current_payments))
        )

        if tuple(current_payments) > tuple(best_payments):
            best = candidate
            continue

        if tuple(current_payments) < tuple(best_payments):
            continue

        # ---------------------------------------------------------------
        # Final deterministic tie-breaker:
        #
        # If both fee collection and creditor recovery are identical,
        # prefer the plan which finishes sooner.
        # ---------------------------------------------------------------

        if len(dates) < len(best[3]):
            best = candidate

    return best


# ---------------------------------------------------------------------------
# Part 2 - additional funding
# ---------------------------------------------------------------------------


def _with_lump(
    client: Client,
    lump_date: date,
    amount: int,
) -> Client:
    """Return a client with one additional future credit."""

    return replace(
        client,
        ledger=client.ledger
        + [
            LedgerEntry(
                lump_date,
                amount,
                "credit",
            )
        ],
    )


def _with_monthly_increment(
    client: Client,
    amount: int,
) -> tuple[Client, int]:
    """Add the same amount to every future draft."""

    ledger: list[LedgerEntry] = []
    num_drafts = 0

    for entry in client.ledger:

        if (
            entry.date > client.as_of_date
            and entry.date <= client.last_draft_date
            and entry.type == "credit"
        ):
            ledger.append(
                LedgerEntry(
                    entry.date,
                    entry.amount_cents + amount,
                    entry.type,
                )
            )

            num_drafts += 1

        else:
            # Existing debit entries and past entries remain unchanged.
            ledger.append(entry)

    return replace(
        client,
        ledger=ledger,
    ), num_drafts


def _minimum_lump_sum(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    lump_date: date,
) -> int:
    """Find the smallest lump sum which makes some schedule feasible."""

    if _find_best(
        _with_lump(
            client,
            lump_date,
            0,
        ),
        offer,
        rules,
    ) is not None:
        return 0

    # Find a feasible upper bound first.
    high = 1

    while _find_best(
        _with_lump(
            client,
            lump_date,
            high,
        ),
        offer,
        rules,
    ) is None:
        high *= 2

    # Binary-search the minimum feasible amount.
    low = 1

    while low < high:
        mid = (low + high) // 2

        if _find_best(
            _with_lump(
                client,
                lump_date,
                mid,
            ),
            offer,
            rules,
        ) is not None:
            high = mid
        else:
            low = mid + 1

    return low


def _minimum_monthly_increment(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
) -> tuple[int, int]:
    """Find the smallest uniform increment applied to every future draft."""

    num_drafts = sum(
        1
        for entry in client.ledger
        if (
            entry.date > client.as_of_date
            and entry.date <= client.last_draft_date
            and entry.type == "credit"
        )
    )

    if num_drafts == 0:
        return 0, 0

    # The offer may already be feasible.
    if _find_best(
        client,
        offer,
        rules,
    ) is not None:
        return 0, num_drafts

    # Find a feasible upper bound.
    high = 1

    while True:
        test_client, _ = _with_monthly_increment(
            client,
            high,
        )

        if _find_best(
            test_client,
            offer,
            rules,
        ) is not None:
            break

        high *= 2

    # Binary-search the minimum feasible increment.
    low = 1

    while low < high:
        mid = (low + high) // 2

        test_client, _ = _with_monthly_increment(
            client,
            mid,
        )

        if _find_best(
            test_client,
            offer,
            rules,
        ) is not None:
            high = mid
        else:
            low = mid + 1

    return low, num_drafts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def evaluate_offer(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
) -> Result:
    """Evaluate a single settlement offer."""

    # ---------------------------------------------------------------
    # First determine whether the creditor rules allow at least one
    # structurally valid payment plan.
    # ---------------------------------------------------------------

    structural_candidates = list(
        _candidate_schedules(
            client,
            offer,
            rules,
        )
    )

    # ---------------------------------------------------------------
    # Part 1:
    # Try the offer with the existing account funding.
    # ---------------------------------------------------------------

    if structural_candidates:

        best = _find_best(
            client,
            offer,
            rules,
        )

        if best is not None:
            return Result(
                feasible=True,
                pay_shape_used=best[2],
                schedule=best[0],
                additional_funds=None,
            )

    # ---------------------------------------------------------------
    # If no structural payment plan exists, extra money cannot solve
    # the problem. For example, the required number of payments may
    # not fit before the horizon.
    # ---------------------------------------------------------------

    if not structural_candidates:

        reason = (
            "No payment structure satisfies the creditor rules and "
            "payment-count limits."
        )

        impossible = FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason=reason,
        )

        return Result(
            feasible=False,
            pay_shape_used=None,
            schedule=None,
            additional_funds=AdditionalFunds(
                impossible,
                impossible,
            ),
        )

    # ---------------------------------------------------------------
    # Part 2:
    # The structure is valid, but the existing cash flow is insufficient.
    #
    # An earlier lump sum is always at least as useful as the same lump
    # sum later, so use the earliest future date after as_of_date.
    # ---------------------------------------------------------------

    lump_date = client.as_of_date + timedelta(days=1)

    if lump_date > client.last_draft_date:
        lump_date = client.last_draft_date

    lump_sum = _minimum_lump_sum(
        client,
        offer,
        rules,
        lump_date,
    )

    monthly_increment, num_drafts = _minimum_monthly_increment(
        client,
        offer,
        rules,
    )

    # ---------------------------------------------------------------
    # Guardrails from ASSIGNMENT.md
    # ---------------------------------------------------------------

    lump_guardrail = _round_half_up(
        Decimal("0.65")
        * Decimal(_offer_total(offer))
    )

    monthly_guardrail = max(
        10_000,
        _round_half_up(
            Decimal("0.40")
            * Decimal(client.draft_amount_cents)
        ),
    )

    lump_ok = lump_sum <= lump_guardrail

    monthly_ok = (
        num_drafts > 0
        and monthly_increment <= monthly_guardrail
    )

    lump_reason = ""

    if not lump_ok:
        lump_reason = (
            f"Lump sum exceeds guardrail of "
            f"{lump_guardrail} cents."
        )

    if num_drafts == 0:
        monthly_reason = (
            "No future drafts are available for a monthly increment."
        )
    elif not monthly_ok:
        monthly_reason = (
            f"Monthly increment exceeds guardrail of "
            f"{monthly_guardrail} cents."
        )
    else:
        monthly_reason = ""

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=FundsOption(
                amount_cents=lump_sum,
                within_guardrail=lump_ok,
                reason=lump_reason,
                date=lump_date,
            ),
            monthly_increment=FundsOption(
                amount_cents=monthly_increment,
                within_guardrail=monthly_ok,
                reason=monthly_reason,
                num_drafts=num_drafts,
            ),
        ),
    )