"""Public decision vocabulary for the next local-model experiment."""

CAUSES = {
    "none": "Revenue reconciles and there is no pending source data.",
    "duplicate_payment_event": "Repeated payment IDs inflate payment totals.",
    "multiply_payment_join": "A join multiplies payment amounts in the order model.",
    "delay_refund_ingestion": "A refund event is pending ingestion; no SQL change is needed.",
    "omit_fx_conversion": "A payment model omits currency conversion.",
    "rename_payment_amount": "A payment source renamed its amount column.",
}

CAUSE_BY_SUPPORT = {
    "revenue_reconciliation": "none",
    "duplicate_payment_ids": "duplicate_payment_event",
    "payment_join_multiplication": "multiply_payment_join",
    "pending_refunds": "delay_refund_ingestion",
    "missing_fx_conversion": "omit_fx_conversion",
    "payment_amount_schema": "rename_payment_amount",
}

MODEL_BY_SUPPORT = {
    "duplicate_payment_ids": "models/staging/stg_payments.sql",
    "payment_join_multiplication": "models/orders.sql",
    "missing_fx_conversion": "models/staging/stg_payments.sql",
    "payment_amount_schema": "models/staging/stg_payments.sql",
}

REPAIR_GUIDANCE_BY_SUPPORT = {
    "duplicate_payment_ids": (
        "Use row_number() over (partition by id order by ingested_at desc, event_id desc) "
        "to keep exactly one raw row per payment id. Filter to row_number = 1 before "
        "currency conversion. Do not group by event_id or rejoin all raw payment rows."
    ),
    "payment_join_multiplication": (
        "Remove row multiplication from the order model. Aggregate payment and refund "
        "amounts to one row per order before joining them to orders."
    ),
    "missing_fx_conversion": (
        "Convert each payment amount to USD using the existing currency rate join. "
        "Preserve payment identity and all existing output columns."
    ),
    "payment_amount_schema": (
        "Use the existing source column gross_cents for the payment amount where the "
        "old amount column no longer exists. Keep the currency conversion."
    ),
}
