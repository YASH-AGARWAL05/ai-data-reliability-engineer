{% set payment_columns = adapter.get_columns_in_relation(ref('raw_payments')) %}
{% set payment_names = payment_columns | map(attribute='name') | list %}
{% set amount_column = 'amount'
    if 'amount' in payment_names else 'gross_cents' %}

with latest_payment as (
    select
        *,
        row_number() over (
            partition by id order by ingested_at desc, event_id desc
        ) as event_rank
    from {{ ref('raw_payments') }}
),

expected_payments as (
    select
        p.order_id,
        sum(p.{{ amount_column }} / 100.0 * c.usd_rate) as gross_amount
    from latest_payment as p
    inner join {{ ref('raw_currencies') }} as c on p.currency = c.currency
    where p.event_rank = 1
    group by p.order_id
),

expected_refunds as (
    select
        order_id,
        sum(amount_cents / 100.0) as refund_amount
    from {{ ref('raw_refunds') }}
    group by order_id
)

select o.order_id
from {{ ref('orders') }} as o
left join expected_payments as p on o.order_id = p.order_id
left join expected_refunds as r on o.order_id = r.order_id
where
    abs(
        o.net_amount
        - (coalesce(p.gross_amount, 0) - coalesce(r.refund_amount, 0))
    )
    > 0.001
