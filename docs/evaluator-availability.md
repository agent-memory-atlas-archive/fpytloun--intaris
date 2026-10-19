# Evaluator routing availability

Temporary failures fetching Cognis model-routing metadata return HTTP 503 with
code `evaluation_backend_unavailable` and a numeric `Retry-After` header.
The header covers the remaining short failure-cache cooldown, rounded up.
Concurrent lookups remain coalesced; recovery after the cooldown refreshes the
metadata normally.

This response is an infrastructure error, not a policy decision or an approval.
Clients should retain their logical call identity and use bounded retries.
Invalid metadata, authentication failures and protocol mismatches are not
reclassified as transient by this change. No stale backend selection is used
to permit execution.
