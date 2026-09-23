"""Fresh arithmetic fixtures for validator tests, not recorded research results.

The public project seed deliberately contains no audit history. These records
exercise validation of that history without importing any private research.
The arithmetic and exclusions below are self-contained test data; they make no
claim about the unresolved RH target or the inherited moment-program scaffold.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
STATE_PATH = REPO_ROOT / "projects" / "riemann_hypothesis" / "research_state.json"
TARGET_CLAIM = "claim.public.moment_program"
EXACT_CLAIM = "claim.fixture.square_at_two"
SUCCESSOR_CLAIM = "claim.fixture.successor_at_two"
PAUSED_CLAIM = "claim.fixture.paused_generalization"
RAW_SOURCE = "source.fixture.arithmetic_text"
PROCESS_RECEIPT = "receipt.fixture.process_only"
FIXTURE_PATH = "packages/research-core/tests/public_validator_fixture_support.py"


def _claim(claim_id: str, statement: str) -> dict:
    return {
        "id": claim_id,
        "title": "Synthetic arithmetic validator fixture",
        "statement": statement,
        "hypotheses": ["Arithmetic over the integers."],
        "status": "active",
        "claim_type": "exact_identity",
        "audit_verdict": "verified",
        "dag_role": "diagnostic",
        "rh_chain_status": "diagnostic",
        "depends_on": [],
        "implies": [],
        "citation_refs": [],
        "evidence_refs": [],
        "dag_node_refs": [],
        "dag_edge_refs": [],
        "proof_restrictions": ["No implication for RH or any general moment property."],
    }


def _gate(gate_id: str, scope: str, basis: str, evidence: str) -> dict:
    return {
        "id": gate_id,
        "title": "Synthetic exact arithmetic counterexample",
        "record_kind": "exact_counterexample",
        "exact_scope": scope,
        "basis": basis,
        "route_effect": "exclude_within_scope",
        "does_not_exclude": ["Other integer identities or any RH argument."],
        "revival_trigger": None,
        "status": "active",
        "audit_verdict": "verified",
        "proof_restrictions": ["Only the explicitly stated arithmetic proposition."],
        "evidence_refs": [evidence],
    }


def _receipt(receipt_id: str, receipt_kind: str, conclusion: str) -> dict:
    return {
        "id": receipt_id,
        "receipt_kind": receipt_kind,
        "date": "2026-01-01",
        "verdict": "Synthetic software test fixture.",
        "source_refs": [],
        "claim_refs": [],
        "evidence_refs": [],
        "tracked_receipt_path": FIXTURE_PATH,
        "conclusion": conclusion,
    }


def make_validator_state() -> dict:
    """Return independent seed data enriched only with these public fixtures."""
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    square = _claim(EXACT_CLAIM, "2 squared equals 4, by integer multiplication.")
    successor = _claim(SUCCESSOR_CLAIM, "2 + 1 = 3 and 3 is not equal to 2.")
    paused = _claim(PAUSED_CLAIM, "An unspecified generalization is still unproved.")
    paused.update(
        status="paused",
        claim_type="paused_branch",
        audit_verdict="unproved_gap",
        revival_trigger="Supply a precise proposition and an independently checkable argument.",
    )
    state["claims"].extend([square, successor, paused])

    square_gate = _gate(
        "counterexample.fixture.square_identity",
        "The proposition n squared equals n for every integer n.",
        "At n = 2, n squared = 4, whereas n = 2; 4 is not 2.",
        EXACT_CLAIM,
    )
    successor_gate = _gate(
        "counterexample.fixture.successor_identity",
        "The proposition n + 1 equals n for every integer n.",
        "At n = 2, n + 1 = 3, whereas n = 2; 3 is not 2.",
        SUCCESSOR_CLAIM,
    )
    paused_record = _gate(
        "counterexample.fixture.unfinished_search",
        "A synthetic search that has not established a counterexample.",
        "The search was paused without a mathematical result.",
        PROCESS_RECEIPT,
    )
    paused_record.update(
        record_kind="historical_non_gate",
        route_effect="no_inference",
        status="paused",
        audit_verdict="not_audited",
        revival_trigger="A new explicit search method becomes available.",
    )
    state["counterexamples"].extend([square_gate, successor_gate, paused_record])

    raw_text = b"Synthetic raw capture: 2 * 2 = 4.\n"
    state["sources"].append(
        {
            "id": RAW_SOURCE,
            "source_type": "synthetic_test_capture",
            "title": "Fixture raw arithmetic text; not an independent audit",
            "locator": FIXTURE_PATH,
            "sha256": hashlib.sha256(raw_text).hexdigest(),
        }
    )
    audit = _receipt(
        "receipt.fixture.arithmetic_audit",
        "audit",
        "The elementary integer equalities in this fixture can be checked directly.",
    )
    audit["claim_refs"] = [EXACT_CLAIM, SUCCESSOR_CLAIM]
    process = _receipt(
        PROCESS_RECEIPT,
        "process_audit",
        "The synthetic search paused; this receipt supplies no mathematical refutation.",
    )
    closeout = _receipt(
        "receipt.fixture.campaign_closeout",
        "campaign_closeout",
        "Exclude only the universal square identity refuted at n = 2.",
    )
    closeout.update(
        campaign_id="campaign.fixture.integer_identities",
        route_dispositions=[
            {
                "decision": "exclude",
                "scope": square_gate["exact_scope"],
                "basis": square_gate["basis"],
                "evidence_refs": [square_gate["id"]],
                "truth_effect": "scoped_exclusion",
                "non_inferences": ["This says nothing about other propositions or RH."],
                "revival_trigger": None,
            }
        ],
    )
    state["audit_receipts"].extend([audit, process, closeout])
    return state
