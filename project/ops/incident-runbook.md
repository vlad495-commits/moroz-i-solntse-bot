# Incident Runbook

## Roles

- technical owner: checks containers, logs, metrics, backups, deploy state and provider errors.
- business owner: approves client-facing pauses, manual processing and customer communication.

## First Ten Minutes

1. Open admin health, `/metrics`, logs and alert history.
2. Check Telegram webhook status and recent outbound failures.
3. Check YCLIENTS availability before any booking operation retry.
4. Check Redis, RabbitMQ, Postgres, worker and scheduler health.
5. Preserve logs without exposing PII in chat, tickets or screenshots.

## Escalation

Use technical alerts for infrastructure and provider failures. Use business alerts only when clients may be affected, bookings need manual handling, or staff must act.

Never paste raw PII into incident channels. Redact phone numbers, names, Telegram handles and free-form message text unless the business owner explicitly needs the exact value in a private approved channel.
