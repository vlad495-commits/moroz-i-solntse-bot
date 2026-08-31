from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

import asyncpg

from moroz.common.db import Database
from moroz.privacy import customer_lock_subject


PROCESSING_CONSENT_VERSION = "v1"
MARKETING_CONSENT_VERSION = "marketing-v1"
_EXPLICIT_MARKETING_SOURCE = "telegram_explicit"


@dataclass(frozen=True, slots=True)
class MarketingConsentState:
    consent_id: UUID | None
    active: bool
    consent_version: str | None
    proof_text_hash: str | None
    source: str | None
    source_event_id: str | None
    suppressed: bool
    suppression_reason: str | None


class ConsentService:
    def __init__(self, database: Database):
        self._database = database

    async def has_processing_consent(self, channel: str, user_id: str) -> bool:
        async with self._database.acquire() as connection:
            return await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM processing_consents
                    WHERE channel = $1
                      AND user_id = $2
                      AND consent_version = $3
                )
                """,
                channel,
                user_id,
                PROCESSING_CONSENT_VERSION,
            )

    async def grant_processing_consent(
        self,
        channel: str,
        user_id: str,
        consent_version: str,
        *,
        connection=None,
    ) -> None:
        if connection is not None:
            await self._grant_processing(
                connection, channel, user_id, consent_version
            )
            return
        async with self._database.acquire() as owned_connection:
            await self._grant_processing(
                owned_connection, channel, user_id, consent_version
            )

    async def _grant_processing(
        self, connection, channel: str, user_id: str, consent_version: str
    ) -> None:
        await connection.execute(
            """
            INSERT INTO processing_consents
                (channel, user_id, consent_version)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            channel,
            user_id,
            consent_version,
        )

    async def get_marketing_status(
        self, channel: str, user_id: str
    ) -> MarketingConsentState:
        async with self._database.acquire() as connection:
            return await self._get_marketing_status(
                connection, channel, user_id
            )

    async def grant_marketing(
        self,
        *,
        channel: str,
        user_id: str,
        proof_text: str,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
        connection: asyncpg.Connection | None = None,
    ) -> MarketingConsentState:
        self._validate_event(
            channel, user_id, source, source_event_id, occurred_at
        )
        if source != _EXPLICIT_MARKETING_SOURCE:
            raise ValueError(
                "marketing consent requires an explicit Telegram action"
            )
        if not proof_text:
            raise ValueError("marketing proof text is required")
        return await self._run_marketing_event(
            connection,
            channel=channel,
            user_id=user_id,
            action="granted",
            proof_text=proof_text,
            source=source,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
        )

    async def revoke_marketing(
        self,
        *,
        channel: str,
        user_id: str,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
        connection: asyncpg.Connection | None = None,
    ) -> MarketingConsentState:
        self._validate_event(
            channel, user_id, source, source_event_id, occurred_at
        )
        return await self._run_marketing_event(
            connection,
            channel=channel,
            user_id=user_id,
            action="revoked",
            proof_text=None,
            source=source,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
        )

    async def suppress_marketing(
        self,
        *,
        channel: str,
        user_id: str,
        reason: str,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
        connection: asyncpg.Connection | None = None,
    ) -> MarketingConsentState:
        self._validate_event(
            channel, user_id, source, source_event_id, occurred_at
        )
        if not reason.strip():
            raise ValueError("marketing suppression reason is required")
        return await self._run_marketing_event(
            connection,
            channel=channel,
            user_id=user_id,
            action="suppressed",
            proof_text=None,
            source=source,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
            suppression_reason=reason.strip(),
        )

    async def unsuppress_marketing(
        self,
        *,
        channel: str,
        user_id: str,
        proof_text: str,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
        connection: asyncpg.Connection | None = None,
    ) -> MarketingConsentState:
        self._validate_event(
            channel, user_id, source, source_event_id, occurred_at
        )
        if source != _EXPLICIT_MARKETING_SOURCE:
            raise ValueError(
                "marketing consent requires an explicit Telegram action"
            )
        if not proof_text:
            raise ValueError("marketing proof text is required")
        return await self._run_marketing_event(
            connection,
            channel=channel,
            user_id=user_id,
            action="unsuppressed",
            proof_text=proof_text,
            source=source,
            source_event_id=source_event_id,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _validate_event(
        channel: str,
        user_id: str,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
    ) -> None:
        if not all(
            value.strip()
            for value in (channel, user_id, source, source_event_id)
        ):
            raise ValueError("marketing consent event fields are required")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("marketing consent timestamp must be timezone-aware")

    async def _run_marketing_event(
        self,
        connection: asyncpg.Connection | None,
        **event,
    ) -> MarketingConsentState:
        if connection is not None:
            return await self._apply_marketing_event(connection, **event)
        async with self._database.acquire() as owned_connection:
            async with owned_connection.transaction():
                return await self._apply_marketing_event(
                    owned_connection, **event
                )

    async def _apply_marketing_event(
        self,
        connection: asyncpg.Connection,
        *,
        channel: str,
        user_id: str,
        action: str,
        proof_text: str | None,
        source: str,
        source_event_id: str,
        occurred_at: datetime,
        suppression_reason: str | None = None,
    ) -> MarketingConsentState:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            customer_lock_subject(user_id),
        )
        proof_text_hash = (
            sha256(proof_text.encode()).hexdigest()
            if proof_text is not None
            else None
        )
        inserted_event_id = await connection.fetchval(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at,
                 created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, clock_timestamp())
            ON CONFLICT (channel, user_id, action, source, source_event_id)
            DO NOTHING
            RETURNING id
            """,
            uuid4(),
            channel,
            user_id,
            action,
            MARKETING_CONSENT_VERSION,
            proof_text_hash,
            source,
            source_event_id,
            occurred_at,
        )
        if inserted_event_id is None:
            return await self._get_marketing_status(
                connection, channel, user_id
            )

        if source_event_id.isascii() and source_event_id.isdigit():
            has_newer_event = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM marketing_consent_events
                    WHERE channel = $1
                      AND user_id = $2
                      AND id <> $3
                      AND source_event_id ~ '^[0-9]+$'
                      AND source_event_id::numeric > $4::numeric
                )
                """,
                channel,
                user_id,
                inserted_event_id,
                source_event_id,
            )
        else:
            has_newer_event = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM marketing_consent_events
                    WHERE channel = $1
                      AND user_id = $2
                      AND id <> $3
                      AND occurred_at > $4
                )
                """,
                channel,
                user_id,
                inserted_event_id,
                occurred_at,
            )
        if has_newer_event:
            return await self._get_marketing_status(
                connection, channel, user_id
            )

        if action == "granted":
            await connection.execute(
                """
                INSERT INTO marketing_consents
                    (id, channel, user_id, consent_version, active,
                     granted_at, revoked_at, source, proof_event_id,
                     proof_text_hash)
                VALUES ($1, $2, $3, $4, true, $5, NULL, $6, $7, $8)
                ON CONFLICT (channel, user_id) DO UPDATE SET
                    consent_version = EXCLUDED.consent_version,
                    active = marketing_consents.suppressed_at IS NULL,
                    granted_at = EXCLUDED.granted_at,
                    revoked_at = NULL,
                    source = EXCLUDED.source,
                    proof_event_id = EXCLUDED.proof_event_id,
                    proof_text_hash = EXCLUDED.proof_text_hash,
                    updated_at = now()
                """,
                uuid4(),
                channel,
                user_id,
                MARKETING_CONSENT_VERSION,
                occurred_at,
                source,
                inserted_event_id,
                proof_text_hash,
            )
        elif action == "unsuppressed":
            await connection.execute(
                """
                INSERT INTO marketing_consents
                    (id, channel, user_id, consent_version, active,
                     revoked_at, source)
                VALUES ($1, $2, $3, $4, false, $5, $6)
                ON CONFLICT (channel, user_id) DO UPDATE SET
                    active = false,
                    suppressed_at = NULL,
                    suppression_reason = NULL,
                    suppression_source = NULL,
                    updated_at = now()
                """,
                uuid4(),
                channel,
                user_id,
                MARKETING_CONSENT_VERSION,
                occurred_at,
                source,
            )
        else:
            await connection.execute(
                """
                INSERT INTO marketing_consents
                    (id, channel, user_id, consent_version, active,
                     revoked_at, source, suppressed_at, suppression_reason,
                     suppression_source)
                VALUES (
                    $1, $2, $3, $4, false, $5::timestamptz, $6,
                    CASE WHEN $7 = 'suppressed' THEN $5::timestamptz END,
                    CASE WHEN $7 = 'suppressed' THEN $8 END,
                    CASE WHEN $7 = 'suppressed' THEN $6 END
                )
                ON CONFLICT (channel, user_id) DO UPDATE SET
                    active = false,
                    revoked_at = EXCLUDED.revoked_at,
                    suppressed_at = CASE
                        WHEN $7 = 'suppressed' THEN EXCLUDED.suppressed_at
                        ELSE marketing_consents.suppressed_at
                    END,
                    suppression_reason = CASE
                        WHEN $7 = 'suppressed' THEN EXCLUDED.suppression_reason
                        ELSE marketing_consents.suppression_reason
                    END,
                    suppression_source = CASE
                        WHEN $7 = 'suppressed' THEN EXCLUDED.suppression_source
                        ELSE marketing_consents.suppression_source
                    END,
                    updated_at = now()
                """,
                uuid4(),
                channel,
                user_id,
                MARKETING_CONSENT_VERSION,
                occurred_at,
                source,
                action,
                suppression_reason,
            )
            await connection.execute(
                """
                UPDATE reactivation_journey_steps AS step
                SET status = 'cancelled',
                    terminal_reason = $3,
                    updated_at = now()
                FROM reactivation_journeys AS journey
                WHERE journey.id = step.journey_id
                  AND journey.channel = $1
                  AND journey.user_id = $2
                  AND step.status IN ('scheduled', 'reserved')
                """,
                channel,
                user_id,
                "suppressed" if action == "suppressed" else "consent_revoked",
            )

        return await self._get_marketing_status(connection, channel, user_id)

    @staticmethod
    async def _get_marketing_status(
        connection: asyncpg.Connection,
        channel: str,
        user_id: str,
    ) -> MarketingConsentState:
        row = await connection.fetchrow(
            """
            SELECT consent.id, consent.active, consent.consent_version,
                   consent.proof_text_hash, consent.source,
                   proof.source_event_id, consent.suppressed_at,
                   consent.suppression_reason, consent.proof_event_id
            FROM marketing_consents AS consent
            LEFT JOIN marketing_consent_events AS proof
              ON proof.id = consent.proof_event_id
            WHERE consent.channel = $1 AND consent.user_id = $2
            """,
            channel,
            user_id,
        )
        if row is None:
            return MarketingConsentState(
                consent_id=None,
                active=False,
                consent_version=None,
                proof_text_hash=None,
                source=None,
                source_event_id=None,
                suppressed=False,
                suppression_reason=None,
            )
        proven = (
            row["proof_event_id"] is not None
            and row["proof_text_hash"] is not None
        )
        suppressed = row["suppressed_at"] is not None
        return MarketingConsentState(
            consent_id=row["id"],
            active=bool(row["active"] and proven and not suppressed),
            consent_version=row["consent_version"],
            proof_text_hash=row["proof_text_hash"],
            source=row["source"],
            source_event_id=row["source_event_id"],
            suppressed=suppressed,
            suppression_reason=row["suppression_reason"],
        )
