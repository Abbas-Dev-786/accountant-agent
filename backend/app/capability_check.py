"""Run the Phase 0 operator readiness check.

Provider adapters or operators produce baseline/evidence JSON from actual demo
calls. This command validates that evidence; it never contacts a provider and
therefore cannot fabricate a successful capability spike.
"""

from __future__ import annotations

import argparse
import json

from .scenario import (
    CapabilityEvidence,
    ScenarioError,
    DemoScenario,
    XeroBaselineObservation,
    readiness_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AccountingOS Phase 0 demo evidence")
    parser.add_argument("--scenario", help="Path to versioned scenario manifest")
    parser.add_argument("--xero-baseline", required=True, help="Path to observed Xero baseline JSON")
    parser.add_argument("--evidence", help="Path to provider capability evidence JSON")
    parser.add_argument(
        "--print-xero-fingerprint",
        action="store_true",
        help="Print the baseline fingerprint for the supplied observation and exit",
    )
    args = parser.parse_args()

    if args.print_xero_fingerprint:
        try:
            baseline = XeroBaselineObservation.load(args.xero_baseline)
        except ScenarioError as exc:
            print(json.dumps({"detail": str(exc)}))
            return 1
        print(json.dumps({"xero_baseline_fingerprint": baseline.fingerprint}, sort_keys=True))
        return 0

    if not args.scenario or not args.evidence:
        parser.error("--scenario and --evidence are required unless --print-xero-fingerprint is used")
    try:
        report = readiness_report(
            DemoScenario.load(args.scenario),
            XeroBaselineObservation.load(args.xero_baseline),
            CapabilityEvidence.load_all(args.evidence),
        )
    except ScenarioError as exc:
        print(json.dumps({"ready": False, "detail": str(exc)}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
