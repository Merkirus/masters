#!/usr/bin/env python3
from __future__ import annotations

import argparse, json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .cm_pm_parser import parse_cm_pm
from .bim_parser import parse_bim


def _unique_diagnostics(*groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen=set(); out=[]
    for group in groups:
        for d in group or []:
            key=(d.get("level"), d.get("code"), d.get("message"))
            if key not in seen:
                seen.add(key); out.append(d)
    return out


def _body_type(response: Dict[str, Any]) -> Optional[str]:
    return response.get("responseBody") or response.get("body")


def _collect_known_types(ir: Dict[str, Any]) -> Set[str]:
    known=set(ir.get("typeSummary",{}).get("primitiveTypes",[]))
    for section in ("api", "persistence"):
        for enum in ir.get(section,{}).get("enums",[]): known.add(enum.get("name"))
    for dto in ir.get("api",{}).get("dtos",[]): known.add(dto.get("name"))
    for ent in ir.get("persistence",{}).get("entities",[]): known.add(ent.get("name"))
    b=ir.get("bim",{})
    for r in b.get("responses",[]): known.add(r.get("name"))
    for ps in b.get("parameterSets",[]): known.add(ps.get("name"))
    known.update(["HttpResponse", "void", "VOID"])
    return {x for x in known if x}


def _validate_cross_model(ir: Dict[str, Any]) -> List[Dict[str, Any]]:
    diagnostics=[]
    api_dtos={d.get("name") for d in ir.get("api",{}).get("dtos",[]) if d.get("name")}
    entities={e.get("name") for e in ir.get("persistence",{}).get("entities",[]) if e.get("name")}
    b=ir.get("bim",{})
    parameter_sets={p.get("name") for p in b.get("parameterSets",[]) if p.get("name")}
    responses={r.get("name") for r in b.get("responses",[]) if r.get("name")}
    behavior_interfaces={i.get("name") for i in b.get("behaviorInterfaces",[]) if i.get("name")}
    endpoint_ops={o.get("name") for o in b.get("endpointOperations",[]) if o.get("name")}
    known_types=_collect_known_types(ir)
    for dto in ir.get("api",{}).get("dtos",[]):
        mt=dto.get("mapsTo")
        if mt and mt not in entities:
            diagnostics.append({"level":"error","code":"DTO_MAPS_TO_UNKNOWN_ENTITY","message":f"DTO {dto.get('name')} mapsTo unknown entity {mt}."})
    for ep in b.get("endpointOperations",[]):
        name=ep.get("name")
        if not ep.get("httpMethod"): diagnostics.append({"level":"error","code":"ENDPOINT_MISSING_HTTP_METHOD","message":f"Endpoint operation {name} has no httpMethod."})
        if not ep.get("uri"): diagnostics.append({"level":"error","code":"ENDPOINT_MISSING_URI","message":f"Endpoint operation {name} has no uri."})
        params=ep.get("parameters")
        if params and params not in parameter_sets: diagnostics.append({"level":"error","code":"ENDPOINT_UNKNOWN_PARAMETER_SET","message":f"Endpoint operation {name} uses unknown parameter set {params}."})
        if not ep.get("responses"): diagnostics.append({"level":"error","code":"ENDPOINT_WITHOUT_RESPONSES","message":f"Endpoint operation {name} has no responses."})
        for resp in ep.get("responses") or []:
            if resp not in responses: diagnostics.append({"level":"error","code":"ENDPOINT_UNKNOWN_RESPONSE","message":f"Endpoint operation {name} references unknown response {resp}."})
        controller=ep.get("controller")
        if controller and controller not in behavior_interfaces: diagnostics.append({"level":"error","code":"ENDPOINT_UNKNOWN_CONTROLLER","message":f"Endpoint operation {name} references unknown controller/interface {controller}."})
    for ps in b.get("parameterSets",[]):
        for field in ps.get("fields",[]):
            fname=f"{ps.get('name')}.{field.get('name')}"; typ=field.get("type"); loc=field.get("location")
            if loc=="UNKNOWN": diagnostics.append({"level":"warning","code":"PARAMETER_UNKNOWN_LOCATION","message":f"Parameter {fname} has unknown location stereotype."})
            if typ and typ not in known_types: diagnostics.append({"level":"warning","code":"PARAMETER_UNKNOWN_TYPE","message":f"Parameter {fname} uses unknown type {typ}."})
            if loc=="BODY" and typ not in api_dtos: diagnostics.append({"level":"error","code":"BODY_NOT_DTO","message":f"BODY parameter {fname} should reference an API DTO, got {typ}."})
    for resp in b.get("responses",[]):
        name=resp.get("name")
        if resp.get("abstractBase"): continue
        if not resp.get("status") and not resp.get("statusCode"): diagnostics.append({"level":"error","code":"RESPONSE_MISSING_STATUS","message":f"Response {name} has no status/httpStatus."})
        body=_body_type(resp)
        if body and body not in api_dtos: diagnostics.append({"level":"error","code":"RESPONSE_BODY_NOT_DTO","message":f"Response {name} body should reference an API DTO, got {body}."})
        if not body and resp.get("statusCode") not in {204,205,304}: diagnostics.append({"level":"warning","code":"RESPONSE_WITHOUT_BODY","message":f"Response {name} has no responseBody."})
    for interface in b.get("behaviorInterfaces",[]):
        iname=interface.get("name")
        if not interface.get("operations"): diagnostics.append({"level":"warning","code":"INTERFACE_WITHOUT_OPERATIONS","message":f"Behavior interface {iname} has no operations."})
        for op in interface.get("operations") or []:
            opname=f"{iname}.{op.get('name')}"; rtype=op.get("returnType")
            if rtype and rtype not in known_types: diagnostics.append({"level":"warning","code":"OPERATION_UNKNOWN_RETURN_TYPE","message":f"Operation {opname} returns unknown type {rtype}."})
            for p in op.get("parameters") or []:
                ptype=p.get("type")
                if ptype and ptype not in known_types: diagnostics.append({"level":"warning","code":"OPERATION_PARAMETER_UNKNOWN_TYPE","message":f"Operation {opname} parameter {p.get('name')} uses unknown type {ptype}."})
            if (op.get("stereotype") or "").strip("<>").lower()=="endpointoperation":
                if not op.get("mapsTo"): diagnostics.append({"level":"error","code":"ENDPOINT_OPERATION_WITHOUT_MAPS_TO","message":f"Endpoint interface operation {opname} has no mapsTo ApiOperation."})
                elif op.get("mapsTo") not in endpoint_ops: diagnostics.append({"level":"error","code":"ENDPOINT_OPERATION_MAPS_TO_UNKNOWN","message":f"Endpoint interface operation {opname} mapsTo unknown ApiOperation {op.get('mapsTo')}."})
    return diagnostics


def parse_structural(cm_path: Path, pm_path: Path, bim_path: Path) -> Dict[str, Any]:
    cm_pm_ir=parse_cm_pm(cm_path, pm_path)
    bim_ir=parse_bim(bim_path)
    bim_section=bim_ir.get("bim") or bim_ir.get("bifm", {})
    ir={
        "metadata":{"stage":"structural","description":"Class Model + Persistence Model + Behavior Interface Model structural IR.","sources":{"classModel":str(cm_path),"persistenceModel":str(pm_path),"behaviorInterfaceModel":str(bim_path)}},
        "api":cm_pm_ir.get("api",{}),
        "persistence":cm_pm_ir.get("persistence",{}),
        "mappings":cm_pm_ir.get("mappings",{}),
        "typeSummary":cm_pm_ir.get("typeSummary",{}),
        "bim":bim_section,
        "diagnostics":[],
    }
    ir["diagnostics"]=_unique_diagnostics(cm_pm_ir.get("diagnostics",[]), bim_ir.get("diagnostics",[]), _validate_cross_model(ir))
    return ir


def main():
    ap=argparse.ArgumentParser(description="Parse structural IR from CM + PM + BIM XMI files.")
    ap.add_argument("--cm", type=Path, required=True)
    ap.add_argument("--pm", type=Path, required=True)
    ap.add_argument("--bim", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("structural_ir.json"))
    args=ap.parse_args()
    ir=parse_structural(args.cm,args.pm,args.bim)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(ir, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"Structural IR written to: {args.out}")
    print(f"DTOs: {len(ir.get('api',{}).get('dtos',[]))}")
    print(f"Entities: {len(ir.get('persistence',{}).get('entities',[]))}")
    print(f"Endpoint operations: {len(ir.get('bim',{}).get('endpointOperations',[]))}")
    print(f"Diagnostics: {len(ir.get('diagnostics',[]))}")

if __name__ == "__main__": main()
