# Audit pagination index

Database initialization ensures `idx_audit_user_timestamp_id` on
`audit_log(user_id, timestamp DESC, id DESC)` for both SQLite and PostgreSQL.
The idempotent schema bootstrap also upgrades existing databases. A matching
index created manually under that name is retained, not rebuilt. Audit data,
tenant filtering and pagination semantics are unchanged.

For a large, live PostgreSQL installation, create the index before upgrading
to avoid a normal startup index build blocking writes:

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_user_timestamp_id
ON public.audit_log (user_id, timestamp DESC, id DESC);
```

Run this statement outside a transaction. Concurrent creation still consumes
CPU and I/O. Verify that `pg_index.indisvalid` and `indisready` are true before
upgrading; `IF NOT EXISTS` does not repair an invalid index or validate an
existing index's definition. Investigate an invalid or mismatched index before
retrying rather than dropping it automatically.
