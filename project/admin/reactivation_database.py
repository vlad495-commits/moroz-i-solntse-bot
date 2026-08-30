"""PostgreSQL access for the internal reactivation admin queue."""

from datetime import datetime
from uuid import UUID, uuid4


SEGMENTS = {"after_visit", "sleeping", "regular"}


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
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active,
                 granted_at, revoked_at)
            VALUES (
                $1, $2, $3, $4, $5,
                CASE WHEN $5 THEN now() END,
                CASE WHEN $5 THEN NULL ELSE now() END
            )
            ON CONFLICT (channel, user_id) DO UPDATE SET
                consent_version = CASE
                    WHEN EXCLUDED.active THEN EXCLUDED.consent_version
                    ELSE marketing_consents.consent_version
                END,
                active = EXCLUDED.active,
                granted_at = CASE
                    WHEN EXCLUDED.active THEN now()
                    ELSE marketing_consents.granted_at
                END,
                revoked_at = CASE
                    WHEN EXCLUDED.active THEN NULL
                    ELSE now()
                END,
                updated_at = now()
            RETURNING id, channel, user_id, consent_version, active,
                      granted_at, revoked_at, updated_at
            """,
            uuid4(),
            channel.strip(),
            user_id.strip(),
            consent_version.strip(),
            active,
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
