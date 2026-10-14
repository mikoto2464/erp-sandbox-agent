import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from erp_agent.application.contracts import dumps
from erp_agent.application.llm_recovery import recovery_call
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMRequest, LLMResponse, ModelBinding, TaskContext
from erp_agent.application.task_budget import TaskCallBudget, task_budget
from erp_agent.application.task_reporting import TaskLLMClient
from erp_agent.domain.errors import Cancelled, ERPError
from tests.helpers import Control


def binding(name, priority=100, endpoint=None, parallel=1, client=None):
    class Client:
        configured = True

        def generate(self, request):
            return LLMResponse("{}", name)

    config = {
        "model": name,
        "priority": priority,
        "max_parallel": parallel,
        "max_input_chars": 100000,
    }
    if endpoint:
        config.update(provider="openai_chat", base_url=endpoint)
    return ModelBinding(name, name, 1, config, client or Client())


def request(identifier="a", parent="", role="产销规划"):
    return LLMRequest(
        "任务", "{}", "test", "{}", task=TaskContext(identifier, "Solver", role, "对象", parent)
    )


class DirectSchedulingTests(unittest.TestCase):
    def tiered_pool(self, capacity=None, **options):
        models = tuple(
            replace(
                binding(name, priority),
                configuration={
                    **binding(name, priority).configuration,
                    "task_tier": tier,
                },
            )
            for name, priority, tier in (
                ("strong", 900, "strong"),
                ("general", 500, "general"),
                ("light", 10, "light"),
            )
        )
        return ScheduledLLMClient(models, capacity or ModelCapacity(), **options)

    def test_simple_reader_budget_report_prefer_light_over_priority_without_dispatch(self):
        pool = self.tiered_pool()
        with patch.object(pool.dispatcher, "decide", side_effect=AssertionError("no dispatch")):
            for agent, role in (
                ("Reader", "整表整理"),
                ("Solver", "预算来源映射"),
                ("Mapper", "来源核对"),
                ("Answerer", "执行风险报告"),
            ):
                req = replace(request(agent), task=TaskContext(agent, agent, role, "整表"))
                self.assertEqual(pool.generate(req).model, "light")

    def test_complex_planning_never_uses_light_even_when_strong_busy(self):
        pool = self.tiered_pool()
        for role in ("产销规划", "资金核对", "全周期统筹"):
            self.assertEqual(pool.generate(request(role, role=role)).model, "strong")
        pool.capacity.used["strong"] = 1
        pool.capacity.endpoint_used[pool.capacity.endpoint(pool.models[0])] = 1
        self.assertEqual(pool.generate(request("busy")).model, "general")
        only_light = ScheduledLLMClient((pool.models[-1],), ModelCapacity())
        with self.assertRaises(ERPError):
            only_light.generate(request())

    def test_large_conflicted_review_and_dependency_tasks_upgrade(self):
        pool = self.tiered_pool(simple_max_chars=100, moderate_max_chars=200)
        base = replace(request("base"), task=TaskContext("base", "Reader", "整表整理", "表"))
        for identifier, user, complexity, expected in (
            ("ordinary", "{}", "auto", "light"),
            ("large", "x" * 110, "auto", "general"),
            ("huge", "x" * 210, "auto", "strong"),
            ("conflict", '{"facts":[{"state":"conflicted"}]}', "auto", "strong"),
            ("dependency", "{}", "moderate", "general"),
        ):
            req = replace(
                base, user=user, task=replace(base.task, id=identifier), task_complexity=complexity
            )
            self.assertEqual(pool.generate(req).model, expected)
        review = replace(base, task=replace(base.task, id="review", role="关联复核"))
        self.assertEqual(pool.generate(review).model, "general")

    def test_light_input_capacity_and_shared_quota_upgrade_with_diagnostics(self):
        pool = self.tiered_pool()
        req = replace(request(), task=TaskContext("table", "Reader", "整理", "表"))
        small = replace(
            pool.models[-1],
            configuration={
                **pool.models[-1].configuration,
                "max_input_chars": 1,
            },
        )
        limited = ScheduledLLMClient((*pool.models[:-1], small), ModelCapacity())
        self.assertEqual(limited.generate(req).model, "general")
        _, checks = limited._candidate_review(req)
        self.assertIn("字符容量", checks[-1]["reason"])
        pool.capacity.used["light"] = 1
        pool.capacity.endpoint_used[pool.capacity.endpoint(pool.models[-1])] = 1
        self.assertEqual(pool.generate(req).model, "general")

    def test_light_repair_remains_sticky_when_repair_input_changes_classification(self):
        pool = self.tiered_pool(simple_max_chars=100, moderate_max_chars=200)
        req = replace(request(), task=TaskContext("table", "Reader", "整理", "表"))
        self.assertEqual(pool.generate(req).model, "light")
        repair = replace(req, user=dumps({"errors": ["x" * 250], "previous_output": "{}"}))
        self.assertEqual(pool.generate(repair).model, "light")

    def test_frontend_role_scope_intersects_environment_candidates(self):
        pool = self.tiered_pool(role_candidates={"reader": ("light", "strong")})
        models = tuple(
            replace(m, configuration={**m.configuration, "task_roles": ["budget"]})
            if m.preset_id == "light"
            else m
            for m in pool.models
        )
        scoped = ScheduledLLMClient(models, ModelCapacity(), role_candidates=pool.role_candidates)
        req = replace(request(), task=TaskContext("table", "Reader", "整理", "表"))
        self.assertEqual(scoped.generate(req).model, "strong")

    def test_complex_tier_precedes_higher_priority_general_and_light(self):
        pool = self.tiered_pool()
        models = tuple(
            replace(
                m,
                configuration={
                    **m.configuration,
                    "priority": 1 if m.preset_id == "strong" else 1000,
                },
            )
            for m in pool.models
        )
        self.assertEqual(
            ScheduledLLMClient(models, ModelCapacity()).generate(request()).model, "strong"
        )

    def test_shared_endpoint_exclusion_is_reported_before_and_at_selection(self):
        class Reporting(Control):
            def __init__(self):
                super().__init__()
                self.updates = []

            def report_task(self, identifier, metadata):
                self.updates.append(metadata)

        pool = self.tiered_pool()
        models = tuple(
            replace(
                m,
                configuration={
                    **m.configuration,
                    "provider": "openai_chat",
                    "base_url": "https://shared.test/v1",
                },
            )
            if m.preset_id != "general"
            else m
            for m in pool.models
        )
        pool = ScheduledLLMClient(models, ModelCapacity())
        # 模拟同端点已有一个真实在途请求，轻量别名也必须受共享额度限制。
        pool.capacity.used["strong"] = 1
        pool.capacity.endpoint_used[pool.capacity.endpoint(pool.models[0])] = 1
        req = replace(request(), task=TaskContext("table", "Reader", "整理", "表"))
        control = Reporting()
        self.assertEqual(pool.generate_controlled(req, control).model, "general")
        waiting = control.updates[-2]
        self.assertIn("共享端点", waiting["candidate_checks"][-1]["reason"])
        selected = control.updates[-1]
        self.assertEqual(waiting["difficulty_source"], "local")
        self.assertIn("额度占满", selected["assignment_reason"])
        checks = {check["id"]: check for check in selected["candidate_checks"]}
        self.assertIn("共享端点", checks["light"]["reason"])
        only_light = ScheduledLLMClient((models[-1],), ModelCapacity())
        with self.assertRaises(ERPError) as error:
            only_light.generate(request("unsupported"))
        self.assertEqual(error.exception.code, "llm_task_suitability")

    def test_default_has_no_dispatch_and_role_override_repair_sticks(self):
        client = ScheduledLLMClient(
            (binding("main"), binding("other", 90)),
            ModelCapacity(),
            role_candidates={"operations": ("other",)},
        )
        control = Control()
        with patch.object(client.dispatcher, "decide", side_effect=AssertionError("no dispatch")):
            client.plan_solver((request(),), control, 2)
            self.assertEqual(client.generate_controlled(request(), control).model, "other")
            repaired = replace(request(), user='{"errors":["引用无效"]}')
            self.assertEqual(client.generate_controlled(repaired, control).model, "other")
        self.assertEqual(task_budget(control, request().task).calls, 2)

    def test_shared_endpoint_presets_do_not_bypass_limit_or_cancel_release(self):
        entered, finish = threading.Event(), threading.Event()

        class Blocking:
            configured = True

            def generate(self, request):
                entered.set()
                self.assert_wait = finish.wait(3)
                return LLMResponse("{}", "main")

        class CancelControl(Control):
            cancelled = False

            def check_cancelled(self):
                if self.cancelled:
                    raise Cancelled()

        capacity = ModelCapacity()
        models = (
            binding("main", endpoint="https://x.test/v1", client=Blocking()),
            binding("alias", 90, endpoint="https://x.test/v1/chat/completions"),
        )
        pool = ScheduledLLMClient(models, capacity)
        control = CancelControl()
        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(pool.generate_controlled, request(), control)
            self.assertTrue(entered.wait(2))
            control.cancelled = True
            self.assertEqual(capacity.catalog(models)[1]["available"], 0)
            self.assertEqual(capacity.catalog(models)[1]["endpoint_in_use"], 1)
            waiting = executor.submit(pool.generate_controlled, request("b"), control)
            with self.assertRaises(Cancelled):
                waiting.result(timeout=2)
            self.assertEqual(capacity.used["main"], 1)
            finish.set()
            with self.assertRaises(Cancelled):
                running.result(timeout=2)
        self.assertEqual(sum(capacity.used.values()), 0)

    def test_retry_then_switch_one_candidate_without_duplicate_reservations(self):
        calls = []

        class Failing:
            configured = True

            def generate(self, request):
                calls.append("main")
                raise ERPError("llm_timeout", "timeout", 504)

        pool = ScheduledLLMClient(
            (binding("main", client=Failing()), binding("other", 90)), ModelCapacity()
        )
        control = Control()
        wrapped = TaskLLMClient(pool, control, request().task)
        with patch(
            "erp_agent.application.llm_recovery.wait_random_exponential",
            return_value=lambda state: 0,
        ):
            response = recovery_call(wrapped, request(), control)
        self.assertEqual(response.model, "other")
        self.assertEqual(calls, ["main", "main"])
        self.assertEqual(task_budget(control, request().task).calls, 3)

    def test_splits_repairs_share_call_boundary_and_deadline(self):
        pool = ScheduledLLMClient((binding("main"),), ModelCapacity(), task_max_calls=2)
        control = Control()
        pool.generate_controlled(request("root"), control)
        pool.generate_controlled(request("child", "root"), control)
        with self.assertRaisesRegex(ERPError, "调用上限"):
            pool.generate_controlled(request("grandchild", "child"), control)
        self.assertEqual(sum(pool.capacity.used.values()), 0)
        budget = TaskCallBudget(8, 10)
        budget.deadline = 0
        with self.assertRaisesRegex(ERPError, "截止时间"):
            budget.reserve()

    def test_token_capacity_filters_even_when_chars_fit(self):
        class Limited:
            configured = True

            def check_request_capacity(self, request):
                raise ERPError("llm_input_limit", "tokens too large")

            def generate(self, request):
                raise AssertionError("must not be selected")

        pool = ScheduledLLMClient(
            (binding("main", client=Limited()), binding("other", 90)), ModelCapacity()
        )
        self.assertEqual(pool.generate(request()).model, "other")

    def test_persistent_failure_does_not_cycle_back_or_try_third_model(self):
        calls = []

        class Failing:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                calls.append(self.name)
                raise ERPError("llm_timeout", "timeout", 504)

        pool = ScheduledLLMClient(
            tuple(
                binding(name, priority, client=Failing(name))
                for name, priority in (("a", 100), ("b", 90), ("c", 80))
            ),
            ModelCapacity(),
        )
        control = Control()
        with patch(
            "erp_agent.application.llm_recovery.wait_random_exponential",
            return_value=lambda state: 0,
        ):
            with self.assertRaises(ERPError):
                recovery_call(TaskLLMClient(pool, control, request().task), request(), control)
        self.assertEqual(calls, ["a", "a", "b", "b"])
