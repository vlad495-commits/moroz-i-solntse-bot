from moroz.common.db import Database


PROCESSING_CONSENT_VERSION = "v1"


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
    ) -> None:
        async with self._database.acquire() as connection:
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
