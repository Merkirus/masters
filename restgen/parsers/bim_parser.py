#!/usr/bin/env python3
"""
Parser XMI -> IR for Behavior Interface Model (BIM).

Scope:
- behavior lifeline interfaces and their operations
- endpoint operations and their HTTP method / URI
- parameter sets and parameter fields with BODY/PATH/QUERY/HEADER/COOKIE locations
- model HTTP responses with status + responseBody
- endpoint operation links to parameters and responses
- endpoint operation mapping from interface operation <<endpointOperation>> to ApiOperation

Usage:
  python parser_bim_ir.py --bim bim-xmi.xml --out bim_ir.json

The output is intentionally backend-friendly and independent from Enterprise Architect/XMI.
It is meant to be merged later with API DTO/Persistence IR and, later, Behavior IR.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

NS = {"UML": "omg.org/UML1.3"}

ENDPOINT_OPERATION_ST = {"endpointoperation"}
PARAMETERS_ST = {"parameters", "apioperationparameters"}
RESPONSE_ST = {"response", "httpresponse"}
INTERFACE_ROLE_SUFFIXES = {
    "controller": "controller",
    "service": "service",
    "repository": "repository",
}
PARAMETER_LOCATIONS = {"BODY", "PATH", "QUERY", "HEADER", "COOKIE"}


def xmi_id(el: ET.Element) -> Optional[str]:
    return el.get("xmi.id") or el.get("{http://www.omg.org/XMI}id")


def tagged_values(el: ET.Element) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for tv in el.findall("./UML:ModelElement.taggedValue/UML:TaggedValue", NS):
        tag = tv.get("tag")
        if tag is not None:
            tags[tag] = tv.get("value") or ""
    return tags


def stereotype_names(el: ET.Element) -> List[str]:
    vals: List[str] = []
    for st in el.findall("./UML:ModelElement.stereotype/UML:Stereotype", NS):
        if st.get("name"):
            vals.append(st.get("name") or "")
    tags = tagged_values(el)
    if tags.get("stereotype"):
        vals.append(tags["stereotype"])
    # EA often stores stereotypes in xref payloads.
    xref = tags.get("$ea_xref_property", "")
    for m in re.finditer(r"Name=([^;@]+)", xref):
        vals.append(m.group(1))
    # unique, preserve order
    seen = set()
    out = []
    for v in vals:
        key = norm_st(v)
        if key and key not in seen:
            seen.add(key)
            out.append(v.strip())
    return out


def stereotype_of(el: ET.Element) -> Optional[str]:
    sts = stereotype_names(el)
    return sts[0] if sts else None


def norm_st(st: Optional[str]) -> str:
    return (st or "").strip().strip("<>").lower()


def has_st(el: ET.Element, allowed: set[str]) -> bool:
    return any(norm_st(s) in allowed for s in stereotype_names(el))


def initial_value(attr: ET.Element) -> Optional[str]:
    expr = attr.find("./UML:Attribute.initialValue/UML:Expression", NS)
    if expr is not None:
        val = expr.get("body")
        if val not in (None, ""):
            return val
    return None


def attr_type(attr: ET.Element, id_to_name: Dict[str, str] | None = None) -> Optional[str]:
    tags = tagged_values(attr)
    if tags.get("type"):
        return tags["type"]
    # StructuralFeature.type fallback.
    cref = attr.find("./UML:StructuralFeature.type/UML:Classifier", NS)
    if cref is not None:
        ref = cref.get("xmi.idref")
        if ref and id_to_name and ref in id_to_name:
            return id_to_name[ref]
    return attr.get("type")


def attr_multiplicity(attr: ET.Element) -> str:
    tags = tagged_values(attr)
    lower = tags.get("lowerBound") or "1"
    upper = tags.get("upperBound") or "1"
    if upper in {"-1", "*"}:
        upper = "*"
    if lower == upper:
        return lower
    return f"{lower}..{upper}"


def multiplicity_flags(multiplicity: str) -> Tuple[bool, bool]:
    if multiplicity in {"1", "1..1"}:
        return True, False
    if multiplicity in {"0", "0..1"}:
        return False, False
    if multiplicity in {"*", "0..*"}:
        return False, True
    if multiplicity == "1..*":
        return True, True
    return not multiplicity.startswith("0"), "*" in multiplicity


def status_code(status: Optional[str]) -> Optional[int]:
    if not status:
        return None
    m = re.search(r"(\d{3})", status)
    return int(m.group(1)) if m else None


def parse_classes(root: ET.Element) -> Dict[str, ET.Element]:
    result: Dict[str, ET.Element] = {}
    for cls in root.findall(".//UML:Class", NS):
        cid = xmi_id(cls)
        name = cls.get("name")
        if cid and name and name != "EARootClass":
            result[cid] = cls
    return result


def parse_interfaces(root: ET.Element) -> Dict[str, ET.Element]:
    result: Dict[str, ET.Element] = {}
    for intf in root.findall(".//UML:Interface", NS):
        iid = xmi_id(intf)
        name = intf.get("name")
        if iid and name:
            result[iid] = intf
    return result


def parse_all_named_elements(root: ET.Element) -> Dict[str, str]:
    id_to_name: Dict[str, str] = {}
    for tag in ["Class", "Interface", "DataType", "Enumeration"]:
        for el in root.findall(f".//UML:{tag}", NS):
            eid = xmi_id(el)
            name = el.get("name")
            if eid and name:
                id_to_name[eid] = name
    # EA primitive datatypes often have ids like eaxmiid0 and are DataType under Core.
    for dt in root.findall(".//UML:DataType", NS):
        did = xmi_id(dt)
        name = dt.get("name")
        if did and name:
            id_to_name[did] = name
    return id_to_name


def class_stereotype_index(classes_by_id: Dict[str, ET.Element]) -> Dict[str, str]:
    return {cid: norm_st(stereotype_of(cls)) for cid, cls in classes_by_id.items()}


def attrs_by_name(cls: ET.Element) -> Dict[str, ET.Element]:
    return {a.get("name") or "": a for a in cls.findall("./UML:Classifier.feature/UML:Attribute", NS) if a.get("name")}


def parse_endpoint_operations(root: ET.Element, classes_by_id: Dict[str, ET.Element], id_to_name: Dict[str, str]) -> List[Dict[str, Any]]:
    endpoint_ops: List[Dict[str, Any]] = []
    for cid, cls in classes_by_id.items():
        if not has_st(cls, ENDPOINT_OPERATION_ST):
            continue
        name = cls.get("name") or "UNKNOWN"
        attrs = attrs_by_name(cls)
        http_method = initial_value(attrs["httpMethod"]) if "httpMethod" in attrs else None
        uri = initial_value(attrs["uri"]) if "uri" in attrs else None
        endpoint_ops.append({
            "id": cid,
            "name": name,
            "operationId": None,
            "operationRef": None,
            "controller": None,
            "returnType": None,
            "httpMethod": http_method,
            "uri": uri,
            "parameters": None,
            "responses": [],
        })
    return sorted(endpoint_ops, key=lambda x: x["name"])


def parse_parameter_sets(classes_by_id: Dict[str, ET.Element], id_to_name: Dict[str, str]) -> List[Dict[str, Any]]:
    parameter_sets: List[Dict[str, Any]] = []
    for cid, cls in classes_by_id.items():
        if not has_st(cls, PARAMETERS_ST):
            continue
        fields: List[Dict[str, Any]] = []
        attrs = list(cls.findall("./UML:Classifier.feature/UML:Attribute", NS))
        attrs.sort(key=lambda a: int(tagged_values(a).get("position", "999999")) if tagged_values(a).get("position", "").isdigit() else 999999)
        for attr in attrs:
            name = attr.get("name")
            if not name:
                continue
            typ = attr_type(attr, id_to_name) or "UNKNOWN"
            mult = attr_multiplicity(attr)
            required, collection = multiplicity_flags(mult)
            st = stereotype_of(attr)
            loc = (st or "").strip("<>").upper()
            if loc not in PARAMETER_LOCATIONS:
                loc = "UNKNOWN"
            fields.append({
                "name": name,
                "type": typ,
                "location": loc,
                "multiplicity": mult,
                "required": required,
                "collection": collection,
            })
        parameter_sets.append({
            "id": cid,
            "name": cls.get("name") or "UNKNOWN",
            "stereotype": stereotype_of(cls) or "parameters",
            "fields": fields,
        })
    return sorted(parameter_sets, key=lambda x: x["name"])


def parse_responses(classes_by_id: Dict[str, ET.Element], id_to_name: Dict[str, str], generalizations: Dict[str, str]) -> List[Dict[str, Any]]:
    responses: List[Dict[str, Any]] = []
    for cid, cls in classes_by_id.items():
        if not has_st(cls, RESPONSE_ST):
            continue
        # Include HttpResponse base too, but mark it; generator can ignore as concrete response.
        name = cls.get("name") or "UNKNOWN"
        attrs = attrs_by_name(cls)
        status = initial_value(attrs["status"]) if "status" in attrs else None
        body = attr_type(attrs["responseBody"], id_to_name) if "responseBody" in attrs else None
        base_id = generalizations.get(cid)
        base_name = id_to_name.get(base_id) if base_id else None
        responses.append({
            "id": cid,
            "name": name,
            "stereotype": stereotype_of(cls) or "response",
            "status": status,
            "statusCode": status_code(status),
            "responseBody": body,
            "baseType": base_name,
            "abstractBase": name == "HttpResponse",
        })
    return sorted(responses, key=lambda x: (x["abstractBase"], x["name"]))


def assoc_ends(assoc: ET.Element) -> List[ET.Element]:
    return list(assoc.findall(".//UML:AssociationEnd", NS))


def parse_associations(root: ET.Element, classes_by_id: Dict[str, ET.Element], interfaces_by_id: Dict[str, ET.Element]) -> List[Dict[str, Any]]:
    assocs: List[Dict[str, Any]] = []
    id_to_el = {**classes_by_id, **interfaces_by_id}
    for assoc in root.findall(".//UML:Association", NS):
        name = assoc.get("name") or tagged_values(assoc).get("mt") or ""
        tags = tagged_values(assoc)
        ends_info: List[Dict[str, Optional[str]]] = []
        for end in assoc_ends(assoc):
            ref = end.get("type")
            el = id_to_el.get(ref or "")
            ends_info.append({
                "id": ref,
                "name": el.get("name") if el is not None else None,
                "kind": el.tag.split("}")[-1] if el is not None else None,
                "stereotype": norm_st(stereotype_of(el)) if el is not None else None,
            })
        assocs.append({
            "id": xmi_id(assoc),
            "name": name,
            "eaType": tags.get("ea_type"),
            "sourceName": tags.get("ea_sourceName"),
            "targetName": tags.get("ea_targetName"),
            "ends": ends_info,
        })
    return assocs


def parse_generalizations(root: ET.Element) -> Dict[str, str]:
    """Return child_id -> parent_id."""
    result: Dict[str, str] = {}
    for gen in root.findall(".//UML:Generalization", NS):
        child = gen.get("subtype")
        parent = gen.get("supertype")
        if not child or not parent:
            tags = tagged_values(gen)
            # EA sometimes stores names only in tags, but XML attributes are usually present.
        if child and parent:
            result[child] = parent
    return result


def normalize_name_for_match(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def op_name_to_endpoint_candidate(op_name: str) -> str:
    # createUserOperation -> CreateUserOperation, getUserOperation -> GetUserOperation
    parts = re.split(r"[_\s-]+", op_name)
    if len(parts) == 1:
        s = op_name[:1].upper() + op_name[1:]
    else:
        s = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return s


def parse_operation(op: ET.Element, id_to_name: Dict[str, str]) -> Dict[str, Any]:
    tags = tagged_values(op)
    params: List[Dict[str, Any]] = []
    return_type: Optional[str] = tags.get("type") or None
    for p in op.findall("./UML:BehavioralFeature.parameter/UML:Parameter", NS):
        ptags = tagged_values(p)
        kind = p.get("kind") or ptags.get("kind")
        pname = p.get("name")
        ptype = ptags.get("type") or p.get("type")
        if not ptype:
            cref = p.find("./UML:Parameter.type/UML:Classifier", NS)
            if cref is not None:
                ref = cref.get("xmi.idref")
                ptype = id_to_name.get(ref or "")
        if kind == "return" or pname is None:
            if ptype:
                return_type = ptype
        else:
            params.append({"name": pname, "type": ptype or "UNKNOWN"})
    st = stereotype_of(op)
    return {
        "id": tagged_values(op).get("ea_guid") or xmi_id(op),
        "name": op.get("name") or "UNKNOWN",
        "parameters": params,
        "returnType": return_type,
        "stereotype": st,
        "mapsTo": None,
    }


def infer_interface_role(name: str, stereotype: Optional[str]) -> Optional[str]:
    st = norm_st(stereotype)
    if st:
        return st
    lname = name.lower()
    for suffix, role in INTERFACE_ROLE_SUFFIXES.items():
        if lname.endswith(suffix):
            return role
    return None


def parse_behavior_interfaces(root: ET.Element, id_to_name: Dict[str, str]) -> List[Dict[str, Any]]:
    interfaces: List[Dict[str, Any]] = []
    for intf in root.findall(".//UML:Interface", NS):
        name = intf.get("name")
        if not name:
            continue
        st = stereotype_of(intf)
        operations = [parse_operation(op, id_to_name) for op in intf.findall("./UML:Classifier.feature/UML:Operation", NS)]
        interfaces.append({
            "id": xmi_id(intf),
            "name": name,
            "stereotype": st,
            "role": infer_interface_role(name, st),
            "operations": operations,
        })
    return sorted(interfaces, key=lambda x: x["name"])


def link_endpoint_operations(
    endpoint_ops: List[Dict[str, Any]],
    parameter_sets: List[Dict[str, Any]],
    responses: List[Dict[str, Any]],
    behavior_interfaces: List[Dict[str, Any]],
    assocs: List[Dict[str, Any]],
) -> None:
    ep_by_id = {e["id"]: e for e in endpoint_ops}
    ep_by_name = {e["name"]: e for e in endpoint_ops}
    param_names = {p["name"] for p in parameter_sets}
    response_names = {r["name"] for r in responses if not r.get("abstractBase")}

    # Link parameters/responses from associations semantically, not by raw source/target direction.
    for assoc in assocs:
        aname = (assoc.get("name") or "").lower()
        end_names = [e.get("name") for e in assoc.get("ends", []) if e.get("name")]
        if aname == "parameters":
            ep = next((n for n in end_names if n in ep_by_name), None)
            ps = next((n for n in end_names if n in param_names), None)
            if ep and ps:
                ep_by_name[ep]["parameters"] = ps
        elif aname == "responses":
            ep = next((n for n in end_names if n in ep_by_name), None)
            rs = next((n for n in end_names if n in response_names), None)
            if ep and rs and rs not in ep_by_name[ep]["responses"]:
                ep_by_name[ep]["responses"].append(rs)

    # Link interface endpoint operations to ApiOperation. Prefer explicit mapsTo associations
    # interface -> endpointOperation, then match operation name to endpoint operation name.
    interface_to_endpoint_names: Dict[str, List[str]] = {}
    for assoc in assocs:
        if (assoc.get("name") or "").lower() != "mapsto":
            continue
        end_names = [e.get("name") for e in assoc.get("ends", []) if e.get("name")]
        interface_name = None
        endpoint_name = None
        for n in end_names:
            if n in ep_by_name:
                endpoint_name = n
            else:
                # crude: any non-endpoint in mapsTo here is interface
                interface_name = n
        if interface_name and endpoint_name:
            interface_to_endpoint_names.setdefault(interface_name, []).append(endpoint_name)

    for intf in behavior_interfaces:
        candidates = interface_to_endpoint_names.get(intf["name"], [])
        for op in intf["operations"]:
            if norm_st(op.get("stereotype")) not in ENDPOINT_OPERATION_ST:
                continue
            matched = None
            if candidates:
                op_candidate = op_name_to_endpoint_candidate(op["name"])
                norm_op_candidate = normalize_name_for_match(op_candidate)
                # Match CreateUserOperation directly.
                for epn in candidates:
                    if normalize_name_for_match(epn) == norm_op_candidate:
                        matched = epn
                        break
                if matched is None and len(candidates) == 1:
                    matched = candidates[0]
            if matched is None:
                # Fallback: match all endpoint ops by operation name.
                op_candidate = op_name_to_endpoint_candidate(op["name"])
                for epn in ep_by_name:
                    if normalize_name_for_match(epn) == normalize_name_for_match(op_candidate):
                        matched = epn
                        break
            op["mapsTo"] = matched
            if matched:
                ep = ep_by_name[matched]
                ep["operationId"] = op["name"]
                ep["operationRef"] = f"{intf['name']}.{op['name']}"
                ep["controller"] = intf["name"]
                ep["returnType"] = op.get("returnType")

    for ep in endpoint_ops:
        ep["responses"] = sorted(ep["responses"])


def diagnostics_for(ir: Dict[str, Any]) -> List[Dict[str, str]]:
    diags: List[Dict[str, str]] = []
    eps = ir["bim"]["endpointOperations"]
    params = {p["name"] for p in ir["bim"]["parameterSets"]}
    responses = {r["name"] for r in ir["bim"]["responses"]}
    response_concrete = {r["name"] for r in ir["bim"]["responses"] if not r.get("abstractBase")}
    for ep in eps:
        if not ep.get("httpMethod"):
            diags.append({"level": "error", "code": "MISSING_HTTP_METHOD", "message": f"EndpointOperation {ep['name']} has no httpMethod initial value."})
        if not ep.get("uri"):
            diags.append({"level": "error", "code": "MISSING_URI", "message": f"EndpointOperation {ep['name']} has no uri initial value."})
        if ep.get("parameters") and ep["parameters"] not in params:
            diags.append({"level": "error", "code": "UNKNOWN_PARAMETER_SET", "message": f"EndpointOperation {ep['name']} points to unknown parameter set {ep['parameters']}."})
        if not ep.get("responses"):
            diags.append({"level": "error", "code": "MISSING_RESPONSES", "message": f"EndpointOperation {ep['name']} has no responses."})
        for r in ep.get("responses", []):
            if r not in response_concrete:
                diags.append({"level": "error", "code": "UNKNOWN_RESPONSE", "message": f"EndpointOperation {ep['name']} points to unknown response {r}."})
        if not ep.get("operationRef"):
            diags.append({"level": "warning", "code": "MISSING_OPERATION_REF", "message": f"EndpointOperation {ep['name']} is not mapped from an interface operation."})
    for r in ir["bim"]["responses"]:
        if r.get("abstractBase"):
            continue
        if not r.get("status"):
            diags.append({"level": "error", "code": "MISSING_RESPONSE_STATUS", "message": f"Response {r['name']} has no status initial value."})
        if r.get("statusCode") is None:
            diags.append({"level": "warning", "code": "UNPARSED_STATUS_CODE", "message": f"Response {r['name']} status {r.get('status')} does not include a numeric HTTP code."})
        if r.get("responseBody") is None and r.get("statusCode") not in {204, 304}:
            diags.append({"level": "warning", "code": "MISSING_RESPONSE_BODY", "message": f"Response {r['name']} has no responseBody."})
    for ps in ir["bim"]["parameterSets"]:
        for f in ps["fields"]:
            if f["location"] == "UNKNOWN":
                diags.append({"level": "warning", "code": "UNKNOWN_PARAMETER_LOCATION", "message": f"Parameter {ps['name']}.{f['name']} has no BODY/PATH/QUERY/HEADER/COOKIE stereotype."})
    for intf in ir["bim"]["behaviorInterfaces"]:
        for op in intf["operations"]:
            if norm_st(op.get("stereotype")) in ENDPOINT_OPERATION_ST and not op.get("mapsTo"):
                diags.append({"level": "error", "code": "ENDPOINT_OPERATION_NOT_MAPPED", "message": f"Operation {intf['name']}.{op['name']} has <<endpointOperation>> but no ApiOperation mapping."})
            if norm_st(op.get("stereotype")) not in ENDPOINT_OPERATION_ST and op.get("mapsTo"):
                diags.append({"level": "warning", "code": "HELPER_OPERATION_MAPPED", "message": f"Operation {intf['name']}.{op['name']} mapsTo ApiOperation but is not <<endpointOperation>>."})
    return diags


def parse_bim(path: Path) -> Dict[str, Any]:
    root = ET.parse(path).getroot()
    classes_by_id = parse_classes(root)
    interfaces_by_id = parse_interfaces(root)
    id_to_name = parse_all_named_elements(root)
    id_to_name.update({cid: cls.get("name") or "" for cid, cls in classes_by_id.items()})
    id_to_name.update({iid: intf.get("name") or "" for iid, intf in interfaces_by_id.items()})
    generalizations = parse_generalizations(root)
    assocs = parse_associations(root, classes_by_id, interfaces_by_id)

    endpoint_ops = parse_endpoint_operations(root, classes_by_id, id_to_name)
    parameter_sets = parse_parameter_sets(classes_by_id, id_to_name)
    responses = parse_responses(classes_by_id, id_to_name, generalizations)
    behavior_interfaces = parse_behavior_interfaces(root, id_to_name)
    link_endpoint_operations(endpoint_ops, parameter_sets, responses, behavior_interfaces, assocs)

    ir = {
        "metadata": {
            "source": "Enterprise Architect XMI",
            "model": "Behavior Interface Model",
            "file": str(path),
        },
        "bim": {
            "endpointOperations": endpoint_ops,
            "parameterSets": parameter_sets,
            "responses": responses,
            "behaviorInterfaces": behavior_interfaces,
        },
        "diagnostics": [],
    }
    ir["diagnostics"] = diagnostics_for(ir)
    return ir


def parse_bifm(path: Path) -> Dict[str, Any]:
    """Backward compatible alias."""
    ir = parse_bim(path)
    if "bim" in ir and "bifm" not in ir:
        ir["bifm"] = ir["bim"]
    return ir


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse Behavior Interface Model XMI to backend-friendly IR JSON.")
    ap.add_argument("--bim", required=True, type=Path, help="Path to Behavior Interface Model XMI file")
    ap.add_argument("--out", type=Path, default=Path("bim_ir.json"), help="Output JSON path")
    args = ap.parse_args()
    ir = parse_bim(args.bim)
    args.out.write_text(json.dumps(ir, indent=2, ensure_ascii=False), encoding="utf-8")
    b = ir["bim"]
    print(f"Wrote {args.out}")
    print(f"Endpoint operations: {len(b['endpointOperations'])}")
    print(f"Parameter sets: {len(b['parameterSets'])}")
    print(f"Responses: {len(b['responses'])}")
    print(f"Behavior interfaces: {len(b['behaviorInterfaces'])}")
    print(f"Diagnostics: {len(ir['diagnostics'])}")


if __name__ == "__main__":
    main()
