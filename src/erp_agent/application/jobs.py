from erp_agent.application.contracts import JSONObject, string
from erp_agent.application.ports import JobRunner, Repository
from erp_agent.application.template_reader import LIBRARY_SCOPE
from erp_agent.domain.errors import ERPError


class ApplicationJobs:
    def __init__(self, repository: Repository, runner: JobRunner) -> None:
        self.repository, self.runner = repository, runner

    def list(self) -> list[JSONObject]:
        projects = {string(p["id"]): string(p["name"]) for p in self.repository.list_all("project")}
        result = []
        for job in self.repository.list_all("job"):
            scope = string(job["project_id"])
            if scope != LIBRARY_SCOPE and scope not in projects:
                continue
            result.append(
                {**job, "scope_name": "空表库" if scope == LIBRARY_SCOPE else projects[scope]}
            )
        return sorted(result, key=lambda job: string(job.get("created_at", "")))

    def get(self, job_id: str) -> JSONObject:
        job = next((j for j in self.list() if j["id"] == job_id), None)
        if job is None:
            raise ERPError("not_found", "任务不存在或所属项目已删除。", 404)
        return job

    def cancel(self, job_id: str) -> JSONObject:
        job = self.get(job_id)
        return self.runner.cancel(string(job["project_id"]), job_id)
