import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

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
