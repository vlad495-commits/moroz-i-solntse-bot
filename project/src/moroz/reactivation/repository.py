from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
import json
from hashlib import sha256
from uuid import UUID, uuid4

from moroz.messaging.repository import MessageRepository
from moroz.reactivation.policy import (
    REASON_PRIORITY,
    EligibilityInput,
    ProgramPolicy,
    evaluate_eligibility,
    next_send_at,
    template_checksum,
)


PREVIEW_TTL = timedelta(minutes=30)
MODES = frozenset({"dry_run", "paused", "active"})
_ELIGIBILITY_SELECT = """
    SELECT consent.channel, consent.user_id, consent.active,
           consent.proof_event_id, consent.proof_text_hash,
           consent.suppressed_at,
           activity.identity_status, activity.last_completed_visit_at,
           activity.last_meaningful_inbound_at,
           activity.next_active_booking_at, activity.history_synced_at,
           activity.recent_bookings_synced_at, activity.sync_status,
           COALESCE(journey.has_active, false) AS has_active_journey,
           journey.latest_started_at,
           COALESCE(mode.enabled, false) AS human_mode,
           COALESCE(escalation.has_open, false) AS has_open_escalation
    FROM marketing_consents AS consent
    LEFT JOIN customer_activity_projection AS activity
      ON activity.channel = consent.channel AND activity.user_id = consent.user_id
    LEFT JOIN LATERAL (
        SELECT bool_or(item.status != 'closed') AS has_active,
               max(item.created_at) AS latest_started_at
        FROM reactivation_journeys AS item
        WHERE item.channel = consent.channel AND item.user_id = consent.user_id
    ) AS journey ON true
    LEFT JOIN LATERAL (
        SELECT bool_or(item.enabled AND
                       (item.expires_at IS NULL OR item.expires_at >= $1)) AS enabled
        FROM human_mode AS item WHERE item.customer_id = consent.user_id
    ) AS mode ON true
    LEFT JOIN LATERAL (
        SELECT bool_or(item.status = 'open') AS has_open
        FROM escalations AS item WHERE item.customer_id = consent.user_id
    ) AS escalation ON true
"""


class ActivationBlocked(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreviewResult:
    version_id: UUID
    created_at: datetime
    template_checksum: str
    total: int
    eligible: int
    planned_main: int
    planned_reminder: int
    excluded_by_reason: dict[str, int]
    population_watermark: datetime | None
    history_watermark: datetime | None
    recent_watermark: datetime | None
    masked_samples: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Population:
    result: PreviewResult
    checksum: str


class ReactivationRepository:
    def __init__(
        self,
        database,
        *,
        session_secret: str = "",
        business_alert_chat_id: str = "",
    ) -> None:
        self._database = database
        self._secret = session_secret.strip().encode("utf-8")
        self.business_alert_chat_id = business_alert_chat_id.strip()

    async def create_draft(
        self,
        policy: ProgramPolicy,
        actor_id: int,
        now: datetime,
    ) -> UUID:
        current = _aware(now)
        checksum = template_checksum(policy)
        version_id = uuid4()
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                await connection.fetchrow(
                    "SELECT id FROM reactivation_settings WHERE id = 1 FOR UPDATE"
                )
                version_number = await connection.fetchval(
                    "SELECT COALESCE(max(version_number), 0) + 1 "
                    "FROM reactivation_program_versions"
                )
                await connection.execute(
                    """
                    INSERT INTO reactivation_program_versions
                        (id, version_number, status, inactivity_days,
                         reminder_enabled, reminder_after_days, cooldown_days,
                         main_text, reminder_text, template_checksum,
                         created_by, created_at)
                    VALUES ($1, $2, 'draft', $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    """,
                    version_id,
                    version_number,
                    policy.inactivity_days,
                    policy.reminder_after_days is not None,
                    policy.reminder_after_days,
                    policy.cooldown_days,
                    policy.main_text,
                    policy.reminder_text,
                    checksum,
                    actor_id,
                    current,
                )
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.version_created",
                    object_type="reactivation_program_version",
                    object_id=str(version_id),
                    before={},
                    after={
                        "status": "draft",
                        "version_number": version_number,
                        "template_checksum": checksum,
                    },
                )
        return version_id

    async def preview_version(
        self,
        version_id: UUID,
        actor_id: int,
        now: datetime,
    ) -> PreviewResult:
        current = _aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                version = await self._version(connection, version_id, lock=True)
                if version["status"] == "retired":
                    raise ValueError("retired reactivation version cannot be previewed")
                before = _version_audit(version)
                population = await self._population(connection, version, current)
                counts = {
                    "total": population.result.total,
                    "eligible": population.result.eligible,
                    "planned_main": population.result.planned_main,
                    "planned_reminder": population.result.planned_reminder,
                    "excluded_by_reason": population.result.excluded_by_reason,
                }
                await connection.execute(
                    """
                    UPDATE reactivation_program_versions
                    SET preview_created_at = $2,
                        preview_checksum = $3,
                        preview_counts = $4::jsonb,
                        preview_population_watermark = $5,
                        preview_history_watermark = $6,
                        preview_recent_watermark = $7
                    WHERE id = $1
                    """,
                    version_id,
                    current,
                    population.checksum,
                    json.dumps(counts, sort_keys=True),
                    population.result.population_watermark,
                    population.result.history_watermark,
                    population.result.recent_watermark,
                )
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.version_previewed",
                    object_type="reactivation_program_version",
                    object_id=str(version_id),
                    before=before,
                    after={
                        **before,
                        "preview_created_at": current.isoformat(),
                        "preview_counts": counts,
                    },
                )
        return population.result

    async def queue_test_send(
        self,
        version_id: UUID,
        actor_id: int,
        now: datetime,
    ) -> UUID | None:
        _aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                version = await self._version(connection, version_id, lock=True)
                if version["status"] == "retired":
                    raise ValueError("retired reactivation version cannot be tested")
                self._require_same_template(version)
                if not self.business_alert_chat_id:
                    return None
                outbound_id = await MessageRepository(
                    self._database
                ).enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=self.business_alert_chat_id,
                    text=version["main_text"],
                    idempotency_key=(
                        f"reactivation-test:{version_id}:{version['template_checksum']}"
                    ),
                )
                if (
                    version["test_outbound_id"] == outbound_id
                    and version["test_sent_at"] is not None
                ):
                    return outbound_id
                await connection.execute(
                    """
                    UPDATE reactivation_program_versions
                    SET test_sent_at = CASE
                            WHEN test_outbound_id = $2 THEN test_sent_at
                            ELSE NULL
                        END,
                        test_outbound_id = $2
                    WHERE id = $1
                    """,
                    version_id,
                    outbound_id,
                )
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.test_queued",
                    object_type="reactivation_program_version",
                    object_id=str(version_id),
                    before={"test_outbound_id": _uuid_text(version["test_outbound_id"])},
                    after={"test_outbound_id": str(outbound_id), "test_sent": False},
                )
        return outbound_id

    async def preview_samples(
        self,
        version_id: UUID,
        actor_id: int,
        now: datetime,
    ) -> tuple[str, ...]:
        current = _aware(now)
        async with self._database.acquire() as connection:
            await self._require_owner(connection, actor_id)
            version = await self._version(connection, version_id)
            if version["status"] == "retired":
                raise ValueError("retired reactivation version cannot be previewed")
            population = await self._population(connection, version, current)
        return population.result.masked_samples

    async def record_test_sent(self, outbound_id: UUID, now: datetime) -> bool:
        current = _aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE reactivation_program_versions AS version
                    SET test_sent_at = COALESCE(version.test_sent_at, $2)
                    FROM outbound_messages AS outbound
                    WHERE version.test_outbound_id = $1
                      AND outbound.id = $1
                      AND outbound.status = 'sent'
                      AND version.test_sent_at IS NULL
                    RETURNING version.id, version.test_sent_at
                    """,
                    outbound_id,
                    current,
                )
                if row is None:
                    return False
                await _audit(
                    connection,
                    actor_id=None,
                    action="reactivation.test_sent",
                    object_type="reactivation_program_version",
                    object_id=str(row["id"]),
                    before={"test_sent": False},
                    after={"test_sent": True, "test_sent_at": row["test_sent_at"].isoformat()},
                )
        return True

    async def approve_legal(
        self,
        actor_id: int,
        reference: str,
        now: datetime,
    ) -> dict:
        current = _aware(now)
        legal_reference = reference.strip()
        if not legal_reference or len(legal_reference) > 500:
            raise ValueError("legal reference is required")
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                before = await connection.fetchrow(
                    """
                    SELECT legal_status, legal_reference, legal_approved_at,
                           legal_approved_by
                    FROM reactivation_settings WHERE id = 1 FOR UPDATE
                    """
                )
                if before is None:
                    raise RuntimeError("reactivation settings are missing")
                row = await connection.fetchrow(
                    """
                    UPDATE reactivation_settings
                    SET legal_status = 'approved', legal_reference = $1,
                        legal_approved_at = $2, legal_approved_by = $3,
                        updated_at = $2
                    WHERE id = 1
                    RETURNING legal_status, legal_reference, legal_approved_at,
                              legal_approved_by
                    """,
                    legal_reference,
                    current,
                    actor_id,
                )
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.legal_approved",
                    object_type="reactivation_settings",
                    object_id="1",
                    before=_legal_audit(before),
                    after=_legal_audit(row),
                )
        return dict(row)

    async def activate_version(
        self,
        version_id: UUID,
        actor_id: int,
        now: datetime,
    ) -> dict:
        current = _aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                settings = await connection.fetchrow(
                    "SELECT * FROM reactivation_settings WHERE id = 1 FOR UPDATE"
                )
                if settings is None:
                    raise RuntimeError("reactivation settings are missing")
                version = await self._version(connection, version_id, lock=True)
                if version["status"] == "retired":
                    raise ValueError("retired reactivation version cannot be activated")
                await _lock_population(connection)
                await self._check_activation_gates(
                    connection, settings, version, current
                )
                if not (
                    version["status"] == "active"
                    and settings["active_version_id"] == version_id
                ):
                    await connection.execute(
                        """
                        UPDATE reactivation_program_versions
                        SET status = 'retired'
                        WHERE status = 'active' AND id != $1
                        """,
                        version_id,
                    )
                    await connection.execute(
                        """
                        UPDATE reactivation_program_versions
                        SET status = 'active', activated_by = $2, activated_at = $3
                        WHERE id = $1
                        """,
                        version_id,
                        actor_id,
                        current,
                    )
                    await connection.execute(
                        """
                        UPDATE reactivation_settings
                        SET active_version_id = $1,
                            program_revision = program_revision + 1,
                            updated_at = $2
                        WHERE id = 1
                        """,
                        version_id,
                        current,
                    )
                row = await self._version(connection, version_id)
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.version_activated",
                    object_type="reactivation_program_version",
                    object_id=str(version_id),
                    before=_version_audit(version),
                    after=_version_audit(row),
                )
        return dict(row)

    async def set_mode(
        self,
        mode: str,
        actor_id: int,
        now: datetime,
    ) -> dict:
        current = _aware(now)
        if mode not in MODES:
            raise ValueError("unsupported reactivation mode")
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._require_owner(connection, actor_id)
                settings = await connection.fetchrow(
                    "SELECT * FROM reactivation_settings WHERE id = 1 FOR UPDATE"
                )
                if settings is None:
                    raise RuntimeError("reactivation settings are missing")
                if mode == "active":
                    if settings["active_version_id"] is None:
                        raise ActivationBlocked("active_version")
                    version = await self._version(
                        connection, settings["active_version_id"], lock=True
                    )
                    await _lock_population(connection)
                    await self._check_activation_gates(
                        connection, settings, version, current
                    )
                row = await connection.fetchrow(
                    """
                    UPDATE reactivation_settings
                    SET mode = $1,
                        stopped_at = CASE
                            WHEN $1 = 'active' THEN NULL
                            ELSE $2::timestamptz
                        END,
                        updated_at = $2
                    WHERE id = 1
                    RETURNING *
                    """,
                    mode,
                    current,
                )
                await _audit(
                    connection,
                    actor_id=actor_id,
                    action="reactivation.mode_changed",
                    object_type="reactivation_settings",
                    object_id="1",
                    before={"mode": settings["mode"]},
                    after={"mode": mode},
                )
        return dict(row)

    async def get_dashboard(self, actor_id: int) -> dict:
        async with self._database.acquire() as connection:
            await self._require_owner(connection, actor_id)
            settings = await connection.fetchrow(
                """
                SELECT mode, active_version_id, legal_status, legal_reference,
                       legal_approved_at, legal_approved_by, program_revision,
                       stopped_at, updated_at
                FROM reactivation_settings WHERE id = 1
                """
            )
            versions = await connection.fetch(
                """
                SELECT id, version_number, status, inactivity_days,
                       reminder_enabled, reminder_after_days, cooldown_days,
                       main_text, reminder_text, template_checksum, created_by,
                       created_at, activated_by, activated_at,
                       preview_created_at, preview_counts,
                       preview_population_watermark, preview_history_watermark,
                       preview_recent_watermark, test_outbound_id, test_sent_at
                FROM reactivation_program_versions
                ORDER BY version_number DESC
                """
            )
        if settings is None:
            raise RuntimeError("reactivation settings are missing")
        return {
            "settings": dict(settings),
            "versions": [dict(row) for row in versions],
        }

    async def fail_closed_yclients_unavailable(self, now: datetime) -> bool:
        current = _aware(now)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                settings = await connection.fetchrow(
                    "SELECT mode FROM reactivation_settings WHERE id = 1 FOR UPDATE"
                )
                await connection.execute(
                    """
                    UPDATE scheduler_jobs
                    SET status = 'skipped', finished_at = $1,
                        last_error_code = 'yclients_unavailable', updated_at = $1
                    WHERE kind IN ('reactivation_activity_sync', 'reactivation_tick')
                      AND status IN ('pending', 'claimed')
                    """,
                    current,
                )
                if settings is None or settings["mode"] == "dry_run":
                    return False
                await connection.execute(
                    """
                    UPDATE reactivation_settings
                    SET mode = 'dry_run', stopped_at = $1,
                        program_revision = program_revision + 1, updated_at = $1
                    WHERE id = 1
                    """,
                    current,
                )
                await _audit(
                    connection,
                    actor_id=None,
                    action="reactivation.yclients_unavailable",
                    object_type="reactivation_settings",
                    object_id="1",
                    before={"mode": settings["mode"]},
                    after={"mode": "dry_run", "gate": "yclients_unavailable"},
                )
        return True

    async def run_planner_cycle(
        self,
        now: datetime,
        *,
        planner_limit: int = 100,
        step_claim_limit: int = 100,
    ) -> int:
        current = _aware(now)
        planner_limit = max(0, min(planner_limit, 100))
        step_claim_limit = max(0, min(step_claim_limit, 100))
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await self._refresh_journey_outcomes(connection, current)
                settings = await connection.fetchrow(
                    "SELECT * FROM reactivation_settings WHERE id = 1 FOR SHARE"
                )
                if not _runtime_gates_open(settings):
                    return 0
                version = await connection.fetchrow(
                    "SELECT * FROM reactivation_program_versions "
                    "WHERE id = $1 AND status = 'active'",
                    settings["active_version_id"],
                )
                if version is None:
                    return 0
                policy = _policy(version)
                if planner_limit:
                    candidates = await self._planner_candidates(
                        connection, current, policy, limit=planner_limit
                    )
                    for row in candidates:
                        decision = _eligibility(row, policy, current)
                        if not decision.eligible or decision.activity_anchor_at is None:
                            continue
                        journey_id = uuid4()
                        inserted = await connection.fetchval(
                            """
                            INSERT INTO reactivation_journeys
                                (id, channel, user_id, program_version_id, status,
                                 activity_anchor_at, created_at, updated_at)
                            VALUES ($1, $2, $3, $4, 'scheduled', $5, $6, $6)
                            ON CONFLICT DO NOTHING
                            RETURNING id
                            """,
                            journey_id,
                            row["channel"],
                            row["user_id"],
                            version["id"],
                            decision.activity_anchor_at,
                            current,
                        )
                        if inserted is None:
                            continue
                        await connection.execute(
                            """
                            INSERT INTO reactivation_journey_steps
                                (id, journey_id, step_kind, status, due_at,
                                 idempotency_key, created_at, updated_at)
                            VALUES ($1, $2, 'main', 'scheduled', $3, $4, $5, $5)
                            ON CONFLICT DO NOTHING
                            """,
                            uuid4(),
                            journey_id,
                            next_send_at(current),
                            f"reactivation:{journey_id}:main",
                            current,
                        )
                return await self._reserve_due_steps(
                    connection, policy, current, limit=step_claim_limit
                )

    async def record_delivery_sent(
        self, outbound_id: UUID, sent_at: datetime
    ) -> bool:
        delivered_at = _aware(sent_at)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                step = await connection.fetchrow(
                    """
                    SELECT step.id, step.journey_id, step.step_kind, step.status,
                           journey.status AS journey_status,
                           version.reminder_enabled, version.reminder_after_days
                    FROM reactivation_journey_steps AS step
                    JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
                    JOIN reactivation_program_versions AS version
                      ON version.id = journey.program_version_id
                    WHERE step.outbound_id = $1
                    FOR UPDATE OF step, journey
                    """,
                    outbound_id,
                )
                if step is None or step["status"] != "reserved":
                    return False
                await connection.execute(
                    """
                    UPDATE reactivation_journey_steps
                    SET status = 'sent', sent_at = $2, updated_at = $2
                    WHERE id = $1
                    """,
                    step["id"], delivered_at,
                )
                if step["journey_status"] == "closed":
                    return True
                if step["step_kind"] == "reminder":
                    await self._close_journey(
                        connection, step["journey_id"], "exhausted", delivered_at
                    )
                    return True
                await connection.execute(
                    """
                    UPDATE reactivation_journeys
                    SET status = 'active', first_sent_at = COALESCE(first_sent_at, $2),
                        updated_at = $2
                    WHERE id = $1 AND status != 'closed'
                    """,
                    step["journey_id"], delivered_at,
                )
                if not step["reminder_enabled"]:
                    await self._close_journey(
                        connection, step["journey_id"], "exhausted", delivered_at
                    )
                    return True
                due_at = next_send_at(
                    delivered_at + timedelta(days=step["reminder_after_days"])
                )
                await connection.execute(
                    """
                    INSERT INTO reactivation_journey_steps
                        (id, journey_id, step_kind, status, due_at,
                         idempotency_key, created_at, updated_at)
                    VALUES ($1, $2, 'reminder', 'scheduled', $3, $4, $5, $5)
                    ON CONFLICT DO NOTHING
                    """,
                    uuid4(), step["journey_id"], due_at,
                    f"reactivation:{step['journey_id']}:reminder", delivered_at,
                )
        return True

    async def _planner_candidates(
        self, connection, now: datetime, policy: ProgramPolicy, *, limit: int
    ):
        return await connection.fetch(
            _ELIGIBILITY_SELECT
            + """
              WHERE consent.channel = 'telegram'
                AND consent.active
                AND consent.proof_event_id IS NOT NULL
                AND consent.proof_text_hash IS NOT NULL
                AND consent.suppressed_at IS NULL
                AND activity.identity_status = 'verified'
                AND activity.sync_status = 'current'
                AND activity.history_synced_at >= $1 - interval '24 hours'
                AND activity.recent_bookings_synced_at >= $1 - interval '15 minutes'
                AND activity.last_completed_visit_at IS NOT NULL
                AND GREATEST(
                    activity.last_completed_visit_at,
                    COALESCE(activity.last_meaningful_inbound_at,
                             activity.last_completed_visit_at)
                ) <= $1 - $2 * interval '1 day'
                AND (activity.next_active_booking_at IS NULL
                     OR activity.next_active_booking_at < $1)
                AND NOT COALESCE(journey.has_active, false)
                AND (journey.latest_started_at IS NULL
                     OR journey.latest_started_at <= $1 - $3 * interval '1 day')
                AND NOT COALESCE(mode.enabled, false)
                AND NOT COALESCE(escalation.has_open, false)
              ORDER BY consent.updated_at, consent.id
              LIMIT $4
            """,
            now,
            policy.inactivity_days,
            policy.cooldown_days,
            limit,
        )

    async def _reserve_due_steps(
        self, connection, policy: ProgramPolicy, now: datetime, *, limit: int
    ) -> int:
        if limit <= 0:
            return 0
        rows = await connection.fetch(
            """
            SELECT step.id, step.journey_id, step.step_kind,
                   journey.channel, journey.user_id,
                   CASE WHEN step.step_kind = 'main'
                        THEN version.main_text ELSE version.reminder_text END AS text
            FROM reactivation_journey_steps AS step
            JOIN reactivation_journeys AS journey ON journey.id = step.journey_id
            JOIN reactivation_program_versions AS version
              ON version.id = journey.program_version_id
            WHERE step.status = 'scheduled' AND step.due_at <= $1
              AND journey.status != 'closed'
            ORDER BY step.due_at, step.id
            FOR UPDATE OF step SKIP LOCKED
            LIMIT $2
            """,
            now,
            limit,
        )
        reserved = 0
        messages = MessageRepository(self._database)
        for step in rows:
            state = await connection.fetchrow(
                _ELIGIBILITY_SELECT
                + " WHERE consent.channel = $2 AND consent.user_id = $3",
                now,
                step["channel"],
                step["user_id"],
            )
            decision = (
                _eligibility(state, policy, now, existing_journey=True)
                if state is not None
                else None
            )
            if decision is None or not decision.eligible:
                reason = decision.reason if decision is not None else "consent_revoked"
                await connection.execute(
                    """
                    UPDATE reactivation_journey_steps
                    SET status = 'cancelled', terminal_reason = $2, updated_at = $3
                    WHERE id = $1
                    """,
                    step["id"], reason, now,
                )
                await self._close_journey(connection, step["journey_id"], _close_reason(reason), now)
                continue
            key = f"reactivation:{step['journey_id']}:{step['step_kind']}"
            outbound_id = await messages.enqueue_outbound_in_transaction(
                connection,
                channel=step["channel"],
                chat_id=step["user_id"],
                text=step["text"],
                idempotency_key=key,
            )
            await connection.execute(
                """
                UPDATE reactivation_journey_steps
                SET status = 'reserved', reserved_at = $2,
                    outbound_id = $3, updated_at = $2
                WHERE id = $1 AND status = 'scheduled'
                """,
                step["id"], now, outbound_id,
            )
            reserved += 1
        return reserved

    async def _refresh_journey_outcomes(self, connection, now: datetime) -> None:
        await connection.execute(
            """
            UPDATE reactivation_journeys AS journey
            SET completed_visit_at = activity.last_completed_visit_at,
                updated_at = $1
            FROM customer_activity_projection AS activity
            WHERE journey.channel = activity.channel
              AND journey.user_id = activity.user_id
              AND journey.first_sent_at IS NOT NULL
              AND activity.last_completed_visit_at > journey.first_sent_at
              AND (journey.completed_visit_at IS NULL
                   OR activity.last_completed_visit_at > journey.completed_visit_at)
            """,
            now,
        )
        rows = await connection.fetch(
            """
            SELECT journey.id,
                   CASE
                     WHEN consent.id IS NULL OR NOT consent.active
                       OR consent.suppressed_at IS NOT NULL THEN 'suppressed'
                     WHEN activity.last_meaningful_inbound_at >
                          COALESCE(journey.first_sent_at, journey.created_at)
                       THEN 'responded'
                     WHEN activity.next_active_booking_at >
                          COALESCE(journey.first_sent_at, journey.created_at)
                       THEN 'booked'
                   END AS reason,
                   activity.last_meaningful_inbound_at,
                   activity.next_active_booking_at
            FROM reactivation_journeys AS journey
            LEFT JOIN marketing_consents AS consent
              ON consent.channel = journey.channel AND consent.user_id = journey.user_id
            LEFT JOIN customer_activity_projection AS activity
              ON activity.channel = journey.channel AND activity.user_id = journey.user_id
            WHERE journey.status != 'closed'
            FOR UPDATE OF journey
            """
        )
        for row in rows:
            if row["reason"] is None:
                continue
            await connection.execute(
                """
                UPDATE reactivation_journeys
                SET replied_at = CASE WHEN $2 = 'responded' THEN $3 ELSE replied_at END,
                    booked_at = CASE WHEN $2 = 'booked' THEN $4 ELSE booked_at END
                WHERE id = $1
                """,
                row["id"], row["reason"], row["last_meaningful_inbound_at"],
                row["next_active_booking_at"],
            )
            await self._close_journey(connection, row["id"], row["reason"], now)

    @staticmethod
    async def _close_journey(connection, journey_id: UUID, reason: str, now: datetime):
        await connection.execute(
            """
            UPDATE reactivation_journeys
            SET status = 'closed', close_reason = $2, closed_at = $3, updated_at = $3
            WHERE id = $1 AND status != 'closed'
            """,
            journey_id, reason, now,
        )
        await connection.execute(
            """
            UPDATE reactivation_journey_steps
            SET status = 'cancelled', terminal_reason = $2, updated_at = $3
            WHERE journey_id = $1 AND status IN ('scheduled', 'reserved')
            """,
            journey_id, reason, now,
        )

    async def _check_activation_gates(
        self,
        connection,
        settings,
        version,
        now: datetime,
    ) -> None:
        preview_created_at = version["preview_created_at"]
        if (
            preview_created_at is None
            or preview_created_at > now
            or now - preview_created_at >= PREVIEW_TTL
        ):
            raise ActivationBlocked("fresh_preview")
        self._require_same_template(version)
        current = await self._population(connection, version, now)
        stored_watermarks = (
            version["preview_population_watermark"],
            version["preview_history_watermark"],
            version["preview_recent_watermark"],
        )
        current_watermarks = (
            current.result.population_watermark,
            current.result.history_watermark,
            current.result.recent_watermark,
        )
        if version["preview_checksum"] != current.checksum or stored_watermarks != current_watermarks:
            raise ActivationBlocked("current_watermarks")
        if self.business_alert_chat_id:
            test_ok = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM outbound_messages AS outbound
                    WHERE outbound.id = $1
                      AND outbound.status = 'sent'
                      AND outbound.channel = 'telegram'
                      AND outbound.chat_id = $3
                      AND outbound.text = $4
                      AND outbound.idempotency_key = $2
                )
                """,
                version["test_outbound_id"],
                f"reactivation-test:{version['id']}:{version['template_checksum']}",
                self.business_alert_chat_id,
                version["main_text"],
            )
            if version["test_sent_at"] is None or not test_ok:
                raise ActivationBlocked("test_sent")
        if (
            settings["legal_status"] != "approved"
            or settings["legal_approved_at"] is None
            or settings["legal_approved_by"] is None
            or not settings["legal_reference"]
        ):
            raise ActivationBlocked("legal_approved")

    def _require_same_template(self, version) -> None:
        if version["template_checksum"] != template_checksum(_policy(version)):
            raise ActivationBlocked("same_checksum")

    async def _population(self, connection, version, now: datetime) -> _Population:
        if not self._secret:
            raise ValueError("admin session secret is required for reactivation preview")
        policy = _policy(version)
        rows = await connection.fetch(
            """
            SELECT consent.id AS consent_id,
                   consent.channel, consent.user_id, consent.active,
                   consent.proof_event_id, consent.proof_text_hash,
                   consent.suppressed_at, consent.updated_at AS consent_updated_at,
                   activity.identity_status, activity.last_completed_visit_at,
                   activity.last_meaningful_inbound_at,
                   activity.next_active_booking_at, activity.history_synced_at,
                   activity.recent_bookings_synced_at, activity.sync_status,
                   activity.updated_at AS activity_updated_at,
                   COALESCE(journey.has_active, false) AS has_active_journey,
                   journey.latest_started_at, journey.updated_at AS journey_updated_at,
                   journey.fingerprint_rows AS journey_fingerprint_rows,
                   COALESCE(mode.enabled, false) AS human_mode,
                   mode.mutated_at AS human_mode_updated_at,
                   mode.fingerprint_row AS human_mode_fingerprint_row,
                   COALESCE(escalation.has_open, false) AS has_open_escalation,
                   escalation.mutated_at AS escalation_updated_at,
                   escalation.fingerprint_rows AS escalation_fingerprint_rows
            FROM marketing_consents AS consent
            LEFT JOIN customer_activity_projection AS activity
              ON activity.channel = consent.channel
             AND activity.user_id = consent.user_id
            LEFT JOIN LATERAL (
                SELECT bool_or(item.status != 'closed') AS has_active,
                       max(item.created_at) AS latest_started_at,
                       max(item.updated_at) AS updated_at,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object(
                                   'status', item.status,
                                   'created_at', item.created_at,
                                   'updated_at', item.updated_at
                               )
                               ORDER BY item.status, item.created_at,
                                        item.updated_at, item.id
                           ),
                           '[]'::jsonb
                       ) AS fingerprint_rows
                FROM reactivation_journeys AS item
                WHERE item.channel = consent.channel
                  AND item.user_id = consent.user_id
            ) AS journey ON true
            LEFT JOIN LATERAL (
                SELECT bool_or(item.enabled AND
                               (item.expires_at IS NULL OR item.expires_at >= $1)) AS enabled,
                       max(GREATEST(item.enabled_at,
                           COALESCE(item.expires_at, item.enabled_at))) AS mutated_at,
                       CASE WHEN count(*) = 0 THEN NULL ELSE jsonb_build_object(
                           'enabled', bool_or(item.enabled),
                           'enabled_at', max(item.enabled_at),
                           'expires_at', max(item.expires_at)
                       ) END AS fingerprint_row
                FROM human_mode AS item
                WHERE item.customer_id = consent.user_id
            ) AS mode ON true
            LEFT JOIN LATERAL (
                SELECT bool_or(item.status = 'open') AS has_open,
                       max(GREATEST(item.created_at,
                           COALESCE(item.resolved_at, item.created_at))) AS mutated_at,
                       COALESCE(
                           jsonb_agg(
                               jsonb_build_object(
                                   'status', item.status,
                                   'created_at', item.created_at,
                                   'resolved_at', item.resolved_at
                               )
                               ORDER BY item.status, item.created_at,
                                        item.resolved_at, item.id
                           ),
                           '[]'::jsonb
                       ) AS fingerprint_rows
                FROM escalations AS item
                WHERE item.customer_id = consent.user_id
            ) AS escalation ON true
            ORDER BY consent.id
            """,
            now,
        )
        excluded = {reason: 0 for reason in REASON_PRIORITY}
        canonical = []
        samples = []
        eligible = 0
        population_watermark = None
        history_watermark = None
        recent_watermark = None
        for row in rows:
            value = EligibilityInput(
                identity_status=row["identity_status"] or "unverified",
                consent_active=bool(row["active"]),
                consent_proven=(
                    row["proof_event_id"] is not None
                    and row["proof_text_hash"] is not None
                ),
                suppressed=row["suppressed_at"] is not None,
                last_completed_visit_at=row["last_completed_visit_at"],
                last_meaningful_inbound_at=row["last_meaningful_inbound_at"],
                next_active_booking_at=row["next_active_booking_at"],
                history_synced_at=row["history_synced_at"],
                recent_bookings_synced_at=row["recent_bookings_synced_at"],
                sync_status=row["sync_status"] or "never",
                has_active_journey=bool(row["has_active_journey"]),
                latest_journey_started_at=row["latest_started_at"],
                human_mode=bool(row["human_mode"]),
                has_open_escalation=bool(row["has_open_escalation"]),
                deletion_active=False,
            )
            decision = evaluate_eligibility(value, policy, now)
            if decision.eligible:
                eligible += 1
                if len(samples) < 5:
                    samples.append(_mask(row["channel"], row["user_id"]))
            else:
                excluded[decision.reason] += 1
            row_population = _max_timestamp(
                row["consent_updated_at"],
                row["activity_updated_at"],
                row["journey_updated_at"],
                row["human_mode_updated_at"],
                row["escalation_updated_at"],
            )
            population_watermark = _max_timestamp(population_watermark, row_population)
            history_watermark = _max_timestamp(history_watermark, row["history_synced_at"])
            recent_watermark = _max_timestamp(
                recent_watermark, row["recent_bookings_synced_at"]
            )
            canonical.append(
                {
                    "consent_id": str(row["consent_id"]),
                    "decision": "eligible" if decision.eligible else "excluded",
                    "reason": decision.reason,
                    "activity_anchor_at": _iso(decision.activity_anchor_at),
                    "next_active_booking_at": _iso(row["next_active_booking_at"]),
                    "history_synced_at": _iso(row["history_synced_at"]),
                    "recent_bookings_synced_at": _iso(row["recent_bookings_synced_at"]),
                    "identity_status": value.identity_status,
                    "consent_active": value.consent_active,
                    "consent_proven": value.consent_proven,
                    "consent_updated_at": _iso(row["consent_updated_at"]),
                    "suppressed": value.suppressed,
                    "sync_status": value.sync_status,
                    "activity_updated_at": _iso(row["activity_updated_at"]),
                    "has_active_journey": value.has_active_journey,
                    "latest_journey_started_at": _iso(value.latest_journey_started_at),
                    "journey_rows": _json_value(row["journey_fingerprint_rows"]),
                    "human_mode": value.human_mode,
                    "human_mode_row": _json_value(
                        row["human_mode_fingerprint_row"]
                    ),
                    "has_open_escalation": value.has_open_escalation,
                    "escalation_rows": _json_value(
                        row["escalation_fingerprint_rows"]
                    ),
                    "population_mutated_at": _iso(row_population),
                }
            )
        safe_payload = json.dumps(
            {"template_checksum": version["template_checksum"], "rows": canonical},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        checksum = hmac.new(self._secret, safe_payload, sha256).hexdigest()
        result = PreviewResult(
            version_id=version["id"],
            created_at=now,
            template_checksum=version["template_checksum"],
            total=len(rows),
            eligible=eligible,
            planned_main=eligible,
            planned_reminder=eligible if version["reminder_enabled"] else 0,
            excluded_by_reason={key: value for key, value in excluded.items() if value},
            population_watermark=population_watermark,
            history_watermark=history_watermark,
            recent_watermark=recent_watermark,
            masked_samples=tuple(samples),
        )
        return _Population(result, checksum)

    async def _version(self, connection, version_id: UUID, *, lock: bool = False):
        row = await connection.fetchrow(
            "SELECT * FROM reactivation_program_versions WHERE id = $1"
            + (" FOR UPDATE" if lock else ""),
            version_id,
        )
        if row is None:
            raise ValueError("reactivation version not found")
        return row

    @staticmethod
    async def _require_owner(connection, actor_id: int) -> None:
        owner = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM admin_users
                WHERE id = $1 AND role = 'owner' AND enabled = true
            )
            """,
            actor_id,
        )
        if not owner:
            raise PermissionError("reactivation backend is owner-only")


def _policy(version) -> ProgramPolicy:
    return ProgramPolicy(
        inactivity_days=version["inactivity_days"],
        reminder_after_days=(
            version["reminder_after_days"] if version["reminder_enabled"] else None
        ),
        cooldown_days=version["cooldown_days"],
        main_text=version["main_text"],
        reminder_text=version["reminder_text"],
    )


def _runtime_gates_open(settings) -> bool:
    return bool(
        settings
        and settings["mode"] == "active"
        and settings["active_version_id"] is not None
        and settings["legal_status"] == "approved"
        and settings["legal_reference"]
        and settings["legal_approved_at"] is not None
        and settings["legal_approved_by"] is not None
    )


def _eligibility(
    row, policy: ProgramPolicy, now: datetime, *, existing_journey: bool = False
):
    return evaluate_eligibility(
        EligibilityInput(
            identity_status=row["identity_status"] or "unverified",
            consent_active=bool(row["active"]),
            consent_proven=(
                row["proof_event_id"] is not None
                and row["proof_text_hash"] is not None
            ),
            suppressed=row["suppressed_at"] is not None,
            last_completed_visit_at=row["last_completed_visit_at"],
            last_meaningful_inbound_at=row["last_meaningful_inbound_at"],
            next_active_booking_at=row["next_active_booking_at"],
            history_synced_at=row["history_synced_at"],
            recent_bookings_synced_at=row["recent_bookings_synced_at"],
            sync_status=row["sync_status"] or "never",
            has_active_journey=(
                False if existing_journey else bool(row["has_active_journey"])
            ),
            latest_journey_started_at=(
                None if existing_journey else row["latest_started_at"]
            ),
            human_mode=bool(row["human_mode"]),
            has_open_escalation=bool(row["has_open_escalation"]),
            deletion_active=False,
        ),
        policy,
        now,
    )


def _close_reason(reason: str) -> str:
    if reason in {"consent_revoked", "suppressed", "no_proven_consent"}:
        return "suppressed"
    if reason == "open_escalation":
        return "escalated"
    return "cancelled"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("reactivation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _max_timestamp(*values):
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _mask(channel: str, user_id: str) -> str:
    visible = user_id[-4:] if len(user_id) > 4 else "*" * len(user_id)
    return f"{channel}:***{visible}"


def _uuid_text(value) -> str | None:
    return str(value) if value is not None else None


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _version_audit(version) -> dict:
    counts = version.get("preview_counts")
    if isinstance(counts, str):
        counts = json.loads(counts)
    return {
        "status": version["status"],
        "version_number": version["version_number"],
        "template_checksum": version["template_checksum"],
        "preview_created_at": _iso(version["preview_created_at"]),
        "preview_counts": counts,
        "test_outbound_id": _uuid_text(version["test_outbound_id"]),
        "test_sent": version["test_sent_at"] is not None,
    }


def _legal_audit(settings) -> dict:
    return {
        "legal_status": settings["legal_status"],
        "legal_reference": settings["legal_reference"],
        "legal_approved_at": _iso(settings["legal_approved_at"]),
        "legal_approved_by": settings["legal_approved_by"],
    }


async def _audit(
    connection,
    *,
    actor_id,
    action: str,
    object_type: str,
    object_id: str,
    before: dict,
    after: dict,
) -> None:
    await connection.execute(
        """
        INSERT INTO admin_audit_events
            (actor_id, action, object_type, object_id, before, after)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        """,
        actor_id,
        action,
        object_type,
        object_id,
        json.dumps(before, ensure_ascii=False, sort_keys=True),
        json.dumps(after, ensure_ascii=False, sort_keys=True),
    )


async def _lock_population(connection) -> None:
    await connection.execute(
        """
        LOCK TABLE escalations,
                   human_mode,
                   marketing_consents,
                   customer_activity_projection,
                   reactivation_journeys
        IN SHARE MODE
        """
    )
