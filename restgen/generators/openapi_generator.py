#!/usr/bin/env python3
"""
Generate an OpenAPI YAML contract from the structural IR produced from UML/XMI.

Input: structural_ir.json containing API DTO, enum and BIM endpoint-operation data.
Output: openapi.yaml

This generator intentionally covers only the structural contract layer:
- DTO/enums -> components.schemas
- endpointOperations -> paths/methods
- parameterSets -> OpenAPI parameters/requestBody
- responses -> OpenAPI responses

It does not generate service logic from Behavior Model diagrams.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


PRIMITIVE_TYPE_MAP: Dict[str, Dict[str, str]] = {
    "STRING": {"type": "string"},
    "BOOLEAN": {"type": "boolean"},
    "INTEGER": {"type": "integer", "format": "int32"},
    "LONG": {"type": "integer", "format": "int64"},
    "FLOAT": {"type": "number", "format": "float"},
    "DOUBLE": {"type": "number", "format": "double"},
    "DECIMAL": {"type": "number", "format": "double"},
    "UUID": {"type": "string", "format": "uuid"},
    "DATE": {"type": "string", "format": "date"},
    "DATETIME": {"type": "string", "format": "date-time"},
    "void": {"type": "null"},
    "VOID": {"type": "null"},
}

BODY = "BODY"
PARAM_LOCATIONS = {"PATH": "path", "QUERY": "query", "HEADER": "header", "COOKIE": "cookie"}


def lower_camel(name: str) -> str:
    if not name:
        return name
    # Keep already camel-ish names mostly intact.
    return name[0].lower() + name[1:]


def operation_id_from_endpoint(name: str) -> str:
    # CreateUserOperation -> createUserOperation; fallback: sanitize + lower first.
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name or "operation")
    return lower_camel(clean)


def status_text(status: Optional[str], code: Optional[int]) -> str:
    if status:
        # BAD_REQUEST_400 -> Bad Request
        text = re.sub(r"_?\d+$", "", status).replace("_", " ").title()
        return text or f"HTTP {code}"
    return f"HTTP {code}" if code else "Response"


def normalize_status_code(resp: Dict[str, Any]) -> str:
    code = resp.get("statusCode")
    if code is None:
        # fallback from status literal suffix: CREATED_201
        status = resp.get("status") or ""
        m = re.search(r"(\d{3})$", status)
        if m:
            return m.group(1)
        return "default"
    return str(code)


def make_schema_ref(name: str) -> Dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def schema_for_type(type_name: Optional[str], collection: bool = False) -> Dict[str, Any]:
    if not type_name:
        schema: Dict[str, Any] = {"type": "object"}
    else:
        schema = dict(PRIMITIVE_TYPE_MAP.get(type_name, make_schema_ref(type_name)))

    if collection:
        return {"type": "array", "items": schema}
    return schema


def parse_required_and_collection(multiplicity: Optional[str], required: Optional[bool], collection: Optional[bool]) -> Tuple[bool, bool]:
    mult = (multiplicity or "1").strip()
    is_collection = bool(collection) or "*" in mult
    if required is not None:
        is_required = bool(required)
    else:
        is_required = not (mult.startswith("0") or mult == "*")
    return is_required, is_collection


def build_schema_from_fields(name: str, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for field in fields:
        field_name = field.get("name")
        if not field_name:
            continue
        is_required, is_collection = parse_required_and_collection(
            field.get("multiplicity"), field.get("required"), field.get("collection")
        )
        properties[field_name] = schema_for_type(field.get("type"), is_collection)
        if is_required:
            required.append(field_name)

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def build_components(ir: Dict[str, Any]) -> Dict[str, Any]:
    schemas: Dict[str, Any] = {}

    # DTO classes -> object schemas
    for dto in ir.get("api", {}).get("dtos", []):
        name = dto.get("name")
        if not name:
            continue
        schemas[name] = build_schema_from_fields(name, dto.get("fields", []))

    # Enums -> string enum schemas. Include all enums even if some are internal; harmless for first contract generation.
    for enum in ir.get("api", {}).get("enums", []):
        name = enum.get("name")
        literals = enum.get("literals", [])
        if not name:
            continue
        schemas[name] = {
            "type": "string",
            "enum": literals,
        }

    return {"schemas": schemas}


def index_by_name(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {item.get("name"): item for item in items if item.get("name")}


def build_parameter_object(field: Dict[str, Any]) -> Dict[str, Any]:
    location = (field.get("location") or "QUERY").upper()
    openapi_location = PARAM_LOCATIONS.get(location, "query")
    is_required, is_collection = parse_required_and_collection(
        field.get("multiplicity"), field.get("required"), field.get("collection")
    )

    # OpenAPI requires path parameters to be required.
    if openapi_location == "path":
        is_required = True

    return {
        "name": field.get("name"),
        "in": openapi_location,
        "required": is_required,
        "schema": schema_for_type(field.get("type"), is_collection),
    }


def build_request_body(field: Dict[str, Any]) -> Dict[str, Any]:
    is_required, is_collection = parse_required_and_collection(
        field.get("multiplicity"), field.get("required"), field.get("collection")
    )
    return {
        "required": is_required,
        "content": {
            "application/json": {
                "schema": schema_for_type(field.get("type"), is_collection)
            }
        },
    }


def build_responses(operation: Dict[str, Any], responses_by_name: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}

    for response_name in operation.get("responses", []) or []:
        resp = responses_by_name.get(response_name)
        if not resp:
            result.setdefault("default", {"description": f"Unresolved response {response_name}"})
            continue

        code = normalize_status_code(resp)
        response_obj: Dict[str, Any] = {
            "description": status_text(resp.get("status"), resp.get("statusCode")),
        }
        body = resp.get("responseBody")
        if body:
            response_obj["content"] = {
                "application/json": {
                    "schema": schema_for_type(body)
                }
            }
        result[code] = response_obj

    if not result:
        result["default"] = {"description": "Default response"}
    return result


def build_paths(ir: Dict[str, Any]) -> Dict[str, Any]:
    bim = ir.get("bim", {})
    parameter_sets = index_by_name(bim.get("parameterSets", []))
    responses_by_name = index_by_name(bim.get("responses", []))

    paths: Dict[str, Any] = {}

    for endpoint in bim.get("endpointOperations", []):
        uri = endpoint.get("uri")
        method = (endpoint.get("httpMethod") or "GET").lower()
        if not uri:
            continue

        operation_id = endpoint.get("operationId") or operation_id_from_endpoint(endpoint.get("name", "operation"))
        op_obj: Dict[str, Any] = {
            "operationId": operation_id,
            "summary": endpoint.get("name", operation_id),
            "responses": build_responses(endpoint, responses_by_name),
        }

        controller = endpoint.get("controller")
        if controller:
            op_obj["tags"] = [controller]

        param_set_name = endpoint.get("parameters")
        if param_set_name:
            param_set = parameter_sets.get(param_set_name)
            if param_set:
                parameters: List[Dict[str, Any]] = []
                body_fields: List[Dict[str, Any]] = []
                for field in param_set.get("fields", []):
                    location = (field.get("location") or "QUERY").upper()
                    if location == BODY:
                        body_fields.append(field)
                    else:
                        parameters.append(build_parameter_object(field))

                if parameters:
                    op_obj["parameters"] = parameters
                if body_fields:
                    # Common case: exactly one BODY field. If more appear, use the first and annotate via extension.
                    op_obj["requestBody"] = build_request_body(body_fields[0])
                    if len(body_fields) > 1:
                        op_obj["x-ir-warning"] = "Multiple BODY parameters found; only the first was used as requestBody."

        paths.setdefault(uri, {})[method] = op_obj

    return paths


def build_openapi(ir: Dict[str, Any], title: str, version: str) -> Dict[str, Any]:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": title,
            "version": version,
            "description": "Generated from structural IR extracted from UML/XMI.",
        },
        "paths": build_paths(ir),
        "components": build_components(ir),
    }


class QuotedStringDumper(yaml.SafeDumper):
    pass


def str_presenter(dumper: yaml.Dumper, data: str):  # type: ignore[type-arg]
    # Keep ordinary strings readable; quote only strings that look ambiguous is left to PyYAML defaults.
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


QuotedStringDumper.add_representer(str, str_presenter)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate OpenAPI YAML from structural IR JSON.")
    parser.add_argument("--ir", required=True, help="Path to structural_ir.json")
    parser.add_argument("--out", required=True, help="Path to output openapi.yaml")
    parser.add_argument("--title", default="Generated REST API", help="OpenAPI info.title")
    parser.add_argument("--version", default="0.1.0", help="OpenAPI info.version")
    args = parser.parse_args()

    with Path(args.ir).open("r", encoding="utf-8") as f:
        ir = json.load(f)

    openapi = build_openapi(ir, title=args.title, version=args.version)

    with Path(args.out).open("w", encoding="utf-8") as f:
        yaml.safe_dump(openapi, f, sort_keys=False, allow_unicode=True, default_flow_style=False)

    print(f"Generated OpenAPI YAML: {args.out}")
    print(f"Paths: {len(openapi.get('paths', {}))}")
    print(f"Schemas: {len(openapi.get('components', {}).get('schemas', {}))}")


if __name__ == "__main__":
    main()
