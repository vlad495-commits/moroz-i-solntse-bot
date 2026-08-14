# Admin bookings final minors

## Scope

- The presentation read model now derives `customer_chat_id` only from a
  canonical integer string accepted by the existing `/chats/{chat_id:int}`
  route. It removes the raw `customer_id` from the rendered model.
- Both booking templates use a chat link only when `customer_chat_id` exists;
  incompatible values render the neutral non-link label `Клиент`.
- Integration coverage inserts a booking between keyset pages for ascending
  upcoming and descending attention/history views, and verifies page two does
  not repeat the viewed booking.

## TDD evidence

- RED: focused Docker run produced `7 failed, 17 passed`. The expected
  customer-route failures showed the missing `customer_chat_id` and raw-link
  behavior. The first pagination setup also exposed a test-fixture issue: a
  one-row first page has no cursor, so it cannot exercise pagination.
- GREEN: after adding a pre-existing next row to establish the cursor, the
  same focused Docker gate passed `24 passed in 17.33s`.
- Affected Docker gate passed `79 passed in 78.42s`:
  booking views/read model/UI, repository, customer deletion/event journal,
  CSRF/RBAC/audit, and architecture visual contracts.

## Decision and boundaries

The existing PostgreSQL predicates already implement strict keyset semantics:
ascending upcoming uses `>` and descending attention/history use `<` on the
sort tuple plus UUID. The new insertion regressions pass without changing
production SQL. No migration, dependency, provider/YCLIENTS call, deployment,
or push was performed.
