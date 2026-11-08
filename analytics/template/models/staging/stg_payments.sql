select
    p.id as payment_id,
    p.event_id,
    p.order_id,
    p.payment_method,
    p.currency,
    p.ingested_at,
    p.amount / 100.0 * c.usd_rate as amount_usd
from {{ ref('raw_payments') }} as p
left join {{ ref('raw_currencies') }} as c on p.currency = c.currency
