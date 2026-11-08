with payment_totals as (
    select
        order_id,
        sum(amount_usd) as gross_amount
    from {{ ref('stg_payments') }}
    group by order_id
),

refund_totals as (
    select
        order_id,
        sum(amount_usd) as refund_amount
    from {{ ref('stg_refunds') }}
    group by order_id
)

select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    coalesce(p.gross_amount, 0) as gross_amount,
    coalesce(r.refund_amount, 0) as refund_amount,
    coalesce(p.gross_amount, 0) - coalesce(r.refund_amount, 0) as net_amount
from {{ ref('stg_orders') }} as o
left join payment_totals as p on o.order_id = p.order_id
left join refund_totals as r on o.order_id = r.order_id
