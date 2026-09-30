import contextlib
import csv
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

import vm_config


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vm_generate_tag_plan", ROOT / "03_vm_generate_tag_plan.py")
plan_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_module)


class ConfigAndPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.old_config_path = vm_config.CONFIG_PATH
        vm_config.CONFIG_PATH = self.work / "config.json"
        self.addCleanup(setattr, vm_config, "CONFIG_PATH", self.old_config_path)
        vm_config.CONFIG_PATH.write_text(
            json.dumps({"SUBSCRIPTION_ID": "test-subscription", "SKIP_VM": ["ManualVM"]}),
            encoding="utf-8",
        )

    def test_pending_entries_are_added_without_losing_manual_entries(self):
        self.assertEqual(vm_config.add_pending_vms(["pendingVM", "manualvm"]), (1, 2))
        self.assertEqual(vm_config.add_pending_vms(["PENDINGvm"]), (0, 2))
        self.assertEqual(vm_config.load_config()["SKIP_VM"], ["ManualVM", "pendingVM"])

    def test_survey_settings_are_loaded_from_local_config(self):
        config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
        config["SURVEY_FILE"] = "private-survey.xlsx"
        config["DEPARTMENT_SHEETS"] = ["Team A", "Team B"]
        vm_config.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")

        self.assertEqual(vm_config.load_survey_config()["SURVEY_FILE"], "private-survey.xlsx")
        self.assertEqual(vm_config.load_survey_config()["DEPARTMENT_SHEETS"], ["Team A", "Team B"])

        config["EXPECTED_VM_COUNT"] = True
        vm_config.CONFIG_PATH.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "EXPECTED_VM_COUNT"):
            vm_config.load_survey_config()

    def test_plan_keeps_skipped_vm_tags_and_processes_others_normally(self):
        headers = ["VM", "RG", "Overall_Risk", "Unrelated_Tags", "Active_VM (AO)"]
        for field in plan_module.FIELDS:
            headers += [
                f"AO_{field}", f"Azure_{field}_Key", f"Azure_{field}_Value",
                f"{field}_Case", f"{field}_Action",
            ]

        def row(vm_name, risk="Low"):
            data = dict.fromkeys(headers, "")
            data.update({"VM": vm_name, "RG": "rg-one", "Overall_Risk": risk})
            for field in plan_module.FIELDS:
                data[f"AO_{field}"] = f"new-{field}"
                data[f"Azure_{field}_Key"] = field.capitalize()
                data[f"Azure_{field}_Value"] = f"old-{field}"
                data[f"{field}_Case"] = "4"
            return data

        summary = self.work / "02_vm_summary.csv"
        with summary.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows([row("MANUALvm"), row("regular-vm"), row("gone-vm", plan_module.NOT_FOUND_RISK)])

        output_paths = {
            "INPUT_SUMMARY_CSV": summary,
            "OUTPUT_DIR": self.work,
            "OUTPUT_PLAN_JSON": self.work / "plan.json",
            "OUTPUT_HIGH_RISK_JSON": self.work / "risk.json",
            "OUTPUT_NOT_FOUND_JSON": self.work / "missing.json",
        }
        old_paths = {name: getattr(plan_module, name) for name in output_paths}
        for name, path in output_paths.items():
            setattr(plan_module, name, path)
            self.addCleanup(setattr, plan_module, name, old_paths[name])

        with contextlib.redirect_stdout(io.StringIO()):
            plan_module.main()
        plan = json.loads(output_paths["OUTPUT_PLAN_JSON"].read_text(encoding="utf-8"))
        self.assertEqual(len(plan), 2)
        skipped, regular = plan
        self.assertTrue(skipped["skip_vm"])
        self.assertEqual(skipped["tags_to_remove"], [])
        self.assertEqual(skipped["tags_to_apply"], {"flag": "Pending Owner Review"})
        self.assertEqual(skipped["subscription_id"], "test-subscription")
        self.assertIn("/subscriptions/test-subscription/", skipped["resource_id"])
        self.assertFalse(regular["skip_vm"])
        self.assertIn("Owner", regular["tags_to_remove"])
        self.assertEqual(regular["tags_to_apply"]["owner"], "new-owner")
        self.assertEqual(len(json.loads(output_paths["OUTPUT_NOT_FOUND_JSON"].read_text(encoding="utf-8"))), 1)


if __name__ == "__main__":
    unittest.main()
