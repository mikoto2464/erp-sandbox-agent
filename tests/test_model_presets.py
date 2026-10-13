import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from erp_agent.adapters.model_settings import ModelSettingsService
from erp_agent.application.contracts import dumps, loads
from erp_agent.config import Settings
from erp_agent.domain.errors import ERPError
from tests.helpers import FakeLLM
from erp_agent.application.ports import LLMResponse


class ModelPresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="erp-presets-")
        self.settings = Settings(data_dir=Path(self.temp.name), llm_model="primary",
                                 llm_api_key="primary-secret")
        self.service = ModelSettingsService(self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, name="备用模型"):
        return self.service.create_preset({"name": name, "provider": "anthropic",
                                          "model": "backup", "api_key": "backup-secret"})

    def test_create_does_not_switch_and_keys_stay_independent(self):
        preset = self.create()
        self.assertEqual(self.service.public()["id"], "default")
        self.assertEqual(self.service.settings.llm_api_key, "primary-secret")
        self.service.activate(preset["id"], {"expected_revision": 1})
        self.assertEqual(self.service.settings.llm_api_key, "backup-secret")
        self.service.activate("default", {"expected_revision": 1})
        self.assertEqual(self.service.settings.llm_api_key, "primary-secret")
        public = dumps(self.service.catalog())
        self.assertNotIn("primary-secret", public)
        self.assertNotIn("backup-secret", public)

    def test_edit_inactive_preset_rename_and_optimistic_conflict(self):
        preset = self.create()
        changed = self.service.update_preset(preset["id"], {
            "expected_revision": 1, "name": "灾备", "model": "new-backup", "api_key": "",
        })
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(self.service.settings.llm_model, "primary")
        with self.assertRaises(ERPError) as conflict:
            self.service.update_preset(preset["id"], {"expected_revision": 1, "name": "旧修改"})
        self.assertEqual(conflict.exception.code, "preset_conflict")
        with self.assertRaises(ERPError):
            self.service.activate(preset["id"], {"expected_revision": 1})
        self.service.activate(preset["id"], {"expected_revision": 2})
        self.assertEqual(self.service.settings.llm_api_key, "backup-secret")

    def test_capture_is_stable_after_edit_switch_or_delete(self):
        fake = FakeLLM("result")
        service = ModelSettingsService(self.settings, fake)
        binding = service.capture()
        preset = service.create_preset({"name": "本地", "provider": "ollama", "model": "local"})
        service.activate(preset["id"], {"expected_revision": 1})
        service.delete_preset("default", 1)
        self.assertIs(binding.client, fake)
        self.assertEqual(binding.metadata()["preset_id"], "default")
        self.assertEqual(binding.metadata()["model"], "primary")
        self.assertNotIn("primary-secret", dumps(binding.metadata()))
        self.assertNotIn("primary-secret", repr(binding))

    def test_delete_active_and_activate_incomplete_are_rejected(self):
        incomplete = self.service.create_preset({"name": "未配置"})
        with self.assertRaises(ERPError) as active:
            self.service.delete_preset("default", 1)
        self.assertEqual(active.exception.code, "preset_in_use")
        with self.assertRaises(ERPError) as invalid:
            self.service.activate(incomplete["id"], {"expected_revision": 1})
        self.assertEqual(invalid.exception.code, "preset_not_configured")
        self.service.delete_preset(incomplete["id"], 1)
        self.assertEqual(len(self.service.catalog()["presets"]), 1)

    def test_reload_retains_active_and_all_presets_and_file_permissions(self):
        preset = self.create()
        self.service.activate(preset["id"], {"expected_revision": 1})
        reloaded = ModelSettingsService(self.settings)
        self.assertEqual(reloaded.public()["id"], preset["id"])
        self.assertEqual(len(reloaded.catalog()["presets"]), 2)
        self.assertEqual(reloaded.settings.llm_api_key, "backup-secret")
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)

    def test_atomic_save_failure_leaves_memory_and_disk_unchanged(self):
        self.create()
        before = self.service.path.read_bytes()
        current = self.service.client.current()
        with patch("erp_agent.adapters.model_settings.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.update_preset("default", {"expected_revision": 1, "model": "changed"})
        self.assertEqual(self.service.path.read_bytes(), before)
        self.assertEqual(self.service.public()["revision"], 1)
        self.assertIs(self.service.client.current(), current)
        self.assertEqual([p for p in self.service.path.parent.glob("model-*")
                          if p != self.service.path], [])

    def test_legacy_configuration_migrates_and_empty_key_does_not_restore_env_secret(self):
        self.service.path.write_text(dumps({"llm_model": "legacy", "llm_api_key": ""}))
        migrated = ModelSettingsService(self.settings)
        self.assertEqual(migrated.public()["name"], "默认配置")
        self.assertFalse(migrated.public()["api_key_set"])
        self.assertEqual(loads(migrated.path.read_text())["schema_version"], 3)
        self.assertFalse(ModelSettingsService(self.settings).public()["api_key_set"])

    def test_names_and_new_preset_do_not_inherit_env_key(self):
        preset = self.service.create_preset({"name": "draft", "model": "model"})
        self.assertFalse(preset["api_key_set"])
        for name in ["", "  ", "默认配置", "DRAFT"]:
            with self.subTest(name=name), self.assertRaises(ERPError):
                self.service.create_preset({"name": name})

    def test_connection_test_uses_saved_protocol_without_changing_preset(self):
        calls=[]
        def generate(client, request):
            calls.append((client.settings, request))
            return LLMResponse('{"status":"ok"}', "returned-model")
        before=self.service.public()
        with patch("erp_agent.adapters.model_settings.LangChainLLMClient.generate",generate):
            result=self.service.test_preset("default",{"expected_revision":1})
        self.assertTrue(result["succeeded"])
        self.assertEqual(result["model"],"returned-model")
        self.assertEqual(self.service.public(),before)
        self.assertEqual(calls[0][0].llm_retries,0)
        self.assertEqual(calls[0][0].llm_max_output_tokens,12000)
        self.assertEqual(calls[0][0].llm_api_key,"primary-secret")
        self.assertNotIn("primary-secret",dumps(result))
        with self.assertRaises(ERPError):
            self.service.test_preset("default",{"expected_revision":2})

    def test_connection_test_checks_json_contract(self):
        with patch("erp_agent.adapters.model_settings.LangChainLLMClient.generate",
                   return_value=LLMResponse('{"status":"wrong"}',"model")):
            with self.assertRaises(ERPError) as caught:
                self.service.test_preset("default",{"expected_revision":1})
        self.assertEqual(caught.exception.code,"llm_test_contract")
