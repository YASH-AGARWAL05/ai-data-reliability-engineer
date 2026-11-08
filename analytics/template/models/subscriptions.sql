select
    subscription_id,
    customer_id,
    plan,
    cast(started_at as date) as started_at
from {{ ref('raw_subscriptions') }}
