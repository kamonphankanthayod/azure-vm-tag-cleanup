"""Load the local, unpublished VM tagging configuration."""

import json
from pathlib import Path


CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def load_config(require_subscription=False):
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            config = json.load(f)
    else:
        config = {"SUBSCRIPTION_ID": "", "SKIP_VM": []}

    if not isinstance(config, dict):
        raise ValueError(f"{CONFIG_PATH}: expected a JSON object")
    subscription_id = config.get("SUBSCRIPTION_ID", "")
    skip_vm = config.get("SKIP_VM", [])
    if not isinstance(subscription_id, str):
        raise ValueError(f"{CONFIG_PATH}: SUBSCRIPTION_ID must be a string")
    if not isinstance(skip_vm, list) or any(
        not isinstance(vm, str) or not vm.strip() for vm in skip_vm
    ):
        raise ValueError(f"{CONFIG_PATH}: SKIP_VM must be a list of non-empty VM names")
    if require_subscription and (not subscription_id.strip() or subscription_id.startswith("<")):
        raise ValueError(
            f"Set SUBSCRIPTION_ID in {CONFIG_PATH} before running this script "
            "(see config.example.json)"
        )
    return config


def load_survey_config():
    """Validate the local workbook settings used by the survey and gap checks."""
    config = load_config()
    survey_file = config.get("SURVEY_FILE")
    if not isinstance(survey_file, str) or not survey_file.strip():
        raise ValueError(f"Set SURVEY_FILE in {CONFIG_PATH} (see config.example.json)")

    for key in (
        "DEPARTMENT_SHEETS",
        "OWNER_DOMAIN_WHITELIST",
        "DEPARTMENT_MASTER_LIST",
        "ENVIRONMENT_MASTER_LIST",
    ):
        values = config.get(key)
        if not isinstance(values, list) or not values or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(f"Set {key} to a non-empty list in {CONFIG_PATH}")

    expected_count = config.get("EXPECTED_VM_COUNT")
    if expected_count is not None and (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 0
    ):
        raise ValueError(f"EXPECTED_VM_COUNT in {CONFIG_PATH} must be a non-negative integer or null")

    legacy_values = config.get("DEPARTMENT_SAFE_LEGACY_VALUES", [])
    if not isinstance(legacy_values, list) or any(
        not isinstance(value, str) or not value.strip() for value in legacy_values
    ):
        raise ValueError(f"DEPARTMENT_SAFE_LEGACY_VALUES in {CONFIG_PATH} must be a list of values")
    return config


def add_pending_vms(vm_names):
    """Add Pending VM names without removing entries already present in SKIP_VM."""
    config = load_config()
    skip_vm = config["SKIP_VM"]
    known = {name.strip().casefold() for name in skip_vm}
    added = 0
    for name in vm_names:
        name = name.strip()
        if name.casefold() not in known:
            skip_vm.append(name)
            known.add(name.casefold())
            added += 1
    config["SKIP_VM"] = skip_vm
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return added, len(skip_vm)
