#!/usr/bin/env python3
"""
Parser XMI -> IR for Class Model + Persistence Model.

Scope (current stage):
- API DTO model (CM): DTO classes, DTO fields, enums, enum literals, DTO -> Entity mapsTo.
- Persistence model (PM): entity classes, entity fields, persistence relations.

The parser intentionally does not parse endpoint operations, behavior interfaces,
or sequence diagrams. Those are handled in later stages.

Usage:
  python parser_api_persistence_ir.py --cfm cfm-xmi.xml --pfm pfm-xmi.xml --out api_persistence_ir.json
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import xml.etree.ElementTree as ET

NS = {"UML": "omg.org/UML1.3"}

PRIMITIVE_TYPES = {
    "STRING", "BOOLEAN", "INTEGER", "LONG", "UUID", "DECIMAL", "DATE", "DATETIME", "DOUBLE", "FLOAT"
}

DTO_STEREOTYPES = {"dto", "apidto"}
ENTITY_STEREOTYPES = {"entity", "dbentity", "persistenceentity"}
ENUM_STEREOTYPES = {"enumeration", "enum", "apienumtype"}


def xmi_id(el: ET.Element) -> Optional[str]:
    return el.get("xmi.id") or el.get("{http://www.omg.org/XMI}id")


def tagged_values(el: ET.Element) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for tv in el.findall("./UML:ModelElement.taggedValue/UML:TaggedValue", NS):
        tag = tv.get("tag")
        if tag is not None:
            tags[tag] = tv.get("value") or ""
    return tags


def stereotype_of(el: ET.Element) -> Optional[str]:
    tags = tagged_values(el)
    st = tags.get("stereotype")
    if st:
        return st.strip()
    # Sometimes EA stores stereotype info inside xref-style payloads; we keep this as a fallback.
    xref = tags.get("$ea_xref_property", "")
    m = re.search(r"Name=([^;@]+)", xref)
    if m:
        return m.group(1).strip()
    return None


def norm_st(st: Optional[str]) -> str:
    return (st or "").strip().strip("<>").lower()


def attr_type(attr: ET.Element) -> Optional[str]:
    tags = tagged_values(attr)
    return tags.get("type") or attr.get("type")


def attr_default_value(attr: ET.Element) -> Optional[str]:
    """Return UML/EA attribute default value if present.

    EA XMI can store default values in a few shapes, depending on export/version, e.g.:
    - <UML:Attribute.initialValue><UML:Expression body="true"/></...>
    - <UML:Attribute.initialValue><UML:Expression value="true"/></...>
    - tagged values like defaultValue / initialValue / default.
    """
    # Tagged value fallbacks first.
    tags = tagged_values(attr)
    for key in ("defaultValue", "initialValue", "default", "ea_default", "Default"):
        val = tags.get(key)
        if val not in (None, ""):
            return val.strip()

    # UML 1.3 / EA initialValue expression.
    for expr in attr.findall("./UML:Attribute.initialValue/UML:Expression", NS):
        for key in ("body", "value", "expression", "name"):
            val = expr.get(key)
            if val not in (None, ""):
                return val.strip()
        text = (expr.text or "").strip()
        if text:
            return text

    # Some XMI variants use generic names without the UML prefix.
    for expr in attr.findall(".//UML:Expression", NS):
        for key in ("body", "value", "expression", "name"):
            val = expr.get(key)
            if val not in (None, ""):
                return val.strip()
    return None


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
    """Return (required, collection)."""
    if multiplicity in {"1", "1..1"}:
        return True, False
    if multiplicity in {"0", "0..1"}:
        return False, False
    if multiplicity in {"*", "0..*"}:
        return False, True
    if multiplicity == "1..*":
        return True, True
    # Conservative defaults.
    collection = "*" in multiplicity
    required = not multiplicity.startswith("0")
    return required, collection


def is_id_field(name: str, attr: ET.Element) -> bool:
    tags = tagged_values(attr)
    st = norm_st(stereotype_of(attr))
    if name.lower() == "id":
        return True
    if st in {"id", "identifier", "primarykey", "pk"}:
        return True
    if tags.get("isId", "").lower() == "true":
        return True
    return False


def parse_fields(cls: ET.Element, include_id: bool = False) -> List[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    attrs = list(cls.findall("./UML:Classifier.feature/UML:Attribute", NS))
    # Fallback for EA variants where attributes may be deeper.
    if not attrs:
        attrs = list(cls.findall(".//UML:Attribute", NS))

    def pos(a: ET.Element) -> int:
        try:
            return int(tagged_values(a).get("position", "999999"))
        except ValueError:
            return 999999

    for attr in sorted(attrs, key=pos):
        name = attr.get("name")
        if not name:
            continue
        st = norm_st(stereotype_of(attr))
        # Enum literals are represented as attributes too, but should not be DTO/entity fields.
        if st == "enum":
            continue
        typ = attr_type(attr) or "UNKNOWN"
        mult = attr_multiplicity(attr)
        required, collection = multiplicity_flags(mult)
        item: Dict[str, Any] = {
            "name": name,
            "type": typ,
            "multiplicity": mult,
            "required": required,
            "collection": collection,
        }
        default = attr_default_value(attr)
        if default is not None:
            item["defaultValue"] = default
        if include_id:
            item["id"] = is_id_field(name, attr)
        fields.append(item)
    return fields


def parse_classes(root: ET.Element) -> Dict[str, ET.Element]:
    result: Dict[str, ET.Element] = {}
    for cls in root.findall(".//UML:Class", NS):
        cid = xmi_id(cls)
        name = cls.get("name")
        if cid and name and name != "EARootClass":
            result[cid] = cls
    return result


def parse_enums(root: ET.Element) -> List[Dict[str, Any]]:
    enums: List[Dict[str, Any]] = []
    for cls in root.findall(".//UML:Class", NS):
        name = cls.get("name")
        if not name or name == "EARootClass":
            continue
        if norm_st(stereotype_of(cls)) not in ENUM_STEREOTYPES:
            continue
        literals: List[str] = []
        attrs = list(cls.findall("./UML:Classifier.feature/UML:Attribute", NS))
        if not attrs:
            attrs = list(cls.findall(".//UML:Attribute", NS))
        for attr in attrs:
            aname = attr.get("name")
            if aname:
                literals.append(aname)
        enums.append({"name": name, "literals": literals})
    return sorted(enums, key=lambda e: e["name"])


def parse_dtos(root: ET.Element, dto_to_entity: Dict[str, str]) -> List[Dict[str, Any]]:
    dtos: List[Dict[str, Any]] = []
    for cls in root.findall(".//UML:Class", NS):
        name = cls.get("name")
        if not name or name == "EARootClass":
            continue
        if norm_st(stereotype_of(cls)) not in DTO_STEREOTYPES:
            continue
        dtos.append({
            "name": name,
            "stereotype": stereotype_of(cls) or "dto",
            "fields": parse_fields(cls, include_id=False),
            "mapsTo": dto_to_entity.get(name),
        })
    return sorted(dtos, key=lambda d: d["name"])


def parse_entities(root: ET.Element) -> List[Dict[str, Any]]:
    entities: List[Dict[str, Any]] = []
    for cls in root.findall(".//UML:Class", NS):
        name = cls.get("name")
        if not name or name == "EARootClass":
            continue
        if norm_st(stereotype_of(cls)) not in ENTITY_STEREOTYPES:
            continue
        entities.append({
            "name": name,
            "stereotype": stereotype_of(cls) or "entity",
            "fields": parse_fields(cls, include_id=True),
        })
    return sorted(entities, key=lambda e: e["name"])


def assoc_ends(assoc: ET.Element) -> List[ET.Element]:
    return list(assoc.findall(".//UML:AssociationEnd", NS))


def parse_maps_to(root: ET.Element, classes_by_id: Dict[str, ET.Element]) -> List[Dict[str, str]]:
    mappings: List[Dict[str, str]] = []
    for assoc in root.findall(".//UML:Association", NS):
        if (assoc.get("name") or "").lower() != "mapsto":
            continue
        tags = tagged_values(assoc)
        src_name = tags.get("ea_sourceName")
        tgt_name = tags.get("ea_targetName")
        if src_name and tgt_name:
            mappings.append({"dto": src_name, "entity": tgt_name})
            continue
        # Fallback: infer from ends and stereotypes.
        ends = assoc_ends(assoc)
        resolved = []
        for e in ends:
            ref = e.get("type")
            cls = classes_by_id.get(ref or "")
            if cls is not None:
                resolved.append((cls.get("name"), norm_st(stereotype_of(cls))))
        dto = next((n for n, st in resolved if st in DTO_STEREOTYPES), None)
        entity = next((n for n, st in resolved if st in ENTITY_STEREOTYPES), None)
        if dto and entity:
            mappings.append({"dto": dto, "entity": entity})
    # unique
    seen = set()
    uniq = []
    for m in mappings:
        key = (m["dto"], m["entity"])
        if key not in seen:
            seen.add(key)
            uniq.append(m)
    return sorted(uniq, key=lambda m: (m["dto"], m["entity"]))

def collect_used_types(ir: Dict[str, Any]) -> List[str]:
    used = set()
    for dto in ir["api"]["dtos"]:
        for f in dto["fields"]:
            used.add(f["type"])
    for ent in ir["persistence"]["entities"]:
        for f in ent["fields"]:
            used.add(f["type"])
    return sorted(used)

def validate(ir: Dict[str, Any]) -> List[Dict[str, str]]:
    diagnostics: List[Dict[str, str]] = []
    dto_names = {d["name"] for d in ir["api"]["dtos"]}
    entity_names = {e["name"] for e in ir["persistence"]["entities"]}
    enum_names = {e["name"] for e in ir["api"]["enums"]}
    known = set(PRIMITIVE_TYPES) | dto_names | entity_names | enum_names

    for dto in ir["api"]["dtos"]:
        mt = dto.get("mapsTo")
        if mt and mt not in entity_names:
            diagnostics.append({"level": "warning", "code": "DTO_MAPS_TO_UNKNOWN_ENTITY", "message": f"DTO {dto['name']} mapsTo {mt}, but entity was not found in persistence model."})
        for f in dto["fields"]:
            if f["type"] not in known:
                diagnostics.append({"level": "warning", "code": "UNKNOWN_DTO_FIELD_TYPE", "message": f"DTO {dto['name']}.{f['name']} uses unknown type {f['type']}."})
    for ent in ir["persistence"]["entities"]:
        if not any(f.get("id") for f in ent["fields"]):
            diagnostics.append({"level": "warning", "code": "ENTITY_WITHOUT_ID", "message": f"Entity {ent['name']} has no id field detected."})
        for f in ent["fields"]:
            if f["type"] not in known:
                diagnostics.append({"level": "warning", "code": "UNKNOWN_ENTITY_FIELD_TYPE", "message": f"Entity {ent['name']}.{f['name']} uses unknown type {f['type']}."})
    return diagnostics


def parse_cm_pm(cm_path: Path, pm_path: Path) -> Dict[str, Any]:
    return parse_api_persistence(cm_path, pm_path)


def parse_api_persistence(cfm_path: Path, pfm_path: Path) -> Dict[str, Any]:
    cfm_root = ET.parse(cfm_path).getroot()
    pfm_root = ET.parse(pfm_path).getroot()
    cfm_classes = parse_classes(cfm_root)
    pfm_classes = parse_classes(pfm_root)
    all_classes = {**cfm_classes, **pfm_classes}

    dto_entity_mappings = parse_maps_to(cfm_root, all_classes)
    # PM may duplicate mapsTo associations because EA exports related external elements. Merge too.
    dto_entity_mappings += parse_maps_to(pfm_root, all_classes)
    seen = set()
    dto_entity_mappings_unique = []
    for m in dto_entity_mappings:
        key = (m["dto"], m["entity"])
        if key not in seen:
            seen.add(key)
            dto_entity_mappings_unique.append(m)
    dto_to_entity = {m["dto"]: m["entity"] for m in dto_entity_mappings_unique}

    ir: Dict[str, Any] = {
        "metadata": {
            "stage": "api+persistence",
            "sources": {
                "apiDtoModel": str(cfm_path),
                "persistenceModel": str(pfm_path),
            },
        },
        "api": {
            "dtos": parse_dtos(cfm_root, dto_to_entity),
            "enums": parse_enums(cfm_root),
        },
        "persistence": {
            "entities": parse_entities(pfm_root),
        },
        "mappings": {
            "dtoToEntity": sorted(dto_entity_mappings_unique, key=lambda m: (m["dto"], m["entity"])),
        },
        "typeSummary": {
            "primitiveTypes": sorted(PRIMITIVE_TYPES),
            "usedTypes": [],
        },
        "diagnostics": [],
    }
    ir["typeSummary"]["usedTypes"] = collect_used_types(ir)
    ir["diagnostics"] = validate(ir)
    return ir


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse API DTO + Persistence XMI into REST backend-friendly IR.")
    parser.add_argument("--cm", required=True, type=Path, help="Path to Class Model XMI, e.g. cm-xmi.xml")
    parser.add_argument("--pm", required=True, type=Path, help="Path to Persistence Model XMI, e.g. pm-xmi.xml")
    parser.add_argument("--out", type=Path, default=Path("cm_pm_ir.json"), help="Output JSON file")
    args = parser.parse_args()

    ir = parse_cm_pm(args.cm, args.pm)
    args.out.write_text(json.dumps(ir, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")
    print(f"DTOs: {len(ir['api']['dtos'])}")
    print(f"API enums: {len(ir['api']['enums'])}")
    print(f"Entities: {len(ir['persistence']['entities'])}")
    print(f"DTO->Entity mappings: {len(ir['mappings']['dtoToEntity'])}")
    if ir["diagnostics"]:
        print(f"Diagnostics: {len(ir['diagnostics'])}")
        for d in ir["diagnostics"][:10]:
            print(f"  [{d['level']}] {d['code']}: {d['message']}")


if __name__ == "__main__":
    main()
