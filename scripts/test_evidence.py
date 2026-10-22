#!/usr/bin/env python
"""
Write docs/TEST_EVIDENCE.md from the test suite.

    pytest accounts analytics farms notifications partners production \
        --junitxml=test-results/junit.xml
    python scripts/test_evidence.py --junit test-results/junit.xml [--commit <sha>]

1. Collects the suite with `pytest --collect-only -q` (no database needed)
   for the test counts per app and per file.
2. Reads the JUnit XML report of a full run for the result of every test,
   when the run happened and how long it took.
3. Maps each review concern to the tests that prove it (CONCERNS below) by
   node-id prefix. A selector that matches nothing stops the script, so a
   renamed or deleted test cannot drop out of the evidence unnoticed.

Standard library only; run it with the project's virtualenv active so the
`pytest` it calls is the project's.
"""
import argparse
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "docs" / "TEST_EVIDENCE.md"
APPS = ["accounts", "analytics", "farms", "notifications", "partners", "production"]

# Review concern -> selectors. A selector is a node-id prefix: a file, a
# class, a test function, or one parametrized case.
CONCERNS = OrderedDict([
    ("Failed-sync recovery", [
        "production/tests/test_house_scoping.py::TestBulkSync",
        "production/tests/test_inventory.py::TestBulkSyncIdempotency::test_one_bad_row_does_not_reject_the_good_ones",
        "production/tests/test_inventory.py::TestEditLock::test_bulk_sync_after_24h_reports_failed_with_id_not_500",
        "production/tests/test_sale_events.py::TestBulkSync::test_bulk_sync_reports_per_record_outcomes",
        "production/tests/test_stale_and_closed_writes.py::TestDailyRecordPastTheLock::test_the_rest_of_the_backlog_still_lands",
        "production/tests/test_stale_and_closed_writes.py::TestDailyRecordPastTheLock::test_bulk_sync_reports_it_failed_and_saves_nothing",
        "production/tests/test_access_lost_before_sync.py",
        "production/tests/test_daily_record_inventory_link.py::TestTransactionAtomicity",
        "production/tests/test_inventory_archive.py::TestOfflineUsageLogAcrossArchiving",
        "production/tests/test_tasks.py::TestHouseWriteScoping::test_bulk_sync_rejects_unassigned_house_per_record",
        "production/tests/test_sync_policy.py::TestBulkSyncLimits",
    ]),
    ("Duplicate submissions", [
        "production/tests/test_daily_record_idempotency.py",
        "production/tests/test_inventory.py::TestSinglePostIdempotency",
        "production/tests/test_inventory.py::TestBulkSyncIdempotency",
        "production/tests/test_sale_events.py::TestIdempotencyAndLock::test_a_duplicate_id_updates_rather_than_erroring",
        "production/tests/test_sale_events.py::TestBulkSync::test_bulk_sync_is_idempotent",
        "production/tests/test_tasks.py::TestIdempotency",
        "production/tests/test_daily_record_inventory_link.py::TestBulkSyncIdempotency",
        "production/tests/test_concurrent_uploads.py::TestSequentialRetry",
        "production/tests/test_sync_policy.py::TestTheDayIsFixed",
        "production/tests/test_sync_policy.py::TestDuplicatesAreAnsweredCleanly",
    ]),
    ("Concurrent uploads", [
        "production/tests/test_concurrent_uploads.py::TestSameUuidManyUploads",
        "production/tests/test_concurrent_uploads.py::TestDifferentUuidsSameBatchAndDate",
    ]),
    ("Cross-farm access", [
        "farms/tests/test_farm_id_substitution.py",
        "farms/tests/test_isolation.py::TestCrossFarmIsolation",
        "farms/tests/test_house_assignment.py::TestAssignmentEndpoint::test_house_from_another_farm_is_rejected",
        "accounts/tests/test_invitations.py::TestInvitationRevoke::test_cannot_revoke_invitation_from_another_farm",
        "analytics/tests/test_access.py::TestAnalyticsAccess::test_rival_owner_blocked",
        "analytics/tests/test_alert_history.py::TestHistoryAccess",
        "analytics/tests/test_alerts.py::TestAccess::test_member_of_another_farm_gets_403",
        "analytics/tests/test_reports.py::TestReportAccess::test_other_farms_member_is_forbidden",
        "analytics/tests/test_reports_xlsx.py::TestXLSXAccess::test_other_farms_member_is_forbidden",
        "partners/tests/test_buyer_management.py::TestRestoreBuyer::test_restoring_another_farms_link_is_404",
        "production/tests/test_feed_delivery_with_stock.py::test_item_from_another_farm_is_rejected",
        "production/tests/test_feed_sacks.py::TestSackEntry::test_item_from_another_farm_is_rejected",
        "production/tests/test_inventory.py::TestPermissions::test_worker_cannot_log_against_another_farms_item",
        "production/tests/test_payments.py::TestFarmIsolation",
        "production/tests/test_sale_events.py::TestSaleBuyerLink::test_a_buyer_linked_to_another_farm_is_rejected",
        "production/tests/test_tasks.py::TestCrossFarmIsolation",
    ]),
    ("Buyer isolation", [
        "partners/tests/test_buyer_two_farms.py",
        "partners/tests/test_my_purchases.py",
        "partners/tests/test_my_payments.py",
        "farms/tests/test_isolation.py::TestExternalPartners",
        "farms/tests/test_farm_registration.py",
        "analytics/tests/test_access.py::TestAnalyticsAccess::test_consumer_blocked",
    ]),
    ("PIN-change gate", [
        "farms/tests/test_rotation_gate.py",
        "accounts/tests/test_auth.py::TestRotationGate",
        "accounts/tests/test_auth.py::TestSessionRevocation::test_pin_change_kills_existing_sessions",
        "accounts/tests/test_login_throttle.py::test_pin_change_is_throttled_at_five_an_hour",
    ]),
    ("Login throttling", [
        "accounts/tests/test_login_throttle.py",
        "accounts/tests/test_admin_hardening.py::TestApiLoginUntouchedByAxes",
        "accounts/tests/test_admin_hardening.py::TestAdminLoginLockout",
    ]),
    ("The 24-hour lock", [
        "production/tests/test_stale_and_closed_writes.py::TestDailyRecordPastTheLock",
        "production/tests/test_stale_and_closed_writes.py::TestWeightSamplePastTheLock",
        "production/tests/test_stale_and_closed_writes.py::TestNamingALockedRecordFromAnUnlockedOne",
        "production/tests/test_sale_events.py::TestIdempotencyAndLock::test_a_sale_older_than_24_hours_cannot_be_edited",
        "production/tests/test_sale_events.py::TestSaleBuyerLink::test_changing_buyer_link_past_24_hours_is_locked_same_as_any_other_edit",
        "production/tests/test_sale_events.py::TestSaleBuyerLink::test_bulk_sync_cannot_change_buyer_link_on_a_locked_row",
        "production/tests/test_inventory.py::TestEditLock",
        "production/tests/test_tasks.py::TestTickAndUntick",
        "production/tests/test_sync_policy.py::TestServerTimeIsRecorded::test_the_lock_runs_from_receipt_not_from_the_typed_time",
    ]),
    ("KPI formulas (mortality, survival, FCR, feed margin; TC-25)", [
        "analytics/tests/test_kpi_tc25.py",
        "analytics/tests/test_calculations.py",
        "production/tests/test_sale_events.py::TestOthersSaleType::test_tc25_fcr_and_survival_unaffected_by_an_others_sale",
        "production/tests/test_sale_events.py::TestFcrEquivalence",
    ]),
    ("Closed or ended batches", [
        "production/tests/test_stale_and_closed_writes.py::test_an_ended_batch_refuses_every_field_write",
        "production/tests/test_harvest.py::test_closed_batch_rejects_new_daily_records",
        "production/tests/test_harvest.py::test_double_close_is_refused",
        "production/tests/test_sale_events.py::TestClosing::test_a_closed_batch_refuses_new_sales",
    ]),
    ("Permission matrix is current and true", [
        "farms/tests/test_permission_matrix.py",
    ]),
])


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def collect():
    """Every collected node id, in collection order."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts=", "-p", "no:cacheprovider", *APPS],
        cwd=ROOT, capture_output=True, text=True,
    )
    ids = [line.strip() for line in result.stdout.splitlines() if "::" in line]
    if not ids:
        sys.exit(f"pytest collected nothing:\n{result.stdout}\n{result.stderr}")
    return ids


def _junit_key(node_id):
    """'a/b/test_x.py::TestA::test_y[p]' -> 'a.b.test_x.TestA::test_y[p]', JUnit's classname::name."""
    path, *rest = node_id.split("::")
    module = path[:-3].replace("/", ".")
    return ".".join([module, *rest[:-1]]) + "::" + rest[-1]


def read_junit(path, ids):
    """(node id -> outcome, failure messages by node id, run attributes)."""
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    by_key = {_junit_key(i): i for i in ids}
    outcomes, messages = {}, {}
    for case in suite.iter("testcase"):
        key = f"{case.get('classname')}::{case.get('name')}"
        node = by_key.get(key, key)
        outcome = "passed"
        for tag in ("skipped", "failure", "error"):
            element = case.find(tag)
            if element is not None:
                outcome = {"skipped": "skipped", "failure": "failed", "error": "error"}[tag]
                if tag != "skipped":
                    messages[node] = (element.get("message") or "").splitlines()[0][:220] if element.get("message") else ""
        outcomes[node] = outcome
    return outcomes, messages, dict(suite.attrib)


def _function(node_id):
    """The node id without its parameter: one entry per test function."""
    return re.sub(r"\[.*\]$", "", node_id)


def _matches(node_id, selector):
    return node_id == selector or node_id.startswith(selector + "::") or node_id.startswith(selector + "[")


def _result_cell(counts):
    total = sum(counts.values())
    if not counts:
        return "not run"
    if counts.get("passed", 0) == total:
        return f"{total} passed"
    parts = [f"{counts[k]} {k}" for k in ("passed", "failed", "error", "skipped", "not run") if counts.get(k)]
    return ", ".join(parts)


def render(ids, outcomes, messages, run, commit=None):
    lines = []
    out = lines.append
    head = _git("rev-parse", "HEAD")
    tested = _git("rev-parse", commit) if commit else head
    # Uncommitted changes only matter when the run was on the working tree.
    dirty = tested == head and any(
        "docs/TEST_EVIDENCE.md" not in line for line in _git("status", "--porcelain").splitlines()
    )

    out("# Test evidence")
    out("")
    out("Generated by `python scripts/test_evidence.py --junit <report>` from a full run of")
    out("`pytest accounts analytics farms notifications partners production --junitxml=<report>`.")
    out("Do not edit by hand; rerun the suite and the script.")
    out("")

    totals = Counter(outcomes.get(i, "not run") for i in ids)
    out("## Full-suite result")
    out("")
    timestamp = run.get("timestamp", "")
    try:
        when = datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M %Z").strip()
    except ValueError:
        when = timestamp or "unknown"
    out("| | |")
    out("|---|---|")
    out(f"| Date | {when} |")
    out(f"| Commit tested | `{tested}`{' (with uncommitted changes)' if dirty else ''} |")
    out(f"| Python | {platform.python_version()} |")
    out(f"| Collected | {len(ids)} |")
    for key in ("passed", "failed", "error", "skipped", "not run"):
        if totals.get(key):
            out(f"| {key.capitalize()} | {totals[key]} |")
    if run.get("time"):
        minutes, seconds = divmod(round(float(run["time"])), 60)
        out(f"| Duration | {minutes} min {seconds} s |")
    out("")

    failing = [i for i in ids if outcomes.get(i) in ("failed", "error")]
    if failing:
        out("Failing tests, with the first line of each error.")
        out("")
        out("| Test | Message |")
        out("|---|---|")
        for node in failing:
            message = messages.get(node, "").replace("|", "\\|")
            out(f"| `{node}` | {message} |")
        out("")

    out("## Tests per app")
    out("")
    out("| App | Tests | Result |")
    out("|---|---|---|")
    by_app = defaultdict(Counter)
    for node in ids:
        by_app[node.split("/")[0]][outcomes.get(node, "not run")] += 1
    for app in APPS:
        counts = by_app.get(app, Counter())
        out(f"| {app} | {sum(counts.values())} | {_result_cell(counts)} |")
    out(f"| **total** | **{len(ids)}** | {_result_cell(totals)} |")
    out("")

    out("## Tests per file")
    out("")
    out("| File | Tests | Result |")
    out("|---|---|---|")
    by_file = OrderedDict()
    for node in ids:
        by_file.setdefault(node.split("::")[0], Counter())[outcomes.get(node, "not run")] += 1
    for path in sorted(by_file):
        counts = by_file[path]
        out(f"| `{path}` | {sum(counts.values())} | {_result_cell(counts)} |")
    out("")

    out("## Concerns")
    out("")
    out("Each review concern and the tests that prove it. A test can serve more than one concern.")
    out("")
    out("| Concern | Tests | Result | Files |")
    out("|---|---|---|---|")
    selected = OrderedDict()
    dead = [s for selectors in CONCERNS.values() for s in selectors if not any(_matches(i, s) for i in ids)]
    if dead:
        sys.exit(f"These CONCERNS selectors match no collected test: {dead}")
    for concern, selectors in CONCERNS.items():
        nodes = [i for i in ids if any(_matches(i, s) for s in selectors)]
        selected[concern] = nodes
        counts = Counter(outcomes.get(i, "not run") for i in nodes)
        files = sorted({n.split("::")[0] for n in nodes})
        out(f"| {concern} | {len(nodes)} | {_result_cell(counts)} | " + "<br>".join(f"`{f}`" for f in files) + " |")
    out("")

    for concern, nodes in selected.items():
        out(f"### {concern}")
        out("")
        out("| Test | Cases | Result |")
        out("|---|---|---|")
        grouped = OrderedDict()
        for node in nodes:
            grouped.setdefault(_function(node), Counter())[outcomes.get(node, "not run")] += 1
        for function, counts in grouped.items():
            out(f"| `{function}` | {sum(counts.values())} | {_result_cell(counts)} |")
        out("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--junit", required=True, type=Path, help="JUnit XML report of a full run")
    parser.add_argument("--commit", help="the commit the report was produced on (default: HEAD)")
    args = parser.parse_args()

    ids = collect()
    outcomes, messages, run = read_junit(args.junit, ids)
    unknown = sorted(set(outcomes) - set(ids))
    if unknown:
        sys.exit(f"The report has tests that are not collected now (stale report?): {unknown[:5]}")
    OUTPUT.write_text(render(ids, outcomes, messages, run, args.commit) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}: {len(ids)} tests")


if __name__ == "__main__":
    main()
