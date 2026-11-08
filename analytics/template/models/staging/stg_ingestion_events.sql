select
    event_id,
    event_type,
    order_id,
    status,
    cast(expected_at as date) as expected_at,
    cast(arrived_at as date) as arrived_at
from {{ ref('raw_ingestion_events') }}
