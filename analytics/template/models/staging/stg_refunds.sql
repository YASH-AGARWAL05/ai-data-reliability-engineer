select
    id as refund_id,
    order_id,
    cast(created_at as date) as created_at,
    cast(ingested_at as date) as ingested_at,
    amount_cents / 100.0 as amount_usd
from {{ ref('raw_refunds') }}
