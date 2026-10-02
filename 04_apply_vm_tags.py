"""
04_apply_vm_tags.py

Takes .output/03_vm_tag_change_plan.json and applies the real tag changes to Azure VMs.

Standard tags: owner, department, environment, project + `server` (optional).
`server` is optional: if a VM's tags_to_apply has no `server` key, the script never touches
that tag on the VM (Merge only adds/overwrites the keys listed; it never deletes anything).
Extra fields in the plan (vm_name, resource_group, active_vm, note, ...) are ignored.
Must be run with an account that has permission to edit tags on the scope of these VMs
(Tag Contributor or above).

Default = always dry-run (shows the full diff, changes nothing for real). Only runs for
real if --apply is passed.

Dry-run   : python 04_apply_vm_tags.py
Apply real: python 04_apply_vm_tags.py --apply

In apply mode, VMs run in batches of 10. After each batch, press Enter for the
next batch, type all to run every remaining batch without prompting, or stop.

Expected plan format (a JSON list, one entry per VM):
  {
    "subscription_id": "...",
    "resource_id": "/subscriptions/.../resourceGroups/<rg>/providers/Microsoft.Compute/virtualMachines/<vm>",
    "tags_to_remove": ["Owner", "dept"],
    "tags_to_apply": {"owner": "owner@gmail.com", "department": "..."}
  }
"""

import json
import logging
import re
import sys
from datetime import datetime

from azure.identity import InteractiveBrowserCredential, DeviceCodeCredential
from azure.mgmt.resource.resources import ResourceManagementClient

# Using InteractiveBrowserCredential instead of DefaultAzureCredential so we don't depend on
# the Azure CLI, which can't be installed yet (admin rights issue) — this pops up a browser
# to log in when the script runs instead.
# If the machine running this has no browser available (e.g. a pure remote terminal session),
# switch to DeviceCodeCredential() instead (it prints a code + URL to open in a browser
# elsewhere).
USE_DEVICE_CODE = False  # set to True if this machine can't open a browser

PLAN_FILE = ".output/03_vm_tag_change_plan.json"
BACKUP_FILE = f"backup_vm_tags_{datetime.now():%Y%m%d_%H%M%S}.json"
LOG_FILE_JSONL = f"vm_tag_update_log_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
LOG_FILE_TXT = LOG_FILE_JSONL.replace(".jsonl", ".txt")
BATCH_SIZE = 10

# Safety guard: this script is scoped to VMs only. Anything whose resource_id is not a VM
# gets skipped and reported as a failure instead of being written to.
VM_ID_PATTERN = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/([^/]+)/providers/Microsoft\.Compute/virtualMachines/([^/]+)$",
    re.IGNORECASE,
)


def parse_vm_id(resource_id):
    """Return (resource_group, vm_name) or None if this is not a VM resource ID."""
    m = VM_ID_PATTERN.match(resource_id)
    return (m.group(1), m.group(2)) if m else None


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE_TXT, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    # Silence the Azure SDK's own HTTP request/response logging at INFO level, otherwise it
    # floods the log file and our own diff output becomes impossible to find
    # (still logs Azure's WARNING/ERROR in case connection debugging is needed)
    logging.getLogger("azure").setLevel(logging.WARNING)


# ── Diff: compare the real current tags on Azure against what the plan will do ────────────
def compute_diff(current_tags: dict, tags_to_remove: list, tags_to_apply: dict) -> dict:
    removed, changed, added, unchanged = {}, {}, {}, {}
    for k in tags_to_remove:
        if k in current_tags:
            removed[k] = current_tags[k]
    for k, new_v in tags_to_apply.items():
        if k in current_tags:
            if current_tags[k] != new_v:
                changed[k] = {"old": current_tags[k], "new": new_v}
            else:
                unchanged[k] = new_v
        else:
            added[k] = new_v
    kept_untouched = {
        k: v for k, v in current_tags.items()
        if k not in tags_to_remove and k not in tags_to_apply
    }
    return {"removed": removed, "changed": changed, "added": added,
            "unchanged": unchanged, "kept_untouched": kept_untouched}


def print_diff(label, diff):
    lines = [f"--- {label} ---"]
    for k, v in diff["removed"].items():
        lines.append(f"  [-] removed        {k} = {v!r}")
    for k, ov in diff["changed"].items():
        lines.append(f"  [~] value changed  {k}: {ov['old']!r} -> {ov['new']!r}")
    for k, v in diff["added"].items():
        lines.append(f"  [+] added new      {k} = {v!r}")
    for k, v in diff["unchanged"].items():
        lines.append(f"  [=] unchanged      {k} = {v!r} (already matches)")
    for k, v in diff["kept_untouched"].items():
        lines.append(f"  [ ] left as-is     {k} = {v!r} (not related to the standard tags)")
    # logging.info writes to both screen and .txt file at once (print() only goes to screen,
    # never makes it into the log file)
    logging.info("\n" + "\n".join(lines))


# ── Backup: snapshot the current tags before touching anything at all (written to disk
# immediately, before any writes happen) ──
def save_backup(cumulative_backup_list):
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(cumulative_backup_list, f, ensure_ascii=False, indent=2)
    logging.info(f"Backup saved: {BACKUP_FILE} ({len(cumulative_backup_list)} VMs so far)")


def fetch_current_tags(client, resource_id):
    # Tags API at scope: works for any resource ID (same API family we use to write below),
    # so no need for the Compute SDK
    return client.tags.get_at_scope(resource_id).properties.tags or {}


def process_item(client, item, current, dry_run, log_f):
    """Compute the diff, log it, and (if not dry_run) write the tags for real — writes the log
    immediately for each VM"""
    resource_id = item["resource_id"]
    rg_name, vm_name = parse_vm_id(resource_id)
    label = f"{rg_name}/{vm_name}"
    result = {"resource_id": resource_id, "resource_group": rg_name, "vm_name": vm_name,
              "timestamp": datetime.now().isoformat()}

    diff = compute_diff(current, item["tags_to_remove"], item["tags_to_apply"])
    print_diff(label, diff)
    result["diff"] = diff

    if not diff["removed"] and not diff["changed"] and not diff["added"]:
        result["status"] = "SKIPPED_NO_OP"
        logging.info(f"{label}: nothing to change (no-op)")
    else:
        try:
            if not dry_run:
                # Delete first, then Merge — so a case-variant key (e.g. remove "Owner",
                # apply "owner") can't collide, since Azure tag names are case-insensitive
                if item["tags_to_remove"]:
                    client.tags.begin_update_at_scope(
                        resource_id,
                        {"operation": "Delete",
                         "properties": {"tags": {k: "" for k in item["tags_to_remove"]}}},
                    ).result()
                if item["tags_to_apply"]:
                    client.tags.begin_update_at_scope(
                        resource_id,
                        {"operation": "Merge", "properties": {"tags": item["tags_to_apply"]}},
                    ).result()
            result["status"] = "DRY_RUN_OK" if dry_run else "SUCCESS"
        except Exception as e:
            result["status"] = "FAILED"
            result["error"] = str(e)
            logging.error(f"{label}: FAILED -> {e}")

    # write the log immediately for each VM + flush to disk right away, so the log doesn't
    # get lost if the script crashes partway through
    log_f.write(json.dumps(result, ensure_ascii=False) + "\n")
    log_f.flush()
    return result


def process_batch(client, batch, dry_run, log_f, cumulative_backup):
    # Phase A: read the current tags for the whole batch first (read-only), then back them up
    # immediately — before a single write happens anywhere in this batch.
    # Keyed by resource_id (VM names are only unique within one RG).
    current_map, pre_errors = {}, {}
    for item in batch:
        rid = item["resource_id"]
        if parse_vm_id(rid) is None:
            pre_errors[rid] = ("FAILED_NOT_A_VM",
                               "resource_id is not a Microsoft.Compute/virtualMachines ID — skipped")
            continue
        try:
            current_map[rid] = fetch_current_tags(client, rid)
        except Exception as e:
            pre_errors[rid] = ("FAILED_TO_READ_CURRENT", str(e))

    if not dry_run:
        for item in batch:
            rid = item["resource_id"]
            if rid in current_map:
                rg_name, vm_name = parse_vm_id(rid)
                cumulative_backup.append({
                    "resource_group": rg_name,
                    "vm_name": vm_name,
                    "resource_id": rid,
                    "tags_before": current_map[rid],
                })
        save_backup(cumulative_backup)  # always written to disk before this batch's real writes start

    # Phase B: diff + write (if applying for real) + log each VM immediately
    batch_results = []
    for item in batch:
        rid = item["resource_id"]
        if rid in pre_errors:
            status, err = pre_errors[rid]
            result = {"resource_id": rid, "timestamp": datetime.now().isoformat(),
                      "status": status, "error": err}
            logging.error(f"{rid}: {status} -> {err}")
            log_f.write(json.dumps(result, ensure_ascii=False) + "\n")
            log_f.flush()
        else:
            result = process_item(client, item, current_map[rid], dry_run, log_f)
        batch_results.append(result)

    return batch_results


def main(dry_run=True):
    setup_logging()
    with open(PLAN_FILE, encoding="utf-8") as f:
        plan = json.load(f)

    credential = DeviceCodeCredential() if USE_DEVICE_CODE else InteractiveBrowserCredential()
    sub_id = plan[0]["subscription_id"]
    client = ResourceManagementClient(credential, sub_id)

    results, failed, cumulative_backup = [], [], []
    run_remaining = False

    with open(LOG_FILE_JSONL, "w", encoding="utf-8") as log_f:
        for i in range(0, len(plan), BATCH_SIZE):
            batch = plan[i : i + BATCH_SIZE]
            batch_results = process_batch(client, batch, dry_run, log_f, cumulative_backup)
            results.extend(batch_results)
            failed.extend([r for r in batch_results
                            if r["status"] not in ("SUCCESS", "DRY_RUN_OK", "SKIPPED_NO_OP")])

            if not dry_run and not run_remaining and (i + BATCH_SIZE) < len(plan):
                cont = input(
                    f"\nBatch {i // BATCH_SIZE + 1} done ({len(batch)} VMs). "
                    f"Check the Portal, then press Enter to run the next batch "
                    f"(all = run all remaining batches, stop = halt)... "
                )
                choice = cont.strip().casefold()
                if choice == "stop":
                    print("Stopped by user — any VM not yet reached will not be touched at all")
                    break

                if choice == "all":
                    run_remaining = True
                    print("Running all remaining batches without further prompts")

    print(f"\n=== Summary ===")
    print(f"Total {len(results)} VMs | succeeded/no-op {len(results)-len(failed)} | failed {len(failed)}")
    if failed:
        print("VMs that failed (needs manual review):", [f["resource_id"] for f in failed])
    print(f"Log: {LOG_FILE_JSONL}, {LOG_FILE_TXT}")
    if not dry_run:
        print(f"Backup: {BACKUP_FILE}")


if __name__ == "__main__":
    DRY_RUN = "--apply" not in sys.argv  # default = always dry-run, must pass --apply to run for real
    if DRY_RUN:
        print("=== DRY-RUN MODE (nothing changed for real yet) — run with --apply to apply for real ===")
    main(dry_run=DRY_RUN)
