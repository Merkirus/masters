#!/usr/bin/env python3
"""
Parser BM (Behavior Flow/Behavior Model) XMI from Enterprise Architect to a simple IR.

Scope:
- sequence diagram metadata
- behavior endpoints (MessageEndpoint)
- lifelines (ClassifierRole)
- messages: sync/self/return, arguments, return assignment, return type
- combined fragments: break/alt/opt/loop, guards/operands
- geometric message -> fragment assignment
- optional validation/enrichment with structural_ir.json

This parser intentionally does NOT parse DTO/entity/endpoint contract structures.
Those belong to the structural parser.
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

UML_NS = "omg.org/UML1.3"
NS = {"UML": UML_NS}

# EA-specific InteractionFragment ntype mapping observed in the current model.
EA_FRAGMENT_KIND = {
    "0": "alt",
    "1": "opt",
    "2": "break",
    "4": "loop",
}

PRIMITIVE_TYPES = {
    "STRING", "BOOLEAN", "INTEGER", "LONG", "FLOAT", "DOUBLE", "DECIMAL",
    "UUID", "DATE", "DATETIME", "void", "VOID"
}


@dataclass
class EndpointIR:
    id: str
    name: str
    kind: str = "endpoint"
    bounds: Optional[dict[str, float]] = None


@dataclass
class LifelineIR:
    id: str
    name: str
    represents: Optional[str] = None
    role: Optional[str] = None
    bounds: Optional[dict[str, float]] = None


@dataclass
class MessageIR:
    id: str
    seq: Optional[int]
    kind: str  # sync | self | return
    source: Optional[str]
    target: Optional[str]
    sourceId: Optional[str] = None
    targetId: Optional[str] = None
    operation: Optional[str] = None
    operationGuid: Optional[str] = None
    operationRef: Optional[str] = None
    arguments: list[str] = field(default_factory=list)
    assignTo: Optional[str] = None
    declaredReturnType: Optional[str] = None
    returnMode: Optional[str] = None  # assignment | forward
    returnType: Optional[str] = None
    returnValue: Optional[str] = None
    fragment: Optional[str] = None
    geometry: Optional[dict[str, float]] = None
    rawLabel: Optional[str] = None


@dataclass
class OperandIR:
    guard: Optional[str]
    messages: list[str] = field(default_factory=list)
    fragments: list[str] = field(default_factory=list)
    size: Optional[float] = None
    bounds: Optional[dict[str, float]] = None


@dataclass
class FragmentIR:
    id: str
    name: str
    kind: Optional[str]
    bounds: Optional[dict[str, float]] = None
    operands: list[OperandIR] = field(default_factory=list)
    parent: Optional[str] = None
    children: list[str] = field(default_factory=list)
    rawEaNtype: Optional[str] = None


@dataclass
class BehaviorIR:
    id: Optional[str]
    name: Optional[str]
    represents: Optional[str] = None
    endpoints: list[EndpointIR] = field(default_factory=list)
    lifelines: list[LifelineIR] = field(default_factory=list)
    messages: list[MessageIR] = field(default_factory=list)
    fragments: list[FragmentIR] = field(default_factory=list)


@dataclass
class BmIR:
    metadata: dict[str, Any]
    bm: dict[str, Any]
    diagnostics: list[dict[str, str]] = field(default_factory=list)


# -------------------------
# XML helpers
# -------------------------

def parse_xml(path: str | Path) -> ET.Element:
    return ET.parse(path).getroot()


def xmi_id(el: ET.Element) -> Optional[str]:
    return el.attrib.get("xmi.id") or el.attrib.get("xmi.idref")


def tag_values(el: ET.Element) -> dict[str, str]:
    return {
        tv.attrib.get("tag"): tv.attrib.get("value", "")
        for tv in el.findall("./UML:ModelElement.taggedValue/UML:TaggedValue", NS)
        if tv.attrib.get("tag")
    }


def parse_kv_semicolon(text: Optional[str]) -> dict[str, str]:
    if not text:
        return {}
    out: dict[str, str] = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_bounds(geometry: Optional[str]) -> Optional[dict[str, float]]:
    if not geometry:
        return None
    pairs = dict(re.findall(r"(Left|Top|Right|Bottom)=(-?\d+(?:\.\d+)?)", geometry))
    if not pairs:
        return None
    return {k.lower(): float(v) for k, v in pairs.items()}


def parse_sequence_points(text: Optional[str]) -> Optional[dict[str, float]]:
    if not text:
        return None
    pairs = dict(re.findall(r"(PtStartX|PtStartY|PtEndX|PtEndY)=(-?\d+(?:\.\d+)?)", text))
    if not pairs:
        return None
    sx = float(pairs.get("PtStartX", 0))
    ex = float(pairs.get("PtEndX", sx))
    sy = abs(float(pairs.get("PtStartY", 0)))
    ey = abs(float(pairs.get("PtEndY", sy)))
    return {
        "xStart": sx,
        "xEnd": ex,
        "xMin": min(sx, ex),
        "xMax": max(sx, ex),
        "xMid": (sx + ex) / 2,
        "yStart": sy,
        "yEnd": ey,
        "yMid": (sy + ey) / 2,
    }


def normalize_guid(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip().strip("{}").lower()
    value = re.sub(r"^eaid[_-]", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def split_values(text: Optional[str]) -> list[str]:
    """Split EA paramvalues while preserving simple quoted strings."""
    if not text:
        return []
    values: list[str] = []
    buf: list[str] = []
    in_quote = False
    quote_char = ""
    for ch in text:
        if ch in ('"', "'"):
            if not in_quote:
                in_quote = True
                quote_char = ch
            elif quote_char == ch:
                in_quote = False
            buf.append(ch)
        elif ch == "," and not in_quote:
            val = "".join(buf).strip()
            if val:
                values.append(val)
            buf = []
        else:
            buf.append(ch)
    val = "".join(buf).strip()
    if val:
        values.append(val)
    return values


def operation_name_from_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return label.split("(", 1)[0].strip() or None


def parse_partition_specs(tags: dict[str, str], element_id: str) -> list[dict[str, Any]]:
    """Read EA xref partition/operand definitions from $ea_xref_property.

    EA stores combined-fragment operands as @PAR entries, usually with fields like:
      @PAR;Name=else;Size=63;@ENDPAR;

    `Name` is the guard label and `Size` is the vertical height of the operand
    inside the fragment frame. This lets us assign messages to concrete operands
    by their y coordinate.
    """
    raw = tags.get("$ea_xref_property") or ""
    if not raw:
        return []
    # Make sure the xref belongs to this element if CLT exists.
    clt = re.search(r"\$CLT=\{([^}]+)\}\$CLT", raw)
    if clt and normalize_guid(clt.group(1)) != normalize_guid(element_id):
        return []

    specs: list[dict[str, Any]] = []
    for block in re.findall(r"@PAR;(.*?);@ENDPAR;", raw, flags=re.S):
        name_match = re.search(r"(?:^|;)Name=([^;]*)(?:;|$)", block)
        size_match = re.search(r"(?:^|;)Size=(-?\d+(?:\.\d+)?)(?:;|$)", block)
        name = name_match.group(1).strip() if name_match else None
        if not name:
            continue
        size = float(size_match.group(1)) if size_match else None
        specs.append({"guard": name, "size": size})

    # Older/other exports may expose only Name values without @ENDPAR blocks.
    if not specs:
        names = re.findall(r"@PAR;Name=([^;]+);", raw)
        specs = [{"guard": n.strip(), "size": None} for n in names if n.strip()]
    return specs


def parse_partition_guards(tags: dict[str, str], element_id: str) -> list[str]:
    return [spec["guard"] for spec in parse_partition_specs(tags, element_id)]


# -------------------------
# Structural IR optional enrichment
# -------------------------

def load_structural_index(path: Optional[str | Path]) -> dict[str, Any]:
    if not path:
        return {"roles": {}, "operationsByGuid": {}, "endpointByOperationGuid": {}, "knownTypes": set(PRIMITIVE_TYPES)}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    roles: dict[str, str] = {}
    operations_by_guid: dict[str, dict[str, Any]] = {}
    endpoint_by_op_guid: dict[str, str] = {}
    known_types: set[str] = set(PRIMITIVE_TYPES)

    for dto in data.get("api", {}).get("dtos", []):
        known_types.add(dto.get("name"))
    for enum in data.get("api", {}).get("enums", []):
        known_types.add(enum.get("name"))
    for ent in data.get("persistence", {}).get("entities", []):
        known_types.add(ent.get("name"))
    for resp in data.get("bifm", {}).get("responses", []):
        known_types.add(resp.get("name"))
    known_types.add("HttpResponse")

    for iface in data.get("bifm", {}).get("behaviorInterfaces", []):
        name = iface.get("name")
        if name:
            roles[name] = iface.get("role")
        for op in iface.get("operations", []):
            op_id = op.get("id")
            if op_id:
                ref = f"{name}.{op.get('name')}" if name else op.get("name")
                entry = dict(op)
                entry["operationRef"] = ref
                entry["owner"] = name
                operations_by_guid[normalize_guid(op_id)] = entry
                if op.get("mapsTo"):
                    endpoint_by_op_guid[normalize_guid(op_id)] = op.get("mapsTo")

    return {
        "roles": roles,
        "operationsByGuid": operations_by_guid,
        "endpointByOperationGuid": endpoint_by_op_guid,
        "knownTypes": known_types,
    }



def looks_like_type(value: Optional[str], known_types: set[str]) -> bool:
    """Heuristic for EA retval values. Types are known structural types, primitives,
    or conventionally PascalCase identifiers like UserDto/CreateUserResponse.
    Lower camelCase values such as response/errorDto are treated as variables.
    """
    if not value:
        return False
    value = value.strip()
    if value in known_types:
        return True
    if value in PRIMITIVE_TYPES:
        return True
    # void is a return type, even though lowercase.
    if value.lower() == "void":
        return True
    # UML classes are expected to begin with uppercase in this method.
    return bool(re.match(r"^[A-Z][A-Za-z0-9_]*(Dto|Entity|Response|Operation|Role|Status|Method)?$", value))


def classify_return(raw_retval: Optional[str], assign_to: Optional[str], known_types: set[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Return messages have two semantics in this IR:
    - assignment: x = :Type, used to declare/assign result of previous call;
    - forward: :x or x = :x, used to return/pass an existing variable.

    For forward returns we intentionally do not infer a returnType. The generator
    only needs `returnValue`, e.g. `return response;`.
    """
    val = raw_retval.strip() if raw_retval else None
    if not val:
        return "forward", None, None
    if assign_to and val == assign_to:
        return "forward", None, val
    if not looks_like_type(val, known_types):
        return "forward", None, val
    return "assignment", val, val

# -------------------------
# BM parser
# -------------------------

def parse_bm(path: str | Path, structural: Optional[str | Path] = None, geometry_mode: str = "vertical") -> BmIR:
    root = parse_xml(path)
    structural_idx = load_structural_index(structural)
    diagnostics: list[dict[str, str]] = []

    diagram = root.find(".//UML:Diagram", NS)
    behavior = BehaviorIR(id=xmi_id(diagram) if diagram is not None else None,
                          name=diagram.attrib.get("name") if diagram is not None else "Behavior")

    # Diagram element geometry by subject id.
    geometry_by_subject: dict[str, dict[str, float]] = {}
    for de in root.findall(".//UML:DiagramElement", NS):
        subject = de.attrib.get("subject")
        b = parse_bounds(de.attrib.get("geometry"))
        if subject and b:
            geometry_by_subject[subject] = b

    # Lifelines.
    for ll in root.findall(".//UML:ClassifierRole", NS):
        eid = xmi_id(ll) or ""
        name = ll.attrib.get("name") or ""
        behavior.lifelines.append(LifelineIR(
            id=eid,
            name=name,
            represents=name,
            role=structural_idx["roles"].get(name),
            bounds=geometry_by_subject.get(eid),
        ))

    # Classes: endpoints and fragments.
    endpoint_ids: set[str] = set()
    raw_fragments: dict[str, FragmentIR] = {}
    for cls in root.findall(".//UML:Class", NS):
        eid = xmi_id(cls) or ""
        name = cls.attrib.get("name") or ""
        tags = tag_values(cls)
        stype = tags.get("ea_stype")
        if stype == "MessageEndpoint":
            endpoint_ids.add(eid)
            behavior.endpoints.append(EndpointIR(id=eid, name=name, bounds=geometry_by_subject.get(eid)))
        elif stype == "InteractionFragment":
            raw_ntype = tags.get("ea_ntype")
            kind = EA_FRAGMENT_KIND.get(raw_ntype or "", raw_ntype)
            partition_specs = parse_partition_specs(tags, eid)
            if not partition_specs:
                # Some exports may use direct condition/guard tags.
                for key in ("condition", "guard"):
                    if tags.get(key):
                        partition_specs.append({"guard": tags[key], "size": None})
            operands = [OperandIR(guard=spec.get("guard"), size=spec.get("size")) for spec in partition_specs] or [OperandIR(guard=None)]
            frag_bounds = geometry_by_subject.get(eid)
            raw_fragments[eid] = FragmentIR(
                id=eid,
                name=name,
                kind=kind,
                bounds=frag_bounds,
                operands=operands,
                rawEaNtype=raw_ntype,
            )
            compute_operand_bounds(raw_fragments[eid])

    # Keep geometry-backed fragments. EA may export duplicated interaction fragments without diagram bounds.
    for frag in raw_fragments.values():
        if frag.bounds:
            behavior.fragments.append(frag)
        else:
            diagnostics.append({
                "level": "warning",
                "code": "FRAGMENT_WITHOUT_GEOMETRY_SKIPPED",
                "message": f"Fragment {frag.name} ({frag.id}) has no diagram bounds and was not used for message assignment."
            })

    # ID -> displayed name for endpoints/lifelines.
    participant_name_by_id = {e.id: e.name for e in behavior.endpoints}
    participant_name_by_id.update({l.id: l.name for l in behavior.lifelines})

    # Fragment nesting by geometry. If a fragment is inside another fragment, keep the relation.
    assign_fragment_nesting(behavior)

    # Messages.
    messages: list[MessageIR] = []
    variable_types: dict[str, str] = {}
    for m in root.findall(".//UML:Message", NS):
        mid = xmi_id(m) or ""
        tags = tag_values(m)
        kv = parse_kv_semicolon(tags.get("privatedata2"))
        styleex = parse_kv_semicolon(tags.get("styleex"))
        seq: Optional[int]
        try:
            seq = int(tags.get("seqno", ""))
        except ValueError:
            seq = None

        source_id = m.attrib.get("sender")
        target_id = m.attrib.get("receiver")
        source = tags.get("ea_sourceName") or participant_name_by_id.get(source_id)
        target = tags.get("ea_targetName") or participant_name_by_id.get(target_id)
        geom = parse_sequence_points(tags.get("sequence_points"))
        operation_guid = tags.get("operation_guid")
        op_info = structural_idx["operationsByGuid"].get(normalize_guid(operation_guid)) if operation_guid else None
        operation = operation_name_from_label(m.attrib.get("name"))
        operation_ref = None
        declared_return_type = kv.get("retval")

        if op_info:
            operation = op_info.get("name") or operation
            operation_ref = op_info.get("operationRef")
            declared_return_type = op_info.get("returnType") or declared_return_type

        raw_retval = kv.get("retval")
        assign_to = kv.get("retatt")
        is_return = tags.get("privatedata4") == "1" or (not m.attrib.get("name") and ("retatt" in kv or "retval" in kv))

        return_mode: Optional[str] = None
        return_type: Optional[str] = None
        return_value: Optional[str] = None
        if is_return:
            kind = "return"
            return_mode, return_type, return_value = classify_return(raw_retval, assign_to, structural_idx.get("knownTypes", set(PRIMITIVE_TYPES)))
        else:
            kind = "self" if source and target and source == target else "sync"
            return_type = declared_return_type

        args = split_values(styleex.get("paramvalues"))
        if not args:
            args = split_values(kv.get("paramsDlg"))

        msg = MessageIR(
            id=mid,
            seq=seq,
            kind=kind,
            source=source,
            target=target,
            sourceId=source_id,
            targetId=target_id,
            operation=operation if not is_return else None,
            operationGuid=operation_guid,
            operationRef=operation_ref,
            arguments=args,
            assignTo=assign_to,
            declaredReturnType=declared_return_type if not is_return else None,
            returnMode=return_mode,
            returnType=return_type,
            returnValue=return_value if is_return else None,
            geometry=geom,
            rawLabel=tags.get("mt") or m.attrib.get("name"),
        )
        messages.append(msg)

        # Track only assignment returns. Forward returns such as :response do not create types.
        if is_return and return_mode == "assignment" and assign_to and return_type:
            variable_types[assign_to] = return_type

    messages.sort(key=lambda mm: (mm.seq if mm.seq is not None else 10**9))
    behavior.messages = messages

    # Represents: preferably from first endpoint operation message mapped by structural IR.
    for msg in behavior.messages:
        if msg.operationGuid:
            endpoint_name = structural_idx["endpointByOperationGuid"].get(normalize_guid(msg.operationGuid))
            if endpoint_name:
                behavior.represents = endpoint_name
                break
    if not behavior.represents:
        first_op = next((m.operation for m in behavior.messages if m.operation and m.operation.endswith("Operation")), None)
        if first_op:
            behavior.represents = first_op[0].upper() + first_op[1:]
            diagnostics.append({
                "level": "warning",
                "code": "REPRESENTS_INFERRED_BY_NAME",
                "message": f"Behavior represents was inferred as {behavior.represents}. Provide structural IR for stronger resolution."
            })

    # Assign messages to fragments.
    assign_messages_to_fragments(behavior, geometry_mode=geometry_mode, diagnostics=diagnostics)

    # Basic validation with structural IR.
    validate_behavior(behavior, structural_idx, diagnostics)

    # DEBUG_KEYS = {"geometry", "bounds", "rawEaNtype", "rawLabel", "size"}

    # def strip_debug_fields(obj):
    #     if isinstance(obj, dict):
    #         return {
    #             key: strip_debug_fields(value)
    #             for key, value in obj.items()
    #             if key not in DEBUG_KEYS
    #         }

    #     if isinstance(obj, list):
    #         return [strip_debug_fields(item) for item in obj]

    #     return obj

    # behavior_clean = strip_debug_fields(asdict(behavior))

    return BmIR(
        metadata={
            "stage": "behavior",
            "description": "Behavior sequence diagram IR parsed from EA XMI. Structural API/Persistence/BIFM IR is not embedded.",
            "source": str(path),
            "geometryMode": geometry_mode,
            "structuralValidation": bool(structural),
        },
        bm={"behaviors": [asdict(behavior)]},
        diagnostics=diagnostics,
    )



def compute_operand_bounds(frag: FragmentIR) -> None:
    """Compute operand vertical bounds from EA partition sizes when available.

    For EA combined fragments, partition Size values normally sum to fragment height.
    We keep the full fragment x-range and split only the y-range. If sizes are
    missing, a single operand gets the whole fragment bounds; multiple operands
    without sizes are left without operand bounds and parser will fall back to
    the first operand with a diagnostic.
    """
    if not frag.bounds or not frag.operands:
        return

    b = frag.bounds
    # Single operand: it owns the full fragment.
    if len(frag.operands) == 1:
        frag.operands[0].bounds = dict(b)
        return

    sizes = [op.size for op in frag.operands]
    if any(size is None for size in sizes):
        return

    y = b["top"]
    for i, op in enumerate(frag.operands):
        size = float(op.size or 0)
        # Last operand ends exactly at fragment bottom to avoid rounding gaps.
        bottom = b["bottom"] if i == len(frag.operands) - 1 else y + size
        op.bounds = {"left": b["left"], "right": b["right"], "top": y, "bottom": bottom}
        y = bottom


def operand_contains_message(op: OperandIR, msg: MessageIR) -> bool:
    if not op.bounds or not msg.geometry:
        return False
    y = msg.geometry["yMid"]
    return op.bounds["top"] <= y <= op.bounds["bottom"]

def fragment_area(frag: FragmentIR) -> float:
    b = frag.bounds or {}
    return abs((b.get("right", 0) - b.get("left", 0)) * (b.get("bottom", 0) - b.get("top", 0)))


def bounds_contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
    return (
        outer["left"] <= inner["left"] <= inner["right"] <= outer["right"]
        and outer["top"] <= inner["top"] <= inner["bottom"] <= outer["bottom"]
    )


def assign_fragment_nesting(behavior: BehaviorIR) -> None:
    for child in behavior.fragments:
        if not child.bounds:
            continue
        candidates = [
            parent for parent in behavior.fragments
            if parent.id != child.id and parent.bounds and bounds_contains(parent.bounds, child.bounds)
        ]
        if not candidates:
            continue
        parent = min(candidates, key=fragment_area)
        child.parent = parent.id
        if child.id not in parent.children:
            parent.children.append(child.id)
        if parent.operands:
            parent.operands[0].fragments.append(child.id)


def message_fits_fragment(msg: MessageIR, frag: FragmentIR, geometry_mode: str) -> bool:
    if not msg.geometry or not frag.bounds:
        return False
    g = msg.geometry
    b = frag.bounds
    y_inside = b["top"] <= g["yMid"] <= b["bottom"]
    if not y_inside:
        return False
    if geometry_mode == "vertical":
        return True
    # overlap: message segment intersects fragment horizontally.
    x_overlap = g["xMin"] <= b["right"] and g["xMax"] >= b["left"]
    if geometry_mode == "overlap":
        return x_overlap
    # point: midpoint must be inside bbox.
    return b["left"] <= g["xMid"] <= b["right"]


def assign_messages_to_fragments(behavior: BehaviorIR, geometry_mode: str, diagnostics: list[dict[str, str]]) -> None:
    for msg in behavior.messages:
        candidates = [frag for frag in behavior.fragments if message_fits_fragment(msg, frag, geometry_mode)]
        if not candidates:
            continue
        owner = min(candidates, key=fragment_area)
        msg.fragment = owner.id
        if not owner.operands:
            owner.operands.append(OperandIR(guard=None, bounds=dict(owner.bounds) if owner.bounds else None))

        operand_owner = None
        # If EA partition sizes are available, route the message to the concrete operand.
        for op in owner.operands:
            if operand_contains_message(op, msg):
                operand_owner = op
                break

        if operand_owner is None:
            # Fallback for fragments without operand geometry. This is expected for
            # single-operand break/opt/loop; for multi-operand alt it is diagnostic-worthy.
            operand_owner = owner.operands[0]

        operand_owner.messages.append(msg.id)

    for frag in behavior.fragments:
        if len(frag.operands) > 1 and not all(op.bounds for op in frag.operands):
            diagnostics.append({
                "level": "warning",
                "code": "MULTI_OPERAND_NOT_SPLIT",
                "message": f"Fragment {frag.name} has {len(frag.operands)} guards/operands, but operand geometry was not available. Messages were assigned to the first operand."
            })


def validate_behavior(behavior: BehaviorIR, structural_idx: dict[str, Any], diagnostics: list[dict[str, str]]) -> None:
    if not behavior.represents:
        diagnostics.append({"level": "warning", "code": "MISSING_REPRESENTS", "message": "Behavior does not resolve to an endpointOperation."})

    known_types = structural_idx.get("knownTypes", set())
    if known_types:
        for msg in behavior.messages:
            rt = msg.returnType or msg.declaredReturnType
            if rt and rt not in known_types and rt not in {"response"}:
                diagnostics.append({
                    "level": "warning",
                    "code": "UNKNOWN_RETURN_TYPE",
                    "message": f"Message {msg.seq} has return type {rt}, which was not found in structural IR."
                })

    for frag in behavior.fragments:
        if frag.kind in {"break", "opt", "loop", "alt"}:
            guards = [op.guard for op in frag.operands if op.guard]
            if not guards:
                diagnostics.append({
                    "level": "warning",
                    "code": "FRAGMENT_WITHOUT_GUARD",
                    "message": f"Fragment {frag.name} ({frag.kind}) has no guard/operand condition."
                })

def main() -> None:
    ap = argparse.ArgumentParser(description="Parse EA BM sequence XMI into Behavior IR JSON.")
    ap.add_argument("--bm", required=True, help="Behavior Model XMI file")
    ap.add_argument("--structural", help="Optional structural_ir.json for validation/enrichment")
    ap.add_argument("--out", default=Path("bm_ir.json"), help="Output BM IR JSON")
    ap.add_argument("--geometry-mode", choices=["vertical", "overlap", "point"], default="vertical",
                    help="Message-to-fragment assignment mode. Default vertical uses y containment only.")
    args = ap.parse_args()

    ir = parse_bm(args.bm, structural=args.structural, geometry_mode=args.geometry_mode)
    Path(args.out).write_text(json.dumps(asdict(ir), ensure_ascii=False, indent=2), encoding="utf-8")

    behaviors = ir.bm.get("behaviors", [])
    b = behaviors[0] if behaviors else {}
    print("BM IR written to", args.out)
    print("Behaviors:", len(behaviors))
    print("Lifelines:", len(b.get("lifelines", [])))
    print("Endpoints:", len(b.get("endpoints", [])))
    print("Messages:", len(b.get("messages", [])))
    print("Fragments:", len(b.get("fragments", [])))
    print("Diagnostics:", len(ir.diagnostics))


if __name__ == "__main__":
    main()
