from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import ROOT_DIR


RULEBOOK_VERSION = "MAE-FINANCIAL-MAPPING-2.0.0"
STRICT_CONTRACT_MAJOR = "2"
REQUIRED_FINANCIAL_GATES = ("asset", "row", "region", "direction", "horizon")
ALL_GATE_NAMES = ("document", *REQUIRED_FINANCIAL_GATES, "evidence", "source")
CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
ALLOWED_DIRECTIONS = {"BULLISH", "BEARISH", "NEUTRAL"}
ALLOWED_HORIZON = "MEDIUM_6_12M"
REJECT_REASONS = {
    "WRONG_ASSET_CLASS",
    "WRONG_SEGMENT",
    "WRONG_REGION",
    "IRRELEVANT_DOCUMENT",
}
UNSCORED_REASONS = {
    "UNSUPPORTED_DIRECTION",
    "HORIZON_MISMATCH",
    "INSUFFICIENT_EVIDENCE",
}


@dataclass(frozen=True)
class StrictContractPaths:
    playbook: Path = ROOT_DIR / "FINANCIAL_MAPPING_PLAYBOOK_v2_APPROVED.md"
    source_policy: Path = ROOT_DIR / "data" / "source_governance_v2.json"
    fixture: Path = ROOT_DIR / "tests" / "fixtures" / "financial_mapping_v2.json"


class StrictFinancialContractError(ValueError):
    """Raised when strict input or pinned contract artifacts are malformed."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_contract(paths: StrictContractPaths | None = None) -> dict[str, Any]:
    paths = paths or StrictContractPaths()
    for path in (paths.playbook, paths.source_policy, paths.fixture):
        if not path.is_file():
            raise StrictFinancialContractError(f"strict contract artifact is missing: {path}")

    playbook = paths.playbook.read_text(encoding="utf-8")
    if RULEBOOK_VERSION not in playbook:
        raise StrictFinancialContractError(f"playbook does not declare {RULEBOOK_VERSION}")
    if "does **not** constitute financial or analyst approval" not in playbook:
        raise StrictFinancialContractError("playbook analyst-approval disclaimer is missing")

    policy = json.loads(paths.source_policy.read_text(encoding="utf-8"))
    fixture = json.loads(paths.fixture.read_text(encoding="utf-8"))
    validate_source_policy(policy)
    validate_fixture_payload(fixture, validate_cases=False)
    return {
        "rulebook_version": RULEBOOK_VERSION,
        "source_policy_version": policy["policy_version"],
        "fixture_version": fixture["fixture_version"],
        "hashes": {
            "playbook_sha256": sha256_file(paths.playbook),
            "source_policy_sha256": sha256_file(paths.source_policy),
            "fixture_sha256": sha256_file(paths.fixture),
        },
        "policy": policy,
    }


def validate_source_policy(policy: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "policy_version",
        "approval_scope",
        "allowed_roles",
        "tiers",
        "allowed_lifecycle_states",
        "admission_lifecycle_states",
        "required_source_fields",
        "admission_review_decisions",
        "evidence_modes",
    }
    missing = sorted(required - set(policy))
    if missing:
        raise StrictFinancialContractError(f"source policy missing fields: {missing}")
    if "does not approve any snapshot" not in str(policy["approval_scope"]):
        raise StrictFinancialContractError("source policy approval scope is unsafe")
    expected_modes = {"DIRECT", "COMPOSITE", "ANCHOR"}
    if set(policy["evidence_modes"]) != expected_modes:
        raise StrictFinancialContractError("source policy must define exactly DIRECT, COMPOSITE, and ANCHOR")
    for mode, rule in policy["evidence_modes"].items():
        mode_fields = {
            "minimum_approved_sources",
            "minimum_independent_institutions",
            "allowed_roles",
            "allowed_tiers",
            "maximum_confidence",
            "absolute_score_cap",
            "required_disclosures",
        }
        absent = sorted(mode_fields - set(rule))
        if absent:
            raise StrictFinancialContractError(f"{mode} source policy missing fields: {absent}")
        if rule["maximum_confidence"] not in CONFIDENCE_RANK:
            raise StrictFinancialContractError(f"{mode} has invalid confidence cap")


def validate_mapping_payload(payload: dict[str, Any]) -> None:
    required = {
        "case_id",
        "document_type",
        "publication_title",
        "exact_quote",
        "context",
        "evidence_mode",
        "confidence",
        "sources",
        "disclosures",
        "targets",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise StrictFinancialContractError(f"mapping payload missing fields: {missing}")
    if not isinstance(payload["case_id"], str) or not payload["case_id"].strip():
        raise StrictFinancialContractError("case_id must be a non-empty string")
    if payload["evidence_mode"] not in {"DIRECT", "COMPOSITE", "ANCHOR"}:
        raise StrictFinancialContractError("evidence_mode must be DIRECT, COMPOSITE, or ANCHOR")
    if payload["confidence"] not in CONFIDENCE_RANK:
        raise StrictFinancialContractError("confidence must be LOW, MEDIUM, or HIGH")
    if not isinstance(payload["sources"], list) or not payload["sources"]:
        raise StrictFinancialContractError("sources must contain at least one source record")
    if not isinstance(payload["disclosures"], dict):
        raise StrictFinancialContractError("disclosures must be an object")
    if not isinstance(payload["targets"], list) or not payload["targets"]:
        raise StrictFinancialContractError("targets must contain at least one explicit target")

    target_ids: set[str] = set()
    for target in payload["targets"]:
        target_required = {
            "target_id",
            "canonical_row",
            "asset_class",
            "region",
            "direction",
            "horizon",
            "proposed_score",
        }
        target_missing = sorted(target_required - set(target))
        if target_missing:
            raise StrictFinancialContractError(f"target missing fields: {target_missing}")
        if target["target_id"] in target_ids:
            raise StrictFinancialContractError(f"duplicate target_id: {target['target_id']}")
        target_ids.add(target["target_id"])
        if target["direction"] not in ALLOWED_DIRECTIONS:
            raise StrictFinancialContractError(f"invalid target direction: {target['direction']}")
        if isinstance(target["proposed_score"], bool) or not isinstance(target["proposed_score"], int):
            raise StrictFinancialContractError("proposed_score must be an integer")


def validate_fixture_payload(payload: dict[str, Any], *, validate_cases: bool = True) -> None:
    required = {"schema_version", "fixture_version", "rulebook_version", "cases"}
    missing = sorted(required - set(payload))
    if missing:
        raise StrictFinancialContractError(f"fixture missing fields: {missing}")
    if not str(payload["schema_version"]).startswith(f"{STRICT_CONTRACT_MAJOR}."):
        raise StrictFinancialContractError("fixture schema must be strict contract major version 2")
    if payload["rulebook_version"] != RULEBOOK_VERSION:
        raise StrictFinancialContractError("fixture rulebook version mismatch")
    if not isinstance(payload["cases"], list) or not payload["cases"]:
        raise StrictFinancialContractError("fixture cases must be a non-empty list")
    if len({case.get("case_id") for case in payload["cases"]}) != len(payload["cases"]):
        raise StrictFinancialContractError("fixture case_id values must be unique")
    if validate_cases:
        for case in payload["cases"]:
            validate_mapping_payload(case)
            if "expected" not in case:
                raise StrictFinancialContractError(f"fixture case has no expected result: {case.get('case_id')}")


def evaluate_mapping(payload: dict[str, Any], paths: StrictContractPaths | None = None) -> dict[str, Any]:
    """Evaluate a mapping with no writes and one complete result per explicit target."""
    validate_mapping_payload(payload)
    contract = load_contract(paths)
    policy = contract.pop("policy")
    text = _normalize(" ".join((payload["publication_title"], payload["exact_quote"], payload["context"])))
    source_gate = _source_gate(payload, policy)
    evidence_gate = _evidence_gate(payload, policy, source_gate)
    document_gate = _document_gate(payload, text)

    target_results = []
    for target in payload["targets"]:
        gates = {
            "document": document_gate,
            "asset": _asset_gate(target, text),
            "row": _row_gate(target, text),
            "region": _region_gate(target, text),
            "direction": _direction_gate(target, text),
            "horizon": _horizon_gate(target, text),
            "evidence": evidence_gate,
            "source": source_gate,
        }
        reason_code = _primary_reason(gates)
        decision = _decision_for_reason(reason_code)
        admitted = decision == "ADMITTED"
        target_result = {
            "target_id": target["target_id"],
            "canonical_row": target["canonical_row"],
            "region": target["region"],
            "requested_direction": target["direction"],
            "admission_decision": decision,
            "financial_status": "TRUE_PASS" if admitted else reason_code,
            "reason_code": reason_code,
            "admitted": admitted,
            "score": target["proposed_score"] if admitted else None,
            "confidence": payload["confidence"] if admitted else None,
            "evidence_mode": payload["evidence_mode"],
            "gates": gates,
            "auto_remap_applied": False,
            "suggested_target": None,
        }
        target_result["result_hash"] = canonical_hash(target_result)
        target_results.append(target_result)

    result = {
        "strict_contract": True,
        "case_id": payload["case_id"],
        "is_demo": bool(payload.get("is_demo", False)),
        "contract": contract,
        "target_results": target_results,
        "all_targets_admitted": all(row["admitted"] for row in target_results),
        "human_approval_created": False,
        "release_authorized": False,
    }
    result["output_hash"] = canonical_hash(result)
    return result


def evaluate_fixture_file(paths: StrictContractPaths | None = None) -> dict[str, Any]:
    paths = paths or StrictContractPaths()
    fixture = json.loads(paths.fixture.read_text(encoding="utf-8"))
    validate_fixture_payload(fixture)
    results = []
    mismatches = []
    for case in fixture["cases"]:
        result = evaluate_mapping(case, paths)
        results.append(result)
        expected_targets = case["expected"].get("targets", [])
        actual_by_id = {row["target_id"]: row for row in result["target_results"]}
        if len(expected_targets) != len(actual_by_id):
            mismatches.append(f"{case['case_id']}: target count")
            continue
        for expected in expected_targets:
            actual = actual_by_id.get(expected["target_id"])
            if actual is None:
                mismatches.append(f"{case['case_id']}: missing {expected['target_id']}")
                continue
            for key in ("admission_decision", "reason_code", "financial_status"):
                if actual[key] != expected[key]:
                    mismatches.append(
                        f"{case['case_id']}/{expected['target_id']}: {key}={actual[key]} expected={expected[key]}"
                    )
            for gate_name, passed in expected.get("gates", {}).items():
                if actual["gates"][gate_name]["passed"] is not passed:
                    mismatches.append(
                        f"{case['case_id']}/{expected['target_id']}: gate {gate_name}="
                        f"{actual['gates'][gate_name]['passed']} expected={passed}"
                    )
    return {
        "fixture_version": fixture["fixture_version"],
        "case_count": len(fixture["cases"]),
        "target_count": sum(len(result["target_results"]) for result in results),
        "passed": not mismatches,
        "mismatches": mismatches,
        "results": results,
        "output_hash": canonical_hash([result["output_hash"] for result in results]),
    }


def assert_strict_contract(result: dict[str, Any]) -> None:
    """Fail closed when a caller presents a legacy/permissive admission result."""
    if result.get("strict_contract") is not True:
        raise StrictFinancialContractError("legacy or unmarked validator result is forbidden")
    contract = result.get("contract") or {}
    if contract.get("rulebook_version") != RULEBOOK_VERSION:
        raise StrictFinancialContractError("strict rulebook version mismatch")
    hashes = contract.get("hashes") or {}
    if set(hashes) != {"playbook_sha256", "source_policy_sha256", "fixture_sha256"}:
        raise StrictFinancialContractError("strict contract artifact hashes are incomplete")
    if any(not re.fullmatch(r"[0-9a-f]{64}", value or "") for value in hashes.values()):
        raise StrictFinancialContractError("strict contract artifact hash is invalid")
    if not result.get("target_results"):
        raise StrictFinancialContractError("strict result has no target decisions")
    for target in result["target_results"]:
        gates = target.get("gates") or {}
        if any(name not in gates for name in ALL_GATE_NAMES):
            raise StrictFinancialContractError("strict result omits required gates")
        if target.get("auto_remap_applied") is not False or target.get("suggested_target") is not None:
            raise StrictFinancialContractError("automatic remapping is forbidden")


def _gate(name: str, passed: bool, code: str, detail: str, observed: Any = None) -> dict[str, Any]:
    return {"name": name, "passed": passed, "code": code, "detail": detail, "observed": observed}


def _document_gate(payload: dict[str, Any], text: str) -> dict[str, Any]:
    document_type = _normalize(payload["document_type"]).replace(" ", "_")
    forbidden_types = {"navigation", "marketing", "corporate_news", "press_release", "service_page"}
    forbidden_terms = {
        "accept cookies",
        "skip to content",
        "subscribe to our newsletter",
        "all rights reserved",
        "our products and services",
        "careers",
    }
    passed = document_type not in forbidden_types and not any(term in text for term in forbidden_terms)
    return _gate(
        "document",
        passed,
        "PASS" if passed else "IRRELEVANT_DOCUMENT",
        "investable document type and body evidence" if passed else "document is navigation, marketing, corporate, or boilerplate",
        document_type,
    )


def _asset_gate(target: dict[str, Any], text: str) -> dict[str, Any]:
    row_asset = target["canonical_row"].split("|", 1)[0].upper()
    requested = target["asset_class"].upper()
    detected = _detected_assets(text)
    passed = requested == row_asset and requested in detected and len(detected) == 1
    return _gate(
        "asset",
        passed,
        "PASS" if passed else "WRONG_ASSET_CLASS",
        "one explicit asset class matches the canonical row" if passed else "asset is absent, ambiguous, or conflicts with the row",
        sorted(detected),
    )


def _row_gate(target: dict[str, Any], text: str) -> dict[str, Any]:
    row = target["canonical_row"]
    patterns: dict[str, tuple[str, ...]] = {
        "EQUITY|Wide Market|Wide Market": (
            "broad equity",
            "equity market",
            "equity benchmark",
            "equity index",
            "equities in 2026",
            "constructive outlook for equities",
            "across sectors and regions",
        ),
        "EQUITY|Other categories|Market Breadth (Equal Weight)": (
            "market breadth",
            "equal weight",
            "equal-weight",
            "broad participation",
            "participation broadens",
        ),
        "EQUITY|Other categories|Growth": ("growth equities", "growth stocks", "growth style", "growth factor"),
        "EQUITY|Other categories|Value": ("value equities", "value stocks", "value style", "value factor"),
        "EQUITY|Other categories|Small Cap": ("small cap", "small-cap", "smaller listed companies"),
        "EQUITY|Other categories|Preferred": ("preferred securities", "preferred equity", "hybrid capital"),
        "FIXED INCOME|GOV|Short Term": ("front end", "front-end", "2 year", "2-year", "short term government"),
        "FIXED INCOME|GOV|Mid Term": ("intermediate maturity", "intermediate-maturity", "curve belly", "belly of the curve"),
        "FIXED INCOME|GOV|Long Term": ("long end", "long-end", "10 year", "10-year", "long duration government"),
        "FIXED INCOME|CORP IG|Short Term": ("short term investment grade", "short-term investment-grade"),
        "FIXED INCOME|CORP IG|Mid Term": ("investment grade", "investment-grade", "ig credit", "ig spreads"),
        "FIXED INCOME|CORP IG|Long Term": ("long term investment grade", "long-term investment-grade"),
        "FIXED INCOME|CORP HY|Short Term": ("short term high yield", "short-term high-yield"),
        "FIXED INCOME|CORP HY|Mid Term": ("high yield", "high-yield", "hy credit", "default cycle"),
        "FIXED INCOME|CORP HY|Long Term": ("long term high yield", "long-term high-yield"),
        "FIXED INCOME|Other categories|Inflation Linked": ("inflation linked", "inflation-linked", "breakeven", "real yield"),
        "FIXED INCOME|Other categories|Mortgage-Backed": ("mortgage backed", "mortgage-backed", "mbs", "prepayment"),
        "COMMODITIES|Commodities|Gold": ("gold", "bullion"),
        "COMMODITIES|Commodities|Other precious metals": ("silver", "platinum", "palladium"),
    }
    matched = [term for term in patterns.get(row, ()) if term in text]
    passed = bool(matched)
    return _gate(
        "row",
        passed,
        "PASS" if passed else "WRONG_SEGMENT",
        "canonical segment/style/maturity is explicit" if passed else "target row is not explicitly supported",
        matched,
    )


def _region_gate(target: dict[str, Any], text: str) -> dict[str, Any]:
    detected = _detected_regions(text)
    requested = target["region"]
    if len(detected) > 1:
        return _gate(
            "region",
            False,
            "MANUAL_REVIEW_REQUIRED",
            "multiple explicit regions require an independent thesis per target; Global fallback is forbidden",
            sorted(detected),
        )
    passed = detected == {requested}
    return _gate(
        "region",
        passed,
        "PASS" if passed else "WRONG_REGION",
        "quoted thesis explicitly matches the target region" if passed else "target region is absent or conflicts; Global is not inferred",
        sorted(detected),
    )


def _direction_gate(target: dict[str, Any], text: str) -> dict[str, Any]:
    positive = _matched_terms(
        text,
        ("constructive", "overweight", "outperform", "attractive", "positive total return", "should benefit", "extend rally"),
    )
    negative = _matched_terms(
        text,
        ("underweight", "underperform", "unattractive", "negative total return", "downside", "retreat", "should fall"),
    )
    neutral = _matched_terms(
        text,
        ("explicitly neutral", "remain unchanged", "range bound", "range-bound", "balanced view", "market weight", "neither overweight nor underweight"),
    )
    inferred: str | None = None
    if neutral and not positive and not negative:
        inferred = "NEUTRAL"
    elif positive and not negative and not neutral:
        inferred = "BULLISH"
    elif negative and not positive and not neutral:
        inferred = "BEARISH"
    score = target["proposed_score"]
    score_consistent = (
        (target["direction"] == "BULLISH" and score > 0)
        or (target["direction"] == "BEARISH" and score < 0)
        or (target["direction"] == "NEUTRAL" and score == 0)
    )
    passed = inferred == target["direction"] and score_consistent
    return _gate(
        "direction",
        passed,
        "PASS" if passed else "UNSUPPORTED_DIRECTION",
        "explicit direction and score sign agree" if passed else "direction is absent, mixed, or inconsistent; unknown is not neutral",
        {"inferred": inferred, "positive": positive, "negative": negative, "neutral": neutral, "score": score},
    )


def _horizon_gate(target: dict[str, Any], text: str) -> dict[str, Any]:
    matched = _matched_terms(
        text,
        ("6 12 month", "6-12 month", "six to twelve month", "12 month", "12-month", "year ahead", "year-ahead", "next year", "in 2026", "2026 outlook"),
    )
    passed = target["horizon"] == ALLOWED_HORIZON and bool(matched)
    return _gate(
        "horizon",
        passed,
        "PASS" if passed else "HORIZON_MISMATCH",
        "explicit MAE 6-12 month horizon" if passed else "MAE 6-12 month horizon is not explicit",
        matched,
    )


def _source_gate(payload: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    mode = payload["evidence_mode"]
    mode_rule = policy["evidence_modes"][mode]
    issues: list[str] = []
    approved_sources = []
    required_fields = set(policy["required_source_fields"])
    for index, source in enumerate(payload["sources"]):
        missing = sorted(field for field in required_fields if field not in source or source[field] in (None, ""))
        if missing:
            issues.append(f"source[{index}] missing {','.join(missing)}")
            continue
        if source["role"] not in policy["allowed_roles"] or source["role"] not in mode_rule["allowed_roles"]:
            issues.append(f"source[{index}] role is not eligible for {mode}")
        if source["tier"] not in mode_rule["allowed_tiers"]:
            issues.append(f"source[{index}] tier is not admission eligible")
        if source["lifecycle"] not in policy["admission_lifecycle_states"]:
            issues.append(f"source[{index}] lifecycle is not active")
        if source["review_decision"] not in policy["admission_review_decisions"]:
            issues.append(f"source[{index}] methodology review is not accepted")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source["content_hash"])):
            issues.append(f"source[{index}] content hash is not SHA-256")
        for timestamp in ("retrieved_at", "reviewed_at"):
            if not _timezone_aware_iso(str(source[timestamp])):
                issues.append(f"source[{index}] {timestamp} is not timezone-aware ISO-8601")
        if not issues or not any(issue.startswith(f"source[{index}]") for issue in issues):
            approved_sources.append(source)

    if len(approved_sources) < int(mode_rule["minimum_approved_sources"]):
        issues.append("minimum approved source count not met")
    institutions = {source["institution"].strip().casefold() for source in approved_sources}
    if len(institutions) < int(mode_rule["minimum_independent_institutions"]):
        issues.append("minimum independent institution count not met")
    passed = not issues
    return _gate(
        "source",
        passed,
        "PASS" if passed else "INSUFFICIENT_EVIDENCE",
        "source provenance satisfies role/tier/lifecycle/rights/retrieval/hash/reviewer policy" if passed else "; ".join(issues),
        {"mode": mode, "approved_sources": len(approved_sources), "institutions": len(institutions)},
    )


def _evidence_gate(payload: dict[str, Any], policy: dict[str, Any], source_gate: dict[str, Any]) -> dict[str, Any]:
    mode_rule = policy["evidence_modes"][payload["evidence_mode"]]
    missing_disclosures = [
        name for name in mode_rule["required_disclosures"] if not str(payload["disclosures"].get(name, "")).strip()
    ]
    quote = str(payload["exact_quote"]).strip()
    confidence_ok = CONFIDENCE_RANK[payload["confidence"]] <= CONFIDENCE_RANK[mode_rule["maximum_confidence"]]
    score_ok = all(abs(target["proposed_score"]) <= int(mode_rule["absolute_score_cap"]) for target in payload["targets"])
    issues = []
    if len(quote) < 40:
        issues.append("exact body quote is too short")
    if missing_disclosures:
        issues.append(f"missing disclosures: {','.join(missing_disclosures)}")
    if not confidence_ok:
        issues.append(f"confidence exceeds {payload['evidence_mode']} cap")
    if not score_ok:
        issues.append(f"score exceeds {payload['evidence_mode']} cap")
    if not source_gate["passed"]:
        issues.append("source gate failed")
    passed = not issues
    return _gate(
        "evidence",
        passed,
        "PASS" if passed else "INSUFFICIENT_EVIDENCE",
        "exact evidence, disclosures, confidence, and score cap pass" if passed else "; ".join(issues),
        {
            "mode": payload["evidence_mode"],
            "score_cap": mode_rule["absolute_score_cap"],
            "confidence_cap": mode_rule["maximum_confidence"],
            "missing_disclosures": missing_disclosures,
        },
    )


def _primary_reason(gates: dict[str, dict[str, Any]]) -> str:
    for gate_name in ("document", "asset", "row", "region", "direction", "horizon", "evidence", "source"):
        gate = gates[gate_name]
        if not gate["passed"]:
            return str(gate["code"])
    return "NONE"


def _decision_for_reason(reason_code: str) -> str:
    if reason_code == "NONE":
        return "ADMITTED"
    if reason_code == "MANUAL_REVIEW_REQUIRED":
        return "MANUAL_REVIEW_REQUIRED"
    if reason_code in REJECT_REASONS:
        return "REJECTED"
    if reason_code in UNSCORED_REASONS:
        return "UNSCORED"
    raise StrictFinancialContractError(f"unknown reason code: {reason_code}")


def _detected_assets(text: str) -> set[str]:
    detected = set()
    if re.search(r"\b(equit(?:y|ies)|stocks?|s&p|listed companies|equity benchmark)\b", text):
        detected.add("EQUITY")
    if re.search(r"\b(fixed income|bonds?|yields?|duration|credit spreads?|government debt|treasur(?:y|ies))\b", text):
        detected.add("FIXED INCOME")
    if re.search(r"\b(commodit(?:y|ies)|gold|bullion|silver|platinum|palladium)\b", text):
        detected.add("COMMODITIES")
    return detected


def _detected_regions(text: str) -> set[str]:
    regions = set()
    if re.search(r"\b(global|worldwide|cross regional|cross-region)\b", text) or "across regions" in text:
        regions.add("Global")
    if re.search(r"\b(united states|u s|us equities|us market|federal reserve|s&p|nasdaq)\b", text):
        regions.add("US")
    if re.search(r"\b(europe|european|euro area|eurozone|ecb)\b", text):
        regions.add("Europe")
    if re.search(r"\b(united kingdom|uk market|uk equities|bank of england|boe)\b", text):
        regions.add("UK")
    if re.search(r"\b(japan|japanese|bank of japan|boj)\b", text):
        regions.add("Japan")
    if re.search(r"\b(em ex china|emerging markets ex china|emerging market ex china)\b", text):
        regions.add("EM ex China")
    return regions


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if term in text]


def _normalize(value: Any) -> str:
    text = str(value or "").casefold().replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9|&+\- ]+", " ", text)).strip()


def _timezone_aware_iso(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None

