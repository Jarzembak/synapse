from contextvars import ContextVar

current_job_id: ContextVar[int | None] = ContextVar("current_job_id", default=None)
