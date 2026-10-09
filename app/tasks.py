import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, Integer, String, Text, func, or_, select, update
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    NEEDS_MANUAL = "NEEDS_MANUAL"
    CANCELED = "CANCELED"


class TaskDeferred(RuntimeError):
    def __init__(self, seconds: float = 5):
        self.seconds = seconds


class TaskNeedsManual(RuntimeError):
    """A human must complete the current browser challenge before resume."""


class TaskPartial(RuntimeError):
    def __init__(self, resource_id: str, message: str):
        super().__init__(message)
        self.resource_id = resource_id


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    available_at: Mapped[datetime | None] = mapped_column(DateTime)


def task_to_dict(task: Task) -> dict:
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "resource_id": task.resource_id,
        "status": task.status,
        "created_at": task.created_at.isoformat() + "Z",
        "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
        "finished_at": task.finished_at.isoformat() + "Z" if task.finished_at else None,
        "retry_count": task.retry_count,
        "error_message": task.error_message,
        "heartbeat_at": task.heartbeat_at.isoformat() + "Z" if task.heartbeat_at else None,
        "lease_until": task.lease_until.isoformat() + "Z" if task.lease_until else None,
        "available_at": task.available_at.isoformat() + "Z" if task.available_at else None,
    }


class TaskWorker:
    def __init__(self, session_factory, poll_seconds: float = 1.0, handlers=None, on_stop=None):
        self.session_factory = session_factory
        self.poll_seconds = poll_seconds
        self.handlers = handlers or {}
        self.on_stop = on_stop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _claim(self) -> str | None:
        now = utc_now()
        eligible = or_(
            Task.status == TaskStatus.PENDING,
            (Task.status == TaskStatus.RUNNING) & (Task.lease_until < now),
        )
        oldest = select(Task.task_id).where(eligible, Task.task_type.in_(self.handlers),
            or_(Task.available_at.is_(None), Task.available_at <= now)).order_by(Task.created_at).limit(1)
        with self.session_factory.begin() as session:
            task_id = session.execute(
                update(Task)
                .where(Task.task_id == oldest.scalar_subquery())
                .values(
                    status=TaskStatus.RUNNING,
                    started_at=func.coalesce(Task.started_at, now),
                    finished_at=None,
                    error_message=None,
                    heartbeat_at=now,
                    lease_until=now + timedelta(minutes=5),
                )
                .returning(Task.task_id)
            ).scalar_one_or_none()
        return task_id

    def run_once(self) -> bool:
        task_id = self._claim()
        if task_id is None:
            return False
        with self.session_factory() as session:
            task_type = session.get(Task, task_id).task_type
        if task_type in self.handlers:
            try:
                resource_id = self.handlers[task_type](task_id)
                status, message = TaskStatus.SUCCESS, None
            except TaskDeferred as exc:
                resource_id = None
                status, message = TaskStatus.PENDING, None
                deferred_seconds = exc.seconds
            except TaskNeedsManual as exc:
                resource_id = None
                status, message = TaskStatus.NEEDS_MANUAL, str(exc)
            except TaskPartial as exc:
                resource_id = exc.resource_id
                status, message = TaskStatus.PARTIAL, str(exc)
            except Exception as exc:
                log.exception("Task %s failed", task_id)
                resource_id = None
                status, message = TaskStatus.FAILED, str(exc)
            with self.session_factory.begin() as session:
                task = session.get(Task, task_id)
                if task.status == TaskStatus.CANCELED:
                    return True
                task.status = status
                task.error_message = message
                task.finished_at = None if status in {TaskStatus.NEEDS_MANUAL,TaskStatus.PENDING} else utc_now()
                task.heartbeat_at = utc_now()
                task.lease_until = None
                task.available_at = utc_now() + timedelta(seconds=deferred_seconds) if status == TaskStatus.PENDING else None
                if resource_id:
                    task.resource_id = resource_id
            return True
        return False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="task-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    if self.run_once():
                        continue
                except Exception:
                    log.exception("Worker loop error")
                self._stop.wait(self.poll_seconds)
        finally:
            if self.on_stop:
                self.on_stop()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=50)

    @property
    def stopping(self):
        return self._stop.is_set()

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
