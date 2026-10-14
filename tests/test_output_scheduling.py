import unittest

from erp_agent.application.contracts import dumps, loads
from erp_agent.application.model_scheduler import ModelCapacity, ScheduledLLMClient
from erp_agent.application.ports import LLMRequest, LLMResponse, ModelBinding, TaskContext
from tests.test_workbook_mapper import TaskControl


class OutputSchedulingTests(unittest.TestCase):
    def test_output_dispatch_selects_candidates_and_preserves_output_task_metadata(self):
        calls = []

        class Client:
            configured = True

            def __init__(self, name):
                self.name = name

            def generate(self, request):
                calls.append((self.name, request.schema_name))
                if request.schema_name == "erp_output_dispatch":
                    payload = loads(request.user)
                    return LLMResponse(
                        dumps(
                            {
                                "tasks": [
                                    {
                                        "task_id": task["task_id"],
                                        "difficulty": "simple",
                                        "assignments": [
                                            {
                                                "preset_id": "helper",
                                                "reason": "当前来源核对适合空闲候选。",
                                            }
                                        ],
                                        "summary": "结合任务内容、优先级和空闲额度分配。",
                                    }
                                    for task in payload["tasks"]
                                ]
                            }
                        ),
                        "main",
                    )
                return LLMResponse("{}", self.name)

        models = tuple(
            ModelBinding(
                name,
                name,
                1,
                {
                    "model": name,
                    "priority": priority,
                    "max_parallel": 1,
                    "max_input_chars": 100000,
                },
                Client(name),
            )
            for name, priority in [("main", 100), ("helper", 80)]
        )
        llm, control = ScheduledLLMClient(models, ModelCapacity(), strategy="agent"), TaskControl()
        request = LLMRequest(
            "填写来源核对",
            "{}",
            "erp_workbook_mapping",
            "{}",
            task=TaskContext("mapper:sheet", "Answerer", "填写来源核对", "预算", "mapper"),
        )
        llm.plan_output((request,), control, 2)
        self.assertEqual(llm.generate_controlled(request, control).model, "helper")
        self.assertEqual(
            calls, [("main", "erp_output_dispatch"), ("helper", "erp_workbook_mapping")]
        )
        self.assertTrue(any(meta.get("role") == "统一分配输出协作" for _, meta in control.tasks))
        self.assertFalse(any(meta.get("role") == "统一分配章节与整表" for _, meta in control.tasks))
        self.assertEqual(llm.capacity.used, {"main": 0, "helper": 0})
