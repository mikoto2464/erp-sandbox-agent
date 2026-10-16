import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from erp_agent.adapters.persistence import SQLiteRepository
from erp_agent.application.contracts import JSONObject, JSONValue, obj, to_json
from erp_agent.application.human_resolution import HumanInputRequired
from erp_agent.application.ports import RunControl
from erp_agent.application.task_budget import exclude_human_wait
from erp_agent.domain.errors import Cancelled, ERPError


def now() -> str:
    return datetime.now(UTC).isoformat()


class JobControl:
    def __init__(
        self,
        repository: SQLiteRepository,
        project_id: str,
        job_id: str,
        event: threading.Event,
        lock: threading.RLock,
    ) -> None:
        self.repository, self.project_id, self.job_id = repository, project_id, job_id
        self.event, self.lock = event, lock
        self.answers: dict[str, JSONObject] = {}

    def request_resolution(self, question: JSONObject) -> JSONObject:
        with self.lock:
            self.check_cancelled()
            answer = self.answers.get(str(question["id"]))
            if answer is not None:
                return answer
        raise HumanInputRequired(question)

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
            if changes.get("status") in {"queued", "running", "cached", "succeeded"}:
                task.pop("error", None)
            if changes.get("status") in {"queued", "running"}:
                task.pop("finished_at", None)
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
                terminal = next(
                    (
                        i
                        for i, t in enumerate(tasks)
                        if isinstance(t, dict)
                        and t.get("status") not in {"queued", "waiting", "running"}
                    ),
                    None,
                )
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
        self.paused: dict[str, tuple[JobControl, Callable[[RunControl], JSONValue], float]] = {}

    def submit(
        self,
        project_id: str,
        kind: str,
        operation: Callable[[RunControl], JSONValue],
        metadata: JSONObject | None = None,
    ) -> JSONObject:
        with self.lock:
            self.futures = {
                key: future for key, future in self.futures.items() if not future.done()
            }
            active = sum(not f.done() for f in self.futures.values())
            if active + len(self.paused) >= 10:
                raise ERPError("queue_full", "任务队列已满，请稍后重试。", 429)
            job_id = uuid4().hex
            record = {
                "id": job_id,
                "project_id": project_id,
                "kind": kind,
                "status": "queued",
                "stage": "queued",
                "completed": 0,
                "created_at": now(),
                "updated_at": now(),
                "metadata": metadata or {},
                "result": None,
                "error": None,
            }
            self.repository.put("job", project_id, job_id, record)
            event = threading.Event()
            self.events[job_id] = event
            control = JobControl(self.repository, project_id, job_id, event, self.lock)
            self.futures[job_id] = self.executor.submit(self._run, control, operation)
        return obj(to_json(record))

    def _run(self, control: JobControl, operation: Callable[[RunControl], JSONValue]) -> None:
        paused = False
        try:
            control.checkpoint("starting")
            result = operation(control)
            with self.lock:
                record = self.repository.get("job", control.project_id, control.job_id)
                needs_input = isinstance(result, dict) and result.get("needs_input") is True
                record.update(
                    status="needs_input" if needs_input else "succeeded",
                    stage="done",
                    result=result,
                    updated_at=now(),
                )
                self.repository.put("job", control.project_id, control.job_id, record)
        except Exception as exc:
            with self.lock:
                try:
                    record = self.repository.get("job", control.project_id, control.job_id)
                except ERPError as missing:
                    if missing.code == "not_found":
                        return
                    raise
                if isinstance(exc, HumanInputRequired) and not control.event.is_set():
                    paused = True
                    record.update(
                        status="needs_input",
                        stage="awaiting_resolution",
                        error=None,
                        intervention=exc.question,
                        updated_at=now(),
                    )
                    self.paused[control.job_id] = (control, operation, time.monotonic())
                    for task in record.get("tasks", []):
                        if isinstance(task, dict) and task.get("id") == exc.question["task_id"]:
                            task.update(status="needs_input", error=exc.question["message"])
                elif isinstance(exc, Cancelled) or control.event.is_set():
                    record.update(status="cancelled", updated_at=now())
                elif isinstance(exc, ERPError):
                    record.update(
                        status="failed",
                        error={"code": exc.code, "message": exc.message},
                        updated_at=now(),
                    )
                else:
                    logging.exception(
                        "后台任务失败 project=%s job=%s", control.project_id, control.job_id
                    )
                    record.update(
                        status="failed",
                        error={
                            "code": "internal_error",
                            "message": "后台任务失败，请检查服务端日志。",
                        },
                        updated_at=now(),
                    )
                metadata = obj(record.get("metadata", {}))
                stage_kind = (
                    "budget_result"
                    if record.get("kind") == "budget"
                    else "report_result"
                    if record.get("kind") == "artifact" and metadata.get("kind") == "docx"
                    else None
                )
                if stage_kind and metadata.get("solution_id"):
                    try:
                        stage = self.repository.get(
                            stage_kind, control.project_id, str(metadata["solution_id"])
                        )
                        if stage.get("attempt_id") == metadata.get("attempt_id"):
                            stage.update(
                                status=record["status"], error=record.get("error"), updated_at=now()
                            )
                            self.repository.put(
                                stage_kind, control.project_id, str(metadata["solution_id"]), stage
                            )
                    except ERPError as missing:
                        if missing.code not in {"not_found", "project_deleted", "upload_deleted"}:
                            raise
                tasks = record.get("tasks", [])
                assert isinstance(tasks, list)
                for task in tasks:
                    if isinstance(task, dict) and task.get("status") in {
                        "queued",
                        "waiting",
                        "running",
                    }:
                        task.update(
                            status="needs_input"
                            if paused
                            else "cancelled"
                            if record["status"] == "cancelled"
                            else "failed",
                            finished_at=now(),
                        )
                self.repository.put("job", control.project_id, control.job_id, record)
        finally:
            with self.lock:
                if not paused:
                    self.events.pop(control.job_id, None)

    def answer(self, project_id: str, job_id: str, data: JSONObject) -> JSONObject:
        with self.lock:
            record = self.repository.get("job", project_id, job_id)
            question = obj(record.get("intervention", {}))
            if record["status"] != "needs_input" or not question or job_id not in self.paused:
                raise ERPError(
                    "resolution_stale", "此任务当前没有可继续的人工问题，请刷新任务。", 409
                )
            if data.get("question_id") != question["id"]:
                raise ERPError("resolution_stale", "问题已更新，请查看最新问题后作答。", 409)
            option, text = data.get("option"), data.get("text", "")
            if not isinstance(option, str) or option not in {
                item["id"] for item in question["options"]
            }:
                raise ERPError("resolution_option", "请选择当前问题提供的处理方式。")
            if not isinstance(text, str) or len(text) > 2000:
                raise ERPError("resolution_text", "修正说明必须是2000字以内的文字。")
            if option in {"cancel", "restart"}:
                return self.cancel(project_id, job_id)
            if sum(not f.done() for f in self.futures.values()) >= 10:
                raise ERPError("queue_full", "任务队列已满，请稍后提交，当前问题仍保留。", 429)
            control, operation, started = self.paused.pop(job_id)
            control.answers[str(question["id"])] = {"option": option, "text": text.strip()}
            exclude_human_wait(control, time.monotonic() - started)
            history = record.setdefault("resolution_history", [])
            history.append(
                {
                    "question_id": question["id"],
                    "task_id": question["task_id"],
                    "option": option,
                    "text": text.strip(),
                    "answered_at": now(),
                }
            )
            record.pop("intervention", None)
            record.update(status="queued", stage="resuming", updated_at=now())
            self.repository.put("job", project_id, job_id, record)
            self.futures[job_id] = self.executor.submit(self._run, control, operation)
            return record

    def cancel(self, project_id: str, job_id: str) -> JSONObject:
        with self.lock:
            record = self.repository.get("job", project_id, job_id)
            if job_id in self.paused:
                self.paused.pop(job_id)
                self.events.pop(job_id).set()
                record.pop("intervention", None)
                for task in record.get("tasks", []):
                    if isinstance(task, dict) and task.get("status") == "needs_input":
                        task.update(status="cancelled", finished_at=now())
                record.update(status="cancelled", stage="cancelled", updated_at=now())
                self.repository.put("job", project_id, job_id, record)
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
        self.paused.clear()

    def cancel_project(self, project_id: str) -> None:
        with self.lock:
            for value in self.repository.list("job", project_id):
                record = obj(value)
                event = self.events.get(str(record["id"]))
                if event is not None:
                    event.set()
                if str(record["id"]) in self.paused:
                    self.cancel(project_id, str(record["id"]))
