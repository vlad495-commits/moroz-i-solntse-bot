# Failure Gates

Run these against staging only, never against production without an explicit release window.

| Component | Check | Expected |
|---|---|---|
| Redis | Restart Redis while a chat buffer exists. | No lost confirmed state; temporary visible delay status is acceptable. |
| RabbitMQ | Restart RabbitMQ while worker is consuming. | Worker reconnects; no lost confirmed state; retry/DLQ semantics stay bounded. |
| YCLIENTS | Disable YCLIENTS credentials or point to a local failing endpoint. | Telegram booking and mutations stop with a safe online-booking/admin fallback; FAQ continues; uncertain mutations are not retried. |
| primary LLM | Disable primary LLM key or force timeout. | Reserve/safe fallback path activates; user sees visible delay status or safe escalation. |

Pass criteria:

- no lost confirmed state
- visible delay status during dependency outage
- recovery after component restart
- no PII in logs, metrics, load output or alert text
- no blind retry of uncertain external mutation
