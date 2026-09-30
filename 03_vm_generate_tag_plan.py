"""
03_vm_generate_tag_plan.py

VM version of task22_generate_tag_plan.py. Converts 02_vm_summary.csv (the output of
02_vm_tag_gap_check.py, which pulls the real VM tags from Azure and compares them against
01_vm_all_data.csv) into a tag change plan for the apply step to use for the real Azure update.

Policy (same as the RG version, except for the "skipped field" rule below):
- Case 1/2/3/3b/4: all -> always override with the AO value (AO data has been verified)
- Case 4 (conflicting values) is still applied like the other cases, but flagged separately
  in the high-risk review file for transparency (it does not block the run)
- VM not found on Azure (Overall_Risk == "VM not found on Azure") -> not applied, not put in
  the plan, split out into its own file to check first whether it was deleted/renamed/moved
- VM listed in config.json SKIP_VM -> preserve every existing tag and only merge
  flag=Pending Owner Review. SKIP_VM names are matched without case sensitivity.
- Keys unrelated to the 5 standard fields are never touched (the generator has no idea about
  them: they don't appear in any Azure_{field}_Key column)
- project case 4: besides overriding with the AO value, the old value is preserved under the
  key from Old_Project_Tag_Key / Old_Project_Tag_Value (already finalized by 02_vm_tag_gap_check.py).
  Written generically per field via Old_{Field}_Tag_Key/Value columns. server (case 4) is
  flag-only: the CSV has no Old_Server_* columns, so nothing is preserved and nothing extra is added

Differences from the RG version:
- 1 row = 1 VM, resource_id points at the VM (Microsoft.Compute/virtualMachines)
- 5 fields (server added)
- SKIPPED FIELD RULE (changed on purpose): if a field can't be written, the whole field is left
  completely untouched — no key removal AND no write. The RG version collected the keys to
  remove first and skipped the write afterwards, so a skipped field could still lose its old tag
  with nothing written back. A field is skipped when:
    * AO left it blank (case "AO blank" from 02, or an empty AO value)
    * the AO value is over Azure's 256-character limit
    * the preserved old value (old-tag-*) is over the limit (otherwise the old keys would be
      deleted with nowhere to preserve the value)
    * the case in the CSV isn't one this script recognizes
  Every skipped field is listed in the plan row under skipped_fields for review.

Run: python 03_vm_generate_tag_plan.py
Input : .output/02_vm_summary.csv (the full version, not compact)
Output: .output/03_vm_tag_change_plan.json, .output/03_vm_high_risk_review.json,
        .output/03_vm_not_found.json
"""

import csv
import json
import os
import sys
from vm_config import load_config

OUTPUT_DIR = ".output"
INPUT_SUMMARY_CSV = os.path.join(OUTPUT_DIR, "02_vm_summary.csv")

OUTPUT_PLAN_JSON = os.path.join(OUTPUT_DIR, "03_vm_tag_change_plan.json")
OUTPUT_HIGH_RISK_JSON = os.path.join(OUTPUT_DIR, "03_vm_high_risk_review.json")
OUTPUT_NOT_FOUND_JSON = os.path.join(OUTPUT_DIR, "03_vm_not_found.json")

FIELDS = ["owner", "department", "environment", "project", "server"]
MAX_TAG_VALUE_LEN = 256  # Azure's hard limit per tag value
NOT_FOUND_RISK = "VM not found on Azure"
KNOWN_CASES = {"1", "2", "3", "3b", "4"}


def split_keys(key_found_str: str):
    """Azure_{field}_Key may be a single key, or several keys separated by ', ' (per the
    format that classify_field() in 02_vm_tag_gap_check.py writes during multi-key merges)"""
    if not key_found_str:
        return []
    return [k.strip() for k in key_found_str.split(",") if k.strip()]


def build_plan_row(row: dict, subscription_id: str, skip_vm=False):
    vm_name = row["VM"].strip()
    rg_name = row["RG"].strip()
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{rg_name}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )

    if skip_vm:
        return {
            "subscription_id": subscription_id,
            "vm_name": vm_name,
            "resource_group": rg_name,
            "resource_id": resource_id,
            "active_vm": row.get("Active_VM (AO)", ""),
            "skip_vm": True,
            "tags_to_remove": [],
            "tags_to_apply": {"flag": "Pending Owner Review"},
            "skipped_fields": [
                {"field": field, "reason": "VM is in SKIP_VM — existing tags left untouched"}
                for field in FIELDS
            ],
            "unrelated_tags_kept_untouched": row.get("Unrelated_Tags", ""),
            "value_length_warnings": [],
            "note": row.get("Note", ""),
        }, []

    tags_to_remove = []
    tags_to_apply = {}
    high_risk_fields = []
    value_length_warnings = []
    skipped_fields = []

    for field in FIELDS:
        case_num = (row.get(f"{field}_Case", "") or "").strip()
        ao_value = (row.get(f"AO_{field}", "") or "").strip()
        azure_key_str = row.get(f"Azure_{field}_Key", "") or ""

        # ---- decide first whether this field is written at all; a skipped field is left
        # completely untouched (no removal, no write) ----
        if case_num == "AO blank" or not ao_value:
            skipped_fields.append({"field": field, "reason": "AO value is blank — field left untouched"})
            continue

        if case_num not in KNOWN_CASES:
            skipped_fields.append({
                "field": field,
                "reason": f"unrecognized case {case_num!r} in {field}_Case — field left untouched",
            })
            continue

        if len(ao_value) > MAX_TAG_VALUE_LEN:
            msg = (f"{field}: value is {len(ao_value)} characters, over the Azure limit of "
                   f"{MAX_TAG_VALUE_LEN} — needs manual review before applying")
            value_length_warnings.append(msg)
            skipped_fields.append({"field": field, "reason": f"{msg} (field left untouched)"})
            continue

        # case 4: work out the preserved-old-value tag up front, because if it can't be
        # written the whole field has to be skipped (otherwise the old keys would be deleted
        # with the old value preserved nowhere)
        old_tag_key = old_tag_value = ""
        if case_num == "4":
            old_tag_key = (row.get(f"Old_{field.capitalize()}_Tag_Key", "") or "").strip()
            old_tag_value = (row.get(f"Old_{field.capitalize()}_Tag_Value", "") or "").strip()
            if old_tag_key and old_tag_value and len(old_tag_value) > MAX_TAG_VALUE_LEN:
                msg = (f"{old_tag_key}: value is {len(old_tag_value)} characters, over the Azure "
                       f"limit of {MAX_TAG_VALUE_LEN} — the old value can't be preserved, needs "
                       f"manual review before applying")
                value_length_warnings.append(msg)
                skipped_fields.append({"field": field, "reason": f"{msg} (field left untouched)"})
                continue

        # ---- field is going to be written ----
        if case_num != "1":
            # case 2/3/3b/4 -> only remove keys that don't exactly match the standard field name
            # (if the key already matches the field name, a Merge can overwrite the value
            # directly, no need for delete-then-merge)
            for k in split_keys(azure_key_str):
                if k != field and k not in tags_to_remove:
                    tags_to_remove.append(k)

        tags_to_apply[field] = ao_value

        if case_num == "4":
            high_risk_entry = {
                "field": field,
                "ao_value": ao_value,
                "azure_key_found": azure_key_str,
                "azure_value_found": row.get(f"Azure_{field}_Value", ""),
                "reason": row.get(f"{field}_Action", ""),
            }
            if old_tag_key and old_tag_value:
                tags_to_apply[old_tag_key] = old_tag_value
                high_risk_entry["old_tag_key"] = old_tag_key
                high_risk_entry["old_tag_value"] = old_tag_value
            high_risk_fields.append(high_risk_entry)

    plan_row = {
        "subscription_id": subscription_id,
        "vm_name": vm_name,
        "resource_group": rg_name,
        "resource_id": resource_id,
        "active_vm": row.get("Active_VM (AO)", ""),  # informational only, not used to auto-filter anything
        "skip_vm": False,
        "tags_to_remove": tags_to_remove,
        "tags_to_apply": tags_to_apply,
        "skipped_fields": skipped_fields,
        "unrelated_tags_kept_untouched": row.get("Unrelated_Tags", ""),
        "value_length_warnings": value_length_warnings,
        "note": row.get("Note", ""),
    }
    return plan_row, high_risk_fields


def main():
    config = load_config(require_subscription=True)
    subscription_id = config["SUBSCRIPTION_ID"].strip()
    skip_vm = {name.strip().casefold() for name in config["SKIP_VM"]}
    try:
        with open(INPUT_SUMMARY_CSV, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print(f"!! Could not find {INPUT_SUMMARY_CSV} — run 02_vm_tag_gap_check.py first to generate it", file=sys.stderr)
        sys.exit(1)

    # sanity check on the columns (catches feeding in the wrong CSV, e.g. the compact one or the RG one)
    required_cols = ["VM", "RG", "Overall_Risk", "Unrelated_Tags"]
    for field in FIELDS:
        required_cols += [f"AO_{field}", f"Azure_{field}_Key", f"Azure_{field}_Value",
                          f"{field}_Case", f"{field}_Action"]
    have_cols = set(rows[0].keys()) if rows else set()
    missing_cols = [c for c in required_cols if c not in have_cols]
    if rows and missing_cols:
        print(f"!! Column(s) not found in {INPUT_SUMMARY_CSV}: {', '.join(missing_cols)} — "
              f"is this the full 02_vm_summary.csv (not compact / not the RG version)?", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    plan, high_risk_report, not_found = [], [], []

    for row in rows:
        if row.get("Overall_Risk", "") == NOT_FOUND_RISK:
            not_found.append({
                "vm_name": row.get("VM", ""),
                "resource_group": row.get("RG", ""),
                "department_ao": row.get("Department (AO)", ""),
                "note": "This VM was not found on Azure during the gap-check — verify whether it was deleted/renamed/moved to another RG before applying",
            })
            continue

        plan_row, high_risk_fields = build_plan_row(
            row, subscription_id, row["VM"].strip().casefold() in skip_vm
        )
        plan.append(plan_row)
        if high_risk_fields:
            high_risk_report.append({
                "vm_name": plan_row["vm_name"],
                "resource_group": plan_row["resource_group"],
                "fields": high_risk_fields,
            })

    with open(OUTPUT_PLAN_JSON, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_HIGH_RISK_JSON, "w", encoding="utf-8") as f:
        json.dump(high_risk_report, f, ensure_ascii=False, indent=2)
    with open(OUTPUT_NOT_FOUND_JSON, "w", encoding="utf-8") as f:
        json.dump(not_found, f, ensure_ascii=False, indent=2)

    total_remove = sum(len(p["tags_to_remove"]) for p in plan)
    total_apply = sum(len(p["tags_to_apply"]) for p in plan)
    normal_plan = [p for p in plan if not p["skip_vm"]]
    total_warnings = sum(len(p["value_length_warnings"]) for p in normal_plan)
    no_op_vms = sum(1 for p in plan if not p["tags_to_remove"] and not p["tags_to_apply"])
    skipped_vms = sum(1 for p in plan if p["skip_vm"])
    old_tag_preserved_vms = sum(
        1 for r in high_risk_report if any("old_tag_key" in f for f in r["fields"])
    )
    skipped_blank = sum(1 for p in normal_plan for s in p["skipped_fields"] if s["reason"].startswith("AO value is blank"))
    skipped_other = sum(len(p["skipped_fields"]) for p in normal_plan) - skipped_blank

    print("=== 03_vm_generate_tag_plan.py summary ===")
    print(f"Total VM in summary: {len(rows)}")
    print(f"VM not found on Azure (split out to {OUTPUT_NOT_FOUND_JSON}): {len(not_found)}")
    print(f"VM included in {OUTPUT_PLAN_JSON}: {len(plan)}")
    print(f"  - VM in SKIP_VM (existing tags preserved; flag added): {skipped_vms}")
    print(f"  - VM with nothing to do at all (every field skipped): {no_op_vms}")
    print(f"  - Total tag keys to be removed (Delete): {total_remove}")
    print(f"  - Tags to be added/updated (Merge): {total_apply}")
    print(f"  - Fields skipped and left untouched: {skipped_blank} (AO blank) + {skipped_other} (other reason)")
    if total_warnings or skipped_other:
        print(f"  [!!] fields skipped for length / unrecognized case: {skipped_other} "
              f"(see skipped_fields / value_length_warnings for each VM)")
    print(f"VM with at least one case-4 field (value previously conflicted, applied anyway but flagged for review): {len(high_risk_report)} "
          f"(see details in {OUTPUT_HIGH_RISK_JSON})")
    print(f"  - Of these, VM where the old value was preserved under a new tag name (e.g. old-tag-project) instead of being dropped: "
          f"{old_tag_preserved_vms} VM")
    print(f"\nOutput: {OUTPUT_PLAN_JSON}, {OUTPUT_HIGH_RISK_JSON}, {OUTPUT_NOT_FOUND_JSON}")


if __name__ == "__main__":
    main()
