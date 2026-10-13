import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import JSONObject, JSONValue, obj, to_json
from erp_agent.application.ports import RunControl
from erp_agent.domain.errors import Cancelled, ERPError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobControl:
    def __init__(self, repository: SQLiteRepository, project_id: str, job_id: str,
                 event: threading.Event, lock: threading.RLock) -> None:
        self.repository, self.project_id, self.job_id = repository, project_id, job_id
        self.event, self.lock = event, lock

    def checkpoint(self, stage: str, completed: int = 0) -> None:
        with self.lock:
            if self.event.is_set():
                raise Cancelled()
            record = self.repository.get("job", self.project_id, self.job_id)
            record.update(status="running", stage=stage, completed=completed, updated_at=now())
            self.repository.put("job", self.project_id, self.job_id, record)

    def check_cancelled(self) -> None:
        if self.event.is_set():
            raise Cancelled()

    def report_task(self, task_id: str, changes: JSONObject) -> None:
        with self.lock:
            self.check_cancelled()
            record = self.repository.get("job", self.project_id, self.job_id)
            tasks = record.setdefault("tasks", [])
            assert isinstance(tasks, list)
            task = next((t for t in tasks if isinstance(t, dict) and t["id"] == task_id), None)
            if task is None:
                task = {"id": task_id, "created_at": now(), "attempt": 0}
                tasks.append(task)
            previous = task.get("status")
            if changes.get("status") == "waiting":
                task["attempt"] = int(task.get("attempt", 0)) + 1
                task.pop("error", None)
                task.pop("finished_at", None)
            task.update(changes, updated_at=now())
            if changes.get("status") == "running" and previous != "running":
                task["started_at"] = now()
            if changes.get("status") in {"succeeded", "failed", "cached", "split", "needs_input"}:
                task["finished_at"] = now()
            # 保留在途项，限制历史明细数量，完整处理结果仍在题目快照中。
            if len(tasks) > 1000:
                terminal = next((i for i, t in enumerate(tasks) if isinstance(t, dict)
                                and t.get("status") not in {"queued", "waiting", "running"}), None)
                if terminal is not None:
                    tasks.pop(terminal)
                    record["archived_tasks"] = int(record.get("archived_tasks", 0)) + 1
            record["updated_at"] = now()
            self.repository.put("job", self.project_id, self.job_id, record)


class ThreadJobRunner:
    def __init__(self, repository: SQLiteRepository, workers: int = 2) -> None:
        self.repository = repository
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="erp-job")
        self.events: dict[str, threading.Event] = {}
        self.futures: dict[str, Future[None]] = {}
        self.lock = threading.RLock()

    def submit(self, project_id: str, kind: str, operation: Callable[[RunControl], JSONValue],
               metadata: JSONObject | None = None) -> JSONObject:
        with self.lock:
            self.futures = {
                key: future for key, future in self.futures.items() if not future.done()
            }
            active = sum(not f.done() for f in self.futures.values())
            if active >= 10:
                raise ERPError("queue_full", "任务队列已满，请稍后重试。", 429)
            job_id = uuid4().hex
            record = {"id": job_id, "project_id": project_id, "kind": kind,
                      "status": "queued", "stage": "queued", "completed": 0,
                      "created_at": now(), "updated_at": now(), "metadata": metadata or {},
                      "result": None, "error": None}
            self.repository.put("job", project_id, job_id, record)
            event = threading.Event()
            self.events[job_id] = event
            control = JobControl(self.repository, project_id, job_id, event, self.lock)
            self.futures[job_id] = self.executor.submit(self._run, control, operation)
        return obj(to_json(record))

    def _run(self, control: JobControl, operation: Callable[[RunControl], JSONValue]) -> None:
        try:
            control.checkpoint("starting")
            result = operation(control)
            with self.lock:
                record = self.repository.get("job", control.project_id, control.job_id)
                needs_input = isinstance(result, dict) and result.get("needs_input") is True
                record.update(status="needs_input" if needs_input else "succeeded", stage="done",
                              result=result, updated_at=now())
                self.repository.put("job", control.project_id, control.job_id, record)
        except Exception as exc:
            with self.lock:
                try:
                    record = self.repository.get("job", control.project_id, control.job_id)
                except ERPError as missing:
                    if missing.code == "not_found":
                        return
                    raise
                if isinstance(exc, Cancelled):
                    record.update(status="cancelled", updated_at=now())
                elif isinstance(exc, ERPError):
                    record.update(status="failed", error={"code": exc.code, "message": exc.message},
                                  updated_at=now())
                else:
                    logging.exception("后台任务失败 project=%s job=%s",
                                      control.project_id, control.job_id)
                    record.update(status="failed", error={"code": "internal_error",
                                                          "message": "后台任务失败，请检查服务端日志。"},
                                  updated_at=now())
                tasks = record.get("tasks", [])
                assert isinstance(tasks, list)
                for task in tasks:
                    if isinstance(task, dict) and task.get("status") in {"queued", "waiting", "running"}:
                        task.update(status="cancelled" if isinstance(exc, Cancelled) else "failed",
                                    finished_at=now())
                self.repository.put("job", control.project_id, control.job_id, record)
        finally:
            with self.lock:
                self.events.pop(control.job_id, None)

    def cancel(self, project_id: str, job_id: str) -> JSONObject:
        with self.lock:
            record = self.repository.get("job", project_id, job_id)
            if record["status"] in {"queued", "running"}:
                event = self.events.get(job_id)
                if event is not None:
                    event.set()
                record["stage"] = "cancelling"
                self.repository.put("job", project_id, job_id, record)
            return record

    def close(self) -> None:
        with self.lock:
            for event in self.events.values():
                event.set()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def cancel_project(self, project_id: str) -> None:
        with self.lock:
            for value in self.repository.list("job", project_id):
                record = obj(value)
                event = self.events.get(str(record["id"]))
                if event is not None:
                    event.set()
