"""Factory-only authority for immutable, source-covered research inputs.

The authority consumes already resolved ``RESEARCH_INPUTS`` evidence.  It does
not collect data, call a provider, or trust a provider's certification.  A
receipt is issued only when the caller's actual input document is identical to
the artifact document and every JSON leaf is backed by value-bearing source
row coverage known by the decision cutoff.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Sequence, cast

from market_research.research.multi_asset.evidence import evidence_hash
from market_research.research.multi_asset.research_package import (
    RESEARCH_INPUT_SOURCE_ROW_FIELDS,
    EvidenceArtifactRole,
    ResolvedEvidenceArtifact,
    research_input_document_hash,
    research_input_source_row_hash,
    research_input_source_rows_hash,
)


AUTHORITATIVE_INPUT_SCHEMA_VERSION = 1
_FACTORY_TOKEN = object()
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_ARRAY_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_kind",
        "input_schema_id",
        "input_schema_version",
        "input_document",
        "input_document_hash",
        "source_rows",
        "source_rows_hash",
    }
)
_PATH_ROW_PAYLOAD_FIELDS = frozenset({"bindings", "source_record"})
_PATH_BINDING_FIELDS = frozenset({"input_path", "source_path"})


class AuthoritativeInputError(ValueError):
    """An immutable input artifact cannot support an authoritative receipt."""


class AuthoritativeInputRowKind(StrEnum):
    """Closed source-row vocabulary understood by this authority."""

    CANONICAL_RESEARCH_INPUTS = "CANONICAL_RESEARCH_INPUTS"
    JSON_POINTER_INPUTS = "JSON_POINTER_INPUTS"


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise AuthoritativeInputError(f"{label}_hash_invalid")
    return value


def _require_stable_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise AuthoritativeInputError(f"{label}_invalid")
    return value


def _json_value(value: object, label: str) -> object:
    """Snapshot a value into the exact JSON data model, rejecting coercion."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for raw_key, raw_item in value.items():
            if not isinstance(raw_key, str):
                raise AuthoritativeInputError(f"{label}_json_key_invalid")
            if raw_key in result:
                raise AuthoritativeInputError(f"{label}_json_key_duplicate")
            result[raw_key] = _json_value(
                raw_item,
                f"{label}.{raw_key}",
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, f"{label}.{index}") for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise AuthoritativeInputError(f"{label}_json_value_invalid")


def _canonical_json(value: object, label: str) -> str:
    snapshot = _json_value(value, label)
    try:
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:  # pragma: no cover - snapshot guards.
        raise AuthoritativeInputError(f"{label}_json_invalid") from exc


def _json_equal(left: object, right: object) -> bool:
    return _canonical_json(left, "left_value") == _canonical_json(
        right,
        "right_value",
    )


def _parse_payload_json(raw: str) -> Mapping[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise AuthoritativeInputError(
                    f"authoritative_input_artifact_duplicate_json_key:{key}"
                )
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise AuthoritativeInputError(
            f"authoritative_input_artifact_nonfinite_json:{value}"
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise AuthoritativeInputError(
            "authoritative_input_artifact_payload_invalid"
        ) from exc
    if not isinstance(parsed, dict):
        raise AuthoritativeInputError(
            "authoritative_input_artifact_payload_object_required"
        )
    return cast(Mapping[str, object], parsed)


def _timestamp(value: object, label: str) -> tuple[str, datetime]:
    if not isinstance(value, str):
        raise AuthoritativeInputError(f"{label}_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthoritativeInputError(f"{label}_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthoritativeInputError(f"{label}_timezone_required")
    canonical = parsed.astimezone(UTC)
    return canonical.isoformat().replace("+00:00", "Z"), canonical


def _escape_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer_tokens(pointer: object, label: str) -> tuple[str, ...]:
    if not isinstance(pointer, str):
        raise AuthoritativeInputError(f"{label}_json_pointer_invalid")
    if pointer == "":
        return ()
    if not pointer.startswith("/"):
        raise AuthoritativeInputError(f"{label}_json_pointer_invalid")
    result: list[str] = []
    for encoded in pointer[1:].split("/"):
        decoded: list[str] = []
        index = 0
        while index < len(encoded):
            character = encoded[index]
            if character != "~":
                decoded.append(character)
                index += 1
                continue
            if index + 1 >= len(encoded) or encoded[index + 1] not in {"0", "1"}:
                raise AuthoritativeInputError(f"{label}_json_pointer_invalid")
            decoded.append("~" if encoded[index + 1] == "0" else "/")
            index += 2
        token = "".join(decoded)
        if _escape_pointer_token(token) != encoded:
            raise AuthoritativeInputError(f"{label}_json_pointer_noncanonical")
        result.append(token)
    return tuple(result)


def _resolve_pointer(value: object, pointer: object, label: str) -> object:
    current = value
    for token in _pointer_tokens(pointer, label):
        if isinstance(current, Mapping):
            if token not in current:
                raise AuthoritativeInputError(f"{label}_json_pointer_not_found")
            current = current[token]
            continue
        if isinstance(current, list):
            if not _ARRAY_INDEX.fullmatch(token):
                raise AuthoritativeInputError(f"{label}_json_pointer_not_found")
            index = int(token)
            if index >= len(current):
                raise AuthoritativeInputError(f"{label}_json_pointer_not_found")
            current = current[index]
            continue
        raise AuthoritativeInputError(f"{label}_json_pointer_not_found")
    return current


def _leaf_values(
    value: object,
    *,
    pointer: str = "",
) -> dict[str, object]:
    if isinstance(value, Mapping) and value:
        result: dict[str, object] = {}
        for key in sorted(value):
            child = f"{pointer}/{_escape_pointer_token(key)}"
            result.update(_leaf_values(value[key], pointer=child))
        return result
    if isinstance(value, list) and value:
        result = {}
        for index, item in enumerate(value):
            result.update(_leaf_values(item, pointer=f"{pointer}/{index}"))
        return result
    return {pointer: value}


def _value_hash(value: object) -> str:
    return evidence_hash(value, label="authoritative-input-covered-value")


@dataclass(frozen=True, slots=True)
class AuthoritativeSourceRow:
    """Immutable source row returned by input-path provenance resolution."""

    row_id: str
    row_kind: AuthoritativeInputRowKind
    event_at: str
    knowledge_at: str
    source_id: str
    source_schema_version: str
    payload_json: str = field(repr=False)
    content_hash: str

    def __post_init__(self) -> None:
        _require_stable_id(self.row_id, "authoritative_source_row.row_id")
        if not isinstance(self.row_kind, AuthoritativeInputRowKind):
            raise AuthoritativeInputError("authoritative_source_row_row_kind_invalid")
        _timestamp(self.event_at, "authoritative_source_row.event_at")
        _timestamp(self.knowledge_at, "authoritative_source_row.knowledge_at")
        _require_stable_id(
            self.source_id,
            "authoritative_source_row.source_id",
        )
        _require_stable_id(
            self.source_schema_version,
            "authoritative_source_row.source_schema_version",
        )
        payload = self.payload
        if not payload:
            raise AuthoritativeInputError("authoritative_source_row_payload_required")
        _require_hash(
            self.content_hash,
            "authoritative_source_row.content_hash",
        )

    @property
    def payload(self) -> dict[str, object]:
        try:
            value = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthoritativeInputError(
                "authoritative_source_row_payload_invalid"
            ) from exc
        if not isinstance(value, dict):
            raise AuthoritativeInputError(
                "authoritative_source_row_payload_object_required"
            )
        return cast(dict[str, object], value)

    def as_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "row_kind": self.row_kind.value,
            "event_at": self.event_at,
            "knowledge_at": self.knowledge_at,
            "source_id": self.source_id,
            "source_schema_version": self.source_schema_version,
            "payload": self.payload,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True, order=True)
class InputPathCoverage:
    """One exact input leaf and the source-row value that establishes it."""

    input_path: str
    source_row_id: str
    source_payload_path: str
    source_row_hash: str
    value_hash: str

    def __post_init__(self) -> None:
        _pointer_tokens(self.input_path, "input_path_coverage.input_path")
        _pointer_tokens(
            self.source_payload_path,
            "input_path_coverage.source_payload_path",
        )
        _require_stable_id(
            self.source_row_id,
            "input_path_coverage.source_row_id",
        )
        _require_hash(
            self.source_row_hash,
            "input_path_coverage.source_row_hash",
        )
        _require_hash(self.value_hash, "input_path_coverage.value_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "input_path": self.input_path,
            "source_row_id": self.source_row_id,
            "source_payload_path": self.source_payload_path,
            "source_row_hash": self.source_row_hash,
            "value_hash": self.value_hash,
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeOutputBinding:
    """Factory-only link from one reported output to covered input leaves."""

    output_path: str
    input_paths: tuple[str, ...]
    output_value_hash: str
    computation_hash: str
    input_receipt_hash: str
    source_row_hashes: tuple[str, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _FACTORY_TOKEN:
            raise AuthoritativeInputError(
                "authoritative_output_binding_requires_factory"
            )
        _pointer_tokens(
            self.output_path,
            "authoritative_output_binding.output_path",
        )
        if not self.input_paths or self.input_paths != tuple(
            sorted(set(self.input_paths))
        ):
            raise AuthoritativeInputError(
                "authoritative_output_binding_input_paths_invalid"
            )
        for input_path in self.input_paths:
            _pointer_tokens(
                input_path,
                "authoritative_output_binding.input_path",
            )
        for value, label in (
            (self.output_value_hash, "output_value"),
            (self.computation_hash, "computation"),
            (self.input_receipt_hash, "input_receipt"),
        ):
            _require_hash(value, f"authoritative_output_binding.{label}")
        if not self.source_row_hashes or self.source_row_hashes != tuple(
            sorted(set(self.source_row_hashes))
        ):
            raise AuthoritativeInputError(
                "authoritative_output_binding_source_row_hashes_invalid"
            )
        for row_hash in self.source_row_hashes:
            _require_hash(
                row_hash,
                "authoritative_output_binding.source_row_hash",
            )
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                self.identity_payload(),
                label="authoritative-output-input-binding",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "output_path": self.output_path,
            "input_paths": list(self.input_paths),
            "output_value_hash": self.output_value_hash,
            "computation_hash": self.computation_hash,
            "input_receipt_hash": self.input_receipt_hash,
            "source_row_hashes": list(self.source_row_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class AuthoritativeInputReceipt:
    """Deeply immutable, factory-only binding of inputs to actual source rows."""

    input_schema_id: str
    input_schema_version: int
    decision_cutoff: str
    artifact_logical_id: str
    artifact_version: str
    input_document_hash: str
    artifact_hash: str
    source_rows_hash: str
    source_row_hashes: tuple[str, ...]
    coverage_hash: str
    source_rows: tuple[AuthoritativeSourceRow, ...]
    coverage: tuple[InputPathCoverage, ...]
    _input_document_json: str = field(repr=False)
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _FACTORY_TOKEN:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_requires_factory"
            )
        _require_stable_id(
            self.input_schema_id,
            "authoritative_input_receipt.input_schema_id",
        )
        if (
            isinstance(self.input_schema_version, bool)
            or not isinstance(self.input_schema_version, int)
            or self.input_schema_version < 1
        ):
            raise AuthoritativeInputError(
                "authoritative_input_receipt_schema_version_invalid"
            )
        cutoff, _ = _timestamp(
            self.decision_cutoff,
            "authoritative_input_receipt.decision_cutoff",
        )
        object.__setattr__(self, "decision_cutoff", cutoff)
        _require_stable_id(
            self.artifact_logical_id,
            "authoritative_input_receipt.artifact_logical_id",
        )
        _require_stable_id(
            self.artifact_version,
            "authoritative_input_receipt.artifact_version",
        )
        for value, label in (
            (self.input_document_hash, "input_document"),
            (self.artifact_hash, "artifact"),
            (self.source_rows_hash, "source_rows"),
            (self.coverage_hash, "coverage"),
        ):
            _require_hash(value, f"authoritative_input_receipt.{label}")
        if not self.source_rows or not self.coverage:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_sources_required"
            )
        if tuple(sorted(self.source_rows, key=lambda row: row.row_id)) != (
            self.source_rows
        ):
            raise AuthoritativeInputError(
                "authoritative_input_receipt_source_rows_order_invalid"
            )
        actual_row_hashes = tuple(row.content_hash for row in self.source_rows)
        if self.source_row_hashes != actual_row_hashes:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_source_row_hashes_mismatch"
            )
        if self.coverage != tuple(sorted(self.coverage)):
            raise AuthoritativeInputError(
                "authoritative_input_receipt_coverage_order_invalid"
            )
        if len({item.input_path for item in self.coverage}) != len(self.coverage):
            raise AuthoritativeInputError(
                "authoritative_input_receipt_coverage_duplicate"
            )
        document = self.input_document
        if research_input_document_hash(document) != self.input_document_hash:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_document_hash_mismatch"
            )
        actual_coverage_hash = evidence_hash(
            [item.as_dict() for item in self.coverage],
            label="authoritative-input-path-coverage",
        )
        if actual_coverage_hash != self.coverage_hash:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_coverage_hash_mismatch"
            )
        row_ids = {row.row_id for row in self.source_rows}
        if any(item.source_row_id not in row_ids for item in self.coverage):
            raise AuthoritativeInputError(
                "authoritative_input_receipt_coverage_row_missing"
            )
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                self.identity_payload(),
                label="authoritative-input-receipt",
            ),
        )

    @property
    def input_document(self) -> dict[str, object]:
        try:
            value = json.loads(self._input_document_json)
        except (TypeError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise AuthoritativeInputError(
                "authoritative_input_receipt_document_invalid"
            ) from exc
        if not isinstance(value, dict):  # pragma: no cover - factory invariant.
            raise AuthoritativeInputError(
                "authoritative_input_receipt_document_object_required"
            )
        return cast(dict[str, object], value)

    def identity_payload(self) -> dict[str, object]:
        """Return the immutable identity used to derive ``content_hash``."""

        return {
            "schema_version": AUTHORITATIVE_INPUT_SCHEMA_VERSION,
            "input_schema_id": self.input_schema_id,
            "input_schema_version": self.input_schema_version,
            "decision_cutoff": self.decision_cutoff,
            "artifact_logical_id": self.artifact_logical_id,
            "artifact_version": self.artifact_version,
            "input_document_hash": self.input_document_hash,
            "artifact_hash": self.artifact_hash,
            "source_rows_hash": self.source_rows_hash,
            "source_row_hashes": list(self.source_row_hashes),
            "coverage_hash": self.coverage_hash,
        }

    def as_dict(self) -> dict[str, object]:
        """Expose the complete independently inspectable provenance receipt."""

        return {
            **self.identity_payload(),
            "input_document": self.input_document,
            "source_rows": [row.as_dict() for row in self.source_rows],
            "coverage": [item.as_dict() for item in self.coverage],
            "content_hash": self.content_hash,
        }

    @property
    def input_paths(self) -> tuple[str, ...]:
        return tuple(item.input_path for item in self.coverage)

    @property
    def source_rows_by_input_path(
        self,
    ) -> Mapping[str, tuple[AuthoritativeSourceRow, ...]]:
        rows = {row.row_id: row for row in self.source_rows}
        return MappingProxyType(
            {item.input_path: (rows[item.source_row_id],) for item in self.coverage}
        )

    def source_rows_for_path(
        self,
        input_path: str,
    ) -> tuple[AuthoritativeSourceRow, ...]:
        """Resolve one canonical input JSON Pointer to its actual source row."""

        _pointer_tokens(input_path, "authoritative_input_receipt.input_path")
        try:
            return self.source_rows_by_input_path[input_path]
        except KeyError as exc:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_input_path_not_covered"
            ) from exc

    def coverage_for_path(self, input_path: str) -> InputPathCoverage:
        """Resolve one canonical input JSON Pointer to its coverage receipt."""

        _pointer_tokens(input_path, "authoritative_input_receipt.input_path")
        for item in self.coverage:
            if item.input_path == input_path:
                return item
        raise AuthoritativeInputError(
            "authoritative_input_receipt_input_path_not_covered"
        )

    def input_value_for_path(self, input_path: str) -> object:
        """Resolve and reverify the typed canonical value at one covered path."""

        coverage = self.coverage_for_path(input_path)
        value = _resolve_pointer(
            self.input_document,
            coverage.input_path,
            "authoritative_input_receipt.input_path",
        )
        if _value_hash(value) != coverage.value_hash:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_input_value_hash_mismatch"
            )
        return _json_value(value, "authoritative_input_receipt.input_value")

    def source_value_for_path(self, input_path: str) -> object:
        """Resolve and reverify the source-row value establishing one input."""

        coverage = self.coverage_for_path(input_path)
        rows = self.source_rows_for_path(input_path)
        if len(rows) != 1:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_source_row_cardinality_invalid"
            )
        value = _resolve_pointer(
            rows[0].payload,
            coverage.source_payload_path,
            "authoritative_input_receipt.source_payload_path",
        )
        if _value_hash(value) != coverage.value_hash:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_source_value_hash_mismatch"
            )
        return _json_value(value, "authoritative_input_receipt.source_value")

    def coverages_for_path(
        self,
        input_path: str,
    ) -> tuple[InputPathCoverage, ...]:
        """Resolve a leaf or object/array prefix to all covered input leaves."""

        _pointer_tokens(input_path, "authoritative_input_receipt.input_path")
        prefix = f"{input_path}/" if input_path else "/"
        matches = tuple(
            item
            for item in self.coverage
            if item.input_path == input_path or item.input_path.startswith(prefix)
        )
        if not matches:
            raise AuthoritativeInputError(
                "authoritative_input_receipt_input_path_not_covered"
            )
        return matches

    def bind_output(
        self,
        *,
        output_path: str,
        output_value: object,
        input_paths: Sequence[str],
        computation_hash: str,
    ) -> AuthoritativeOutputBinding:
        """Bind one report value to its covered inputs and actual source rows."""

        paths = tuple(sorted(set(input_paths)))
        if not paths:
            raise AuthoritativeInputError(
                "authoritative_output_binding_input_paths_required"
            )
        coverages = tuple(
            item for path in paths for item in self.coverages_for_path(path)
        )
        return AuthoritativeOutputBinding(
            output_path=output_path,
            input_paths=paths,
            output_value_hash=_value_hash(output_value),
            computation_hash=computation_hash,
            input_receipt_hash=self.content_hash,
            source_row_hashes=tuple(
                sorted({item.source_row_hash for item in coverages})
            ),
            _factory_token=_FACTORY_TOKEN,
        )

    def source_rows_for_output(
        self,
        binding: AuthoritativeOutputBinding,
    ) -> tuple[AuthoritativeSourceRow, ...]:
        """Resolve a bound report output back to its immutable source rows."""

        if (
            not isinstance(binding, AuthoritativeOutputBinding)
            or binding.input_receipt_hash != self.content_hash
        ):
            raise AuthoritativeInputError(
                "authoritative_output_binding_receipt_mismatch"
            )
        rows_by_id = {row.row_id: row for row in self.source_rows}
        rows = {
            rows_by_id[item.source_row_id].content_hash: rows_by_id[item.source_row_id]
            for input_path in binding.input_paths
            for item in self.coverages_for_path(input_path)
        }
        if tuple(sorted(rows)) != binding.source_row_hashes:
            raise AuthoritativeInputError(
                "authoritative_output_binding_source_rows_mismatch"
            )
        return tuple(rows[row_hash] for row_hash in sorted(rows))


@dataclass(frozen=True, slots=True)
class AuthoritativeInputFactory:
    """Issue receipts for one exact input schema and one resolved artifact."""

    input_schema_id: str
    input_schema_version: int

    def __post_init__(self) -> None:
        _require_stable_id(
            self.input_schema_id,
            "authoritative_input_factory.input_schema_id",
        )
        if (
            isinstance(self.input_schema_version, bool)
            or not isinstance(self.input_schema_version, int)
            or self.input_schema_version < 1
        ):
            raise AuthoritativeInputError(
                "authoritative_input_factory_schema_version_invalid"
            )

    def resolve(
        self,
        artifacts: Sequence[ResolvedEvidenceArtifact],
        *,
        input_document: Mapping[str, object],
        decision_cutoff: str,
    ) -> AuthoritativeInputReceipt:
        """Validate source coverage and issue the only constructible receipt."""

        resolved = tuple(artifacts)
        if len(resolved) != 1:
            raise AuthoritativeInputError(
                "authoritative_input_artifact_cardinality_invalid"
            )
        artifact = resolved[0]
        if not isinstance(artifact, ResolvedEvidenceArtifact):
            raise AuthoritativeInputError(
                "authoritative_input_resolved_artifact_required"
            )
        if artifact.reference.role is not EvidenceArtifactRole.RESEARCH_INPUTS:
            raise AuthoritativeInputError("authoritative_input_artifact_role_invalid")
        cutoff_text, cutoff = _timestamp(
            decision_cutoff,
            "authoritative_input_factory.decision_cutoff",
        )
        payload = _parse_payload_json(artifact.payload_json)
        if frozenset(payload) != _ARTIFACT_FIELDS:
            raise AuthoritativeInputError("authoritative_input_artifact_fields_invalid")
        if payload["artifact_kind"] != "IMMUTABLE_RESEARCH_INPUTS":
            raise AuthoritativeInputError("authoritative_input_artifact_kind_invalid")
        if payload["input_schema_id"] != self.input_schema_id:
            raise AuthoritativeInputError(
                "authoritative_input_artifact_schema_id_mismatch"
            )
        if payload["input_schema_version"] != self.input_schema_version:
            raise AuthoritativeInputError(
                "authoritative_input_artifact_schema_version_mismatch"
            )
        raw_document = payload["input_document"]
        if not isinstance(raw_document, dict) or not raw_document:
            raise AuthoritativeInputError(
                "authoritative_input_artifact_document_invalid"
            )
        artifact_document = cast(Mapping[str, object], raw_document)
        artifact_document_json = _canonical_json(
            artifact_document,
            "authoritative_input_artifact.document",
        )
        actual_document_json = _canonical_json(
            input_document,
            "authoritative_input_factory.input_document",
        )
        claimed_document_hash = _require_hash(
            payload["input_document_hash"],
            "authoritative_input_artifact.document",
        )
        actual_document_hash = research_input_document_hash(artifact_document)
        if claimed_document_hash != actual_document_hash:
            raise AuthoritativeInputError(
                "authoritative_input_artifact_document_hash_mismatch"
            )
        if actual_document_json != artifact_document_json:
            raise AuthoritativeInputError(
                "authoritative_input_caller_document_mismatch"
            )
        leaf_values = _leaf_values(artifact_document)
        raw_rows = payload["source_rows"]
        if not isinstance(raw_rows, list) or not raw_rows:
            raise AuthoritativeInputError("authoritative_input_source_rows_required")
        rows, coverage = self._validate_rows(
            raw_rows,
            document=artifact_document,
            leaf_values=leaf_values,
            decision_cutoff=cutoff,
        )
        covered_paths = {item.input_path for item in coverage}
        missing_paths = set(leaf_values).difference(covered_paths)
        if missing_paths:
            raise AuthoritativeInputError(
                "authoritative_input_path_coverage_missing:"
                + ",".join(sorted(missing_paths))
            )
        claimed_rows_hash = _require_hash(
            payload["source_rows_hash"],
            "authoritative_input_artifact.source_rows",
        )
        raw_row_mappings = [
            cast(Mapping[str, object], item)
            for item in raw_rows
            if isinstance(item, dict)
        ]
        if len(raw_row_mappings) != len(raw_rows):
            raise AuthoritativeInputError(
                "authoritative_input_source_row_object_required"
            )
        actual_rows_hash = research_input_source_rows_hash(raw_row_mappings)
        if claimed_rows_hash != actual_rows_hash:
            raise AuthoritativeInputError(
                "authoritative_input_source_rows_hash_mismatch"
            )
        coverage_tuple = tuple(sorted(coverage))
        coverage_hash = evidence_hash(
            [item.as_dict() for item in coverage_tuple],
            label="authoritative-input-path-coverage",
        )
        return AuthoritativeInputReceipt(
            input_schema_id=self.input_schema_id,
            input_schema_version=self.input_schema_version,
            decision_cutoff=cutoff_text,
            artifact_logical_id=artifact.reference.logical_id,
            artifact_version=artifact.reference.version,
            input_document_hash=actual_document_hash,
            artifact_hash=artifact.reference.content_hash,
            source_rows_hash=actual_rows_hash,
            source_row_hashes=tuple(row.content_hash for row in rows),
            coverage_hash=coverage_hash,
            source_rows=rows,
            coverage=coverage_tuple,
            _input_document_json=artifact_document_json,
            _factory_token=_FACTORY_TOKEN,
        )

    def _validate_rows(
        self,
        raw_rows: Sequence[object],
        *,
        document: Mapping[str, object],
        leaf_values: Mapping[str, object],
        decision_cutoff: datetime,
    ) -> tuple[tuple[AuthoritativeSourceRow, ...], list[InputPathCoverage]]:
        rows: list[AuthoritativeSourceRow] = []
        coverage: list[InputPathCoverage] = []
        row_ids: list[str] = []
        row_hashes: list[str] = []
        covered: dict[str, tuple[str, str]] = {}
        for index, raw_row in enumerate(raw_rows):
            if (
                not isinstance(raw_row, dict)
                or frozenset(raw_row) != RESEARCH_INPUT_SOURCE_ROW_FIELDS
            ):
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_fields_invalid"
                )
            row = cast(Mapping[str, object], raw_row)
            row_id = _require_stable_id(
                row["row_id"],
                f"authoritative_input_source_row.{index}.row_id",
            )
            raw_kind = row["row_kind"]
            try:
                row_kind = AuthoritativeInputRowKind(
                    _require_stable_id(
                        raw_kind,
                        f"authoritative_input_source_row.{index}.row_kind",
                    )
                )
            except (TypeError, ValueError) as exc:
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_kind_unsupported"
                ) from exc
            _, event_at = _timestamp(
                row["event_at"],
                f"authoritative_input_source_row.{index}.event_at",
            )
            _, knowledge_at = _timestamp(
                row["knowledge_at"],
                f"authoritative_input_source_row.{index}.knowledge_at",
            )
            if knowledge_at < event_at:
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_knowledge_before_event"
                )
            if knowledge_at > decision_cutoff:
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_future_knowledge"
                )
            source_id = _require_stable_id(
                row["source_id"],
                f"authoritative_input_source_row.{index}.source_id",
            )
            source_schema_version = _require_stable_id(
                row["source_schema_version"],
                f"authoritative_input_source_row.{index}.source_schema_version",
            )
            raw_payload = row["payload"]
            if not isinstance(raw_payload, dict) or not raw_payload:
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_payload_invalid"
                )
            payload = cast(Mapping[str, object], raw_payload)
            claimed_hash = _require_hash(
                row["content_hash"],
                f"authoritative_input_source_row.{index}.content",
            )
            actual_hash = research_input_source_row_hash(row)
            if claimed_hash != actual_hash:
                raise AuthoritativeInputError(
                    "authoritative_input_source_row_hash_mismatch"
                )
            immutable_row = AuthoritativeSourceRow(
                row_id=row_id,
                row_kind=row_kind,
                event_at=cast(str, row["event_at"]),
                knowledge_at=cast(str, row["knowledge_at"]),
                source_id=source_id,
                source_schema_version=source_schema_version,
                payload_json=_canonical_json(
                    payload,
                    f"authoritative_input_source_row.{index}.payload",
                ),
                content_hash=actual_hash,
            )
            rows.append(immutable_row)
            row_ids.append(row_id)
            row_hashes.append(actual_hash)
            bindings = self._row_bindings(
                row_kind,
                payload,
                document=document,
                leaf_values=leaf_values,
            )
            if not bindings:
                raise AuthoritativeInputError("authoritative_input_orphan_source_row")
            for input_path, source_path, source_value in bindings:
                if input_path not in leaf_values:
                    raise AuthoritativeInputError(
                        "authoritative_input_orphan_source_path"
                    )
                document_value = leaf_values[input_path]
                prior = covered.get(input_path)
                if not _json_equal(source_value, document_value):
                    raise AuthoritativeInputError(
                        "authoritative_input_conflicting_source_value"
                    )
                if prior is not None:
                    prior_hash, _ = prior
                    if prior_hash == _value_hash(source_value):
                        raise AuthoritativeInputError(
                            "authoritative_input_duplicate_source_coverage"
                        )
                    raise AuthoritativeInputError(
                        "authoritative_input_conflicting_source_rows"
                    )
                value_hash = _value_hash(source_value)
                covered[input_path] = (value_hash, row_id)
                coverage.append(
                    InputPathCoverage(
                        input_path=input_path,
                        source_row_id=row_id,
                        source_payload_path=source_path,
                        source_row_hash=actual_hash,
                        value_hash=value_hash,
                    )
                )
        if row_ids != sorted(row_ids) or len(row_ids) != len(set(row_ids)):
            raise AuthoritativeInputError(
                "authoritative_input_source_rows_order_or_identity_invalid"
            )
        if len(row_hashes) != len(set(row_hashes)):
            raise AuthoritativeInputError("authoritative_input_duplicate_source_rows")
        return tuple(rows), coverage

    def _row_bindings(
        self,
        row_kind: AuthoritativeInputRowKind,
        payload: Mapping[str, object],
        *,
        document: Mapping[str, object],
        leaf_values: Mapping[str, object],
    ) -> list[tuple[str, str, object]]:
        if row_kind is AuthoritativeInputRowKind.CANONICAL_RESEARCH_INPUTS:
            if not _json_equal(payload, document):
                raise AuthoritativeInputError(
                    "authoritative_input_canonical_row_document_mismatch"
                )
            return [
                (
                    input_path,
                    input_path,
                    _resolve_pointer(
                        payload,
                        input_path,
                        "authoritative_input_canonical_row.source_path",
                    ),
                )
                for input_path in sorted(leaf_values)
            ]
        if frozenset(payload) != _PATH_ROW_PAYLOAD_FIELDS:
            raise AuthoritativeInputError(
                "authoritative_input_pointer_row_payload_fields_invalid"
            )
        source_record = payload["source_record"]
        if not isinstance(source_record, dict) or not source_record:
            raise AuthoritativeInputError(
                "authoritative_input_pointer_row_source_record_invalid"
            )
        raw_bindings = payload["bindings"]
        if not isinstance(raw_bindings, list) or not raw_bindings:
            raise AuthoritativeInputError(
                "authoritative_input_pointer_row_bindings_required"
            )
        result: list[tuple[str, str, object]] = []
        binding_identities: list[tuple[str, str]] = []
        for binding in raw_bindings:
            if (
                not isinstance(binding, dict)
                or frozenset(binding) != _PATH_BINDING_FIELDS
            ):
                raise AuthoritativeInputError(
                    "authoritative_input_pointer_binding_fields_invalid"
                )
            input_path = binding["input_path"]
            source_path = binding["source_path"]
            _pointer_tokens(
                input_path,
                "authoritative_input_pointer_binding.input_path",
            )
            _pointer_tokens(
                source_path,
                "authoritative_input_pointer_binding.source_path",
            )
            input_text = cast(str, input_path)
            source_text = cast(str, source_path)
            source_value = _resolve_pointer(
                source_record,
                source_text,
                "authoritative_input_pointer_binding.source_path",
            )
            binding_identities.append((input_text, source_text))
            result.append((input_text, source_text, source_value))
        if binding_identities != sorted(binding_identities):
            raise AuthoritativeInputError(
                "authoritative_input_pointer_bindings_order_invalid"
            )
        if len(binding_identities) != len(set(binding_identities)):
            raise AuthoritativeInputError(
                "authoritative_input_pointer_bindings_duplicate"
            )
        return result


__all__ = [
    "AUTHORITATIVE_INPUT_SCHEMA_VERSION",
    "AuthoritativeInputError",
    "AuthoritativeInputFactory",
    "AuthoritativeInputReceipt",
    "AuthoritativeInputRowKind",
    "AuthoritativeOutputBinding",
    "AuthoritativeSourceRow",
    "InputPathCoverage",
]
