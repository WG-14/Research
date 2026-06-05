from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .canonical_decision import canonical_payload_hash, sha256_prefixed


def _json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    if raw is None:
        return {}
    try:
        loaded = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def open_read_only_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).absolute()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {
        str(row["name"]) if hasattr(row, "keys") else str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _valid_hash(value: object) -> bool:
    text = str(value or "").strip()
    return text.startswith("sha256:") and len(text.removeprefix("sha256:")) == 64


def _generic_risk_actual(decision: Mapping[str, object]) -> tuple[str, str]:
    evidence = dict(decision.get("evidence") or {})
    evidence_hash = canonical_payload_hash(evidence)
    payload_without_hash = {
        "evaluation_point": decision.get("evaluation_point"),
        "status": decision.get("status"),
        "reason_code": str(decision.get("reason_code") or ""),
        "reason": str(decision.get("reason") or ""),
        "allowed_actions": list(decision.get("allowed_actions") or []),
        "recommended_action": decision.get("recommended_action"),
        "risk_input_hash": str(decision.get("risk_input_hash") or ""),
        "risk_policy_hash": str(decision.get("risk_policy_hash") or ""),
        "risk_evidence_hash": evidence_hash,
        "effective_limits": dict(decision.get("effective_limits") or {}),
        "state_source": str(decision.get("state_source") or ""),
        "evidence": evidence,
    }
    return canonical_payload_hash(payload_without_hash), evidence_hash


def _portfolio_risk_actual(decision: Mapping[str, object]) -> tuple[str, str]:
    evidence = dict(decision.get("evidence") or {})
    evidence_hash = sha256_prefixed(evidence)
    payload_without_hash = {
        "schema_version": int(decision.get("schema_version") or 1),
        "evaluation_point": str(decision.get("evaluation_point") or ""),
        "status": str(decision.get("status") or ""),
        "reason_code": str(decision.get("reason_code") or ""),
        "reason": str(decision.get("reason") or ""),
        "portfolio_risk_policy_hash": str(decision.get("portfolio_risk_policy_hash") or ""),
        "portfolio_risk_input_hash": str(decision.get("portfolio_risk_input_hash") or ""),
        "portfolio_risk_evidence_hash": evidence_hash,
        "state_source": str(decision.get("state_source") or ""),
        "effective_limits": dict(decision.get("effective_limits") or {}),
        "evidence": evidence,
    }
    return sha256_prefixed(payload_without_hash), evidence_hash


def _stable_submit_plan_hash(payload: Mapping[str, object]) -> str:
    hash_input = {
        str(key): value
        for key, value in dict(payload).items()
        if key
        not in {
            "schema_version",
            "authority_label",
            "content_hash",
            "submit_plan_hash",
        }
        and not str(key).startswith("pre_submit_risk_")
    }
    return sha256_prefixed(hash_input)


def _layer_result(
    *,
    layer: str,
    replay_status: str,
    reason: str,
    expected_hash: str = "",
    actual_hash: str = "",
    policy_hash: str = "",
    input_hash: str = "",
    evidence_hash: str = "",
    state_source: str = "",
    risk_status: str = "",
    reason_code: str = "",
    mismatch_reason: str = "",
) -> dict[str, object]:
    return {
        "layer": layer,
        "replay_status": replay_status,
        "reason": reason,
        "expected_decision_hash": expected_hash,
        "actual_decision_hash": actual_hash,
        "policy_hash": policy_hash,
        "input_hash": input_hash,
        "evidence_hash": evidence_hash,
        "state_source": state_source,
        "risk_status": risk_status,
        "reason_code": reason_code,
        "mismatch_reason": mismatch_reason,
    }


def _verify_generic_layer(layer: str, decision: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(decision, Mapping) or not decision:
        return _layer_result(
            layer=layer,
            replay_status="not_applicable",
            reason=f"{layer}_risk_decision_not_recorded",
        )
    expected = str(decision.get("risk_decision_hash") or "").strip()
    policy_hash = str(decision.get("risk_policy_hash") or "").strip()
    input_hash = str(decision.get("risk_input_hash") or "").strip()
    stored_evidence_hash = str(decision.get("risk_evidence_hash") or "").strip()
    actual, actual_evidence_hash = _generic_risk_actual(decision)
    mismatch = ""
    status = "pass"
    if not _valid_hash(expected):
        status = "fail"
        mismatch = "stored_decision_hash_missing_or_malformed"
    elif expected != actual:
        status = "fail"
        mismatch = "decision_hash_mismatch"
    elif not _valid_hash(stored_evidence_hash):
        status = "fail"
        mismatch = "stored_evidence_hash_missing_or_malformed"
    elif stored_evidence_hash != actual_evidence_hash:
        status = "fail"
        mismatch = "evidence_hash_mismatch"
    return _layer_result(
        layer=layer,
        replay_status=status,
        reason="matched" if status == "pass" else "mismatch",
        expected_hash=expected,
        actual_hash=actual,
        policy_hash=policy_hash,
        input_hash=input_hash,
        evidence_hash=actual_evidence_hash,
        state_source=str(decision.get("state_source") or ""),
        risk_status=str(decision.get("status") or ""),
        reason_code=str(decision.get("reason_code") or ""),
        mismatch_reason=mismatch,
    )


def _verify_portfolio_layer(context: Mapping[str, object], target_payload: Mapping[str, object]) -> dict[str, object]:
    decision = target_payload.get("portfolio_risk_decision")
    if not isinstance(decision, Mapping):
        decision = context.get("portfolio_risk_decision")
    if not isinstance(decision, Mapping):
        return _layer_result(
            layer="portfolio",
            replay_status="not_applicable",
            reason="portfolio_risk_decision_not_recorded",
        )
    expected = str(decision.get("portfolio_risk_decision_hash") or decision.get("risk_decision_hash") or "")
    policy_hash = str(decision.get("portfolio_risk_policy_hash") or decision.get("risk_policy_hash") or "")
    input_hash = str(decision.get("portfolio_risk_input_hash") or decision.get("risk_input_hash") or "")
    stored_evidence_hash = str(decision.get("portfolio_risk_evidence_hash") or "")
    actual, actual_evidence_hash = _portfolio_risk_actual(decision)
    mismatch = ""
    status = "pass"
    if not _valid_hash(expected):
        status = "fail"
        mismatch = "stored_decision_hash_missing_or_malformed"
    elif expected != actual:
        status = "fail"
        mismatch = "decision_hash_mismatch"
    elif not _valid_hash(stored_evidence_hash):
        status = "fail"
        mismatch = "stored_evidence_hash_missing_or_malformed"
    elif stored_evidence_hash != actual_evidence_hash:
        status = "fail"
        mismatch = "evidence_hash_mismatch"
    return _layer_result(
        layer="portfolio",
        replay_status=status,
        reason="matched" if status == "pass" else "mismatch",
        expected_hash=expected,
        actual_hash=actual,
        policy_hash=policy_hash,
        input_hash=input_hash,
        evidence_hash=actual_evidence_hash,
        state_source=str(decision.get("state_source") or target_payload.get("portfolio_risk_state_source") or ""),
        risk_status=str(decision.get("status") or ""),
        reason_code=str(decision.get("reason_code") or ""),
        mismatch_reason=mismatch,
    )


def _extract_strategy_decision(context: Mapping[str, object]) -> dict[str, object] | None:
    direct = context.get("strategy_risk_decision")
    if isinstance(direct, Mapping):
        return dict(direct)
    for key in ("strategy_preferences", "allocation_contributions"):
        items = context.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and isinstance(item.get("strategy_risk_decision"), Mapping):
                    return dict(item["strategy_risk_decision"])  # type: ignore[index]
    for result in context.get("runtime_strategy_result_contexts") or []:
        if isinstance(result, Mapping) and result.get("strategy_risk_decision_hash"):
            return {
                "evaluation_point": "pre_decision",
                "status": result.get("strategy_risk_status"),
                "reason_code": result.get("strategy_risk_reason_code"),
                "reason": "",
                "allowed_actions": [],
                "recommended_action": None,
                "risk_input_hash": result.get("strategy_risk_input_hash"),
                "risk_policy_hash": result.get("strategy_risk_policy_hash"),
                "risk_evidence_hash": result.get("strategy_risk_evidence_hash"),
                "risk_decision_hash": result.get("strategy_risk_decision_hash"),
                "effective_limits": {},
                "state_source": result.get("strategy_risk_state_source"),
                "evidence": {},
            }
    return None


def _load_cycle(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    execution_plan_id: int | None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    context: dict[str, object] = {}
    target_payload: dict[str, object] = {}
    submit_payload: dict[str, object] = {}
    if decision_id is None and execution_plan_id is None:
        if not _table_exists(conn, "strategy_decisions"):
            return context, target_payload, submit_payload
        row = conn.execute(
            "SELECT id FROM strategy_decisions ORDER BY decision_ts DESC, id DESC LIMIT 1"
        ).fetchone()
        decision_id = None if row is None else int(row["id"])
    if decision_id is not None and _table_exists(conn, "strategy_decisions"):
        columns = _columns(conn, "strategy_decisions")
        select_cols = ["context_json"]
        if "portfolio_target_id" in columns:
            select_cols.append("portfolio_target_id")
        if "execution_plan_id" in columns:
            select_cols.append("execution_plan_id")
        row = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM strategy_decisions WHERE id=?",
            (int(decision_id),),
        ).fetchone()
        if row is not None:
            context = _json_object(row["context_json"])
            if "portfolio_target_id" in row.keys() and row["portfolio_target_id"] is not None:
                target_payload = _load_portfolio_target(conn, int(row["portfolio_target_id"]))
            if execution_plan_id is None and "execution_plan_id" in row.keys() and row["execution_plan_id"] is not None:
                execution_plan_id = int(row["execution_plan_id"])
    if not target_payload and isinstance(context.get("portfolio_target"), Mapping):
        target_payload = dict(context["portfolio_target"])  # type: ignore[index]
    if execution_plan_id is not None:
        submit_payload = _load_execution_submit_plan(conn, int(execution_plan_id))
    if not submit_payload:
        for key in ("target_submit_plan", "residual_submit_plan", "buy_submit_plan"):
            if isinstance(context.get(key), Mapping):
                submit_payload = dict(context[key])  # type: ignore[index]
                break
    return context, target_payload, submit_payload


def _load_portfolio_target(conn: sqlite3.Connection, portfolio_target_id: int) -> dict[str, object]:
    if not _table_exists(conn, "portfolio_target"):
        return {}
    row = conn.execute("SELECT target_json FROM portfolio_target WHERE id=?", (portfolio_target_id,)).fetchone()
    return {} if row is None else _json_object(row["target_json"])


def _load_execution_submit_plan(conn: sqlite3.Connection, execution_plan_id: int) -> dict[str, object]:
    if not _table_exists(conn, "execution_plan"):
        return {}
    row = conn.execute(
        "SELECT execution_submit_plan_json FROM execution_plan WHERE id=?",
        (execution_plan_id,),
    ).fetchone()
    return {} if row is None else _json_object(row["execution_submit_plan_json"])


def _verify_pre_submit_layer(submit_payload: Mapping[str, object]) -> dict[str, object]:
    decision = submit_payload.get("pre_submit_risk_decision")
    if not isinstance(decision, Mapping):
        if submit_payload and submit_payload.get("pre_submit_risk_required"):
            return _layer_result(
                layer="pre_submit",
                replay_status="fail",
                reason="pre_submit_risk_required_but_decision_missing",
                mismatch_reason="missing_required_source_material",
            )
        return _layer_result(
            layer="pre_submit",
            replay_status="not_applicable",
            reason="pre_submit_risk_decision_not_recorded",
        )
    result = _verify_generic_layer("pre_submit", decision)
    expected_plan_hash = str(submit_payload.get("submit_plan_hash") or "")
    actual_plan_hash = _stable_submit_plan_hash(submit_payload)
    if result["replay_status"] == "pass" and expected_plan_hash != actual_plan_hash:
        result["replay_status"] = "fail"
        result["reason"] = "mismatch"
        result["mismatch_reason"] = "submit_plan_hash_mismatch"
    if result["replay_status"] == "pass":
        proof_plan_hash = str(submit_payload.get("pre_submit_risk_plan_hash") or "")
        if proof_plan_hash != expected_plan_hash:
            result["replay_status"] = "fail"
            result["reason"] = "mismatch"
            result["mismatch_reason"] = "pre_submit_risk_plan_hash_mismatch"
    return result


def verify_risk_layer_replay(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None = None,
    execution_plan_id: int | None = None,
) -> dict[str, object]:
    context, target_payload, submit_payload = _load_cycle(
        conn,
        decision_id=decision_id,
        execution_plan_id=execution_plan_id,
    )
    strategy = _verify_generic_layer("strategy", _extract_strategy_decision(context))
    portfolio = _verify_portfolio_layer(context, target_payload)
    pre_submit = _verify_pre_submit_layer(submit_payload)
    layers = {
        "strategy": strategy,
        "portfolio": portfolio,
        "pre_submit": pre_submit,
    }
    applicable = [item for item in layers.values() if item["replay_status"] != "not_applicable"]
    overall = "pass" if applicable and all(item["replay_status"] == "pass" for item in applicable) else "fail"
    if not applicable:
        overall = "not_applicable"
    return {
        "schema_version": 1,
        "overall_status": overall,
        "read_only": True,
        "strategy_risk_replay_status": strategy["replay_status"],
        "portfolio_risk_replay_status": portfolio["replay_status"],
        "pre_submit_risk_replay_status": pre_submit["replay_status"],
        "layers": layers,
    }


def verify_risk_layer_replay_db(
    db_path: str | Path,
    *,
    decision_id: int | None = None,
    execution_plan_id: int | None = None,
) -> dict[str, object]:
    conn = open_read_only_db(db_path)
    try:
        return verify_risk_layer_replay(
            conn,
            decision_id=decision_id,
            execution_plan_id=execution_plan_id,
        )
    finally:
        conn.close()
