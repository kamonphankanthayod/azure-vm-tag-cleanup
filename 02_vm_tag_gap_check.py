"""
02_vm_tag_gap_check.py

Compares the tags entered in the survey (.output/01_vm_all_data.csv,
1 row = 1 VM) against the real tags on Azure (pulled via Azure Resource Graph), and sorts them
into cases 1 / 2 / 3 / 3b / 4, plus case 5 for unrelated tags.

Details:
  - 1 row = 1 VM, matched on (Resource Group + VM Name)
  - 5 fields: owner, department, environment, project + server (server uses the same
    matching logic as project; case 4 on server is flag-only, no old-tag plan)
  - new "AO blank" result: AO left the field empty (server is optional) -> nothing to write, skip
  - Resource Graph query is paged to handle more than one page of VMs
  - env-suffix stripping also accepts 'qas' and a trailing digit on an env word (DEV1, PRD2)

Run: python 02_vm_tag_gap_check.py
Requires: pip install pandas azure-identity azure-mgmt-resourcegraph
"""

import csv
import os
import re
import sys
from collections import defaultdict

import pandas as pd
from azure.identity import InteractiveBrowserCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from vm_config import load_config

# ── Config ────────────────────────────────────────────────────────────
OUTPUT_DIR = ".output"
INPUT_CSV = os.path.join(OUTPUT_DIR, "01_vm_all_data.csv")

# 'Active VM?' column of 01_vm_all_data.csv (Active / Inactive / Unknown)
ACTIVE_COLUMN = "Active VM?"
KNOWN_ACTIVE_VALUES = {"active", "inactive", "unknown"}

OUTPUT_MAIN_CSV = os.path.join(OUTPUT_DIR, "02_vm_tag_gap_report.csv")
OUTPUT_UNRELATED_CSV = os.path.join(OUTPUT_DIR, "02_vm_unrelated_tags.csv")
OUTPUT_SUMMARY_CSV = os.path.join(OUTPUT_DIR, "02_vm_summary.csv")  # 1 row per VM, pivoted from the same dataset
OUTPUT_SUMMARY_COMPACT_CSV = os.path.join(OUTPUT_DIR, "02_vm_summary_compact.csv")  # 1 row per VM, condensed

FIELDS = ["owner", "department", "environment", "project", "server"]

# fields that share the project matching logic (env-suffix strip, substring+number, acronym, multi-value)
PROJECT_LIKE_FIELDS = ("project", "server")

# spec 2.7 (added Aug 26) — case 4 specifically for the "project" field: instead of just
# flagging it as hands-off, propose a specific action — preserve all the old values (combined
# from every project-like key) under this new key, then write the AO-supplied value into the
# "project" key as usual. Still case 4/high-risk as before (still needs review before apply),
# this just makes the proposed action more concrete rather than auto-applying it.
OLD_PROJECT_TAG_KEY = "old-tag-project"

# spec 2.4 — starter alias list only, not exhaustive — needs expanding after seeing real run results
ALIAS_LIST = {
    "owner": ["owner", "Owner", "OWNER", "OwnerEmail", "Owner_Email", "AppOwner", "Application Owner"],
    "department": ["department", "Department", "DEPARTMENT", "Dept", "dept"],
    "environment": ["environment", "enviroment","Environment", "ENVIRONMENT", "Env", "env", "ENV"],
    "project": ["project", "Project", "PROJECT", "ProjectName", "Project01", "project01", "Proj"],
    "server": ["server", "Server", "SERVER"],
}

# Known-safe legacy department values live in the local config, not the public code.
DEPARTMENT_SAFE_LEGACY_VALUES = {
    value.casefold() for value in load_config().get("DEPARTMENT_SAFE_LEGACY_VALUES", [])
}

# map AO's CSV column names -> standard field names
AO_COLUMN_MAP = {
    "owner": "Owner (Email)",
    "department": "Department",
    "environment": "Environment",
    "project": "Project",
    "server": "Server",
}
VM_NAME_COLUMN = "VM Name"
RG_NAME_COLUMN = "Resource Group"


# ── Helpers ───────────────────────────────────────────────────────────
def normalize_value(raw):
    """trim + lowercase for value comparison (spec 2.5, case-insensitive — see open item 3.1)"""
    if raw is None:
        return set()
    raw = str(raw).strip()
    if raw == "":
        return set()
    parts = [p.strip().lower() for p in raw.split(",") if p.strip() != ""]
    return set(parts)


def normalize_active_flag(raw, vm_name: str) -> str:
    """normalize the 'Active VM?' column value from the CSV.
    Known values: Active / Inactive / Unknown (case-insensitive, trimmed).
    If some other value shows up (typo/blank), warn on the console + default to 'Unknown'
    (safer than guessing Active)."""
    val = str(raw if raw is not None else "").strip()
    val_lower = val.lower()
    if val_lower in KNOWN_ACTIVE_VALUES:
        return val_lower.capitalize()
    print(f"⚠️  VM '{vm_name}': unrecognized value in column '{ACTIVE_COLUMN}' ('{val}') "
          f"— using 'Unknown' instead, worth double-checking {INPUT_CSV}", file=sys.stderr)
    return "Unknown"


def project_key_sort_order(key: str):
    """spec 2.7 — order project-like keys by their trailing number (no number = sorts first),
    e.g. project, project2, project3, project4, so the merged value comes out deterministic
    instead of depending on whatever order the Azure API happens to return the keys in"""
    norm = normalize_key(key)
    m = re.search(r"(\d+)$", norm)
    num = int(m.group(1)) if m else 0
    return (num, norm)


def normalize_key(k: str) -> str:
    """strip spaces/underscores/hyphens/dots + lowercase, so key names compare regardless of
    spacing/separator style"""
    return re.sub(r"[\s_\-.]", "", str(k)).lower()


def base_words_for_field(field: str):
    """
    Combine the field name itself with every alias in ALIAS_LIST[field], normalize each, and
    strip any trailing digits, to get a set of "base words" for pattern matching
    (spec 2.4/2.5 — updated Aug 20 round 2)
    """
    words = [field] + ALIAS_LIST.get(field, [])
    bases = set()
    for w in words:
        norm = normalize_key(w)
        stripped = re.sub(r"\d+$", "", norm)  # strip trailing digits, in case an alias already has one (e.g. project01)
        bases.add(stripped if stripped else norm)
    return bases


def find_matching_keys(azure_tags: dict, field: str):
    """
    Find the Azure keys that match this field (spec 2.5 item 1) — updated Aug 20 round 2:
    uses a "base word + optional trailing number" pattern instead of an exact match against
    ALIAS_LIST directly, to catch cases like project1, project 01, Project_2, owner1,
    Owner-02, where spelling/spacing/numbering is all over the place.
    Returns: (list of keys that match exactly as the standard name, list of keys that only
    match via the pattern/alias)
    """
    standard_hits = [k for k in azure_tags.keys() if k == field]
    bases = base_words_for_field(field)

    alias_hits = []
    for k in azure_tags.keys():
        if k == field:
            continue
        norm_k = normalize_key(k)
        for base in bases:
            if norm_k == base or (norm_k.startswith(base) and norm_k[len(base):].isdigit()):
                alias_hits.append(k)
                break
    return standard_hits, alias_hits


# spec 2.5b — case 3b (added Aug 20 round 4): the values don't match literally, but they're
# just different formats of the same real value
DEPT_STOPWORDS = {"and", "of", "the", "for", "&"}

# spec 2.5b(c) — added Aug 20 round 6: project group A (single value on both sides)
PROJECT_ENV_SUFFIX_WORDS = {
    "dev", "prd", "prod", "production", "uat", "sit", "test",
    "stg", "staging", "qa", "qas", "dr site", "dr", "temp rg", "temp",
}
PROJECT_STOPWORDS = {"and", "of", "the", "for", "&"}  # same as DEPT_STOPWORDS, used to strip initials


def owner_is_same_person(ao_raw: str, azure_raw: str) -> bool:
    """spec 2.5b(a) — one side is an email, the other is a person's name, and the email's
    local-part matches the first word of the name — exact match only, case-insensitive
    (no fuzzy matching)"""
    ao_raw = (ao_raw or "").strip()
    azure_raw = (azure_raw or "").strip()
    if not ao_raw or not azure_raw:
        return False
    ao_is_email = "@" in ao_raw
    azure_is_email = "@" in azure_raw
    if ao_is_email == azure_is_email:
        return False  # only qualifies for 3b if exactly one side is an email and the other is a name (both being emails, or neither, doesn't count)
    email_raw, name_raw = (ao_raw, azure_raw) if ao_is_email else (azure_raw, ao_raw)
    local_part = email_raw.split("@")[0]
    email_first_token = re.split(r"[._\-]", local_part)[0]
    name_first_word = re.split(r"\s+", name_raw)[0] if name_raw else ""
    return bool(email_first_token) and email_first_token.lower() == name_first_word.lower()


def department_is_abbreviation(ao_raw: str, azure_raw: str) -> bool:
    """spec 2.5b(b) — Azure_Value is the initials of the words in AO_Value (stop words stripped first)"""
    ao_raw = (ao_raw or "").strip()
    azure_raw = (azure_raw or "").strip()
    if not ao_raw or not azure_raw:
        return False
    words = [w for w in re.split(r"\s+", ao_raw) if w]
    filtered = [w for w in words if w.lower() not in DEPT_STOPWORDS]
    if not filtered:
        return False
    initials = "".join(w[0] for w in filtered if w).upper()
    azure_clean = re.sub(r"[^A-Za-z]", "", azure_raw).upper()
    return bool(azure_clean) and initials == azure_clean


def _is_env_suffix_word(token: str) -> bool:
    """env word, optionally with a trailing number (DEV, DEV1, prd2, QAS ...)"""
    t = (token or "").strip().lower()
    if t in PROJECT_ENV_SUFFIX_WORDS:
        return True
    stripped = re.sub(r"\d+$", "", t)
    return bool(stripped) and stripped != t and stripped in PROJECT_ENV_SUFFIX_WORDS


def strip_project_env_suffix(value: str) -> str:
    """spec 2.5b(c) — strip a trailing environment/site suffix off the value before comparing
    (both a bare trailing word, e.g. 'Sample App DEV', and a trailing parenthetical, e.g.
    'Sample App (DR Site)')"""
    v = (value or "").strip()
    if not v:
        return v
    # strip a trailing parenthetical if its contents are a known env/site suffix
    m = re.search(r"\s*\(([^)]*)\)\s*$", v)
    if m and _is_env_suffix_word(m.group(1)):
        v = v[: m.start()].strip()
    # strip a trailing bare word that's a known env suffix (repeat in case there are several in a row)
    tokens = v.split()
    while tokens and _is_env_suffix_word(tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip()


def _is_acronym_candidate(v: str) -> bool:
    """spec 2.5b(c) — does this look like an acronym: letters only, no spaces, 2-8 chars long"""
    return bool(re.fullmatch(r"[A-Za-z]{2,8}", v.strip()))


def _initials(v: str, stopwords: set) -> str:
    """get the initials of each word in v (stop words stripped first) — used together with project_is_variant"""
    words = [w for w in re.split(r"\s+", v.strip()) if w]
    filtered = [w for w in words if w.lower() not in stopwords]
    if not filtered:
        return ""
    return "".join(w[0] for w in filtered if w).upper()


def project_is_variant(ao_raw: str, azure_raw: str) -> bool:
    """spec 2.5b(c) — project group A (single value on both sides):
    1) substring + trailing number (e.g. SampleApp ↔ SampleApp2)
    2) two-way acronym match (e.g. WMS ↔ Warehouse Management System)
    Both checks run after stripping the environment/site suffix first"""
    ao = strip_project_env_suffix(ao_raw)
    az = strip_project_env_suffix(azure_raw)
    if not ao or not az:
        return False

    ao_norm = re.sub(r"[^A-Za-z0-9]", "", ao).lower()
    az_norm = re.sub(r"[^A-Za-z0-9]", "", az).lower()
    if ao_norm and az_norm:
        if ao_norm == az_norm:
            return True
        shorter, longer = (ao_norm, az_norm) if len(ao_norm) <= len(az_norm) else (az_norm, ao_norm)
        if longer.startswith(shorter) and longer[len(shorter):].isdigit():
            return True

    if _is_acronym_candidate(ao) and _initials(az, PROJECT_STOPWORDS) == ao.upper():
        return True
    if _is_acronym_candidate(az) and _initials(ao, PROJECT_STOPWORDS) == az.upper():
        return True
    return False


def _normalize_dept_legacy(v: str) -> str:
    """normalize spacing around '/' + trim + lowercase — used by both 2.5c (single-value) and 2.5d(b) (multi-key filter)"""
    return re.sub(r"\s*/\s*", " / ", (v or "").strip()).lower()


def department_is_known_safe_legacy(azure_raw: str) -> bool:
    """spec 2.5c — Azure_Value matches a known-safe old value (regardless of what AO entered),
    compared with trim + '/'-spacing normalization + lowercase"""
    return _normalize_dept_legacy(azure_raw) in DEPARTMENT_SAFE_LEGACY_VALUES


def department_filter_legacy(values: set) -> set:
    """spec 2.5d(b) (added Aug 20 round 7) — filter out known-safe legacy values (2.5c) from
    the merged multi-key value set before comparing against AO_Value — e.g. keys 'department'
    + 'dept' where 'dept' holds a known legacy value (an old catch-all value not specific to any
    department) mixed in alongside a key that already matches"""
    return {v for v in values if _normalize_dept_legacy(v) not in DEPARTMENT_SAFE_LEGACY_VALUES}


def owner_names_match(a: str, b: str) -> bool:
    """spec 2.5d(a) — compare one owner pair at a time (used in the many-to-many check):
    exact email match (case-insensitive), or the existing 2.5b(a) rule (email local-part
    matches the first word of the name, exact only, no fuzzy matching)"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if "@" in a and "@" in b:
        return a.lower() == b.lower()
    return owner_is_same_person(a, b)


def owner_multivalue_match(ao_raw: str, azure_raw: str) -> bool:
    """spec 2.5d(a) (added Aug 20 round 7) — owner where either side (or both) is multi-value:
    split into a list by comma (trimmed), then match many-to-many — passes immediately if
    "at least 1 pair" matches (doesn't need every pair to match like project does, because
    owner represents "adding/removing a co-owner", not swapping the whole dataset — as long
    as the same person is still in the new list, that's consistent enough)"""
    ao_names = [p.strip() for p in (ao_raw or "").split(",") if p.strip()]
    az_names = [p.strip() for p in (azure_raw or "").split(",") if p.strip()]
    return any(owner_names_match(a, b) for a in ao_names for b in az_names)


def project_multivalue_match(ao_raw: str, azure_raw: str):
    """spec 2.5d(c) (added Aug 20 round 7) — project group B: either side (or both) is
    multi-value. Split into a list by comma (trimmed), then match many-to-many using the
    existing project_is_variant rule (2.5b-c) + an exact normalized match (catches pure
    spelling/case differences) — unlike owner, project needs "every name on both sides" to
    match before the whole set counts as consistent (no partial credit).
    Returns (all_matched, matched_count, total_count, unmatched_names), or None if there's
    nothing to compare"""
    ao_names = [p.strip() for p in (ao_raw or "").split(",") if p.strip()]
    az_names = [p.strip() for p in (azure_raw or "").split(",") if p.strip()]
    if not ao_names or not az_names:
        return None

    def names_match(a, b):
        return normalize_value(a) == normalize_value(b) or project_is_variant(a, b)

    ao_matched = [any(names_match(a, b) for b in az_names) for a in ao_names]
    az_matched = [any(names_match(a, b) for a in ao_names) for b in az_names]

    unmatched = [ao_names[i] for i, m in enumerate(ao_matched) if not m] + \
                [az_names[i] for i, m in enumerate(az_matched) if not m]
    total = len(ao_names) + len(az_names)
    matched_count = total - len(unmatched)
    return (len(unmatched) == 0, matched_count, total, unmatched)


def check_case_3b_multivalue(field: str, ao_value, azure_raw_value):
    """spec 2.5d — multi-value version of case 3b (unlike the original check_case_3b, which
    is limited to a single value on both sides). Called after check_case_3b (single-value)
    fails to match — returns an action string if the condition is met, None otherwise"""
    if field == "owner" and owner_multivalue_match(str(ao_value), str(azure_raw_value)):
        return ("At least 1 matching owner pair (exact email match, or name↔email-local-part "
                "for the same person) between the AO-entered set and the Azure set — safe to "
                "override, no need to flag as high-risk")
    if field in PROJECT_LIKE_FIELDS:
        result = project_multivalue_match(str(ao_value), str(azure_raw_value))
        if result and result[0]:
            return ("Every project/server name on both sides matches (after stripping the "
                     "environment/site suffix, substring+trailing-number, or two-way acronym "
                     "match) — safe to override, no need to flag as high-risk")
    return None


def append_project_partial_match_note(base_action: str, ao_value, azure_raw_value) -> str:
    """spec 2.5d(c) — if the project names partially match (not all of them), append the
    X/Y-matched detail plus which names still don't match onto the existing action text
    (this is still case 4 as before — it just speeds up the review)"""
    result = project_multivalue_match(str(ao_value), str(azure_raw_value))
    if result is None or result[0]:
        return base_action
    _, matched_count, total, unmatched = result
    return f"{base_action} ({matched_count}/{total} names matched; still need to check [{', '.join(unmatched)}])"


def check_case_3b(field: str, ao_value, azure_raw_value, ao_norm: set, merged_values: set):
    """spec 2.5b/2.5c — only called when there's a single key + a single value on both sides,
    and the value is about to fall into case 4.
    Returns an action string if the 3b condition is met, None otherwise (falls through to
    case 4 as usual)"""
    if len(ao_norm) != 1 or len(merged_values) != 1:
        return None  # spec 2.5b: limited to a single value on both sides only, to avoid multi-owner/multi-value cases
    if field == "owner" and owner_is_same_person(str(ao_value), str(azure_raw_value)):
        return "The name and the email are the same person (local-part matches the name) — safe to override, no need to flag as high-risk"
    if field == "department":
        # spec 2.5c — check the known-safe legacy value first (regardless of AO), then check the abbreviation rule (2.5b-b)
        if department_is_known_safe_legacy(str(azure_raw_value)):
            return "Azure holds an old (legacy catch-all) value already known to be safe — safe to override, no need to flag as high-risk"
        if department_is_abbreviation(str(ao_value), str(azure_raw_value)):
            return "Azure holds an abbreviation of the same department — safe to override, no need to flag as high-risk"
    if field in PROJECT_LIKE_FIELDS and project_is_variant(str(ao_value), str(azure_raw_value)):
        return "The project/server names match after stripping the environment/site suffix (substring or acronym) — safe to override, no need to flag as high-risk"
    return None


def classify_field(ao_value, azure_tags: dict, field: str):
    """
    Returns (case_number, azure_key_found, azure_value, risk, action)
    per the 5 cases in spec 2.5 (updated Aug 20: when several keys match, "merge the values
    and compare" instead of forcing case 4 immediately — to handle RG with several project
    keys, e.g. project01/project02).
    Round 7 (Aug 20) — spec 2.5d: added multi-value case 3b for owner (many-to-many, passes
    with at least 1 matching pair) and project group B (many-to-many, needs every name to
    match) + department: filters out known-safe legacy values (2.5c) from the merged
    multi-key set before comparing
    """
    standard_hits, alias_hits = find_matching_keys(azure_tags, field)
    all_hits = standard_hits + alias_hits

    # AO left this field blank (e.g. optional 'server') -> nothing to write, skip.
    # Without this, blank + no Azure tag would come out as case 1 "write new (add)" of an empty value
    if not normalize_value(ao_value):
        blank_key = ", ".join(all_hits)
        blank_val = "; ".join(f"{k}={azure_tags.get(k, '')!r}" for k in all_hits)
        return ("AO blank", blank_key, blank_val, "None",
                "AO left this blank — nothing to write, skip (don't overwrite whatever Azure has)")

    # case 1: no tag at all
    if not all_hits:
        return (1, "", "", "None", "write new (add)")

    ao_norm = normalize_value(ao_value)
    # every Azure value matching this field, joined by commas — used for the 2.5d multi-value checks (owner/project)
    azure_raw_combined = ",".join(str(azure_tags.get(k, "")) for k in all_hits)

    # single key + exactly standard + value matches -> case 2 (no-op)
    if len(all_hits) == 1 and standard_hits:
        key_found = all_hits[0]
        azure_raw_value = azure_tags.get(key_found, "")
        azure_norm = normalize_value(azure_raw_value)
        if azure_norm == ao_norm:
            return (2, key_found, azure_raw_value, "None", "safe to overwrite (no-op)")
        else:
            # spec 2.5b (added Aug 20 round 4) — case 3b (single value both sides): different format, same real value
            action_3b = check_case_3b(field, ao_value, azure_raw_value, ao_norm, azure_norm)
            if action_3b:
                return ("3b", key_found, azure_raw_value, "Low", action_3b)
            # spec 2.5d (added Aug 20 round 7) — multi-value case 3b (either or both sides have several values)
            multi_action = check_case_3b_multivalue(field, ao_value, azure_raw_value)
            if multi_action:
                return ("3b", key_found, azure_raw_value, "Low", multi_action)
            action = "flagged — do not auto-apply, value conflicts with AO"
            if field in PROJECT_LIKE_FIELDS:
                action = append_project_partial_match_note(action, ao_value, azure_raw_value)
            return (4, key_found, azure_raw_value, "High", action)

    # every other case (a single key matched via alias, or several keys) -> merge every key's
    # value into one set before comparing (spec 2.5 item 3)
    merged_values = set()
    for k in all_hits:
        merged_values |= normalize_value(azure_tags.get(k, ""))
    key_found_str = ", ".join(all_hits)
    value_found_str = "; ".join(f"{k}={azure_tags.get(k, '')!r}" for k in all_hits)

    # spec 2.5d(b) (added Aug 20 round 7) — department: filter out known-safe legacy values
    # (2.5c) from the merged set before comparing (e.g. key 'department' already matches, but
    # key 'dept' has an old value mixed in alongside it)
    compare_values = department_filter_legacy(merged_values) if field == "department" else merged_values
    legacy_filtered_out = (merged_values - compare_values) if field == "department" else set()

    if compare_values == ao_norm:
        if legacy_filtered_out:
            action = (f"Ignored the known-safe legacy value(s) ({', '.join(sorted(legacy_filtered_out))}) mixed "
                      f"in with {key_found_str} — the remaining value already matches AO, safe to override "
                      f"(also recommend deleting the key holding that old value when applying for real)")
            return ("3b", key_found_str, value_found_str, "Low", action)
        if len(all_hits) > 1:
            action = f"merge keys {key_found_str} into a single key '{field}' (lowercase), values joined by comma"
        else:
            action = f"rename key '{key_found_str}' -> '{field}', keep the existing value"
        return (3, key_found_str, value_found_str, "Low", action)
    else:
        # spec 2.5b (added Aug 20 round 4) — case 3b (single value both sides): only applies when there's a single key
        if len(all_hits) == 1:
            azure_raw_value = azure_tags.get(all_hits[0], "")
            action_3b = check_case_3b(field, ao_value, azure_raw_value, ao_norm, merged_values)
            if action_3b:
                return ("3b", all_hits[0], azure_raw_value, "Low", action_3b)
        # spec 2.5d (added Aug 20 round 7) — multi-value case 3b (owner/project)
        multi_action = check_case_3b_multivalue(field, ao_value, azure_raw_combined)
        if multi_action:
            return ("3b", key_found_str, value_found_str, "Low", multi_action)

        action = "flagged — do not auto-apply, the combined value across all keys doesn't match AO (missing/extra/different spelling)"
        if field in PROJECT_LIKE_FIELDS:
            action = append_project_partial_match_note(action, ao_value, azure_raw_combined)
        return (4, key_found_str, value_found_str, "High", action)


def build_old_project_tag_plan(azure_tags: dict, field: str):
    """spec 2.7 — only called when field == 'project' and it falls into case 4: combines the
    old values from every project-like key found on Azure (sorted by trailing number, via
    project_key_sort_order) into a single comma-separated string, ready to store under
    OLD_PROJECT_TAG_KEY.
    Returns (old_value_combined, sorted_matched_keys), or None if there's no key to combine"""
    if field != "project":
        return None
    std_hits, alias_hits = find_matching_keys(azure_tags, field)
    matched_keys = std_hits + alias_hits
    if not matched_keys:
        return None
    sorted_keys = sorted(matched_keys, key=project_key_sort_order)
    old_value_combined = ",".join(str(azure_tags.get(k, "")) for k in sorted_keys)
    return (old_value_combined, sorted_keys)


def fetch_azure_vm_tags(subscription_id: str) -> dict:
    """
    Pull the real tags for every VM in the subscription via Azure Resource Graph.
    Resource Graph returns 100 rows per page by default, so this asks for the max page size
    (1000) and keeps following the skip token until there are no more pages.
    Returns dict: { (rg_name_lower, vm_name_lower): {tag_key: tag_value, ...} }
    (VM names are only unique within an RG on Azure, so the key is RG + VM name)
    """
    print("Opening the browser for login (InteractiveBrowserCredential) ...")
    credential = InteractiveBrowserCredential()
    client = ResourceGraphClient(credential)

    query = """
    resources
    | where type =~ 'microsoft.compute/virtualmachines'
    | project name, resourceGroup, tags
    """

    vm_tags = {}
    options = QueryRequestOptions(top=1000)
    while True:
        request = QueryRequest(subscriptions=[subscription_id], query=query, options=options)
        response = client.resources(request)
        for row in response.data:
            name = str(row.get("name", "")).lower()
            rg = str(row.get("resourceGroup", "")).lower()
            vm_tags[(rg, name)] = row.get("tags") or {}
        if not response.skip_token:
            break
        options = QueryRequestOptions(top=1000, skip_token=response.skip_token)
    return vm_tags


# ── Main ──────────────────────────────────────────────────────────────
def main():
    config = load_config(require_subscription=True)
    subscription_id = config["SUBSCRIPTION_ID"].strip()
    expected_vm_count = config.get("EXPECTED_VM_COUNT")

    # dtype=str + keep_default_na=False so a blank cell (e.g. Server) stays "" instead of becoming NaN -> "nan"
    ao_df = pd.read_csv(INPUT_CSV, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required_cols = [VM_NAME_COLUMN, RG_NAME_COLUMN, ACTIVE_COLUMN] + list(AO_COLUMN_MAP.values())
    missing_cols = [c for c in required_cols if c not in ao_df.columns]
    if missing_cols:
        print(f"!! Column(s) not found in {INPUT_CSV}: {', '.join(missing_cols)} — double check the column names",
              file=sys.stderr)
        sys.exit(1)

    azure_tags_by_vm = fetch_azure_vm_tags(subscription_id)
    print(f"VMs returned by Azure Resource Graph: {len(azure_tags_by_vm)}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    scanned = len(ao_df)
    if expected_vm_count is not None:
        if scanned != expected_vm_count:
            print(f"⚠️  Warning: VM count in {INPUT_CSV} = {scanned}, doesn't match the expected {expected_vm_count}")
        else:
            print(f"✓ VM count check: {scanned}/{expected_vm_count} OK")

    main_rows = []
    unrelated_rows = []
    summary_rows = []  # 1 row per VM
    compact_rows = []  # 1 row per VM, condensed
    case_counts = defaultdict(lambda: defaultdict(int))  # case_counts[field][case] = n
    high_risk_vms = set()
    not_found_in_azure = []
    risk_rank = {"None": 0, "Low": 1, "High": 2, "VM not found on Azure": 3}

    for _, row in ao_df.iterrows():
        vm_name = str(row[VM_NAME_COLUMN]).strip()
        rg_name = str(row[RG_NAME_COLUMN]).strip()
        department_ao = row.get("Department", "")
        note = ""

        active_flag = normalize_active_flag(row.get(ACTIVE_COLUMN, ""), vm_name)

        azure_tags = azure_tags_by_vm.get((rg_name.lower(), vm_name.lower()))
        if azure_tags is None:
            not_found_in_azure.append(vm_name)
            summary_row = {
                "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao, "Active_VM (AO)": active_flag,
                "Old_Project_Tag_Key": "", "Old_Project_Tag_Value": "",
            }
            compact_rows.append({
                "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao, "Active_VM (AO)": active_flag,
                "Overall_Risk": "VM not found on Azure",
                "Ready_To_Override": "VM not found — check whether it was deleted/renamed/moved to another RG before proceeding",
                "Issues": "VM not found on Azure — check whether it was deleted/renamed/moved to another RG",
                "Unrelated_Tags": "", "Note": note,
            })
            for field in FIELDS:
                ao_value = row.get(AO_COLUMN_MAP[field], "")
                main_rows.append({
                    "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao,
                    "Field": field, "AO_Value": ao_value,
                    "Azure_Key_Found": "", "Azure_Value": "",
                    "Case": "N/A", "Risk": "VM not found on Azure",
                    "Suggested_Action": "Check whether the VM was deleted/renamed/moved to another RG",
                    "Note": note,
                })
                summary_row[f"AO_{field}"] = ao_value
                summary_row[f"Azure_{field}_Key"] = ""
                summary_row[f"Azure_{field}_Value"] = ""
                summary_row[f"{field}_Case"] = "N/A"
                summary_row[f"{field}_Risk"] = "VM not found on Azure"
                summary_row[f"{field}_Action"] = "Check whether the VM was deleted/renamed/moved to another RG"
            summary_row["Overall_Risk"] = "VM not found on Azure"
            summary_row["Unrelated_Tags"] = ""
            summary_row["Note"] = note
            summary_rows.append(summary_row)
            continue

        summary_row = {
            "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao, "Active_VM (AO)": active_flag,
            "Old_Project_Tag_Key": "", "Old_Project_Tag_Value": "",
        }
        overall_risk = "None"
        issues_this_vm = []  # only fields that are case 3/3b/4
        has_conflict = False  # True if any field is case 4

        for field in FIELDS:
            ao_value = row.get(AO_COLUMN_MAP[field], "")
            case_num, azure_key, azure_value, risk, action = classify_field(ao_value, azure_tags, field)
            case_counts[field][case_num] += 1
            if case_num == 4:
                high_risk_vms.add(vm_name)
                has_conflict = True
                # project only: propose a concrete plan (keep old value under old-tag-project).
                # server is flag-only, no plan
                if field == "project":
                    plan = build_old_project_tag_plan(azure_tags, field)
                    if plan:
                        old_value_combined, matched_keys = plan
                        action = (
                            f"{action} | Suggested plan: set '{field}' = {ao_value!r}, "
                            f"move the old value to '{OLD_PROJECT_TAG_KEY}' = {old_value_combined!r}, "
                            f"delete all the old keys ({', '.join(matched_keys)}) — still needs review before applying as before"
                        )
                        summary_row["Old_Project_Tag_Key"] = OLD_PROJECT_TAG_KEY
                        summary_row["Old_Project_Tag_Value"] = old_value_combined
            if risk_rank.get(risk, 0) > risk_rank.get(overall_risk, 0):
                overall_risk = risk
            if case_num in (3, "3b", 4):  # only the cases that need a look
                issues_this_vm.append(f"{field}: case {case_num} ({risk})")

            main_rows.append({
                "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao,
                "Field": field, "AO_Value": ao_value,
                "Azure_Key_Found": azure_key, "Azure_Value": azure_value,
                "Case": case_num, "Risk": risk, "Suggested_Action": action, "Note": note,
            })
            summary_row[f"AO_{field}"] = ao_value
            summary_row[f"Azure_{field}_Key"] = azure_key
            summary_row[f"Azure_{field}_Value"] = azure_value
            summary_row[f"{field}_Case"] = case_num
            summary_row[f"{field}_Risk"] = risk
            summary_row[f"{field}_Action"] = action

        # case 5: other tags that don't match any of the 5 fields — uses the actual matches found
        # (find_matching_keys) so it stays consistent with the pattern matching
        matched_keys_this_vm = set()
        for f in FIELDS:
            std_hits, alias_hits_f = find_matching_keys(azure_tags, f)
            matched_keys_this_vm.update(std_hits)
            matched_keys_this_vm.update(alias_hits_f)
        vm_unrelated = []
        for k, v in azure_tags.items():
            if k not in matched_keys_this_vm:
                unrelated_rows.append({"VM": vm_name, "RG": rg_name, "Tag_Key": k, "Tag_Value": v})
                vm_unrelated.append(f"{k}={v}")

        summary_row["Overall_Risk"] = overall_risk
        summary_row["Unrelated_Tags"] = "; ".join(vm_unrelated)
        summary_row["Note"] = note
        summary_rows.append(summary_row)

        compact_rows.append({
            "VM": vm_name, "RG": rg_name, "Department (AO)": department_ao, "Active_VM (AO)": active_flag,
            "Overall_Risk": overall_risk,
            "Ready_To_Override": "Yes" if not has_conflict else "Needs review (see Issues)",
            "Issues": "; ".join(issues_this_vm),
            "Unrelated_Tags": "; ".join(vm_unrelated),
            "Note": note,
        })

    # ── write output ─────────────────────────────────────────────────
    with open(OUTPUT_MAIN_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "VM", "RG", "Department (AO)", "Field", "AO_Value",
            "Azure_Key_Found", "Azure_Value", "Case", "Risk", "Suggested_Action", "Note"
        ])
        writer.writeheader()
        writer.writerows(main_rows)

    with open(OUTPUT_UNRELATED_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["VM", "RG", "Tag_Key", "Tag_Value"])
        writer.writeheader()
        writer.writerows(unrelated_rows)

    summary_fieldnames = ["VM", "RG", "Department (AO)", "Active_VM (AO)"]
    for field in FIELDS:
        summary_fieldnames += [
            f"AO_{field}", f"Azure_{field}_Key", f"Azure_{field}_Value",
            f"{field}_Case", f"{field}_Risk", f"{field}_Action",
        ]
    summary_fieldnames += [
        "Old_Project_Tag_Key", "Old_Project_Tag_Value",  # blank except for VMs where project fell into case 4
        "Overall_Risk", "Unrelated_Tags", "Note",
    ]
    with open(OUTPUT_SUMMARY_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=summary_fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    compact_fieldnames = ["VM", "RG", "Department (AO)", "Active_VM (AO)", "Overall_Risk",
                          "Ready_To_Override", "Issues", "Unrelated_Tags", "Note"]
    with open(OUTPUT_SUMMARY_COMPACT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=compact_fieldnames)
        writer.writeheader()
        writer.writerows(compact_rows)

    # ── console summary ─────────────────────────────────────────────
    print("\n=== VM Tag Gap Check Summary ===")
    print(f"VM actually found on Azure (out of {scanned} VM in the input): "
          f"{scanned - len(not_found_in_azure)} found / {len(not_found_in_azure)} not found")
    if not_found_in_azure:
        print(f"  Not found: {', '.join(not_found_in_azure)}")

    case_labels = {
        1: "no tag", 2: "exact match", 3: "key doesn't match but value does",
        "3b": "different format, same real value (owner name/email, department abbreviation, project/server substring/abbreviation)",
        4: "conflicting value",
        "AO blank": "AO left it blank, skipped",
    }
    for field in FIELDS:
        print(f"\n[{field}]")
        for case_num in [1, 2, 3, "3b", 4, "AO blank"]:
            n = case_counts[field][case_num]
            if case_num == "AO blank" and n == 0:
                continue
            print(f"  case {case_num} ({case_labels[case_num]}): {n} VM")

    inactive_count = sum(1 for r in summary_rows if r.get("Active_VM (AO)") == "Inactive")
    print(f"\nVM marked Inactive (from the '{ACTIVE_COLUMN}' column in {INPUT_CSV}): {inactive_count}")
    unknown_count = sum(1 for r in summary_rows if r.get("Active_VM (AO)") == "Unknown")
    if unknown_count:
        print(f"VM where '{ACTIVE_COLUMN}' is Unknown (blank/unrecognized values also land here): {unknown_count}")

    print(f"\nVM with at least 1 high-risk (case 4) field: {len(high_risk_vms)}")
    if high_risk_vms:
        print(f"  {', '.join(sorted(high_risk_vms))}")

    unique_unrelated_keys = {r["Tag_Key"] for r in unrelated_rows}
    print(f"\nUnusual tag keys found (case 5, unrelated to any of the 5 fields): {len(unique_unrelated_keys)} unique names "
          f"({len(unrelated_rows)} rows total)")
    if unique_unrelated_keys:
        print(f"  {', '.join(sorted(unique_unrelated_keys))}")
        print(f"  See full details in {OUTPUT_UNRELATED_CSV}")

    print(f"\nOutput: {OUTPUT_MAIN_CSV}, {OUTPUT_UNRELATED_CSV}, {OUTPUT_SUMMARY_CSV}, {OUTPUT_SUMMARY_COMPACT_CSV}")


if __name__ == "__main__":
    main()
