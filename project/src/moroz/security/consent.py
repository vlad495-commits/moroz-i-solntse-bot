from moroz.common.db import Database


PROCESSING_CONSENT_VERSION = "v1"
MARKETING_CONSENT_VERSION = "v1"


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
            await self._grant(
                connection, channel, user_id, consent_version
            )
            return
        async with self._database.acquire() as owned_connection:
            await self._grant(
                owned_connection, channel, user_id, consent_version
            )

    async def _grant(
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

    async def grant_marketing_consent(
        self, channel: str, user_id: str, consent_version: str, *, connection
    ) -> None:
        await connection.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active, granted_at)
            VALUES (gen_random_uuid(), $1, $2, $3, true, now())
            ON CONFLICT (channel, user_id) DO UPDATE SET
                consent_version = EXCLUDED.consent_version,
                active = true,
                granted_at = now(),
                revoked_at = NULL,
                updated_at = now()
            """,
            channel,
            user_id,
            consent_version,
        )
