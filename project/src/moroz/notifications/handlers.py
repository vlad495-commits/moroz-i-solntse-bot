from moroz.notifications.models import JobResult, SchedulerJob


REMINDER_KINDS = {
    "booking_created",
    "day_before",
    "morning",
    "hour_before",
    "morning_hour_before",
}


async def handle_scheduler_job(
    job: SchedulerJob,
    *,
    booking_port,
    outbox,
) -> JobResult:
    if job.kind == "feedback_request":
        customer_id = job.payload.get("customer_id")
        if not isinstance(customer_id, str) or job.booking_key is None:
            return JobResult.skipped("invalid_feedback_payload")
        await outbox.feedback_request(customer_id, job.booking_key)
        return JobResult.sent()

    booking = await booking_port.get_booking(job.booking_key)
    if booking is None or booking.starts_at != job.booking_starts_at:
        return JobResult.skipped("stale")
    if booking.status == "cancelled":
        return JobResult.skipped("stale")

    if job.kind == "no_show_check":
        return await _handle_no_show_check(booking, outbox)
    if job.kind in REMINDER_KINDS:
        await outbox.reminder(booking, job.kind)
        return JobResult.sent()
    return JobResult.skipped("unsupported_kind")


async def _handle_no_show_check(booking, outbox) -> JobResult:
    if booking.status == "no_show":
        await outbox.client_waiting(booking)
        await outbox.staff_no_show(booking)
        return JobResult.sent()
    if booking.status != "confirmed":
        await outbox.staff_status_unknown(booking, booking.status)
        return JobResult.skipped("unknown_status")
    return JobResult.skipped("not_no_show")
