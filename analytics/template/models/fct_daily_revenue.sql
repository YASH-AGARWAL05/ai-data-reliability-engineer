select
    order_date,
    count(*) as order_count,
    sum(gross_amount) as gross_revenue_usd,
    sum(refund_amount) as refunds_usd,
    sum(net_amount) as net_revenue_usd
from {{ ref('orders') }}
group by order_date
