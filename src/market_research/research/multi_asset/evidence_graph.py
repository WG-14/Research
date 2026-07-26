"""Typed, tamper-evident bidirectional evidence lineage for research outputs."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence, cast


EVIDENCE_GRAPH_SCHEMA_VERSION = 1

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")


class EvidenceGraphError(ValueError):
    """The lineage graph is incomplete, cyclic, or tampered."""


class EvidenceNodeKind(StrEnum):
    SOURCE_ROW = "SOURCE_ROW"
    IMMUTABLE_INPUT = "IMMUTABLE_INPUT"
    DATA_CARD = "DATA_CARD"
    MODEL_CARD = "MODEL_CARD"
    POLICY = "POLICY"
    CONFIGURATION = "CONFIGURATION"
    CODE = "CODE"
    NORMALIZED = "NORMALIZED"
    DERIVED = "DERIVED"
    ANALYSIS = "ANALYSIS"
    ACCOUNTING = "ACCOUNTING"
    REPORT_CLAIM = "REPORT_CLAIM"


class EvidenceRelation(StrEnum):
    DESCRIBES = "DESCRIBES"
    NORMALIZES = "NORMALIZES"
    DERIVES = "DERIVES"
    CONFIGURES = "CONFIGURES"
    CALCULATES = "CALCULATES"
    RECONCILES = "RECONCILES"
    SUPPORTS = "SUPPORTS"
    REPORTS = "REPORTS"


_ROOT_KINDS = frozenset(
    {
        EvidenceNodeKind.SOURCE_ROW,
        EvidenceNodeKind.IMMUTABLE_INPUT,
        EvidenceNodeKind.DATA_CARD,
        EvidenceNodeKind.MODEL_CARD,
        EvidenceNodeKind.POLICY,
        EvidenceNodeKind.CONFIGURATION,
        EvidenceNodeKind.CODE,
    }
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _hash_payload(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise EvidenceGraphError(f"{field_name}_invalid")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise EvidenceGraphError(f"{field_name}_invalid")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise EvidenceGraphError(f"{field_name}_invalid")
    return value


def _sorted_ids(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    if result != tuple(sorted(set(result))):
        raise EvidenceGraphError(f"{field_name}_must_be_sorted_unique")
    for value in result:
        _require_id(value, field_name)
    return result


def _quality_flags(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    if result != tuple(sorted(set(result))):
        raise EvidenceGraphError(f"{field_name}_must_be_sorted_unique")
    if any(not _QUALITY_FLAG.fullmatch(item) for item in result):
        raise EvidenceGraphError(f"{field_name}_invalid")
    return result


def _attributes(
    values: Sequence[tuple[str, str]],
    field_name: str,
) -> tuple[tuple[str, str], ...]:
    result = tuple(values)
    if result != tuple(sorted(set(result))):
        raise EvidenceGraphError(f"{field_name}_must_be_sorted_unique")
    keys = tuple(key for key, _ in result)
    if len(keys) != len(set(keys)):
        raise EvidenceGraphError(f"{field_name}_keys_duplicate")
    for key, value in result:
        _require_id(key, f"{field_name}.key")
        _require_text(value, f"{field_name}.value")
    return result


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise EvidenceGraphError(f"{field_name}_object_required")
    return cast(Mapping[str, object], value)


def _exact(
    value: Mapping[str, object],
    expected: frozenset[str],
    field_name: str,
) -> None:
    if set(value) != expected:
        raise EvidenceGraphError(f"{field_name}_fields_invalid")


@dataclass(frozen=True, slots=True)
class EvidenceNode:
    """One stable evidence object or report claim."""

    node_id: str
    version: str
    kind: EvidenceNodeKind
    label: str
    content_hash: str
    parent_ids: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str], ...] = ()
    node_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.node_id, "evidence_node.node_id")
        _require_id(self.version, "evidence_node.version")
        if not isinstance(self.kind, EvidenceNodeKind):
            raise EvidenceGraphError("evidence_node.kind_invalid")
        _require_text(self.label, "evidence_node.label")
        _require_hash(self.content_hash, "evidence_node.content_hash")
        _sorted_ids(self.parent_ids, "evidence_node.parent_ids")
        _quality_flags(self.quality_flags, "evidence_node.quality_flags")
        _attributes(self.attributes, "evidence_node.attributes")
        if self.kind in _ROOT_KINDS and self.parent_ids:
            raise EvidenceGraphError("evidence_node.root_has_parents")
        if self.kind not in _ROOT_KINDS and not self.parent_ids:
            raise EvidenceGraphError("evidence_node.non_root_parent_required")
        if self.node_id in self.parent_ids:
            raise EvidenceGraphError("evidence_node.self_parent")
        object.__setattr__(self, "node_hash", _hash_payload(self.identity_payload()))

    def identity_payload(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "version": self.version,
            "kind": self.kind.value,
            "label": self.label,
            "content_hash": self.content_hash,
            "parent_ids": list(self.parent_ids),
            "quality_flags": list(self.quality_flags),
            "attributes": {key: value for key, value in self.attributes},
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "node_hash": self.node_hash}

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceNode":
        payload = _mapping(value, "evidence_node")
        _exact(
            payload,
            frozenset(
                {
                    "node_id",
                    "version",
                    "kind",
                    "label",
                    "content_hash",
                    "parent_ids",
                    "quality_flags",
                    "attributes",
                    "node_hash",
                }
            ),
            "evidence_node",
        )
        try:
            kind = EvidenceNodeKind(cast(str, payload["kind"]))
        except (TypeError, ValueError) as exc:
            raise EvidenceGraphError("evidence_node.kind_invalid") from exc
        parent_ids = payload["parent_ids"]
        quality_flags = payload["quality_flags"]
        attributes = _mapping(payload["attributes"], "evidence_node.attributes")
        if not isinstance(parent_ids, list) or any(
            not isinstance(item, str) for item in parent_ids
        ):
            raise EvidenceGraphError("evidence_node.parent_ids_array_required")
        if not isinstance(quality_flags, list) or any(
            not isinstance(item, str) for item in quality_flags
        ):
            raise EvidenceGraphError("evidence_node.quality_flags_array_required")
        if any(not isinstance(item, str) for item in attributes.values()):
            raise EvidenceGraphError("evidence_node.attributes_string_values_required")
        result = cls(
            node_id=_require_id(payload["node_id"], "evidence_node.node_id"),
            version=_require_id(payload["version"], "evidence_node.version"),
            kind=kind,
            label=_require_text(payload["label"], "evidence_node.label"),
            content_hash=_require_hash(
                payload["content_hash"],
                "evidence_node.content_hash",
            ),
            parent_ids=tuple(cast(list[str], parent_ids)),
            quality_flags=tuple(cast(list[str], quality_flags)),
            attributes=tuple(
                sorted((key, cast(str, item)) for key, item in attributes.items())
            ),
        )
        if payload["node_hash"] != result.node_hash:
            raise EvidenceGraphError("evidence_node.node_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class EvidenceEdge:
    """A typed relationship binding both endpoint identities and content."""

    source_id: str
    target_id: str
    relation: EvidenceRelation
    source_content_hash: str
    target_content_hash: str
    edge_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.source_id, "evidence_edge.source_id")
        _require_id(self.target_id, "evidence_edge.target_id")
        if self.source_id == self.target_id:
            raise EvidenceGraphError("evidence_edge.self_edge")
        if not isinstance(self.relation, EvidenceRelation):
            raise EvidenceGraphError("evidence_edge.relation_invalid")
        _require_hash(
            self.source_content_hash,
            "evidence_edge.source_content_hash",
        )
        _require_hash(
            self.target_content_hash,
            "evidence_edge.target_content_hash",
        )
        object.__setattr__(self, "edge_hash", _hash_payload(self.identity_payload()))

    @property
    def identity(self) -> tuple[str, str]:
        return (self.source_id, self.target_id)

    def identity_payload(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation.value,
            "source_content_hash": self.source_content_hash,
            "target_content_hash": self.target_content_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "edge_hash": self.edge_hash}

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceEdge":
        payload = _mapping(value, "evidence_edge")
        _exact(
            payload,
            frozenset(
                {
                    "source_id",
                    "target_id",
                    "relation",
                    "source_content_hash",
                    "target_content_hash",
                    "edge_hash",
                }
            ),
            "evidence_edge",
        )
        try:
            relation = EvidenceRelation(cast(str, payload["relation"]))
        except (TypeError, ValueError) as exc:
            raise EvidenceGraphError("evidence_edge.relation_invalid") from exc
        result = cls(
            source_id=_require_id(payload["source_id"], "evidence_edge.source_id"),
            target_id=_require_id(payload["target_id"], "evidence_edge.target_id"),
            relation=relation,
            source_content_hash=_require_hash(
                payload["source_content_hash"],
                "evidence_edge.source_content_hash",
            ),
            target_content_hash=_require_hash(
                payload["target_content_hash"],
                "evidence_edge.target_content_hash",
            ),
        )
        if payload["edge_hash"] != result.edge_hash:
            raise EvidenceGraphError("evidence_edge.edge_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class EvidenceGraph:
    """A complete DAG whose incoming edges exactly match node parent claims."""

    graph_id: str
    version: str
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    terminal_ids: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = EVIDENCE_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_GRAPH_SCHEMA_VERSION:
            raise EvidenceGraphError("evidence_graph.schema_version_invalid")
        _require_id(self.graph_id, "evidence_graph.graph_id")
        _require_id(self.version, "evidence_graph.version")
        if not self.nodes:
            raise EvidenceGraphError("evidence_graph.nodes_required")
        node_ids = tuple(item.node_id for item in self.nodes)
        if node_ids != tuple(sorted(set(node_ids))):
            raise EvidenceGraphError("evidence_graph.nodes_must_be_sorted_unique")
        edge_ids = tuple(item.identity for item in self.edges)
        if edge_ids != tuple(sorted(set(edge_ids))):
            raise EvidenceGraphError("evidence_graph.edges_must_be_sorted_unique")
        _sorted_ids(self.terminal_ids, "evidence_graph.terminal_ids")
        if not self.terminal_ids:
            raise EvidenceGraphError("evidence_graph.terminal_ids_required")
        nodes = {item.node_id: item for item in self.nodes}
        if not set(self.terminal_ids).issubset(nodes):
            raise EvidenceGraphError("evidence_graph.terminal_missing")
        incoming: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        outgoing: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in self.edges:
            source = nodes.get(edge.source_id)
            target = nodes.get(edge.target_id)
            if source is None or target is None:
                raise EvidenceGraphError("evidence_graph.edge_endpoint_missing")
            if (
                edge.source_content_hash != source.content_hash
                or edge.target_content_hash != target.content_hash
            ):
                raise EvidenceGraphError("evidence_graph.edge_content_hash_mismatch")
            incoming[target.node_id].add(source.node_id)
            outgoing[source.node_id].add(target.node_id)
        for node in self.nodes:
            if incoming[node.node_id] != set(node.parent_ids):
                raise EvidenceGraphError(
                    f"evidence_graph.missing_or_extra_parent_edge:{node.node_id}"
                )
        actual_terminals = tuple(
            sorted(node_id for node_id, targets in outgoing.items() if not targets)
        )
        if actual_terminals != self.terminal_ids:
            raise EvidenceGraphError("evidence_graph.terminal_ids_mismatch")
        self._assert_acyclic(incoming, outgoing)
        reachable = self._nodes_reaching_terminals(outgoing)
        if reachable != set(nodes):
            raise EvidenceGraphError("evidence_graph.orphan_node")
        object.__setattr__(self, "content_hash", _hash_payload(self.identity_payload()))

    def _assert_acyclic(
        self,
        incoming: Mapping[str, set[str]],
        outgoing: Mapping[str, set[str]],
    ) -> None:
        indegree = {node_id: len(parents) for node_id, parents in incoming.items()}
        queue = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
        visited = 0
        while queue:
            node_id = queue.popleft()
            visited += 1
            for target_id in sorted(outgoing[node_id]):
                indegree[target_id] -= 1
                if indegree[target_id] == 0:
                    queue.append(target_id)
        if visited != len(self.nodes):
            raise EvidenceGraphError("evidence_graph.cycle_detected")

    def _nodes_reaching_terminals(
        self,
        outgoing: Mapping[str, set[str]],
    ) -> set[str]:
        memo: dict[str, bool] = {}

        def reaches(node_id: str) -> bool:
            if node_id in memo:
                return memo[node_id]
            result = node_id in self.terminal_ids or any(
                reaches(target_id) for target_id in outgoing[node_id]
            )
            memo[node_id] = result
            return result

        return {item.node_id for item in self.nodes if reaches(item.node_id)}

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "version": self.version,
            "nodes": [item.as_dict() for item in self.nodes],
            "edges": [item.as_dict() for item in self.edges],
            "terminal_ids": list(self.terminal_ids),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "EvidenceGraph":
        payload = _mapping(value, "evidence_graph")
        _exact(
            payload,
            frozenset(
                {
                    "schema_version",
                    "graph_id",
                    "version",
                    "nodes",
                    "edges",
                    "terminal_ids",
                    "content_hash",
                }
            ),
            "evidence_graph",
        )
        if payload["schema_version"] != EVIDENCE_GRAPH_SCHEMA_VERSION:
            raise EvidenceGraphError("evidence_graph.schema_version_invalid")
        raw_nodes = payload["nodes"]
        raw_edges = payload["edges"]
        terminal_ids = payload["terminal_ids"]
        if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
            raise EvidenceGraphError("evidence_graph.nodes_or_edges_array_required")
        if not isinstance(terminal_ids, list) or any(
            not isinstance(item, str) for item in terminal_ids
        ):
            raise EvidenceGraphError("evidence_graph.terminal_ids_array_required")
        result = cls(
            graph_id=_require_id(payload["graph_id"], "evidence_graph.graph_id"),
            version=_require_id(payload["version"], "evidence_graph.version"),
            nodes=tuple(EvidenceNode.from_dict(item) for item in raw_nodes),
            edges=tuple(EvidenceEdge.from_dict(item) for item in raw_edges),
            terminal_ids=tuple(cast(list[str], terminal_ids)),
        )
        if payload["content_hash"] != result.content_hash:
            raise EvidenceGraphError("evidence_graph.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class EvidenceGraphResolver:
    """Bidirectional human- and machine-readable lineage queries."""

    graph: EvidenceGraph

    def __post_init__(self) -> None:
        if not isinstance(self.graph, EvidenceGraph):
            raise EvidenceGraphError("evidence_resolver.graph_required")

    @property
    def _nodes(self) -> dict[str, EvidenceNode]:
        return {item.node_id: item for item in self.graph.nodes}

    @property
    def _incoming(self) -> dict[str, tuple[EvidenceEdge, ...]]:
        result: dict[str, list[EvidenceEdge]] = {
            item.node_id: [] for item in self.graph.nodes
        }
        for edge in self.graph.edges:
            result[edge.target_id].append(edge)
        return {
            node_id: tuple(sorted(edges, key=lambda item: item.identity))
            for node_id, edges in result.items()
        }

    @property
    def _outgoing(self) -> dict[str, tuple[EvidenceEdge, ...]]:
        result: dict[str, list[EvidenceEdge]] = {
            item.node_id: [] for item in self.graph.nodes
        }
        for edge in self.graph.edges:
            result[edge.source_id].append(edge)
        return {
            node_id: tuple(sorted(edges, key=lambda item: item.identity))
            for node_id, edges in result.items()
        }

    def node(self, node_id: str) -> EvidenceNode:
        _require_id(node_id, "evidence_resolver.node_id")
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise EvidenceGraphError("evidence_resolver.node_not_found") from exc

    def verify_content(self, node_id: str, payload: bytes) -> None:
        node = self.node(node_id)
        if _hash_bytes(payload) != node.content_hash:
            raise EvidenceGraphError("evidence_resolver.content_hash_mismatch")

    def _query_ids(self, node_id: str, *, upstream: bool) -> tuple[str, ...]:
        self.node(node_id)
        edges = self._incoming if upstream else self._outgoing
        seen: set[str] = set()
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            adjacent = edges[current]
            queue.extend(
                edge.source_id if upstream else edge.target_id for edge in adjacent
            )
        return tuple(sorted(seen))

    def machine_query(
        self,
        node_id: str,
        *,
        direction: str = "upstream",
    ) -> dict[str, object]:
        if direction not in {"upstream", "downstream"}:
            raise EvidenceGraphError("evidence_resolver.direction_invalid")
        ids = set(self._query_ids(node_id, upstream=direction == "upstream"))
        edges = tuple(
            item
            for item in self.graph.edges
            if item.source_id in ids and item.target_id in ids
        )
        payload = {
            "schema_version": 1,
            "graph_id": self.graph.graph_id,
            "graph_hash": self.graph.content_hash,
            "root_node_id": node_id,
            "direction": direction,
            "nodes": [
                self._nodes[item].as_dict() for item in sorted(ids)
            ],
            "edges": [item.as_dict() for item in edges],
        }
        return {**payload, "query_hash": _hash_payload(payload)}

    def human_query(
        self,
        node_id: str,
        *,
        direction: str = "upstream",
    ) -> str:
        payload = self.machine_query(node_id, direction=direction)
        arrow = "<-" if direction == "upstream" else "->"
        lines = [
            f"Evidence graph {self.graph.graph_id}@{self.graph.version}",
            f"{direction} lineage for {node_id}",
        ]
        for node in cast(list[dict[str, object]], payload["nodes"]):
            parent_ids = cast(list[str], node["parent_ids"])
            parent_text = ",".join(parent_ids) if parent_ids else "ROOT"
            lines.append(
                f"- {node['node_id']} [{node['kind']}] {arrow} {parent_text} "
                f"{node['content_hash']}"
            )
        lines.append(f"query_hash={payload['query_hash']}")
        return "\n".join(lines) + "\n"


# Public compatibility spelling used by package/report audit tooling.
EvidenceResolver = EvidenceGraphResolver


__all__ = [
    "EVIDENCE_GRAPH_SCHEMA_VERSION",
    "EvidenceEdge",
    "EvidenceGraph",
    "EvidenceGraphError",
    "EvidenceGraphResolver",
    "EvidenceResolver",
    "EvidenceNode",
    "EvidenceNodeKind",
    "EvidenceRelation",
]
