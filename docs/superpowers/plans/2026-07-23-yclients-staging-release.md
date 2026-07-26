# YCLIENTS Staging App Release and Rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Multi-agent dispatch is intentionally not used in this task.

**Goal:** Развернуть на staging отличающийся YCLIENTS app release, сохранить предыдущие immutable app artifacts и доказать distinct `candidate → previous → candidate` только для app images без downgrade PostgreSQL.

**Architecture:** Текущие `bot`/`worker`/`migrate` images сначала фиксируются по image ID, получают новый immutable previous tag и сохраняются в server-side archive. Candidate source доставляется без push как проверяемый Git bundle, собирается штатным Compose на VPS и применяет только additive Alembic upgrade; rollback переключает только `bot` и `worker`, оставляя PostgreSQL, Redis, RabbitMQ, Caddy и schema без изменений.

**Tech Stack:** Git bundle, Docker Compose `moroz-staging`, Docker image IDs/tags/archive, Alembic, existing staging webhook/smoke/log scanner, password-based Docker/`expect` SSH wrapper with one documented bounded host-OpenSSH transport exception.

## Global Constraints

- Разрешён только staging release; production, shared/prototype resources, merge и push запрещены.
- Working source starts from `7e2ec278ed730edf15b58c2a20cc82d3cfbe42ec`; detached linked worktree сохраняется.
- Docker-only; локальные проверки используют unique task Compose namespace и process-only credentials.
- Временные файлы находятся только в корневом `tmp/`; server-side release artifacts — только в `/opt/moroz-staging/tmp/releases/`.
- Секреты, `.env`, token/DSN/SSH values, ПД, raw provider bodies и raw logs не выводятся и не попадают в evidence.
- Previous/candidate сравниваются по exact per-service image IDs; same-artifact switch не засчитывается.
- Миграция только `alembic upgrade head`; `downgrade`, volume deletion и schema rollback запрещены.
- Rollback пересоздаёт только `bot` и `worker`; stores, Caddy и schema не переключаются.
- Новый real YCLIENTS lifecycle запрещён; readiness использует только read-only services/slots/custom-field calls.
- Любой unexpected state, health/smoke/log failure или недоступный previous artifact оставляет gate открытым.

## Files

- Create and update: `docs/superpowers/plans/2026-07-23-yclients-staging-release.md` — execution checklist и safe evidence.
- Modify: `Дорожная карта.md` — живой статус release/rollback gate.
- Modify: `План реализации.md` — статус Phase 3.
- Modify: `changelog.md` — каждое действие/ошибка сразу.
- Temporary only: `tmp/yclients-staging-release.bundle`, `tmp/yclients_readiness.py` — ignored delivery/readiness artifacts, удаляются после проверки.
- Runtime/test/migration source не меняется.

---

### Task 1: Local and remote preflight; preserve previous

**Produces:** точные previous bot/worker/migrate refs/IDs, healthy snapshot, current migration и immutable return path.

- [x] **Step 1: Verify local identity and source scope**

Run:

```powershell
git rev-parse HEAD
git status --short --branch
git branch -r --contains HEAD
```

Expected: exact local release commit, clean detached worktree before artifact build, candidate absent from remote.

- [x] **Step 2: Restore the safe SSH control channel**

Use the installed deploy skill’s Docker/`expect` wrapper with allowlisted `SERVER_*` process environment. Run only `SAFE_*` output commands. Expected: TCP and SSH banner present, `SAFE_SSH=ok`; otherwise record `staging SSH control channel unavailable` and do not mutate VPS.

Execution exception: if the Windows host still receives the VPS SSH banner but both the Docker bridge and a Docker-to-host forward are proven unavailable, a bounded process-only Windows OpenSSH askpass transport may replace only the client transport. It must preserve the same allowlisted commands, suppressed raw output and credential handling. Remote rollout actions remain Docker Compose-only and the permitted staging scope does not expand.

- [x] **Step 3: Capture read-only staging previous**

Verify exact `/opt/moroz-staging`, clean checkout, Compose project label, six staging services healthy, prototype resources healthy/unchanged, current `bot`/`worker` image refs and IDs, matching current tag suffix, available migrate image, and Alembic revision. Query migration by read-only SQL inside staging PostgreSQL.

- [x] **Step 4: Create immutable previous references and archive**

Fail if target tags or archive already exist. Compute one shared suffix `previous-${previous_source_sha}-${previous_bot_id_prefix}` from the recorded 12-character source SHA and first 12 image-ID hex characters; apply it to `moroz-staging-bot`, `moroz-staging-worker`, and `moroz-staging-migrate`. Save those exact images to `/opt/moroz-staging/tmp/releases/yclients-7e2ec278ed7/previous-images.tar`, calculate SHA-256, restrict directory/file permissions, re-inspect tags and IDs, and keep stores running.

- [x] **Step 5: Record Task 1 evidence and commit**

Evidence contains only source SHA, per-service image refs/IDs, archive SHA-256, health counts and migration revision. Update roadmap/master/changelog and commit the logical checkpoint.

### Task 2: Fresh local verification and candidate delivery without push

**Produces:** verified Git bundle and fresh Docker evidence for the candidate source.

- [x] **Step 1: Run fresh full Docker gate**

In a unique Compose namespace, generate one-time process-only PostgreSQL/Redis/RabbitMQ/webhook values, build the test image without cache, run full `pytest -q -rs`, verify migration head, Compose config, compile/static safety gates, then remove only exact task containers/volumes/network/image and prove `0/0/0/0`.

Expected: `472 passed`, skips `0`, `0006_yclients_booking_key (head)`, no leaked secret values.

- [x] **Step 2: Build and verify the Git bundle**

Run:

```powershell
git bundle create tmp/yclients-staging-release.bundle HEAD
git bundle verify tmp/yclients-staging-release.bundle
git bundle list-heads tmp/yclients-staging-release.bundle
```

Expected: bundle contains exact reviewed release commit; no `.env` or ignored temporary files.

- [x] **Step 3: Transfer via protected artifact handoff**

Use the deploy skill’s `scp`/`expect` wrapper or the documented Task 1 Step 2 transport exception. Copy bundle to `/opt/moroz-staging/tmp/releases/yclients-7e2ec278ed7/candidate.bundle`; never print host/password/path secrets. On VPS verify bundle, fetch it through Git, require `FETCH_HEAD` equals the expected full SHA, require clean staging checkout, then detached-checkout exact candidate.

- [x] **Step 4: Install only allowlisted YCLIENTS staging config**

Presence-check local `YCLIENTS_PARTNER_TOKEN`, `YCLIENTS_USER_TOKEN`, `YCLIENTS_COMPANY_ID`, `YCLIENTS_BASE_URL`, `YCLIENTS_TIMEZONE`, `YCLIENTS_TIMEOUT_SECONDS`, `YCLIENTS_TEST_SERVICE_ID` without values. Through suppressed non-echoing stdin of the active protected transport, atomically replace only these keys in protected server `.env`; verify mode/owner plus presence/count only.

- [x] **Step 5: Record Task 2 evidence and commit**

Record exact source SHA, bundle digest, fresh test/migration/cleanup counts and server checkout boolean. Do not push.

### Task 3: Build candidate, migrate forward and validate

**Produces:** distinct candidate images running on expanded schema with safe staging/YCLIENTS evidence.

- [x] **Step 1: Build immutable candidate**

Set `STAGING_IMAGE_TAG=yclients-7e2ec278ed7`. Validate merged base+staging Compose, build `bot worker migrate`, inspect non-root users and exact IDs, and require candidate bot/worker IDs differ from their previous counterparts. Save candidate images to `/opt/moroz-staging/tmp/releases/yclients-7e2ec278ed7/candidate-images.tar` with SHA-256.

- [x] **Step 2: Apply only forward migration**

Keep stores running and execute only:

```bash
docker compose --env-file ../.env -p moroz-staging \
  -f docker-compose.yml -f docker-compose.staging.yml \
  --profile migration run --rm migrate alembic upgrade head
```

Read back exact `0006_yclients_booking_key (head)`. No downgrade command exists in this task.

- [x] **Step 3: Start candidate app only**

Run `up -d --no-build --wait --wait-timeout 120 bot worker`. Verify per-service configured refs/IDs equal candidate IDs, app health, stores/Caddy unchanged, loopback OpenAPI, webhook status and HTTPS `404/403/403`.

- [x] **Step 4: Run minimal safe Telegram smoke and log scan**

Reuse the existing synthetic staging smoke with a fresh snapshot/ID; require exact inbox/LLM/sent `1/1/1`. Pipe raw bot/worker/Caddy logs directly to `staging-smoke scan-logs`; require all aggregate counters zero.

- [x] **Step 5: Run read-only YCLIENTS readiness**

Run an ignored temporary script inside the candidate `yclients-smoke` image with overridden entrypoint. It may call only GET services, slots and record custom-fields; output only service/staff/slot counts, exact `moroz_booking_key` match count and boolean field properties. No POST/PUT/DELETE and no record lifecycle.

- [x] **Step 6: Record Task 3 evidence and commit**

Record candidate IDs/archive digest, migration, app/store health, webhook/HTTPS, smoke/log counts and read-only YCLIENTS counts/booleans.

### Task 4: Distinct app-only rollback rehearsal

**Produces:** live proof of `candidate → previous → candidate` with expanded DB retained.

- [x] **Step 1: Verify candidate starting point**

Capture candidate bot/worker configured refs and actual IDs, schema `0006`, health, webhook and stores/Caddy IDs/started-at values.

- [x] **Step 2: Switch only bot/worker to previous**

Export the recorded previous shared tag and run only `up -d --no-build --wait ... bot worker`. Require actual bot/worker IDs equal previous IDs and differ from candidate IDs. Confirm schema remains `0006`; PostgreSQL/Redis/RabbitMQ/Caddy IDs and started-at values remain unchanged.

- [x] **Step 3: Verify previous on expanded DB**

Require app health, loopback OpenAPI, webhook status and one fresh synthetic Telegram `1/1/1` smoke. Run safe aggregate log scan. Do not run YCLIENTS mutation/readiness against previous.

- [x] **Step 4: Restore candidate**

With failure-safe trap semantics, export candidate tag and run only `up -d --no-build --wait ... bot worker`. Require actual IDs equal candidate IDs and differ from previous IDs; schema stays `0006`, stores/Caddy unchanged.

- [x] **Step 5: Verify restored candidate**

Repeat app health, loopback, webhook, fresh synthetic `1/1/1` smoke and aggregate log scan. Confirm candidate artifacts and previous archive remain inspectable.

- [x] **Step 6: Record Task 4 evidence and commit**

Persist the sequence, per-service previous/candidate IDs, unchanged store/Caddy identity, migration, health/smoke/log counters and archive availability.

### Task 5: Independent review and fresh completion verification

**Produces:** honest Phase 3 close or exact remaining blocker.

- [x] **Step 1: Self-review requirements line by line**

Check user contract items 1–7, this plan’s Global Constraints, staging runbook section 13 and `ТЗ и архитектура.md` expand–migrate–contract rules. Search evidence for secret-shaped strings and raw IDs/PII.

- [x] **Step 2: Request independent review**

Provide reviewer only base/head SHAs, this plan, tracked diff and safe evidence. Fix every Critical/Important finding; record Minor findings or close them when bounded.

- [x] **Step 3: Run fresh Docker verification**

Repeat the complete isolated Docker suite/config/migration/static/cleanup gates from Task 2 after the final tracked change. Separately re-check current staging candidate health, exact candidate IDs, schema `0006`, webhook and safe logs.

- [x] **Step 4: Close documents and commit**

Only if all evidence passes, mark Phase 3 staging/rollback complete in `Дорожная карта.md` and `План реализации.md`, finalize this document’s Evidence section and commit. Otherwise leave the phase open with one exact blocker. Never merge or push.

## Execution Self-Review

- Spec coverage: all seven approved release/rollback contract items map to Tasks 1–5.
- Destructive scope: no delete/prune/down/volume/schema rollback commands; only new immutable tags/archives, candidate checkout/build, forward migration and app-only `up`.
- Artifact distinction: both bot and worker require per-service previous ID different from candidate ID.
- Previous compatibility: proved live only after schema reaches `0006`; stores and schema remain unchanged during both switches.
- Provider safety: YCLIENTS readiness is GET-only; previous consented lifecycle evidence is reused and no lifecycle mutation is scheduled.
- Delivery authority: no push/merge; Git bundle is the explicit safe artifact handoff. Failure to verify bundle or exact checkout is a blocker.
- Secret safety: output is restricted to digest/count/boolean/revision/health evidence; raw logs go directly to existing scanner.

## Evidence

Status: complete.

- Local start: linked detached worktree, base `7e2ec278ed730edf15b58c2a20cc82d3cfbe42ec`.
- Local release-plan checkpoint: `029510fe761c91f7ab637bbc8bdbfdd5d7f5f6e5`, clean detached worktree immediately after commit.
- Remote candidate availability: absent from existing remote branches; Git bundle handoff selected.
- Restored control channel: SSH banner and password-authenticated Docker/`expect` probe pass; raw SSH output and credentials remain suppressed.
- Read-only previous capture: clean server checkout `a964ab9c2fce`; staging services `6/6` running and `6/6` healthy; non-staging services `4` running and `0` unhealthy. Previous app refs are `moroz-staging-bot:32fa9924a84a`, `moroz-staging-worker:32fa9924a84a`, and `moroz-staging-migrate:32fa9924a84a`; exact image IDs are `sha256:85b96af90cdf884c08cb31fc9a389b5152bd33a1536f758d07e3ddf847797bb8`, `sha256:9884a8df7c29e6f78184b99d9a8ba3477d228ec91cbc6d9a2d9def8e456450cd`, and `sha256:95db27d1162911222f431754a2ebe6f126f718c612cbcd47393455420882f3ec`. Current schema is `0004_pipeline_order_claim`; all three images remain locally inspectable on the VPS.
- Immutable previous: shared suffix `previous-a964ab9c2fce-85b96af90cdf`; re-inspected bot/worker/migrate IDs exactly match the read-only capture. Server-side `previous-images.tar` is `99933696` bytes with SHA-256 `6f48c360dbddf07e6017337a9734d6f706b5cb62a95e37866a02c0652992642f`; staging remained `6/6` running and healthy throughout.
- Transport exception: after the first authenticated command, Docker Desktop bridge/host networking stopped receiving the VPS SSH banner while Windows continued to receive it. After proving both Docker-to-VPS and Docker-to-Windows-forward unavailable, Task 1 Step 2’s bounded process-only Windows OpenSSH askpass exception replaced only the client transport. Password/raw SSH output remained suppressed, every remote command stayed allowlisted, and all server-side rollout operations remained Docker Compose-only.
- Fresh local gate: no-cache test image, `472 passed in 332.93s`, skips `0`; standalone task-prefixed migration image reached `0006_yclients_booking_key (head)`.
- Local cleanup: full-suite namespace `0/0/0/0`; corrected task-prefixed migration namespace/image `0/0/0/0`.
- Local isolation note: the first standalone migration command omitted explicit `MIGRATION_IMAGE` and rebuilt the pre-existing local tag `moroz-i-solntse-migrate:local`. No shared container used it; the prior image ID was not recoverable (`dangling=0`), so no blind retag/delete was attempted. The migration gate was repeated with an exact task-prefixed image and clean teardown.
- Candidate bundle: complete-history Git bundle, head `b5ce49dd405bec817826e6e519effa6218329639`, SHA-256 `0495d183e0f4d230bd859e420bc7164453bd2929946b4bfd73e22e8fe8cd2805`, size `2178684` bytes, `git bundle verify` exit `0`.
- Local evidence head after bundle record: `fca6d8d2ecc1d5b3769d51c7cc2e0af73a45627f`; bundle intentionally remains pinned to the verified source/evidence head above.
- Candidate source equivalence: `git diff --exit-code b5ce49dd405bec817826e6e519effa6218329639..dd96f9c2d5c7 -- project` returned `0`; every post-bundle commit through the completed rollback checkpoint is documentation/evidence-only. The complete app image build context, Compose definitions, migrations and runtime source under `project/` therefore exactly match the candidate bundle that was handed off and deployed. Final close changes remain tracked documentation-only and the same comparison is repeated before completion.
- Protected handoff: server digest and `git bundle verify` match local evidence; Git fetched only from the bundle and checked out exact clean detached `b5ce49dd405bec817826e6e519effa6218329639`. Existing staging remained `6/6` healthy on previous images.
- YCLIENTS config: the unique repo-local ignored env supplied Partner/User tokens, Company ID and Test Service ID; the three missing optional values use exact reviewed runtime defaults. Atomic stdin-only installation confirmed `7/7` keys, allowlist-only replacement and preserved owner/mode; no values were printed.
- Candidate artifacts: tag `yclients-7e2ec278ed7`; exact bot/worker/migrate IDs `sha256:6f3b3f4ef2efbf3756ea91bbce1f04799f7ba82bd37f9277fd59cd6370a74f1b`, `sha256:f779b4a62d7170dd29b8acc7205f4684302eade4d42975744370d0094908ebdf`, and `sha256:4f1dd9c093f3220727f636921f483150bfb48db508d4f4b0fe8b8c3f093f4eb2`. Users are `appuser/appuser/appuser`; bot/worker distinction from previous is `true/true`; image/history scan is `0/0/0/0`. Candidate archive is `99995648` bytes with SHA-256 `a08877037c3c3440f4090576d928e1941fddcbc1d1fcff2cb3f687d466a36fcf`.
- Forward migration and rollout: schema reached exact `0006_yclients_booking_key`; no downgrade command ran. Candidate bot/worker are `2/2` healthy, all four store/Caddy IDs and StartedAt values match the pre-migration baseline, loopback OpenAPI and webhook status pass, HTTPS is `404/403/403`, and staging is `6/6` healthy.
- Candidate smoke: fresh synthetic Telegram deltas are exact `1/1/1`; aggregate secret/traceback/PII/raw log counters are `0/0/0/0`.
- GET-only YCLIENTS readiness: services/staff/slots `1/1/312`, exact `moroz_booking_key` field matches `1`, text/edit/hidden properties all true, `18` calls and methods `GET_ONLY`. No provider lifecycle mutation ran.
- Official rollback rehearsal: exact sequence `candidate → previous → candidate`; bot/worker distinctions `true/true`. Previous on expanded schema and restored candidate each passed app health/webhook/loopback plus fresh Telegram `1/1/1` and log scan `0/0/0/0`. PostgreSQL/Redis/RabbitMQ/Caddy IDs and StartedAt values stayed unchanged, schema stayed `0006_yclients_booking_key`, both archives remained inspectable, and final app is candidate.
- Self-review: all seven approved contract items, Global Constraints, staging runbook rollback rules and expand–migrate–contract invariants checked line by line. Runtime/project diff from release base is `0`, tracked temp/secret-shaped additions are `0`, and no unsupported completion claim remains.
- Independent review fix-loop: the first effective review reported `0 Critical / 2 Important / 1 Minor`; the transport exception and candidate/source equivalence were made explicit. A separate review agent then confirmed both fixes and reported only two Important stale phase-status lines plus `0` other findings; those stale blockers were replaced by the final evidence-backed state before the closing re-review.
- Final independent re-review: `0 Critical / 0 Important / 0 Minor`; exact four-file documentation scope, bundle-to-current `project/` diff `0`, post-bundle non-document changes `0`, diff-check `0`, no stale release blocker, unsupported completion claim or secret-shaped addition.
- Fresh completion verification: no-cache full Docker suite `472 passed`, skips `0`; task-prefixed migration exact `0006_yclients_booking_key (head)`; corrected read-only compile gate true; scoped cleanup `0/0/0/0`. Final staging read-only verification returned clean candidate checkout, exact candidate IDs, app `2/2`, staging `6/6`, unchanged stores/Caddy, schema `0006`, loopback/webhook true, HTTPS `404/403/403`, log scan `0/0/0/0` and both immutable archives inspectable.
