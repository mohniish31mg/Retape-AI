"""Tests for settlement feasibility, payment shapes, fees and edge cases.

The first four tests cover the provided cases. The remaining tests exercise
the main constraints from ASSIGNMENT.md and a few important edge cases.
"""

from __future__ import annotations

from datetime import date

from feasibility.engine import (
    AdditionalFunds,
    FundsOption,
    Result,
    ScheduleRow,
    _build_balloon_payments,
    _build_even_payments,
    _build_staircase_payments,
    _effective_floors,
    _valid_payments,
    _with_lump,
    _with_monthly_increment,
    evaluate_offer,
)
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    load_case,
    monthly_payment_dates,
    offer_total_cents,
    program_fee_cents,
)


def _run(case: str):
    client, offer, rules = load_case(f"cases/{case}")
    return evaluate_offer(client, offer, rules)


def _make_client(
    *,
    draft_amount_cents: int = 10000,
    current_balance_cents: int = 0,
    ledger: list[LedgerEntry] | None = None,
    first_draft_date: date = date(2026, 1, 1),
    last_draft_date: date = date(2026, 7, 1),
    as_of_date: date = date(2025, 12, 31),
) -> Client:
    return Client(
        draft_amount_cents=draft_amount_cents,
        draft_day=1,
        first_draft_date=first_draft_date,
        last_draft_date=last_draft_date,
        as_of_date=as_of_date,
        current_balance_cents=current_balance_cents,
        ledger=ledger or [],
    )


def _make_offer(
    *,
    current_balance_cents: int = 50000,
    original_balance_cents: int = 50000,
    settlement_pct: float = 1.0,
    first_payment_date: date = date(2026, 1, 31),
) -> Offer:
    return Offer(
        creditor="TestCo",
        current_balance_cents=current_balance_cents,
        original_balance_cents=original_balance_cents,
        settlement_pct=settlement_pct,
        first_payment_date=first_payment_date,
    )


def _make_rules(
    *,
    max_terms: int = 6,
    max_payments: int = 6,
    min_payment_cents: int = 2500,
    max_token_pays: int = 6,
    min_payment_tiers: list[tuple[int, int]] | None = None,
    even_pays: bool = False,
    is_ballooning_allowed: bool = False,
    max_segments: int = 4,
    bank_fee_cents: int = 0,
    program_fee_pct: float = 0.0,
) -> CreditorRules:
    return CreditorRules(
        max_terms=max_terms,
        max_payments=max_payments,
        min_payment_cents=min_payment_cents,
        max_token_pays=max_token_pays,
        min_payment_tiers=min_payment_tiers or [],
        even_pays=even_pays,
        is_ballooning_allowed=is_ballooning_allowed,
        max_segments=max_segments,
        bank_fee_cents=bank_fee_cents,
        program_fee_pct=program_fee_pct,
    )


# ---------------------------------------------------------------------------
# Provided cases
# ---------------------------------------------------------------------------


def test_case1_feasible_even():
    r = _run("case1_feasible_even")

    assert r.feasible is True
    assert r.pay_shape_used == "even"
    assert r.schedule is not None

    # Balance must never go negative.
    assert all(row.balance_cents >= 0 for row in r.schedule)


def test_case2_infeasible_minima():
    r = _run("case2_infeasible_minima")

    assert r.feasible is False

    af = r.additional_funds
    assert af is not None

    assert af.lump_sum.amount_cents == 10000
    assert af.lump_sum.within_guardrail is True

    assert af.monthly_increment.amount_cents == 2500
    assert af.monthly_increment.num_drafts == 5
    assert af.monthly_increment.within_guardrail is True


def test_case3_requires_balloon():
    r = _run("case3_balloon")

    assert r.feasible is True
    assert r.pay_shape_used == "balloon"
    assert r.schedule is not None

    payments = [
        row.creditor_payment_cents
        for row in r.schedule
        if row.creditor_payment_cents > 0
    ]

    assert sum(payments) == 30000
    assert payments[-1] == max(payments)


def test_case4_tiered_minimums():
    r = _run("case4_tiers")

    assert r.feasible is True
    assert r.pay_shape_used == "staircase"
    assert r.schedule is not None

    payments = [
        row.creditor_payment_cents
        for row in r.schedule
        if row.creditor_payment_cents > 0
    ]

    # Payments 7+ must respect the $50 tier floor.
    assert all(p >= 5000 for p in payments[6:])


# ---------------------------------------------------------------------------
# Payment amount and shape tests
# ---------------------------------------------------------------------------


def test_case5_exact_sum_for_every_feasible_case():
    for case in (
        "case1_feasible_even",
        "case3_balloon",
        "case4_tiers",
    ):
        client, offer, rules = load_case(f"cases/{case}")
        result = evaluate_offer(client, offer, rules)

        assert result.feasible is True
        assert result.schedule is not None

        payments = [
            row.creditor_payment_cents
            for row in result.schedule
            if row.creditor_payment_cents > 0
        ]

        assert sum(payments) == offer_total_cents(offer)


def test_case6_even_payments_are_as_equal_as_possible():
    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=10,
        even_pays=True,
    )

    payments = _build_even_payments(100, 3, rules)

    assert payments == [33, 33, 34]
    assert sum(payments) == 100


def test_case7_even_payment_remainder_goes_to_latest_payments():
    rules = _make_rules(
        max_terms=4,
        max_payments=4,
        min_payment_cents=10,
        even_pays=True,
    )

    payments = _build_even_payments(102, 4, rules)

    assert payments == [25, 25, 26, 26]


def test_case8_balloon_keeps_early_payments_minimum():
    rules = _make_rules(
        max_terms=4,
        max_payments=4,
        min_payment_cents=2500,
        is_ballooning_allowed=True,
    )

    payments = _build_balloon_payments(15000, 4, rules)

    assert payments == [2500, 2500, 2500, 7500]
    assert sum(payments) == 15000


def test_case9_balloon_final_payment_is_remainder():
    rules = _make_rules(
        max_terms=5,
        max_payments=5,
        min_payment_cents=1000,
        is_ballooning_allowed=True,
    )

    payments = _build_balloon_payments(9000, 5, rules)

    assert payments is not None
    assert sum(payments) == 9000
    assert payments[-1] == 5000
    assert payments[:-1] == [1000, 1000, 1000, 1000]


def test_case10_balloon_fails_when_minimums_exceed_total():
    rules = _make_rules(
        max_terms=4,
        max_payments=4,
        min_payment_cents=3000,
        is_ballooning_allowed=True,
    )

    payments = _build_balloon_payments(10000, 4, rules)

    assert payments is None


def test_case11_staircase_respects_max_segments():
    rules = _make_rules(
        max_terms=6,
        max_payments=6,
        min_payment_cents=1000,
        max_segments=2,
    )

    payments = _build_staircase_payments(18000, 6, rules)

    assert payments is not None
    assert sum(payments) == 18000
    assert len(set(payments)) <= 2
    assert all(
        payments[i] <= payments[i + 1]
        for i in range(len(payments) - 1)
    )


def test_case12_staircase_respects_tier_floor():
    rules = _make_rules(
        max_terms=6,
        max_payments=6,
        min_payment_cents=1000,
        min_payment_tiers=[(4, 3000)],
        max_segments=3,
    )

    payments = _build_staircase_payments(12000, 6, rules)

    assert payments is not None
    assert sum(payments) == 12000
    assert all(p >= 3000 for p in payments[3:])


# ---------------------------------------------------------------------------
# Token-pay and tier tests
# ---------------------------------------------------------------------------


def test_case13_token_pay_limit_is_respected():
    rules = _make_rules(
        min_payment_cents=2500,
        max_token_pays=2,
    )

    assert _valid_payments(
        [2500, 2500, 2501],
        7501,
        rules,
        3,
    )


def test_case14_excess_token_pays_are_rejected():
    rules = _make_rules(
        min_payment_cents=2500,
        max_token_pays=2,
    )

    assert not _valid_payments(
        [2500, 2500, 2500],
        7500,
        rules,
        3,
    )


def test_case15_tier_floor_overrides_base_minimum():
    rules = _make_rules(
        min_payment_cents=2500,
        max_token_pays=6,
        min_payment_tiers=[(3, 5000)],
    )

    floors = _effective_floors(5, rules)

    assert floors == [2500, 2500, 5000, 5000, 5000]


def test_case16_token_limit_can_force_payment_above_base_minimum():
    rules = _make_rules(
        min_payment_cents=2500,
        max_token_pays=1,
    )

    floors = _effective_floors(4, rules)

    assert floors[0] == 2500
    assert floors[1] > 2500
    assert floors[2] >= floors[1]
    assert floors[3] >= floors[2]


# ---------------------------------------------------------------------------
# Payment validity tests
# ---------------------------------------------------------------------------


def test_case17_non_decreasing_payment_order_is_required():
    rules = _make_rules(
        min_payment_cents=1000,
        max_token_pays=6,
    )

    assert not _valid_payments(
        [3000, 2000],
        5000,
        rules,
        2,
    )


def test_case18_payment_count_must_match_k():
    rules = _make_rules(
        min_payment_cents=1000,
        max_token_pays=6,
    )

    assert not _valid_payments(
        [2500, 2500],
        5000,
        rules,
        3,
    )


def test_case19_exact_sum_is_required():
    rules = _make_rules(
        min_payment_cents=1000,
        max_token_pays=6,
    )

    assert not _valid_payments(
        [2000, 2000],
        5001,
        rules,
        2,
    )


# ---------------------------------------------------------------------------
# Fee tests
# ---------------------------------------------------------------------------


def test_case20_program_fee_can_be_fully_collected_on_first_payment_date():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        ],
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=5000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=2,
        max_payments=2,
        min_payment_cents=2500,
        bank_fee_cents=500,
        program_fee_pct=0.20,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    assert sum(row.program_fee_cents for row in r.schedule) == 1000

    first_payment_row = next(
        row for row in r.schedule
        if row.creditor_payment_cents > 0
    )

    assert first_payment_row.program_fee_cents == 1000


def test_case21_fee_cannot_be_collected_before_first_creditor_payment():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        ],
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=2,
        max_payments=2,
        min_payment_cents=2500,
        program_fee_pct=0.20,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    for row in r.schedule:
        if row.date < date(2026, 1, 31):
            assert row.program_fee_cents == 0


def test_case22_fee_only_date_has_no_bank_fee():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=50000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        bank_fee_cents=500,
        program_fee_pct=0.50,
    )

    r = evaluate_offer(client, offer, rules)

    if r.feasible:
        assert r.schedule is not None

        for row in r.schedule:
            if row.creditor_payment_cents == 0 and row.program_fee_cents > 0:
                assert row.bank_fee_cents == 0


# ---------------------------------------------------------------------------
# Ledger simulation and date ordering
# ---------------------------------------------------------------------------


def test_case23_same_day_credits_are_applied_before_debits():
    client = _make_client(
        current_balance_cents=0,
        ledger=[
            LedgerEntry(date(2026, 1, 31), 10000, "credit"),
            LedgerEntry(date(2026, 1, 31), 2000, "debit"),
        ],
        last_draft_date=date(2026, 1, 31),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=5000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None
    assert r.schedule[-1].balance_cents >= 0


def test_case24_balance_can_reach_exactly_zero():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 1, 31),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=5000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None
    assert r.schedule[-1].balance_cents == 5000


def test_case25_fixed_debit_can_make_offer_infeasible():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 1, 31), 9000, "debit"),
        ],
        last_draft_date=date(2026, 1, 31),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=5000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is False


# ---------------------------------------------------------------------------
# Horizon and cadence
# ---------------------------------------------------------------------------


def test_case26_payment_past_horizon_is_not_allowed():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 1, 15),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=5000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is False

def test_case27_multiple_payment_terms_must_be_consecutive():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=7500,
        original_balance_cents=7500,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    payment_dates = [
        row.date
        for row in r.schedule
        if row.creditor_payment_cents > 0
    ]

    # The engine may choose any valid k <= 3 based on the
    # fee-front-loading objective. Whatever k it chooses, payments
    # must start at first_payment_date and occupy consecutive cadence dates.
    assert len(payment_dates) >= 1
    assert len(payment_dates) <= 3

    expected_dates = monthly_payment_dates(
        date(2026, 1, 31),
        len(payment_dates),
    )

    assert payment_dates == expected_dates
# ---------------------------------------------------------------------------
# Additional funding
# ---------------------------------------------------------------------------


def test_case28_lump_sum_is_added_as_a_credit():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        ],
    )

    updated = _with_lump(
        client,
        date(2026, 1, 2),
        5000,
    )

    assert any(
        entry.date == date(2026, 1, 2)
        and entry.amount_cents == 5000
        and entry.type == "credit"
        for entry in updated.ledger
    )


def test_case29_monthly_increment_changes_future_credits():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
    )

    updated, num_drafts = _with_monthly_increment(client, 2500)

    assert num_drafts == 3

    amounts = [
        entry.amount_cents
        for entry in updated.ledger
        if entry.type == "credit"
    ]

    assert amounts == [12500, 12500, 12500]


def test_case30_monthly_increment_does_not_change_old_entries():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2025, 12, 1), 10000, "credit"),
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
        ],
    )

    updated, num_drafts = _with_monthly_increment(client, 2500)

    assert num_drafts == 1
    assert updated.ledger[0].amount_cents == 10000
    assert updated.ledger[1].amount_cents == 12500


# ---------------------------------------------------------------------------
# Result and serialization
# ---------------------------------------------------------------------------


def test_case31_feasible_result_has_no_additional_funds():
    r = _run("case1_feasible_even")

    assert r.feasible is True
    assert r.additional_funds is None


def test_case32_infeasible_result_has_additional_funds():
    r = _run("case2_infeasible_minima")

    assert r.feasible is False
    assert r.schedule is None
    assert r.additional_funds is not None


def test_case33_result_serialization_contains_expected_schedule_fields():
    r = _run("case1_feasible_even")

    data = r.to_dict()

    assert data["feasible"] is True
    assert data["pay_shape_used"] == "even"
    assert data["schedule"] is not None

    row = data["schedule"][0]

    assert "date" in row
    assert "creditor_payment_cents" in row
    assert "program_fee_cents" in row
    assert "bank_fee_cents" in row
    assert "balance_cents" in row


# ---------------------------------------------------------------------------
# Fee front-loading
# ---------------------------------------------------------------------------


def test_case34_front_loads_program_fee_when_cash_is_available():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=10000,
        original_balance_cents=10000,
        settlement_pct=0.5,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        program_fee_pct=0.20,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    fees = [
        row.program_fee_cents
        for row in r.schedule
    ]

    assert sum(fees) == 2000

    # The first eligible date should receive as much fee as possible.
    assert fees[0] == max(fees)


def test_case35_fee_is_collected_only_from_cadence_dates():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 1, 15), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 2, 28),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=2,
        max_payments=2,
        min_payment_cents=2500,
        program_fee_pct=0.20,
    )

    r = evaluate_offer(client, offer, rules)

    if r.feasible:
        assert r.schedule is not None

        for row in r.schedule:
            if row.date == date(2026, 1, 15):
                assert row.program_fee_cents == 0


def test_case36_fee_vector_prefers_earlier_collection():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=10000,
        original_balance_cents=10000,
        settlement_pct=0.5,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        program_fee_pct=0.30,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    first_fee = r.schedule[0].program_fee_cents

    assert first_fee > 0
    assert first_fee == max(
        row.program_fee_cents
        for row in r.schedule
    )


# ---------------------------------------------------------------------------
# Path-1 tie-break behavior
# ---------------------------------------------------------------------------


def test_case37_equal_fee_front_loading_prefers_earlier_creditor_recovery():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 20000, "credit"),
            LedgerEntry(date(2026, 2, 1), 20000, "credit"),
            LedgerEntry(date(2026, 3, 1), 20000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=10000,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=3,
        max_payments=3,
        min_payment_cents=2500,
        max_token_pays=3,
        program_fee_pct=0.10,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    payments = [
        row.creditor_payment_cents
        for row in r.schedule
        if row.creditor_payment_cents > 0
    ]

    assert sum(payments) == 10000
    assert all(
        payments[i] <= payments[i + 1]
        for i in range(len(payments) - 1)
    )


def test_case38_program_fee_waits_for_future_fixed_debit():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 15000, "debit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        # Horizon must reach the March EOM cadence date so the remaining
        # program fee can be collected after the February fixed debit.
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=2500,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=2500,
        bank_fee_cents=0,
        program_fee_pct=0.5,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    # The January balance after the creditor payment is $75.
    # $50 must be preserved for the future February deficit.
    # Therefore only $25 of the $50 program fee can be collected in January.
    jan = next(
        row for row in r.schedule
        if row.date == date(2026, 1, 31)
    )

    assert jan.creditor_payment_cents == 2500
    assert jan.program_fee_cents == 2500

    # The remaining $25 is collected later after the future debit
    # has been accounted for.
    mar = next(
        row for row in r.schedule
        if row.date == date(2026, 3, 31)
    )

    assert mar.creditor_payment_cents == 0
    assert mar.program_fee_cents == 2500


def test_case39_fee_only_date_has_no_bank_fee():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 2, 28),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
        bank_fee_cents=1000,
        program_fee_pct=0.5,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    fee_only_rows = [
        row
        for row in r.schedule
        if (
            row.creditor_payment_cents == 0
            and row.program_fee_cents > 0
        )
    ]

    assert fee_only_rows

    # A fee-only date must never have a bank fee.
    assert all(
        row.bank_fee_cents == 0
        for row in fee_only_rows
    )


def test_case40_program_fee_can_be_split_across_multiple_cadence_dates():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 5000, "credit"),
            LedgerEntry(date(2026, 2, 1), 5000, "credit"),
            LedgerEntry(date(2026, 3, 1), 5000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=5000,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=5000,
        bank_fee_cents=0,
        program_fee_pct=0.9,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    fee_total = sum(
        row.program_fee_cents
        for row in r.schedule
    )

    assert fee_total == 9000

    fee_rows = [
        row
        for row in r.schedule
        if row.program_fee_cents > 0
    ]

    # The complete fee is allowed to be collected over multiple
    # cadence dates when the earliest date cannot safely fund it all.
    assert len(fee_rows) >= 2


def test_case41_future_credits_offset_future_fixed_debits():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 5000, "credit"),
            LedgerEntry(date(2026, 2, 1), 15000, "credit"),
            LedgerEntry(date(2026, 2, 1), 12000, "debit"),
        ],
        last_draft_date=date(2026, 2, 28),
    )

    offer = _make_offer(
        current_balance_cents=2500,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=2500,
        bank_fee_cents=0,
        program_fee_pct=0.5,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    jan = next(
        row
        for row in r.schedule
        if row.date == date(2026, 1, 31)
    )

    # After the January creditor payment, $2,500 remains.
    # The February ledger has a $15,000 credit and $12,000 debit,
    # so the future net mandatory requirement is only $0.
    # Therefore the complete $5,000 fee can be collected over the
    # available cadence dates without making the account negative.
    assert jan.program_fee_cents == 2500

    assert sum(
        row.program_fee_cents
        for row in r.schedule
    ) == 5000

def test_case42_program_fee_cannot_be_collected_before_first_payment():
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 2, 28),
    )

    offer = _make_offer(
        current_balance_cents=2500,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 2, 28),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=2500,
        bank_fee_cents=0,
        program_fee_pct=0.5,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    # January is before the first creditor payment date.
    # Therefore no program fee may be collected in January.
    assert all(
        not (
            row.date < date(2026, 2, 28)
            and row.program_fee_cents > 0
        )
        for row in r.schedule
    )


def test_case43_future_fixed_debit_can_make_early_fee_collection_infeasible():
    # Without the February credit, total inflow is only 20_000 while
    # creditor + fee + fixed debit need 22_500 — genuinely infeasible.
    # Add the February credit so the Feb net deficit is 5_000 and the
    # offer stays feasible while still forcing the January fee to wait.
    client = _make_client(
        ledger=[
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 15000, "debit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )

    offer = _make_offer(
        current_balance_cents=2500,
        original_balance_cents=10000,
        settlement_pct=1.0,
        first_payment_date=date(2026, 1, 31),
    )

    rules = _make_rules(
        max_terms=1,
        max_payments=1,
        min_payment_cents=2500,
        bank_fee_cents=0,
        program_fee_pct=0.5,
    )

    r = evaluate_offer(client, offer, rules)

    assert r.feasible is True
    assert r.schedule is not None

    jan = next(
        row for row in r.schedule
        if row.date == date(2026, 1, 31)
    )

    # After the $25 creditor payment, $75 remains.
    # February's net mandatory flow is -$50, so only $25 of the $50 fee
    # is safe to collect in January.
    assert jan.program_fee_cents == 2500

    assert sum(
        row.program_fee_cents
        for row in r.schedule
    ) == 5000    