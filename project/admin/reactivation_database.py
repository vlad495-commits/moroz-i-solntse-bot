"""PostgreSQL access for owner-only reactivation administration."""

from dataclasses import asdict
from datetime import UTC, datetime
import json
import os
from uuid import UUID, uuid4

from audit_repository import record_audit_in_transaction
from moroz.reactivation.policy import ProgramPolicy
from moroz.reactivation.repository import ReactivationRepository
from moroz.security.consent import ConsentService
from moroz.privacy import customer_lock_subject


SEGMENTS = {"after_visit", "sleeping", "regular"}
PAGE_SIZE = 50
OUTCOME_PERIODS = {7, 30, 90}
OUTCOME_FILTERS = {
    "all": "true",
    "replied": "journey.replied_at IS NOT NULL",
    "booked": "journey.booked_at IS NOT NULL",
    "completed": "journey.completed_visit_at IS NOT NULL",
    "opted_out": (
        "journey.close_reason = 'suppressed' AND EXISTS (SELECT 1 FROM "
        "marketing_consent_events AS outcome_event WHERE "
        "outcome_event.channel = journey.channel AND "
        "outcome_event.user_id = journey.user_id AND "
        "outcome_event.action = 'suppressed' AND "
        "outcome_event.source = 'telegram_explicit' AND "
        "outcome_event.occurred_at = journey.closed_at)"
    ),
    "escalated": (
        "journey.close_reason = 'escalated' OR journey.escalated_at IS NOT NULL"
    ),
}
DELIVERY_FILTERS = {
    "all": "true",
    "failed": (
        "journey.close_reason = 'failed' OR EXISTS (SELECT 1 FROM "
        "reactivation_journey_steps AS filtered_step WHERE "
        "filtered_step.journey_id = journey.id AND filtered_step.status = 'failed')"
    ),
    "delivery_unknown": (
        "journey.close_reason = 'delivery_unknown' OR EXISTS (SELECT 1 FROM "
        "reactivation_journey_steps AS filtered_step WHERE "
        "filtered_step.journey_id = journey.id AND "
        "filtered_step.status = 'delivery_unknown')"
    ),
}


def _v2(database) -> ReactivationRepository:
    return ReactivationRepository(
        database,
        session_secret=os.environ.get("ADMIN_SESSION_SECRET", ""),
        business_alert_chat_id=os.environ.get("BUSINESS_ALERT_CHAT_ID", ""),
    )


async def create_draft(
    database,
    *,
    policy: ProgramPolicy,
    actor_id: int,
    now: datetime | None = None,
):
    return await _v2(database).create_draft(
        policy, actor_id, now or datetime.now(UTC)
    )


async def preview_version(database, version_id: UUID, *, actor_id: int, now=None):
    return await _v2(database).preview_version(
        version_id, actor_id, now or datetime.now(UTC)
    )


async def preview_samples(database, version_id: UUID, *, actor_id: int, now=None):
    return await _v2(database).preview_samples(
        version_id, actor_id, now or datetime.now(UTC)
    )


async def queue_test_send(database, version_id: UUID, *, actor_id: int, now=None):
    return await _v2(database).queue_test_send(
        version_id, actor_id, now or datetime.now(UTC)
    )


async def record_test_sent(database, outbound_id: UUID, *, now=None):
    return await _v2(database).record_test_sent(
        outbound_id, now or datetime.now(UTC)
    )


async def approve_legal(
    database,
    *,
    actor_id: int,
    reference: str,
    now=None,
):
    return await _v2(database).approve_legal(
        actor_id, reference, now or datetime.now(UTC)
    )


async def activate_version(
    database,
    version_id: UUID,
    *,
    actor_id: int,
    start_program: bool = False,
    now=None,
):
    return await _v2(database).activate_version(
        version_id,
        actor_id,
        now or datetime.now(UTC),
        start_program=start_program,
    )


async def set_mode(database, mode: str, *, actor_id: int, now=None):
    return await _v2(database).set_mode(
        mode, actor_id, now or datetime.now(UTC)
    )


async def get_dashboard(database, *, actor_id: int):
    data = await _v2(database).get_dashboard(actor_id)
    for version in data["versions"]:
        if isinstance(version["preview_counts"], str):
            version["preview_counts"] = json.loads(version["preview_counts"])
    return data


async def get_marketing_page_data(
    database,
    *,
    actor_id: int,
    consent_id: UUID | None = None,
    page: int = 1,
    period: int = 30,
    outcome: str = "all",
    delivery: str = "all",
):
    if period not in OUTCOME_PERIODS:
        raise ValueError("unsupported outcome period")
    if outcome not in OUTCOME_FILTERS:
        raise ValueError("unsupported outcome filter")
    if delivery not in DELIVERY_FILTERS:
        raise ValueError("unsupported delivery filter")
    data = await get_dashboard(database, actor_id=actor_id)
    current = datetime.now(UTC)
    funnel = await _v2(database).get_outcome_funnel(current, period_days=period)
    offset = (page - 1) * PAGE_SIZE
    async with database.acquire() as connection:
        consent_query = """
            SELECT id, channel, user_id, consent_version, active,
                   proof_event_id IS NOT NULL AND proof_text_hash IS NOT NULL AS proven,
                   suppression_reason, granted_at, revoked_at, updated_at
            FROM marketing_consents
            {where}
            ORDER BY updated_at DESC, id DESC
            {paging}
            """
        if consent_id:
            consents = await connection.fetch(
                consent_query.format(where="WHERE id = $1", paging=""),
                consent_id,
            )
        else:
            consents = await connection.fetch(
                consent_query.format(where="", paging="LIMIT $1 OFFSET $2"),
                PAGE_SIZE + 1,
                offset,
            )
        if consent_id:
            consent_events = (
                await connection.fetch(
                    """
                    SELECT channel, user_id, action, source, occurred_at
                    FROM marketing_consent_events
                    WHERE channel = $1 AND user_id = $2
                    ORDER BY occurred_at DESC, id DESC
                    LIMIT $3 OFFSET $4
                    """,
                    consents[0]["channel"],
                    consents[0]["user_id"],
                    PAGE_SIZE + 1,
                    offset,
                )
                if consents
                else []
            )
        else:
            consent_events = await connection.fetch(
                """
                SELECT channel, user_id, action, source, occurred_at
                FROM marketing_consent_events
                ORDER BY occurred_at DESC, id DESC
                LIMIT $1 OFFSET $2
                """
                ,
                PAGE_SIZE + 1,
                offset,
            )
        readiness_row = await connection.fetchrow(
            """
            SELECT count(*) AS proven_consents,
                   count(*) FILTER (
                       WHERE activity.sync_status = 'current'
                         AND activity.history_synced_at >= now() - interval '24 hours'
                         AND activity.recent_bookings_synced_at >= now() - interval '15 minutes'
                   ) AS yclients_current,
                   min(activity.history_synced_at) AS oldest_history_synced_at,
                   min(activity.recent_bookings_synced_at) AS oldest_recent_bookings_synced_at,
                   COALESCE((
                       SELECT job.status IN ('pending', 'claimed', 'finished')
                              AND job.updated_at >= now() - interval '20 minutes'
                       FROM scheduler_jobs AS job
                       WHERE job.kind = 'reactivation_activity_sync'
                       ORDER BY job.updated_at DESC,
                                (job.last_error_code = 'yclients_unavailable') DESC,
                                job.id DESC
                       LIMIT 1
                   ), false) AS yclients_available
            FROM marketing_consents AS consent
            LEFT JOIN customer_activity_projection AS activity
              ON activity.channel = consent.channel
             AND activity.user_id = consent.user_id
            WHERE consent.active
              AND consent.proof_event_id IS NOT NULL
              AND consent.proof_text_hash IS NOT NULL
              AND consent.suppressed_at IS NULL
            """
        )
        journeys = await connection.fetch(
            f"""
            SELECT journey.id, journey.channel, journey.user_id,
                   journey.status, journey.close_reason, journey.created_at,
                   journey.first_sent_at, journey.replied_at, journey.booked_at,
                   journey.completed_visit_at, journey.escalated_at,
                   version.version_number
            FROM reactivation_journeys AS journey
            JOIN reactivation_program_versions AS version
              ON version.id = journey.program_version_id
            WHERE journey.created_at BETWEEN
                  $1::timestamptz - $2 * interval '1 day' AND $1
              AND ({OUTCOME_FILTERS[outcome]})
              AND ({DELIVERY_FILTERS[delivery]})
            ORDER BY journey.created_at DESC, journey.id DESC
            LIMIT $3 OFFSET $4
            """,
            current,
            period,
            PAGE_SIZE + 1,
            offset,
        )
        legacy_campaigns = await connection.fetch(
            """
            SELECT status, count(*) AS total,
                   COALESCE(sum(recipient_count), 0) AS recipients,
                   COALESCE(sum(sent_count), 0) AS sent,
                   COALESCE(sum(skipped_count), 0) AS skipped,
                   COALESCE(sum(error_count), 0) AS errors
            FROM reactivation_campaigns
            GROUP BY status
            ORDER BY status
            """
        )
        legacy_deliveries = await connection.fetch(
            """
            SELECT status, count(*) AS total
            FROM reactivation_deliveries
            GROUP BY status
            ORDER BY status
            """
        )
    has_next = any(
        len(rows) > PAGE_SIZE for rows in (consents, consent_events, journeys)
    )
    readiness = dict(readiness_row)
    readiness["yclients_ready"] = (
        readiness["yclients_available"]
        and readiness["proven_consents"] > 0
        and readiness["yclients_current"] == readiness["proven_consents"]
    )
    data.update(
        consents=[_masked_row(row) for row in consents[:PAGE_SIZE]],
        consent_events=[_masked_row(row) for row in consent_events[:PAGE_SIZE]],
        journeys=[_masked_row(row) for row in journeys[:PAGE_SIZE]],
        outcomes={},
        funnel=asdict(funnel),
        latest_preview_eligible=_latest_preview_eligible(data["versions"]),
        filters={"period": period, "outcome": outcome, "delivery": delivery},
        readiness=readiness,
        pagination={"page": page, "has_next": has_next},
        legacy={
            "campaigns": [dict(row) for row in legacy_campaigns],
            "deliveries": [dict(row) for row in legacy_deliveries],
        },
    )
    return data


def _latest_preview_eligible(versions) -> int | None:
    for version in versions:
        counts = version.get("preview_counts")
        if counts is not None and "eligible" in counts:
            return int(counts["eligible"])
    return None


async def revoke_marketing_consent_by_id(
    database,
    *,
    consent_id: UUID,
    actor_id: int,
    ip_address: str | None,
    user_agent: str | None,
):
    service = ConsentService(database)
    source_event_id = str(uuid4())
    occurred_at = datetime.now(UTC)
    async with database.acquire() as connection:
        async with connection.transaction():
            identity = await connection.fetchrow(
                """
                SELECT id, channel, user_id
                FROM marketing_consents
                WHERE id = $1
                """,
                consent_id,
            )
            if identity is None:
                raise ValueError("marketing consent not found")
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                customer_lock_subject(identity["user_id"]),
            )
            current = await connection.fetchrow(
                """
                SELECT id, channel, user_id
                FROM marketing_consents
                WHERE id = $1 AND channel = $2 AND user_id = $3
                """,
                consent_id,
                identity["channel"],
                identity["user_id"],
            )
            if current is None:
                raise ValueError("marketing consent not found")
            event = {
                "channel": current["channel"],
                "user_id": current["user_id"],
                "source": "admin_revoke",
                "source_event_id": source_event_id,
                "occurred_at": occurred_at,
                "connection": connection,
            }
            await service.revoke_marketing(**event)
            await service.suppress_marketing(
                **event,
                reason="admin_revoke",
            )
            result = await connection.fetchrow(
                """
                SELECT id, consent_version, active, revoked_at,
                       suppression_reason, updated_at
                FROM marketing_consents
                WHERE id = $1
                """,
                consent_id,
            )
            await record_audit_in_transaction(
                connection,
                actor_id=actor_id,
                action="reactivation.consent_revoked",
                object_type="marketing_consent",
                object_id=str(consent_id),
                before=None,
                after={
                    "active": result["active"],
                    "consent_version": result["consent_version"],
                },
                ip_address=ip_address,
                user_agent=user_agent,
            )
    return dict(result)


def _masked_row(row):
    value = dict(row)
    user_id = value.pop("user_id")
    value["customer"] = f"***{user_id[-4:]}" if len(user_id) > 4 else "*" * len(user_id)
    return value


async def get_settings(database):
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT after_visit_days, sleeping_days, discount_percent,
                   monthly_message_limit, ignore_limit, base_offer,
                   llm_instruction, updated_at
            FROM reactivation_settings
            WHERE id = 1
            """
        )
    if row is None:
        raise RuntimeError("reactivation settings are missing")
    return dict(row)


async def save_settings(database, **values):
    required = {
        "after_visit_days",
        "sleeping_days",
        "discount_percent",
        "monthly_message_limit",
        "ignore_limit",
        "base_offer",
        "llm_instruction",
    }
    if set(values) != required:
        raise ValueError("invalid reactivation settings")
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE reactivation_settings
            SET after_visit_days = $1,
                sleeping_days = $2,
                discount_percent = $3,
                monthly_message_limit = $4,
                ignore_limit = $5,
                base_offer = $6,
                llm_instruction = $7,
                updated_at = now()
            WHERE id = 1
            RETURNING after_visit_days, sleeping_days, discount_percent,
                      monthly_message_limit, ignore_limit, base_offer,
                      llm_instruction, updated_at
            """,
            values["after_visit_days"],
            values["sleeping_days"],
            values["discount_percent"],
            values["monthly_message_limit"],
            values["ignore_limit"],
            values["base_offer"],
            values["llm_instruction"],
        )
    return dict(row)


async def get_marketing_consent(database, *, channel: str, user_id: str):
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT id, channel, user_id, consent_version, active,
                   granted_at, revoked_at, updated_at
            FROM marketing_consents
            WHERE channel = $1 AND user_id = $2
            """,
            channel,
            user_id,
        )
    return dict(row) if row is not None else None


async def set_marketing_consent(
    database,
    *,
    channel: str,
    user_id: str,
    consent_version: str,
    active: bool,
):
    if not channel.strip() or not user_id.strip() or not consent_version.strip():
        raise ValueError("marketing consent fields are required")
    if active:
        raise ValueError("admin cannot grant marketing consent")
    source_event_id = str(uuid4())
    occurred_at = datetime.now(UTC)
    service = ConsentService(database)
    async with database.acquire() as connection:
        async with connection.transaction():
            event = {
                "channel": channel.strip(),
                "user_id": user_id.strip(),
                "source": "admin_revoke",
                "source_event_id": source_event_id,
                "occurred_at": occurred_at,
                "connection": connection,
            }
            await service.revoke_marketing(**event)
            await service.suppress_marketing(
                **event,
                reason="admin_revoke",
            )
            row = await connection.fetchrow(
                """
                SELECT id, channel, user_id, consent_version, active,
                       granted_at, revoked_at, updated_at
                FROM marketing_consents
                WHERE channel = $1 AND user_id = $2
                """,
                channel.strip(),
                user_id.strip(),
            )
    return dict(row)


async def create_campaign(database, *, segment: str, created_by: int | None):
    if segment not in SEGMENTS:
        raise ValueError("unsupported reactivation segment")
    campaign_id = uuid4()
    async with database.acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
            """
            INSERT INTO reactivation_campaigns
                (id, segment, status, after_visit_days, sleeping_days,
                 discount_percent, base_offer, llm_instruction, created_by)
            SELECT $1, $2, 'draft', after_visit_days, sleeping_days,
                   discount_percent, base_offer, llm_instruction, $3
            FROM reactivation_settings
            WHERE id = 1
            RETURNING id
            """,
                campaign_id,
                segment,
                created_by,
            )
            if row is not None:
                campaign = await connection.fetchrow(
                    "SELECT segment, after_visit_days, sleeping_days "
                    "FROM reactivation_campaigns WHERE id = $1",
                    campaign_id,
                )
                recipients = await _eligible_recipients(
                    connection, campaign=campaign, now=None
                )
                await connection.executemany(
                    """
                    INSERT INTO reactivation_deliveries
                        (id, campaign_id, channel, user_id, status)
                    VALUES ($1, $2, $3, $4, 'draft')
                    """,
                    [
                        (uuid4(), campaign_id, item["channel"], item["user_id"])
                        for item in recipients
                    ],
                )
                await connection.execute(
                    "UPDATE reactivation_campaigns SET recipient_count = $2 "
                    "WHERE id = $1",
                    campaign_id,
                    len(recipients),
                )
    if row is None:
        raise RuntimeError("reactivation settings are missing")
    return row["id"]


async def queue_campaign(
    database,
    *,
    campaign_id: UUID,
    now: datetime | None = None,
):
    async with database.acquire() as connection:
        async with connection.transaction():
            campaign = await connection.fetchrow(
                """
                SELECT id, segment, status, after_visit_days, sleeping_days
                FROM reactivation_campaigns
                WHERE id = $1
                FOR UPDATE
                """,
                campaign_id,
            )
            if campaign is None:
                raise ValueError("reactivation campaign not found")
            if campaign["status"] == "draft":
                await connection.execute(
                    """
                    UPDATE reactivation_deliveries AS delivery
                    SET status = 'skipped',
                        skip_reason = CASE
                            WHEN consent.active IS DISTINCT FROM true
                                THEN 'marketing_consent_inactive'
                            WHEN EXISTS (
                                SELECT 1 FROM human_mode AS mode
                                WHERE mode.customer_id = delivery.user_id
                                  AND mode.enabled = true
                            ) THEN 'human_mode'
                            ELSE 'customer_data_missing'
                        END,
                        updated_at = now()
                    FROM marketing_consents AS consent
                    WHERE delivery.campaign_id = $1
                      AND delivery.status = 'draft'
                      AND consent.channel = delivery.channel
                      AND consent.user_id = delivery.user_id
                      AND (
                        consent.active IS DISTINCT FROM true
                        OR EXISTS (
                            SELECT 1 FROM human_mode AS mode
                            WHERE mode.customer_id = delivery.user_id
                              AND mode.enabled = true
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM bookings AS booking
                            WHERE booking.customer_id = delivery.user_id
                              AND booking.status = 'completed'
                        )
                      )
                    """,
                    campaign_id,
                )
                await connection.execute(
                    "UPDATE reactivation_deliveries SET status = 'queued', "
                    "updated_at = now() WHERE campaign_id = $1 "
                    "AND status = 'draft'",
                    campaign_id,
                )
                skipped = await connection.fetchval(
                    "SELECT count(*) FROM reactivation_deliveries "
                    "WHERE campaign_id = $1 AND status = 'skipped'",
                    campaign_id,
                )
                await connection.execute(
                    """
                    UPDATE reactivation_campaigns
                    SET status = 'queued',
                        skipped_count = $2,
                        queued_at = COALESCE($3, now()),
                        updated_at = now()
                    WHERE id = $1
                    """,
                    campaign_id,
                    skipped,
                    now,
                )
            row = await connection.fetchrow(
                """
                SELECT id, segment, status, recipient_count, skipped_count,
                       sent_count, error_count, created_at, queued_at
                FROM reactivation_campaigns
                WHERE id = $1
                """,
                campaign_id,
            )
    return dict(row)


async def get_page_data(database):
    async with database.acquire() as connection:
        settings = await connection.fetchrow(
            """
            SELECT after_visit_days, sleeping_days, discount_percent,
                   monthly_message_limit, ignore_limit, base_offer,
                   llm_instruction, updated_at
            FROM reactivation_settings WHERE id = 1
            """
        )
        consents = await connection.fetch(
            """
            SELECT channel, user_id, consent_version, active,
                   granted_at, revoked_at, updated_at
            FROM marketing_consents
            ORDER BY updated_at DESC
            LIMIT 100
            """
        )
        campaigns = await connection.fetch(
            """
            SELECT id, segment, status, after_visit_days, sleeping_days,
                   discount_percent, base_offer, llm_instruction,
                   recipient_count, skipped_count, sent_count, error_count,
                   created_by, created_at, queued_at
            FROM reactivation_campaigns
            ORDER BY created_at DESC, id DESC
            LIMIT 100
            """
        )
        deliveries = await connection.fetch(
            """
            SELECT delivery.campaign_id, delivery.channel, delivery.user_id,
                   delivery.status, delivery.skip_reason, delivery.error_code,
                   delivery.created_at, delivery.updated_at,
                   stats.completed_visits, stats.last_visit_at,
                   latest_message.username, consent.granted_at AS consent_at
            FROM reactivation_deliveries AS delivery
            LEFT JOIN LATERAL (
                SELECT count(*)::integer AS completed_visits,
                       max(scheduled_end_at) AS last_visit_at
                FROM bookings
                WHERE customer_id = delivery.user_id
                  AND status = 'completed'
            ) AS stats ON true
            LEFT JOIN LATERAL (
                SELECT username
                FROM messages
                WHERE chat_id::text = delivery.user_id
                  AND username IS NOT NULL
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) AS latest_message ON true
            LEFT JOIN marketing_consents AS consent
              ON consent.channel = delivery.channel
             AND consent.user_id = delivery.user_id
            ORDER BY delivery.created_at DESC, delivery.id DESC
            LIMIT 200
            """
        )
    return {
        "settings": dict(settings),
        "consents": [dict(row) for row in consents],
        "campaigns": [dict(row) for row in campaigns],
        "deliveries": [dict(row) for row in deliveries],
    }


async def _eligible_recipients(connection, *, campaign, now):
    if campaign["segment"] not in SEGMENTS:
        raise ValueError("unsupported reactivation segment")
    return await connection.fetch(
        """
        WITH customer_segments AS (
            SELECT booking.customer_id,
                   CASE
                       WHEN max(booking.scheduled_end_at) <=
                            COALESCE($1, now()) - make_interval(days => $2)
                           THEN 'sleeping'
                       WHEN count(*) >= 2 THEN 'regular'
                       WHEN max(booking.scheduled_end_at) <=
                            COALESCE($1, now()) - make_interval(days => $3)
                           THEN 'after_visit'
                   END AS segment
            FROM bookings AS booking
            WHERE booking.status = 'completed'
              AND booking.scheduled_end_at IS NOT NULL
            GROUP BY booking.customer_id
        )
        SELECT consent.channel, consent.user_id
        FROM customer_segments AS segment
        JOIN marketing_consents AS consent
          ON consent.user_id = segment.customer_id
         AND consent.channel = 'telegram'
         AND consent.active = true
        WHERE segment.segment = $4
          AND NOT EXISTS (
            SELECT 1 FROM human_mode AS mode
            WHERE mode.customer_id = segment.customer_id
              AND mode.enabled = true
        )
        ORDER BY consent.channel, consent.user_id
        """,
        now,
        campaign["sleeping_days"],
        campaign["after_visit_days"],
        campaign["segment"],
    )
