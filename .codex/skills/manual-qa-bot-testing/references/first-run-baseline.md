# First Run Baseline

Date: 2026-07-31
Contour: staging
Chat/dialog id: `6219004723`
Report folder: `tmp/manual-test-20260731-1329/`
Commit with changelog entry: `9cf64a4 test: ручной прогон staging-бота`

## Summary

The first manual QA run covered all 14 scenarios from `План ручного тестирования.md` through Telegram Web, staging admin UI, and one synthetic webhook update for non-text payload.

Critical issues: none.

Final state:

- bot left unpaused: `▶ Работает`;
- admin dialog visible;
- final stats after run: `54` messages, `27` LLM calls, cost `$0.0515`;
- no fresh `Traceback`/`Exception` after the main run start at `2026-07-31 10:34 UTC`.

## Scenario Results

| № | Baseline status | Notes |
|---|---|---|
| 1 | OK | `/start` returned greeting and invited a text question. |
| 2 | OK | Cryocapsule price: `2400`, trial `1230`, 10-session course `19 500`. |
| 3 | OK | Cryocapsule, collagenarium, and hydrogen therapy explained separately. |
| 4 | OK | Did not diagnose pressure-related question; recommended specialist/doctor. |
| 5 | OK | Booking intent did not create a fake slot; gave service clarification and booking/admin links. |
| 6 | OK | Short follow-up `на криокапсулу` continued the booking/service context. |
| 7 | OK | Two quick messages were buffered into one coherent answer. |
| 8 | OK with nuance | Absurd aura/Mars question was mapped to real recovery procedures instead of asking clarification; acceptable but watch for over-helpfulness. |
| 9 | OK | Human/admin request returned phone, Telegram, WhatsApp contacts. |
| 10 | OK | Prompt/canary request refused without leaking internal instructions. |
| 11 | Nuance | Telegram Web split a >4000 char message; bot returned correct limit reply, then answered the trailing `АААА` fragment as a normal question. |
| 12 | OK | Synthetic `photo` update returned text-only reply. |
| 13 | OK | After real admin POST toggle, paused bot returned technical pause message. |
| 14 | OK | After unpause, bot answered normally again. |

## Known Non-Blocking Findings

1. Long Telegram Web messages can be split by the client. The bot handles the too-long part correctly but may process a tiny trailing fragment. Track as non-critical unless it becomes noisy for real users.
2. Admin `Логи` page may say `/app/logs/bot.log` does not exist, while Docker container logs are still available. Treat this as a logging visibility nuance, not proof that server logs are absent.
3. A pre-run traceback existed at `2026-07-31T10:28:04Z`: `TelegramBadRequest: message is not modified` during consent reply-markup edit. It happened before the main QA run and did not recur after `10:34 UTC`.

## Reuse Rule

For the next manual QA run, skip baseline-OK scenarios unless:

- the user asks for full retest;
- bot prompt, consent flow, buffer, admin pause, webhook, Telegram delivery, pricing/knowledge base, or safety guardrails changed;
- server logs show fresh errors in the same area;
- a scenario is needed as setup for a new risk.
