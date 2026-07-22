"""Strict JSON transport for the offline derivative application authority.

The transport deliberately serializes only the dataclasses and enums reachable
from the three typed study requests and their execution/reproduction results.
It never imports a type named by input JSON.  Constructors are rerun on decode,
so all domain invariants and computed hashes are independently reconstructed.
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, TypeAlias, Union, cast, get_args, get_origin, get_type_hints

from market_research.paths import ResearchPathManager
from market_research.research.hashing import sha256_prefixed

from .application import (
    DerivativeFailureResult,
    DerivativeReproductionReceipt,
    DerivativeStudyExecution,
    FuturesStudyRequest,
    MultiLegStudyRequest,
    OptionStudyRequest,
)
from .common import (
    DerivativeExperimentRun,
    decimal_text,
    require_hash,
    require_stable_id,
)
from .workflow import (
    DerivativeEvidenceWorkflowError,
    read_external_derivative_json,
    write_external_derivative_json,
)


DERIVATIVE_APPLICATION_TRANSPORT_SCHEMA_VERSION = 1
DERIVATIVE_APPLICATION_TRANSPORT_ARTIFACT_TYPE = (
    "offline_derivative_application_transport"
)

_TRANSPORT_FIELDS = {
    "schema_version",
    "artifact_type",
    "payload_type",
    "payload",
    "bindings",
    "content_hash",
}
_FORBIDDEN_KEYS = frozenset(
    {
        "approval",
        "approved",
        "approval_status",
        "live_approval",
        "live_account",
        "approved_for_live",
        "account",
        "account_id",
        "broker_account",
        "deployment",
        "deployment_id",
        "deployment_target",
        "capital",
        "capital_allocation",
        "order_route",
        "order_router",
        "order_submission",
        "broker_api_key",
        "exchange_api_key",
        "exchange_api_secret",
        "private_exchange",
        "private_exchange_api",
        "network_market_data",
        "network_market_data_collection",
        "market_data_collection",
    }
)


class DerivativeApplicationCodecError(ValueError):
    """An application transport is unsafe, malformed, or hash-inconsistent."""


@dataclass(frozen=True, slots=True)
class DerivativeApplicationFailureArtifact:
    """Bounded immutable publication of a structured failed application Run."""

    request_transport_hash: str
    failed_run: DerivativeExperimentRun
    failure_result: DerivativeFailureResult
    failure_code: str
    message_sha256: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(
            self.request_transport_hash,
            "derivative_application_failure.request_transport_hash",
        )
        require_hash(
            self.message_sha256,
            "derivative_application_failure.message_sha256",
        )
        require_stable_id(
            self.failure_code,
            "derivative_application_failure.failure_code",
        )
        if self.failed_run.status != "FAILED":
            raise DerivativeApplicationCodecError(
                "derivative_application_failure_failed_run_required"
            )
        if self.failed_run.failure_code != self.failure_code:
            raise DerivativeApplicationCodecError(
                "derivative_application_failure_code_mismatch"
            )
        if (
            self.failure_result.run_id != self.failed_run.run_id
            or self.failure_result.failure_code != self.failure_code
            or self.failure_result.message_sha256 != self.message_sha256
            or self.failure_result.event_stream_hash
            != self.failed_run.event_stream_hash
            or self.failure_result.content_hash != self.failed_run.result_artifact_hash
        ):
            raise DerivativeApplicationCodecError(
                "derivative_application_failure_result_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_application_failure"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "request_transport_hash": self.request_transport_hash,
            "failed_run": self.failed_run.as_dict(),
            "failure_result": self.failure_result.as_dict(),
            "failure_code": self.failure_code,
            "message_sha256": self.message_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


DerivativeStudyRequest: TypeAlias = (
    FuturesStudyRequest | OptionStudyRequest | MultiLegStudyRequest
)
DerivativeApplicationPayload: TypeAlias = (
    DerivativeStudyRequest
    | DerivativeStudyExecution
    | DerivativeReproductionReceipt
    | DerivativeApplicationFailureArtifact
)

REQUEST_TYPES = (FuturesStudyRequest, OptionStudyRequest, MultiLegStudyRequest)
EXECUTION_TYPES = (DerivativeStudyExecution,)
REPRODUCTION_TYPES = (DerivativeReproductionReceipt,)
FAILURE_TYPES = (DerivativeApplicationFailureArtifact,)
PAYLOAD_TYPES = (
    *REQUEST_TYPES,
    *EXECUTION_TYPES,
    *REPRODUCTION_TYPES,
    *FAILURE_TYPES,
)


def _type_name(value: type[object]) -> str:
    return f"{value.__module__}.{value.__qualname__}"


_DATACLASS_TYPES: dict[str, type[object]] = {}
_ENUM_TYPES: dict[str, type[Enum]] = {}
_TYPE_HINTS: dict[type[object], dict[str, object]] = {}


def _collect_allowed_hint(hint: object) -> None:
    if hint is Any or hint is None or hint is type(None) or hint is Ellipsis:
        return
    origin = get_origin(hint)
    if origin is not None:
        for argument in get_args(hint):
            _collect_allowed_hint(argument)
        return
    if not isinstance(hint, type):
        return
    if issubclass(hint, Enum):
        _ENUM_TYPES[_type_name(hint)] = hint
        return
    if not is_dataclass(hint):
        return
    name = _type_name(hint)
    if name in _DATACLASS_TYPES:
        return
    _DATACLASS_TYPES[name] = hint
    hints = get_type_hints(hint)
    _TYPE_HINTS[hint] = hints
    for field_hint in hints.values():
        _collect_allowed_hint(field_hint)


for _root_type in PAYLOAD_TYPES:
    _collect_allowed_hint(_root_type)


@dataclass(frozen=True, slots=True)
class DerivativeApplicationTransport:
    """One self-hashed allowlisted payload with explicit upstream bindings."""

    payload: DerivativeApplicationPayload
    bindings: tuple[tuple[str, str], ...] = ()
    content_hash: str = ""
    schema_version: int = DERIVATIVE_APPLICATION_TRANSPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_APPLICATION_TRANSPORT_SCHEMA_VERSION:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_schema_unsupported"
            )
        if type(self.payload) not in PAYLOAD_TYPES:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_payload_type_forbidden"
            )
        names: set[str] = set()
        normalized: list[tuple[str, str]] = []
        for name, value in self.bindings:
            require_stable_id(name, "derivative_application_transport.binding")
            require_hash(value, "derivative_application_transport.binding_hash")
            if name in names:
                raise DerivativeApplicationCodecError(
                    "derivative_application_transport_binding_duplicate"
                )
            names.add(name)
            normalized.append((name, value))
        normalized.sort()
        object.__setattr__(self, "bindings", tuple(normalized))
        observed = sha256_prefixed(
            self.identity_payload(), label="derivative_application_transport"
        )
        if self.content_hash and self.content_hash != observed:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_content_hash_mismatch"
            )
        object.__setattr__(self, "content_hash", observed)

    @property
    def payload_type(self) -> str:
        return _type_name(type(self.payload))

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": DERIVATIVE_APPLICATION_TRANSPORT_ARTIFACT_TYPE,
            "payload_type": self.payload_type,
            "payload": _encode_node(self.payload),
            "bindings": dict(self.bindings),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeApplicationTransport":
        payload = _mapping(value, "derivative_application_transport")
        _reject_forbidden_fields(payload, "derivative_application_transport")
        _reject_float_values(payload, "derivative_application_transport")
        _require_exact_fields(
            payload, _TRANSPORT_FIELDS, "derivative_application_transport"
        )
        schema_version = _integer(
            payload["schema_version"],
            "derivative_application_transport.schema_version",
        )
        if payload["artifact_type"] != DERIVATIVE_APPLICATION_TRANSPORT_ARTIFACT_TYPE:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_artifact_type_invalid"
            )
        payload_type = _text(
            payload["payload_type"], "derivative_application_transport.payload_type"
        )
        decoded = _decode_node(
            payload["payload"], "derivative_application_transport.payload"
        )
        if (
            type(decoded) not in PAYLOAD_TYPES
            or _type_name(type(decoded)) != payload_type
        ):
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_payload_type_mismatch"
            )
        raw_bindings = _mapping(
            payload["bindings"], "derivative_application_transport.bindings"
        )
        bindings = tuple(
            (
                _text(name, "derivative_application_transport.binding_name"),
                _text(value, f"derivative_application_transport.bindings.{name}"),
            )
            for name, value in raw_bindings.items()
        )
        return cls(
            payload=cast(DerivativeApplicationPayload, decoded),
            bindings=bindings,
            content_hash=_text(
                payload["content_hash"],
                "derivative_application_transport.content_hash",
            ),
            schema_version=schema_version,
        )


def load_derivative_application_transport(
    manager: ResearchPathManager,
    path: str | Path,
    *,
    expected_types: tuple[type[object], ...] = PAYLOAD_TYPES,
) -> DerivativeApplicationTransport:
    """Load and validate an external transport with a narrowed root type."""

    try:
        document = read_external_derivative_json(path, manager, "application_input")
    except DerivativeEvidenceWorkflowError as exc:
        raise DerivativeApplicationCodecError(str(exc)) from exc
    result = DerivativeApplicationTransport.from_dict(document)
    if type(result.payload) not in expected_types:
        raise DerivativeApplicationCodecError(
            "derivative_application_transport_unexpected_root_type"
        )
    return result


def write_derivative_application_transport(
    manager: ResearchPathManager,
    path: str | Path,
    payload: DerivativeApplicationPayload,
    *,
    bindings: Mapping[str, str] | None = None,
) -> DerivativeApplicationTransport:
    """Create-or-verify a typed transport outside the source repository."""

    transport = DerivativeApplicationTransport(
        payload=payload,
        bindings=tuple((bindings or {}).items()),
    )
    try:
        write_external_derivative_json(
            path,
            manager,
            transport.as_dict(),
            "application_output",
        )
    except DerivativeEvidenceWorkflowError as exc:
        raise DerivativeApplicationCodecError(str(exc)) from exc
    return transport


def _encode_node(value: object) -> object:
    if isinstance(value, Enum):
        name = _type_name(type(value))
        if name not in _ENUM_TYPES:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_enum_type_forbidden"
            )
        return {"node_type": "enum", "type_name": name, "value": value.value}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_decimal_non_finite"
            )
        return {"node_type": "decimal", "value": decimal_text(value)}
    if is_dataclass(value) and not isinstance(value, type):
        name = _type_name(type(value))
        if name not in _DATACLASS_TYPES:
            raise DerivativeApplicationCodecError(
                "derivative_application_transport_dataclass_type_forbidden"
            )
        return {
            "node_type": "dataclass",
            "type_name": name,
            "fields": {
                item.name: _encode_node(getattr(value, item.name))
                for item in fields(value)
                if item.init
            },
        }
    if isinstance(value, tuple):
        return {"node_type": "tuple", "items": [_encode_node(item) for item in value]}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise DerivativeApplicationCodecError(
            "derivative_application_transport_float_forbidden"
        )
    raise DerivativeApplicationCodecError(
        "derivative_application_transport_value_type_forbidden"
    )


def _decode_node(value: object, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_float_forbidden:{path}"
        )
    node = _mapping(value, path)
    node_type = _text(node.get("node_type"), f"{path}.node_type")
    if node_type == "decimal":
        _require_exact_fields(node, {"node_type", "value"}, path)
        raw = _text(node["value"], f"{path}.value")
        try:
            result = Decimal(raw)
        except InvalidOperation as exc:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_decimal_invalid:{path}"
            ) from exc
        if not result.is_finite() or decimal_text(result) != raw:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_decimal_noncanonical:{path}"
            )
        return result
    if node_type == "enum":
        _require_exact_fields(node, {"node_type", "type_name", "value"}, path)
        type_name = _text(node["type_name"], f"{path}.type_name")
        enum_type = _ENUM_TYPES.get(type_name)
        if enum_type is None:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_enum_type_unknown:{type_name}"
            )
        raw = _text(node["value"], f"{path}.value")
        try:
            return enum_type(raw)
        except ValueError as exc:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_enum_value_unknown:{path}"
            ) from exc
    if node_type == "tuple":
        _require_exact_fields(node, {"node_type", "items"}, path)
        items = node["items"]
        if not isinstance(items, list):
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_tuple_items_invalid:{path}"
            )
        return tuple(
            _decode_node(item, f"{path}.items[{index}]")
            for index, item in enumerate(items)
        )
    if node_type == "dataclass":
        _require_exact_fields(node, {"node_type", "type_name", "fields"}, path)
        type_name = _text(node["type_name"], f"{path}.type_name")
        data_type = _DATACLASS_TYPES.get(type_name)
        if data_type is None:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_dataclass_type_unknown:{type_name}"
            )
        raw_fields = _mapping(node["fields"], f"{path}.fields")
        expected = {item.name for item in fields(cast(Any, data_type)) if item.init}
        _require_exact_fields(raw_fields, expected, f"{path}.fields")
        decoded_fields: dict[str, object] = {}
        hints = _TYPE_HINTS[data_type]
        for name, raw_value in raw_fields.items():
            decoded_value = _decode_node(raw_value, f"{path}.fields.{name}")
            _require_runtime_type(
                decoded_value,
                hints[name],
                f"{path}.fields.{name}",
            )
            decoded_fields[name] = decoded_value
        try:
            return data_type(**decoded_fields)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_dataclass_invalid:{type_name}"
            ) from exc
    raise DerivativeApplicationCodecError(
        f"derivative_application_transport_node_type_unknown:{node_type}"
    )


def _require_runtime_type(value: object, hint: object, path: str) -> None:
    if hint is Any:
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_any_annotation_forbidden:{path}"
        )
    origin = get_origin(hint)
    arguments = get_args(hint)
    if origin in {Union, types.UnionType}:
        for argument in arguments:
            try:
                _require_runtime_type(value, argument, path)
            except DerivativeApplicationCodecError:
                continue
            return
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_field_type_invalid:{path}"
        )
    if origin is tuple:
        if not isinstance(value, tuple):
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_field_type_invalid:{path}"
            )
        if len(arguments) == 2 and arguments[1] is Ellipsis:
            for index, item in enumerate(value):
                _require_runtime_type(item, arguments[0], f"{path}[{index}]")
            return
        if len(value) != len(arguments):
            raise DerivativeApplicationCodecError(
                f"derivative_application_transport_tuple_length_invalid:{path}"
            )
        for index, (item, argument) in enumerate(zip(value, arguments, strict=True)):
            _require_runtime_type(item, argument, f"{path}[{index}]")
        return
    if hint is type(None):
        valid = value is None
    elif hint is bool:
        valid = isinstance(value, bool)
    elif hint is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif hint is str:
        valid = isinstance(value, str)
    elif hint is Decimal:
        valid = isinstance(value, Decimal)
    elif isinstance(hint, type):
        valid = type(value) is hint
    else:
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_annotation_unsupported:{path}"
        )
    if not valid:
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_field_type_invalid:{path}"
        )


def _reject_forbidden_fields(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _normalize_boundary_token(raw_key)
            if key in _FORBIDDEN_KEYS:
                raise DerivativeApplicationCodecError(
                    f"derivative_application_transport_live_field_forbidden:{path}.{key}"
                )
            _reject_forbidden_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, f"{path}[{index}]")


def _reject_float_values(value: object, path: str) -> None:
    if isinstance(value, float):
        raise DerivativeApplicationCodecError(
            f"derivative_application_transport_float_forbidden:{path}"
        )
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_float_values(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_float_values(child, f"{path}[{index}]")


def _normalize_boundary_token(value: object) -> str:
    raw = str(value).strip()
    acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", acronym_split)
    return re.sub(r"[^A-Za-z0-9]+", "_", camel_split).strip("_").lower()


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeApplicationCodecError(f"{label}_must_be_object")
    return value


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    observed = set(payload)
    if observed != expected:
        missing = ",".join(sorted(expected - observed)) or "none"
        unknown = ",".join(sorted(observed - expected)) or "none"
        raise DerivativeApplicationCodecError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DerivativeApplicationCodecError(f"{label}_must_be_text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeApplicationCodecError(f"{label}_must_be_integer")
    return value
