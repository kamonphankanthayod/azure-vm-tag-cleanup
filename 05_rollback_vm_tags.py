"""
05_rollback_vm_tags.py

Restores a VM's tags back to what they were before apply, by reading from the
backup_vm_tags_<timestamp>.json file that apply_vm_tags.py writes before every real change.

Using the Replace operation is safe in this script only (unlike apply_vm_tags.py which uses
Delete+Merge) because the backup file is a full snapshot of every tag that existed at that
time, not a partial one — Replacing back with it restores the original state 100%, with
nothing dropped. (Side effect: any tag change made on the VM by someone else AFTER the apply
is also overwritten by the snapshot.)

Run: python 05_rollback_vm_tags.py backup_vm_tags_20260925_074227.json
     python 05_rollback_vm_tags.py backup_vm_tags_20260925_074227.json --apply   (omit = dry-run)
"""

import json
import logging
import re
import sys
from datetime import datetime

from azure.identity import InteractiveBrowserCredential, DeviceCodeCredential
from azure.mgmt.resource.resources import ResourceManagementClient

USE_DEVICE_CODE = False
LOG_FILE_TXT = f"vm_rollback_log_{datetime.now():%Y%m%d_%H%M%S}.txt"

# Safety guard: this script is scoped to VMs only (same pattern as apply_vm_tags.py)
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
    logging.getLogger("azure").setLevel(logging.WARNING)


def main(backup_file, dry_run=True):
    setup_logging()
    with open(backup_file, encoding="utf-8") as f:
        backup = json.load(f)

    credential = DeviceCodeCredential() if USE_DEVICE_CODE else InteractiveBrowserCredential()
    sub_id = backup[0]["resource_id"].split("/")[2]
    client = ResourceManagementClient(credential, sub_id)

    logging.info(f"Preparing to roll back {len(backup)} VMs from {backup_file}")
    if dry_run:
        logging.info("=== DRY-RUN MODE (nothing changed for real yet) ===")

    ok, failed = 0, []
    for item in backup:
        resource_id = item["resource_id"]
        tags_before = item["tags_before"]

        parsed = parse_vm_id(resource_id)
        if parsed is None:
            logging.error(f"{resource_id}: not a VM resource ID — skipped")
            failed.append(resource_id)
            continue
        label = f"{parsed[0]}/{parsed[1]}"

        try:
            # Tags API at scope: works for any resource ID (same API family used to write below)
            current = client.tags.get_at_scope(resource_id).properties.tags or {}
        except Exception as e:
            logging.error(f"{label}: could not read current tags -> {e}")
            failed.append(label)
            continue

        if current == tags_before:
            logging.info(f"{label}: already matches the backup, nothing to do (no-op)")
            ok += 1
            continue

        logging.info(f"{label}: current={current} -> will restore to={tags_before}")

        if not dry_run:
            try:
                client.tags.begin_update_at_scope(
                    resource_id,
                    {"operation": "Replace", "properties": {"tags": tags_before}},
                ).result()
                logging.info(f"{label}: rollback succeeded")
            except Exception as e:
                logging.error(f"{label}: rollback FAILED -> {e}")
                failed.append(label)
                continue
        ok += 1

    print(f"\n=== Rollback Summary ===")
    print(f"Total {len(backup)} VMs | succeeded/no-op {ok} | failed {len(failed)}")
    if failed:
        print("VMs that failed to roll back (needs a manual fix via the Portal):", failed)
    print(f"Log: {LOG_FILE_TXT}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: python 05_rollback_vm_tags.py <backup_vm_tags_file.json> [--apply]")
        sys.exit(1)
    DRY_RUN = "--apply" not in sys.argv
    main(args[0], dry_run=DRY_RUN)
