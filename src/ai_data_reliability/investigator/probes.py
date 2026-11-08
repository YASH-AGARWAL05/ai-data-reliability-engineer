"""Fixed warehouse checks available to the investigator."""

from dataclasses import dataclass

PROBES = {
    "duplicate_payment_ids": (
        "select id, count(*) as copies from raw_payments "
        "group by id having count(*) > 1 order by id"
    ),
    "currency_join_cardinality": (
        "select currency, count(*) as copies from raw_currencies "
        "group by currency having count(*) > 1 order by currency"
    ),
    "payment_amount_schema": (
        "select column_name from information_schema.columns "
        "where table_schema = 'main' and table_name = 'raw_payments' "
        "and column_name = 'gross_cents'"
    ),
    "pending_refunds": (
        "select event_id from raw_ingestion_events "
        "where event_type = 'refund' and status = 'pending' order by event_id"
    ),
    "refund_model_error": (
        "select r.id from raw_refunds as r left join stg_refunds as s on r.id = s.refund_id "
        "where s.refund_id is null or abs(s.amount_usd - r.amount_cents / 100.0) > 0.005"
    ),
    "payment_join_multiplication": (
        "select o.order_id from orders as o join "
        "(select order_id, sum(amount_usd) as expected_gross "
        "from stg_payments group by order_id) as p "
        "on o.order_id = p.order_id where abs(o.gross_amount - p.expected_gross) > 0.005"
    ),
    "missing_fx_conversion": (
        "select p.id from raw_payments as p join stg_payments as s on p.event_id = s.event_id "
        "join raw_currencies as c on p.currency = c.currency "
        "where abs(s.amount_usd - p.amount / 100.0 * c.usd_rate) > 0.005"
    ),
    "revenue_reconciliation": (
        "with latest as (select *, row_number() over "
        "(partition by id order by ingested_at desc, event_id desc) as rank from raw_payments), "
        "expected as (select sum(p.amount / 100.0 * c.usd_rate) - "
        "(select coalesce(sum(amount_cents / 100.0), 0) from raw_refunds) as net "
        "from latest as p join raw_currencies as c on p.currency = c.currency where p.rank = 1), "
        "actual as (select sum(net_revenue_usd) as net from fct_daily_revenue) "
        "select 1 from expected, actual where actual.net is null "
        "or abs(expected.net - actual.net) > 0.005"
    ),
}


@dataclass(frozen=True)
class ProbePlan:
    competing_probe: str
    support_has_rows: bool
    outcome: str


PLANS = {
    "revenue_reconciliation": ProbePlan("pending_refunds", False, "verified_clean"),
    "duplicate_payment_ids": ProbePlan("currency_join_cardinality", True, "patched"),
    "payment_join_multiplication": ProbePlan("duplicate_payment_ids", True, "patched"),
    "pending_refunds": ProbePlan("refund_model_error", True, "verified_no_fix"),
    "missing_fx_conversion": ProbePlan("currency_join_cardinality", True, "patched"),
    "payment_amount_schema": ProbePlan("duplicate_payment_ids", True, "patched"),
}
