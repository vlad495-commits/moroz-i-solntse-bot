"""PostgreSQL access for owner-only reactivation administration."""

from datetime import UTC, datetime
import os
from uuid import UUID, uuid4

from moroz.reactivation.policy import ProgramPolicy
from moroz.reactivation.repository import ReactivationRepository
from moroz.security.consent import ConsentService


SEGMENTS = {"after_visit", "sleeping", "regular"}


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


async def activate_version(database, version_id: UUID, *, actor_id: int, now=None):
    return await _v2(database).activate_version(
        version_id, actor_id, now or datetime.now(UTC)
    )


async def set_mode(database, mode: str, *, actor_id: int, now=None):
    return await _v2(database).set_mode(
        mode, actor_id, now or datetime.now(UTC)
    )


async def get_dashboard(database, *, actor_id: int):
    return await _v2(database).get_dashboard(actor_id)


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
                recipients = await _eligible_recipients(
                    connection,
                    campaign=campaign,
                    now=now,
                )
                await connection.executemany(
                    """
                    INSERT INTO reactivation_deliveries
                        (id, campaign_id, channel, user_id, status)
                    VALUES ($1, $2, $3, $4, 'queued')
                    ON CONFLICT (campaign_id, channel, user_id) DO NOTHING
                    """,
                    [
                        (uuid4(), campaign_id, row["channel"], row["user_id"])
                        for row in recipients
                    ],
                )
                await connection.execute(
                    """
                    UPDATE reactivation_campaigns
                    SET status = 'queued',
                        recipient_count = $2,
                        queued_at = COALESCE($3, now()),
                        updated_at = now()
                    WHERE id = $1
                    """,
                    campaign_id,
                    len(recipients),
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
            SELECT campaign_id, channel, user_id, status, skip_reason,
                   error_code, created_at, updated_at
            FROM reactivation_deliveries
            ORDER BY created_at DESC, id DESC
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
    segment = campaign["segment"]
    if segment == "regular":
        segment_clause = "count(*) >= 2"
        parameters = ()
    elif segment == "sleeping":
        segment_clause = (
            "max(booking.scheduled_end_at) <= "
            "COALESCE($1, now()) - make_interval(days => $2)"
        )
        parameters = (now, campaign["sleeping_days"])
    else:
        segment_clause = (
            "max(booking.scheduled_end_at) <= "
            "COALESCE($1, now()) - make_interval(days => $2)"
        )
        parameters = (now, campaign["after_visit_days"])
    return await connection.fetch(
        f"""
        WITH segment_customers AS (
            SELECT booking.customer_id
            FROM bookings AS booking
            WHERE booking.status = 'completed'
              AND booking.scheduled_end_at IS NOT NULL
            GROUP BY booking.customer_id
            HAVING {segment_clause}
        )
        SELECT consent.channel, consent.user_id
        FROM segment_customers AS segment
        JOIN marketing_consents AS consent
          ON consent.user_id = segment.customer_id
         AND consent.channel = 'telegram'
         AND consent.active = true
        WHERE NOT EXISTS (
            SELECT 1 FROM human_mode AS mode
            WHERE mode.customer_id = segment.customer_id
              AND mode.enabled = true
        )
        ORDER BY consent.channel, consent.user_id
        """,
        *parameters,
    )
