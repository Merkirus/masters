#!/usr/bin/env python3
"""
Generate a Spring backend skeleton + first BM-based service method bodies + basic JPA persistence on top of
OpenAPI Generator `interfaceOnly=true` output.

Input:
  - combined REST IR JSON (full.json/rest_ir.json)
  - generated-backend-interface directory from OpenAPI Generator

Output:
  - copied backend with added controller/service/repository/domain/response classes
  - service operation bodies generated from BM behavior

This is a prototype behavior generator. It keeps business helper methods as simple
stubs, generates mapper signatures from model mappings, and basic Spring Data JPA persistence.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

JAVA_TYPE_MAP = {
    "STRING": "String",
    "BOOLEAN": "Boolean",
    "INTEGER": "Integer",
    "LONG": "Long",
    "FLOAT": "Float",
    "DOUBLE": "Double",
    "DECIMAL": "Double",
    "UUID": "java.util.UUID",
    "DATE": "java.time.LocalDate",
    "DATETIME": "java.time.OffsetDateTime",
    "void": "void",
}

def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def prune_unused_imports(java_source: str) -> str:
    """Remove Java import lines whose simple class name is not used in the generated file body.

    Imports can be collected broadly while a file is being generated. This post-processing
    step keeps only imports whose simple type name appears outside the import section,
    so unused OpenAPI model imports such as ApiDto are removed automatically.
    """
    lines = java_source.rstrip().splitlines()
    package_lines: List[str] = []
    import_lines: List[str] = []
    body_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("package "):
            package_lines.append(stripped)
        elif stripped.startswith("import "):
            import_lines.append(stripped)
        else:
            body_lines.append(line)

    body = "\n".join(body_lines)
    kept_imports: List[str] = []
    seen: Set[str] = set()

    for imp in import_lines:
        if imp in seen:
            continue
        seen.add(imp)

        if imp.startswith("import static "):
            kept_imports.append(imp)
            continue

        imported_name = imp.rsplit(".", 1)[-1].replace(";", "").strip()
        if imported_name == "*" or re.search(rf"\b{re.escape(imported_name)}\b", body):
            kept_imports.append(imp)

    result: List[str] = []
    result.extend(package_lines)
    if kept_imports:
        result.append("")
        result.extend(sorted(kept_imports))

    trimmed_body = "\n".join(body_lines).strip("\n")
    if trimmed_body:
        result.append("")
        result.append(trimmed_body)

    return "\n".join(result).rstrip() + "\n"


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    if path.suffix == ".java":
        content = prune_unused_imports(content)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def upper_first(s: str) -> str:
    return s[:1].upper() + s[1:]


def lower_first(s: str) -> str:
    return s[:1].lower() + s[1:]


def model_enum_names(ir: Optional[Dict[str, Any]] = None) -> Set[str]:
    if not ir:
        return set()
    return {e.get("name") for e in ir.get("api", {}).get("enums", []) if e.get("name")}


def model_dto_names(ir: Optional[Dict[str, Any]] = None) -> Set[str]:
    if not ir:
        return set()
    return {d.get("name") for d in ir.get("api", {}).get("dtos", []) if d.get("name")}


def model_entity_names(ir: Optional[Dict[str, Any]] = None) -> Set[str]:
    if not ir:
        return set()
    return {e.get("name") for e in ir.get("persistence", {}).get("entities", []) if e.get("name")}


def bim_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return ir.get("bim") or ir.get("bifm") or {}


def bm_section(ir: Dict[str, Any]) -> Dict[str, Any]:
    return ir.get("bm") or ir.get("bfm") or {}


def model_response_names(ir: Optional[Dict[str, Any]] = None) -> Set[str]:
    if not ir:
        return set()
    return {r.get("name") for r in bim_section(ir).get("responses", []) if r.get("name")}


def known_model_types(ir: Optional[Dict[str, Any]] = None) -> Set[str]:
    return model_dto_names(ir) | model_entity_names(ir) | model_enum_names(ir) | model_response_names(ir)


def java_type(type_name: Optional[str], model_package: str = "org.openapitools.model", entity_package: str = "org.openapitools.domain", ir: Optional[Dict[str, Any]] = None) -> str:
    if not type_name:
        return "Object"
    if type_name in JAVA_TYPE_MAP:
        return JAVA_TYPE_MAP[type_name]
    if type_name == "HttpResponse":
        return "org.openapitools.response.RestResponse"
    if type_name in model_dto_names(ir) or type_name in model_enum_names(ir) or type_name.endswith("Dto"):
        return f"{model_package}.{type_name}"
    if type_name in model_entity_names(ir) or type_name.endswith("Entity"):
        return f"{entity_package}.{type_name}"
    if type_name in model_response_names(ir) or type_name.endswith("Response"):
        return f"org.openapitools.response.{type_name}"
    return type_name


def short_type(fqcn: str) -> str:
    return fqcn.split(".")[-1]


def parse_operation_ref(ref: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not ref or "." not in ref:
        return None, None
    return ref.rsplit(".", 1)


def find_param_set(ir: Dict[str, Any], name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not name:
        return None
    return next((ps for ps in bim_section(ir).get("parameterSets", []) if ps.get("name") == name), None)


def find_behavior_for_endpoint(ir: Dict[str, Any], endpoint_name: str) -> Optional[Dict[str, Any]]:
    return next((b for b in bm_section(ir).get("behaviors", []) if b.get("represents") == endpoint_name), None)


def lifeline_role_map(behavior: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not behavior:
        return {}
    return {l.get("name"): l.get("role") for l in behavior.get("lifelines", []) if l.get("name")}


def role_of(name: Optional[str], roles: Dict[str, str]) -> Optional[str]:
    if not name:
        return None
    return roles.get(name)


def entity_by_name(ir: Dict[str, Any], name: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((e for e in ir.get("persistence", {}).get("entities", []) if e.get("name") == name), None)


def dto_by_name(ir: Dict[str, Any], name: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((d for d in ir.get("api", {}).get("dtos", []) if d.get("name") == name), None)


def fields_for_type(ir: Dict[str, Any], type_name: Optional[str]) -> List[Dict[str, Any]]:
    if not type_name:
        return []
    entity = entity_by_name(ir, type_name)
    if entity:
        return entity.get("fields", [])
    dto = dto_by_name(ir, type_name)
    if dto:
        return dto.get("fields", [])
    return []


def field_by_name(ir: Dict[str, Any], type_name: Optional[str], field_name: str) -> Optional[Dict[str, Any]]:
    return next((f for f in fields_for_type(ir, type_name) if f.get("name") == field_name), None)


def pluralize(word: str) -> str:
    if word.endswith("y") and (len(word) < 2 or word[-2].lower() not in "aeiou"):
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def table_name_for_entity(entity_name: str) -> str:
    base = entity_name[:-len("Entity")] if entity_name.endswith("Entity") else entity_name
    return pluralize(lower_first(base))


def method_param_name_for_controller(endpoint: Dict[str, Any], param_set: Optional[Dict[str, Any]]) -> str:
    if param_set:
        for f in param_set.get("fields", []):
            if f.get("location") == "BODY":
                return lower_first(f.get("type", "requestBody"))
    return "request"


def service_method_for_endpoint(endpoint: Dict[str, Any], behavior: Optional[Dict[str, Any]]) -> str:
    if behavior:
        roles = lifeline_role_map(behavior)
        for m in sorted(behavior.get("messages", []), key=lambda x: x.get("seq", 0)):
            if m.get("kind") == "sync" and role_of(m.get("source"), roles) == "controller" and role_of(m.get("target"), roles) == "service":
                return m.get("operation") or "handle"
    return endpoint.get("operationId") or lower_first(endpoint.get("name", "operation"))



class BehaviorCodeGenerator:
    def __init__(self, behavior: Dict[str, Any], imports: Set[str], ir: Dict[str, Any], method_parameters: Optional[List[Dict[str, Any]]] = None) -> None:
        self.behavior = behavior
        self.imports = imports
        self.ir = ir
        self.messages = {m["id"]: m for m in behavior.get("messages", [])}
        self.fragments = {f["id"]: f for f in behavior.get("fragments", [])}
        self.lifeline_roles = lifeline_role_map(behavior)
        self.repo_fields: Dict[str, str] = {}
        for l in behavior.get("lifelines", []):
            if l.get("role") == "repository" and l.get("name"):
                self.repo_fields[l.get("name")] = lower_first(l.get("name"))
        self.symbol_types: Dict[str, str] = {}
        for p in method_parameters or []:
            if p.get("name") and p.get("type"):
                self.symbol_types[p.get("name")] = p.get("type")
        self.skip_ids: Set[str] = set()

    def generate_method_body(self) -> str:
        top_items = []
        for m in self.behavior.get("messages", []):
            # service method starts after controller -> service call; skip external/controller messages.
            if m.get("fragment") is None and self._message_relevant_to_service(m):
                top_items.append((self._message_y(m), "message", m["id"]))
        for f in self.behavior.get("fragments", []):
            if f.get("parent") is None:
                top_items.append((float(f.get("bounds", {}).get("top", 0)), "fragment", f["id"]))
        top_items.sort(key=lambda x: x[0])
        lines: List[str] = []
        for _, kind, item_id in top_items:
            if kind == "message":
                m = self.messages[item_id]
                if m["id"] not in self.skip_ids:
                    lines.extend(self._generate_message_statement(m, indent=2, scope_message_ids=None))
            else:
                lines.extend(self._generate_fragment(self.fragments[item_id], indent=2))
        return "\n".join(lines) if lines else "        // TODO: no behavior messages were generated."

    def _message_relevant_to_service(self, m: Dict[str, Any]) -> bool:
        source_role = role_of(m.get("source"), self.lifeline_roles)
        target_role = role_of(m.get("target"), self.lifeline_roles)
        if m.get("kind") in {"sync", "self"}:
            # Service body contains calls performed by the service itself and calls to repositories.
            return source_role == "service" or target_role == "repository"
        if m.get("kind") == "return":
            # Include returns from service/repository, including final service -> controller returns.
            return source_role in {"service", "repository"}
        return False

    def _message_y(self, m: Dict[str, Any]) -> float:
        return float(m.get("geometry", {}).get("yMid", 0))

    def _operand_items(self, operand: Dict[str, Any]) -> List[Tuple[float, str, str]]:
        items: List[Tuple[float, str, str]] = []
        for mid in operand.get("messages", []):
            if mid in self.messages:
                m = self.messages[mid]
                if self._message_relevant_to_service(m):
                    items.append((self._message_y(m), "message", mid))
        for fid in operand.get("fragments", []):
            if fid in self.fragments:
                f = self.fragments[fid]
                items.append((float(f.get("bounds", {}).get("top", 0)), "fragment", fid))
        items.sort(key=lambda x: x[0])
        return items

    def _generate_fragment(self, fragment: Dict[str, Any], indent: int) -> List[str]:
        kind = fragment.get("kind")
        if kind == "break":
            guard = self._translate_guard(fragment.get("operands", [{}])[0].get("guard", "true"))
            body = self._generate_operand_body(fragment.get("operands", [{}])[0], indent + 1)
            return self._block(f"if ({guard})", body, indent)
        if kind == "opt":
            guard = self._translate_guard(fragment.get("operands", [{}])[0].get("guard", "true"))
            body = self._generate_operand_body(fragment.get("operands", [{}])[0], indent + 1)
            return self._block(f"if ({guard})", body, indent)
        if kind == "loop":
            guard = fragment.get("operands", [{}])[0].get("guard", "")
            header = self._loop_header(guard)
            body = self._generate_operand_body(fragment.get("operands", [{}])[0], indent + 1)
            return self._block(header, body, indent)
        if kind == "alt":
            return self._generate_alt(fragment, indent)
        return self._comment(f"Unsupported fragment {fragment.get('name')} ({kind})", indent)

    def _generate_alt(self, fragment: Dict[str, Any], indent: int) -> List[str]:
        operands = fragment.get("operands", [])
        non_else = [op for op in operands if str(op.get("guard", "")).strip().lower() != "else"]
        else_ops = [op for op in operands if str(op.get("guard", "")).strip().lower() == "else"]
        ordered = non_else + else_ops
        lines: List[str] = []
        first = True
        for op in ordered:
            guard_raw = str(op.get("guard", "true")).strip()
            body = self._generate_operand_body(op, indent + 1)
            prefix = "if" if first else "else if"
            if guard_raw.lower() == "else":
                if first:
                    # Degenerate case: else only.
                    lines.extend(self._block("if (true)", body, indent))
                else:
                    lines.append("    " * indent + "else {")
                    lines.extend(body or ["    " * (indent + 1) + "// TODO: empty else branch."])
                    lines.append("    " * indent + "}")
            else:
                header = f"{prefix} ({self._translate_guard(guard_raw)})"
                lines.extend(self._block(header, body, indent))
            first = False
        return lines

    def _generate_operand_body(self, operand: Dict[str, Any], indent: int) -> List[str]:
        lines: List[str] = []
        for _, kind, item_id in self._operand_items(operand):
            if kind == "message":
                if item_id in self.skip_ids:
                    continue
                lines.extend(self._generate_message_statement(self.messages[item_id], indent, set(operand.get("messages", []))))
            else:
                lines.extend(self._generate_fragment(self.fragments[item_id], indent))
        return lines

    def _generate_message_statement(self, m: Dict[str, Any], indent: int, scope_message_ids: Optional[Set[str]]) -> List[str]:
        if m.get("kind") in {"sync", "self"}:
            return self._generate_call_statement(m, indent, scope_message_ids)
        if m.get("kind") == "return":
            # assignment returns are normally consumed by the preceding call; forward returns become Java return.
            if m.get("returnMode") == "forward" and role_of(m.get("source"), self.lifeline_roles) == "service":
                return ["    " * indent + f"return {m.get('returnValue') or 'response'};"]
            if m.get("returnMode") == "assignment":
                return self._comment(f"Unpaired return assignment: {m.get('assignTo')} = :{m.get('returnType')}", indent)
        return []

    def _generate_call_statement(self, m: Dict[str, Any], indent: int, scope_message_ids: Optional[Set[str]]) -> List[str]:
        call = self._call_expression(m)
        next_return = self._find_following_assignment_return(m, scope_message_ids)
        if next_return:
            self.skip_ids.add(next_return["id"])
            assign_to = next_return.get("assignTo")
            return_type = next_return.get("returnType")
            if assign_to and return_type:
                self.symbol_types[assign_to] = return_type
            var_type = short_type(java_type(return_type, ir=self.ir))
            self._add_import_for_type(return_type)
            return ["    " * indent + f"{var_type} {assign_to} = {call};"]
        if m.get("declaredReturnType") == "void" or m.get("returnType") == "void":
            return ["    " * indent + f"{call};"]
        return ["    " * indent + f"{call};"]

    def _find_following_assignment_return(self, m: Dict[str, Any], scope_message_ids: Optional[Set[str]]) -> Optional[Dict[str, Any]]:
        seq = m.get("seq", 0)
        candidates = [x for x in self.behavior.get("messages", []) if x.get("seq", 0) > seq]
        candidates.sort(key=lambda x: x.get("seq", 0))
        for r in candidates:
            if scope_message_ids is not None and r.get("id") not in scope_message_ids:
                continue
            if r.get("kind") != "return" or r.get("returnMode") != "assignment":
                # first non-assignment return means this call has no local assignment
                continue
            # Accept next assignment return when it is from target back to source or self return.
            if r.get("source") == m.get("target") and r.get("target") == m.get("source"):
                return r
            if m.get("source") == m.get("target") == r.get("source") == r.get("target"):
                return r
            # Repository call return case: repo -> service.
            if m.get("target") == r.get("source") and m.get("source") == r.get("target"):
                return r
            # Stop searching after first later assignment in scope, even if not matching, to avoid bad pairing.
            return None
        return None

    def _call_expression(self, m: Dict[str, Any]) -> str:
        operation = m.get("operation") or "unknownOperation"
        args = ", ".join(self._translate_expr(a) for a in m.get("arguments", []))
        target = m.get("target")
        source = m.get("source")
        if self.lifeline_roles.get(target) == "repository":
            repo_field = self.repo_fields.get(target, lower_first(target or "repository"))
            return f"{repo_field}.{operation}({args})"
        # self-service calls use local methods.
        return f"{operation}({args})"

    def _translate_expr(self, expr: Optional[str]) -> str:
        if expr is None:
            return "null"
        e = str(expr).strip()
        # Type-only argument labels from EA may appear as model type names. Prefer variable-ish lower camel fallback.
        if e in known_model_types(self.ir):
            return lower_first(e)
        # Convert simple property access foo.bar -> foo.getBar(), preserving string literals around it.
        def repl(match: re.Match[str]) -> str:
            obj, prop = match.group(1), match.group(2)
            return f"{obj}.get{upper_first(prop)}()"
        e = re.sub(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b", repl, e)
        return e

    def _translate_guard(self, guard: Optional[str]) -> str:
        g = (guard or "true").strip()
        if not g:
            return "true"
        if g.startswith("not "):
            return "!" + self._translate_guard(g[4:])
        if " implies " in g:
            left, right = g.split(" implies ", 1)
            return f"(!({self._translate_guard(left)}) || ({self._translate_guard(right)}))"
        g = g.replace("<>", "!=")
        g = g.replace(" = ", " == ")
        # Specific OCL-ish enum check.
        m = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\s+is\s+([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)", g)
        if m:
            return f"{m.group(1)} == {m.group(2)}"
        return self._translate_expr(g)

    def _loop_header(self, guard: str) -> str:
        guard = (guard or "").strip()
        m = re.fullmatch(r"for each\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+in\s+(.+)", guard)
        if m:
            var = m.group(1)
            collection_raw = m.group(2).strip()
            collection = self._translate_expr(collection_raw)
            loop_type = "var"
            path_match = re.fullmatch(r"([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)", collection_raw)
            if path_match:
                obj_name, field_name = path_match.group(1), path_match.group(2)
                owner_type = self.symbol_types.get(obj_name)
                field = field_by_name(self.ir, owner_type, field_name)
                if field and field.get("collection"):
                    loop_type = short_type(java_type(field.get("type"), ir=self.ir))
                    self._add_import_for_type(field.get("type"))
                    self.symbol_types[var] = field.get("type")
            return f"for ({loop_type} {var} : {collection})"
        return f"while ({self._translate_guard(guard)})"

    def _add_import_for_type(self, type_name: Optional[str]) -> None:
        jt = java_type(type_name, ir=self.ir)
        if "." in jt:
            self.imports.add(jt)

    def _block(self, header: str, body: List[str], indent: int) -> List[str]:
        lines = ["    " * indent + header + " {"]
        lines.extend(body or ["    " * (indent + 1) + "// TODO: empty generated block."])
        lines.append("    " * indent + "}")
        return lines

    def _comment(self, text: str, indent: int) -> List[str]:
        return ["    " * indent + f"// TODO: {text}"]


def generate_application(base_dir: Path, base_package: str) -> None:
    path = base_dir / "src/main/java" / Path(*base_package.split(".")) / "OpenApiSkeletonApplication.java"
    if path.exists():
        return
    content = f"""
package {base_package};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = "org.openapitools")
public class OpenApiSkeletonApplication {{

    public static void main(String[] args) {{
        SpringApplication.run(OpenApiSkeletonApplication.class, args);
    }}
}}
"""
    write_text(path, content)


def generate_rest_response(base_dir: Path) -> None:
    package = "org.openapitools.response"
    base_path = base_dir / "src/main/java/org/openapitools/response"
    write_text(base_path / "RestResponse.java", f"""
package {package};

import org.springframework.http.HttpStatus;

public interface RestResponse {{
    HttpStatus status();
    Object responseBody();
}}
""")


def generate_response_classes(base_dir: Path, ir: Dict[str, Any]) -> None:
    package = "org.openapitools.response"
    base_path = base_dir / "src/main/java/org/openapitools/response"
    for r in bim_section(ir).get("responses", []):
        if r.get("abstractBase"):
            continue
        name = r.get("name")
        status = r.get("status") or "OK_200"
        status_name = re.sub(r"_\d+$", "", status)
        body_type_name = r.get("responseBody")
        body_type = java_type(body_type_name, ir=ir) if body_type_name else None
        imports = {"org.springframework.http.HttpStatus"}
        if body_type and "." in body_type:
            imports.add(body_type)
        imports_str = "\n".join(f"import {i};" for i in sorted(imports))
        body_short = short_type(body_type) if body_type else "Void"
        if body_type:
            field = f"    private final {body_short} body;"
            ctor = f"""
    public {name}({body_short} body) {{
        this.body = body;
    }}
"""
            response_body = "body"
        else:
            field = ""
            ctor = f"""
    public {name}() {{
    }}
"""
            response_body = "null"
        content = f"""
package {package};

{imports_str}

public class {name} implements RestResponse {{
{field}
{ctor}
    @Override
    public HttpStatus status() {{
        return HttpStatus.{status_name};
    }}

    @Override
    public Object responseBody() {{
        return {response_body};
    }}
}}
"""
        write_text(base_path / f"{name}.java", content)


def java_default_literal(value: Any, type_name: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if not text:
        return None
    if (type_name or "").upper() in {"BOOLEAN"}:
        return "true" if text.lower() in {"true", "1", "yes"} else "false"
    if (type_name or "").upper() in {"INTEGER", "LONG", "FLOAT", "DOUBLE", "DECIMAL"}:
        return text
    return f'"{text}"'


def field_default(field: Dict[str, Any]) -> Optional[str]:
    for key in ("defaultValue", "default", "initialValue", "value"):
        if key in field and field.get(key) not in (None, ""):
            return java_default_literal(field.get(key), field.get("type"))
    return None


def is_embeddable_entity(entity: Dict[str, Any]) -> bool:
    return not any(f.get("id") for f in entity.get("fields", []))


def generate_entities(base_dir: Path, ir: Dict[str, Any]) -> None:
    package = "org.openapitools.domain"
    base_path = base_dir / "src/main/java/org/openapitools/domain"
    entity_by_name = {e.get("name"): e for e in ir.get("persistence", {}).get("entities", [])}
    for e in ir.get("persistence", {}).get("entities", []):
        name = e.get("name")
        embeddable = is_embeddable_entity(e)
        imports: Set[str] = set()
        annotations: List[str] = []
        if embeddable:
            imports.add("jakarta.persistence.Embeddable")
            annotations.append("@Embeddable")
        else:
            imports.add("jakarta.persistence.Entity")
            imports.add("jakarta.persistence.Table")
            table_name = table_name_for_entity(name)
            annotations.extend(["@Entity", f'@Table(name = "{table_name}")'])

        fields_code: List[str] = []
        methods_code: List[str] = []
        for f in e.get("fields", []):
            jt = java_type(f.get("type"), ir=ir)
            raw_short = short_type(jt)
            field_name = f.get("name")
            field_annotations: List[str] = []
            if f.get("id"):
                imports.add("jakarta.persistence.Id")
                field_annotations.append("    @Id")
            if f.get("collection"):
                imports.add("java.util.List")
                imports.add("java.util.ArrayList")
                imports.add("jakarta.persistence.ElementCollection")
                imports.add("jakarta.persistence.FetchType")
                if f.get("type") in model_enum_names(ir):
                    imports.add("jakarta.persistence.Enumerated")
                    imports.add("jakarta.persistence.EnumType")
                    field_annotations.append("    @ElementCollection(fetch = FetchType.EAGER)")
                    field_annotations.append("    @Enumerated(EnumType.STRING)")
                else:
                    field_annotations.append("    @ElementCollection(fetch = FetchType.EAGER)")
                if "." in jt:
                    imports.add(jt)
                jt_short = f"List<{raw_short}>"
                initializer = " = new ArrayList<>()"
            else:
                if "." in jt:
                    imports.add(jt)
                jt_short = raw_short
                initializer = ""
                if f.get("type") in entity_by_name and is_embeddable_entity(entity_by_name[f.get("type")]):
                    imports.add("jakarta.persistence.Embedded")
                    field_annotations.append("    @Embedded")
                default_literal = field_default(f)
                if default_literal is not None:
                    initializer = f" = {default_literal}"
            fields_code.extend(field_annotations)
            fields_code.append(f"    private {jt_short} {field_name}{initializer};")
            cap = upper_first(field_name)
            methods_code.append("""
    public {jt_short} get{cap}() {{
        return {field_name};
    }}

    public void set{cap}({jt_short} {field_name}) {{
        this.{field_name} = {field_name};
    }}
""".format(jt_short=jt_short, cap=cap, field_name=field_name))
        imports_str = "\n".join(f"import {i};" for i in sorted(imports))
        content = """
package {package};

{imports_str}

{annotations}
public class {name} {{
{fields}

{methods}
}}
""".format(package=package, imports_str=imports_str, annotations=chr(10).join(annotations), name=name, fields=chr(10).join(fields_code), methods=chr(10).join(methods_code))
        write_text(base_path / f"{name}.java", content)

def generate_repository(base_dir: Path, ir: Dict[str, Any]) -> None:
    package = "org.openapitools.repository"
    base_path = base_dir / "src/main/java/org/openapitools/repository"
    entities = ir.get("persistence", {}).get("entities", [])
    main_entity = next((e for e in entities if any(f.get("id") for f in e.get("fields", []))), None)
    if main_entity is None and entities:
        main_entity = entities[0]
    main_entity_name = main_entity.get("name") if main_entity else "Object"
    id_field = next((f for f in (main_entity or {}).get("fields", []) if f.get("id")), {"type": "UUID"})
    id_type = java_type(id_field.get("type"), ir=ir)
    for iface in bim_section(ir).get("behaviorInterfaces", []):
        if iface.get("role") != "repository":
            continue
        name = iface.get("name")
        imports = {
            "org.springframework.stereotype.Repository",
            "org.springframework.data.jpa.repository.JpaRepository",
            f"org.openapitools.domain.{main_entity_name}",
        }
        if "." in id_type:
            imports.add(id_type)
        methods: List[str] = []
        for op in iface.get("operations", []):
            # save(...) is inherited from JpaRepository. We only declare custom finder-like operations.
            if op.get("name") == "save":
                continue
            return_type = java_type(op.get("returnType"), ir=ir)
            if "." in return_type:
                imports.add(return_type)
            rt_short = short_type(return_type)
            params = []
            for p in op.get("parameters", []):
                pt = java_type(p.get("type"), ir=ir)
                if "." in pt:
                    imports.add(pt)
                params.append(f"{short_type(pt)} {p.get('name')}")
            methods.append(f"    {rt_short} {op.get('name')}({', '.join(params)});")
        imports_str = "\n".join(f"import {i};" for i in sorted(imports))
        content = f"""
package {package};

{imports_str}

@Repository
public interface {name} extends JpaRepository<{main_entity_name}, {short_type(id_type)}> {{
{chr(10).join(methods)}
}}
"""
        write_text(base_path / f"{name}.java", content)


def add_common_model_imports(imports: Set[str], ir: Dict[str, Any]) -> None:
    for dto in ir.get("api", {}).get("dtos", []):
        imports.add(f"org.openapitools.model.{dto.get('name')}")
    for enum in ir.get("api", {}).get("enums", []):
        imports.add(f"org.openapitools.model.{enum.get('name')}")
    for entity in ir.get("persistence", {}).get("entities", []):
        imports.add(f"org.openapitools.domain.{entity.get('name')}")


def mapper_stub(method_name: str, source_type: str, target_type: str) -> str:
    return f"""
    private {target_type} {method_name}({source_type} source) {{
        // TODO: implement {source_type} -> {target_type} mapping.
        throw new UnsupportedOperationException("{source_type} -> {target_type} mapping is not implemented yet.");
    }}
"""


def additional_mapper_methods(ir: Dict[str, Any], existing_signatures: Optional[Set[Tuple[str, str]]] = None) -> List[str]:
    # Mapper signatures are generated from structural DTO↔Entity mappings, but their
    # bodies are explicit implementation stubs. The model tells us that a mapping
    # exists; it does not define full mapping semantics.
    existing_signatures = existing_signatures or set()
    methods: List[str] = []
    for mapping in ir.get("mappings", {}).get("dtoToEntity", []):
        dto = mapping.get("dto")
        entity = mapping.get("entity")
        if not dto or not entity:
            continue
        if ("mapToEntity", dto) not in existing_signatures:
            methods.append(mapper_stub("mapToEntity", dto, entity))
            existing_signatures.add(("mapToEntity", dto))
        if ("mapToDto", entity) not in existing_signatures:
            methods.append(mapper_stub("mapToDto", entity, dto))
            existing_signatures.add(("mapToDto", entity))
    return methods

def generate_service(base_dir: Path, ir: Dict[str, Any]) -> None:
    package = "org.openapitools.service"
    base_path = base_dir / "src/main/java/org/openapitools/service"
    behavior_by_endpoint = {b.get("represents"): b for b in bm_section(ir).get("behaviors", [])}
    # For now one behavior; map service method validateRequest to that behavior.
    behavior = next(iter(behavior_by_endpoint.values()), None)
    for iface in bim_section(ir).get("behaviorInterfaces", []):
        if iface.get("role") != "service":
            continue
        name = iface.get("name")
        imports: Set[str] = {"org.springframework.stereotype.Service", "org.openapitools.response.RestResponse"}
        add_common_model_imports(imports, ir)
        # Add repository dependencies from lifelines.
        repo_names: List[str] = []
        if behavior:
            for l in behavior.get("lifelines", []):
                if l.get("role") == "repository" and l.get("name") not in repo_names:
                    repo_names.append(l.get("name"))
        repo_fields, repo_ctor_args, repo_assignments = [], [], []
        for repo in repo_names:
            imports.add(f"org.openapitools.repository.{repo}")
            field = lower_first(repo)
            repo_fields.append(f"    private final {repo} {field};")
            repo_ctor_args.append(f"{repo} {field}")
            repo_assignments.append(f"        this.{field} = {field};")

        methods: List[str] = []
        for op in iface.get("operations", []):
            return_type = java_type(op.get("returnType"), ir=ir)
            if "." in return_type:
                imports.add(return_type)
            rt_short = "RestResponse" if op.get("returnType") == "HttpResponse" else short_type(return_type)
            params: List[str] = []
            for p in op.get("parameters", []):
                pt = java_type(p.get("type"), ir=ir)
                if "." in pt:
                    imports.add(pt)
                # Normalize validateRequest parameter from 'request' to preserve BM argument names.
                params.append(f"{short_type(pt)} {p.get('name')}")
            if op.get("name") == "validateRequest" and behavior:
                # Add imports used by behavior code.
                gen = BehaviorCodeGenerator(behavior, imports, ir, op.get("parameters", []))
                body = gen.generate_method_body()
            else:
                body = helper_stub_body(op.get("name"), rt_short)
                # helper stubs may need DTO/entity imports from params/return already added above
            methods.append(f"""
    public {rt_short} {op.get('name')}({', '.join(params)}) {{
{body}
    }}
""")
        existing_signatures: Set[Tuple[str, str]] = set()
        for op in iface.get("operations", []):
            params0 = op.get("parameters", [])
            if params0:
                existing_signatures.add((op.get("name"), params0[0].get("type")))
        methods.extend(additional_mapper_methods(ir, existing_signatures))
        ctor = ""
        if repo_ctor_args:
            ctor = f"""
    public {name}({', '.join(repo_ctor_args)}) {{
{chr(10).join(repo_assignments)}
    }}
"""
        imports_str = "\n".join(f"import {i};" for i in sorted(imports))
        content = f"""
package {package};

{imports_str}

@Service
public class {name} {{
{chr(10).join(repo_fields)}
{ctor}
{chr(10).join(methods)}
}}
"""
        write_text(base_path / f"{name}.java", content)


def helper_stub_body(method_name: str, rt_short: str) -> str:
    # All helper methods are generated as explicit implementation stubs.
    # The BM generator creates the control-flow structure, but does not invent
    # business logic, validation, mapping, integration, or error construction semantics.
    return (
        f"        // TODO: implement {method_name}.\n"
        f"        throw new UnsupportedOperationException(\"{method_name} is not implemented yet.\");"
    )

def generate_controllers(base_dir: Path, ir: Dict[str, Any]) -> None:
    package = "org.openapitools.controller"
    base_path = base_dir / "src/main/java/org/openapitools/controller"
    endpoints_by_controller: Dict[str, List[Dict[str, Any]]] = {}
    for endpoint in bim_section(ir).get("endpointOperations", []):
        controller = endpoint.get("controller")
        if controller:
            endpoints_by_controller.setdefault(controller, []).append(endpoint)
    behavior_by_endpoint = {b.get("represents"): b for b in bm_section(ir).get("behaviors", [])}
    for controller_name, endpoints in endpoints_by_controller.items():
        api_name = f"{controller_name}Api"
        class_name = controller_name
        imports: Set[str] = {
            "org.springframework.stereotype.Controller",
            "org.springframework.http.ResponseEntity",
            "org.openapitools.response.RestResponse",
            f"org.openapitools.api.{api_name}",
        }
        service_names: List[str] = []
        for endpoint in endpoints:
            behavior = behavior_by_endpoint.get(endpoint.get("name"))
            if behavior:
                for m in sorted(behavior.get("messages", []), key=lambda x: x.get("seq", 0)):
                    if m.get("source") == controller_name and role_of(m.get("target"), lifeline_role_map(behavior)) == "service":
                        svc = m.get("target")
                        if svc not in service_names:
                            service_names.append(svc)
                        break
        if not service_names:
            service_names = []
        fields, ctor_args, assigns = [], [], []
        for svc in service_names:
            imports.add(f"org.openapitools.service.{svc}")
            field = lower_first(svc)
            fields.append(f"    private final {svc} {field};")
            ctor_args.append(f"{svc} {field}")
            assigns.append(f"        this.{field} = {field};")
        methods: List[str] = []
        for endpoint in endpoints:
            param_set = find_param_set(ir, endpoint.get("parameters"))
            body_param_name = method_param_name_for_controller(endpoint, param_set)
            if param_set:
                for f in param_set.get("fields", []):
                    t = java_type(f.get("type"), ir=ir)
                    if "." in t:
                        imports.add(t)
            behavior = behavior_by_endpoint.get(endpoint.get("name"))
            service_method = service_method_for_endpoint(endpoint, behavior)
            service_field = lower_first(service_names[0]) if service_names else "service"
            op_id = endpoint.get("operationId") or lower_first(endpoint.get("name", "operation"))
            body_type_name = "Object"
            if param_set and (param_set.get("fields") or []):
                body_fields = [f for f in param_set.get("fields", []) if f.get("location") == "BODY"]
                selected_field = body_fields[0] if body_fields else param_set.get("fields", [])[0]
                body_type_name = selected_field.get("type", "Object")
            body_type_short = short_type(java_type(body_type_name, ir=ir))
            methods.append(f"""
    @Override
    @SuppressWarnings({{"rawtypes", "unchecked"}})
    public ResponseEntity {op_id}({body_type_short} {body_param_name}) {{
        RestResponse response = {service_field}.{service_method}({body_param_name});
        return ResponseEntity
                .status(response.status())
                .body(response.responseBody());
    }}
""")
        ctor = f"""
    public {class_name}({', '.join(ctor_args)}) {{
{chr(10).join(assigns)}
    }}
"""
        imports_str = "\n".join(f"import {i};" for i in sorted(imports))
        content = f"""
package {package};

{imports_str}

@Controller
public class {class_name} implements {api_name} {{
{chr(10).join(fields)}
{ctor}
{chr(10).join(methods)}
}}
"""
        write_text(base_path / f"{class_name}.java", content)



def ensure_pom_dependency(pom: Path, group_id: str, artifact_id: str, scope: Optional[str] = None) -> None:
    text = pom.read_text(encoding="utf-8")
    if f"<artifactId>{artifact_id}</artifactId>" in text:
        return
    scope_line = f"\n            <scope>{scope}</scope>" if scope else ""
    dep = f"""
        <dependency>
            <groupId>{group_id}</groupId>
            <artifactId>{artifact_id}</artifactId>{scope_line}
        </dependency>"""
    text = text.replace("\n    </dependencies>", dep + "\n    </dependencies>")
    pom.write_text(text, encoding="utf-8")


def configure_jpa(base_dir: Path) -> None:
    pom = base_dir / "pom.xml"
    if pom.exists():
        ensure_pom_dependency(pom, "org.springframework.boot", "spring-boot-starter-data-jpa")
        ensure_pom_dependency(pom, "com.h2database", "h2", "runtime")
    props = base_dir / "src/main/resources/application.properties"
    ensure_dir(props.parent)
    existing = props.read_text(encoding="utf-8") if props.exists() else ""
    additions = """

spring.datasource.url=jdbc:h2:mem:restgen
spring.datasource.driverClassName=org.h2.Driver
spring.datasource.username=sa
spring.datasource.password=
spring.jpa.hibernate.ddl-auto=create-drop
spring.jpa.show-sql=true
spring.h2.console.enabled=true
spring.h2.console.path=/h2-console
""".strip()
    if "spring.datasource.url" not in existing:
        props.write_text((existing.rstrip() + "\n\n" + additions + "\n").lstrip(), encoding="utf-8")

def copy_input(input_dir: Path, output_dir: Path, force: bool) -> None:
    if output_dir.exists():
        if not force:
            raise SystemExit(f"Output directory already exists: {output_dir}. Use --force to overwrite.")
        shutil.rmtree(output_dir)
    shutil.copytree(input_dir, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Spring backend skeleton + BM method structure from REST IR.")
    parser.add_argument("--ir", required=True, type=Path, help="Combined REST IR JSON, e.g. full.json/rest_ir.json")
    parser.add_argument("--input", required=True, type=Path, help="OpenAPI Generator interfaceOnly=true output directory")
    parser.add_argument("--out", required=True, type=Path, help="Output backend directory")
    parser.add_argument("--base-package", default="org.openapitools", help="Spring application base package")
    parser.add_argument("--force", action="store_true", help="Overwrite output directory")
    args = parser.parse_args()

    ir = load_json(args.ir)
    copy_input(args.input, args.out, args.force)
    generate_application(args.out, args.base_package)
    configure_jpa(args.out)
    generate_rest_response(args.out)
    generate_response_classes(args.out, ir)
    generate_entities(args.out, ir)
    generate_repository(args.out, ir)
    generate_service(args.out, ir)
    generate_controllers(args.out, ir)
    print(f"Generated backend with BM method skeletons and JPA persistence in: {args.out}")


if __name__ == "__main__":
    main()
