from moroz.notifications.models import JobResult, SchedulerJob


REMINDER_KINDS = {
    "booking_created",
    "day_before",
    "morning",
    "hour_before",
    "morning_hour_before",
}
LIFECYCLE_KINDS = {"no_show_check", "visit_outcome_check"}


async def handle_scheduler_job(
    job: SchedulerJob,
    *,
    booking_port,
    outbox,
    lifecycle=None,
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

    if job.kind in LIFECYCLE_KINDS:
        if lifecycle is None:
            raise RuntimeError("lifecycle service is not configured")
        booking = await lifecycle.refresh(booking)
        if booking is None:
            return JobResult.skipped("stale")
        return await _handle_lifecycle(job, booking, outbox, lifecycle)
    if job.kind in REMINDER_KINDS:
        if booking.status != "confirmed":
            return JobResult.skipped("stale")
        await outbox.reminder(booking, job.kind)
        return JobResult.sent()
    return JobResult.skipped("unsupported_kind")


async def _handle_lifecycle(job, booking, outbox, lifecycle) -> JobResult:
    current_index = -1
    if job.kind == "visit_outcome_check":
        current_index = job.payload.get("outcome_check_index")
        if type(current_index) is not int or current_index not in range(3):
            return JobResult.skipped("invalid_outcome_payload")

    if booking.status == "no_show":
        await outbox.client_waiting(booking)
        await outbox.staff_no_show(booking)
        return JobResult.sent()
    if booking.status == "completed":
        await lifecycle.schedule_feedback(booking)
        return JobResult.sent()
    if booking.status == "unknown":
        await outbox.staff_status_unknown(booking, booking.status)
        return JobResult.skipped("unknown_status")
    if booking.status == "cancelled":
        return JobResult.skipped("stale")
    if booking.status != "confirmed":
        await outbox.staff_status_unknown(booking, booking.status)
        return JobResult.skipped("unknown_status")
    if await lifecycle.schedule_next(booking, current_index):
        return JobResult.skipped("outcome_pending")
    await outbox.staff_status_unknown(booking, "outcome_unresolved")
    return JobResult.skipped("outcome_unresolved")
