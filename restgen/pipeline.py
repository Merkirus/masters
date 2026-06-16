#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

from restgen.parsers.structural_parser import parse_structural
from restgen.parsers.bm_parser import parse_bm
from restgen.ir.merge import merge
from restgen.generators.openapi_generator import build_openapi
from restgen.generators.spring_backend_generator import (
    copy_input,
    configure_jpa,
    generate_application,
    generate_controllers,
    generate_entities,
    generate_repository,
    generate_response_classes,
    generate_rest_response,
    generate_service,
)


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_openapi(path: Path, data: Dict[str, Any]) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False), encoding="utf-8")


def cmd_parse(args: argparse.Namespace) -> None:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    structural = parse_structural(args.cm, args.pm, args.bim)
    write_json(out / "structural_ir.json", structural)

    bm_ir = parse_bm(args.bm, structural=out / "structural_ir.json", geometry_mode=args.geometry_mode)
    # parse_bm returns a dataclass in the current implementation.
    try:
        from dataclasses import asdict
        bm_data = asdict(bm_ir)
    except Exception:
        bm_data = bm_ir
    write_json(out / "bm_ir.json", bm_data)

    rest = merge(structural, bm_data)
    write_json(out / "rest_ir.json", rest)

    print(f"Wrote {out / 'structural_ir.json'}")
    print(f"Wrote {out / 'bm_ir.json'}")
    print(f"Wrote {out / 'rest_ir.json'}")
    print(f"Diagnostics: structural={len(structural.get('diagnostics', []))}, bm={len(bm_data.get('diagnostics', []))}, rest={len(rest.get('diagnostics', []))}")


def cmd_openapi(args: argparse.Namespace) -> None:
    structural = read_json(args.structural_ir)
    openapi = build_openapi(structural, title=args.title, version=args.version)
    write_openapi(args.out, openapi)
    print(f"Wrote {args.out}")


def run_openapi_generator(openapi_yaml: Path, interface_out: Path, base_package: str, force: bool) -> None:
    if force and interface_out.exists():
        shutil.rmtree(interface_out)
    cmd = [
        "openapi-generator-cli", "generate",
        "-i", str(openapi_yaml),
        "-g", "spring",
        "-o", str(interface_out),
        "--additional-properties", f"interfaceOnly=true,useSpringBoot3=true,useTags=true,basePackage={base_package}",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True)


def generate_backend(rest_ir: Path, interface_dir: Path, out: Path, base_package: str, force: bool) -> None:
    ir = read_json(rest_ir)
    copy_input(interface_dir, out, force)
    generate_application(out, base_package)
    configure_jpa(out)
    generate_rest_response(out)
    generate_response_classes(out, ir)
    generate_entities(out, ir)
    generate_repository(out, ir)
    generate_service(out, ir)
    generate_controllers(out, ir)
    print(f"Wrote {out}")


def cmd_backend(args: argparse.Namespace) -> None:
    generate_backend(args.rest_ir, args.interface_dir, args.out, args.base_package, args.force)


def cmd_all(args: argparse.Namespace) -> None:
    out = args.out
    cmd_parse(args)
    openapi_path = out / "openapi.yaml"
    openapi = build_openapi(read_json(out / "structural_ir.json"), title=args.title, version=args.version)
    write_openapi(openapi_path, openapi)
    print(f"Wrote {openapi_path}")

    interface_dir = args.interface_dir or (out / "generated-backend-interface")
    if args.run_openapi_generator:
        run_openapi_generator(openapi_path, interface_dir, args.base_package, args.force)

    if interface_dir.exists():
        generate_backend(out / "rest_ir.json", interface_dir, out / "generated-backend", args.base_package, args.force)
    else:
        print(f"Skipped backend generation: interface dir does not exist: {interface_dir}")
        print("Run OpenAPI Generator first or pass --run-openapi-generator.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="restgen", description="REST backend generation pipeline from UML/EA XMI models.")
    sub = p.add_subparsers(dest="command", required=True)

    parse = sub.add_parser("parse", help="Parse CM + PM + BIM + BM XMI into IR files.")
    parse.add_argument("--cm", required=True, type=Path, help="Class Model XMI")
    parse.add_argument("--pm", required=True, type=Path, help="Persistence Model XMI")
    parse.add_argument("--bim", required=True, type=Path, help="Behavior Interface Model XMI")
    parse.add_argument("--bm", required=True, type=Path, help="Behavior Model XMI")
    parse.add_argument("--out", required=True, type=Path, help="Output directory")
    parse.add_argument("--geometry-mode", choices=["vertical", "overlap", "point"], default="vertical")
    parse.set_defaults(func=cmd_parse)

    oa = sub.add_parser("openapi", help="Generate openapi.yaml from structural_ir.json.")
    oa.add_argument("--structural-ir", required=True, type=Path)
    oa.add_argument("--out", required=True, type=Path)
    oa.add_argument("--title", default="Generated REST API")
    oa.add_argument("--version", default="0.1.0")
    oa.set_defaults(func=cmd_openapi)

    be = sub.add_parser("backend", help="Generate Spring backend from rest_ir.json and OpenAPI Generator interface output.")
    be.add_argument("--rest-ir", required=True, type=Path)
    be.add_argument("--interface-dir", required=True, type=Path)
    be.add_argument("--out", required=True, type=Path)
    be.add_argument("--base-package", default="org.openapitools")
    be.add_argument("--force", action="store_true")
    be.set_defaults(func=cmd_backend)

    allp = sub.add_parser("all", help="Run parse + OpenAPI + optional OpenAPI Generator + backend generation.")
    allp.add_argument("--cm", required=True, type=Path)
    allp.add_argument("--pm", required=True, type=Path)
    allp.add_argument("--bim", required=True, type=Path)
    allp.add_argument("--bm", required=True, type=Path)
    allp.add_argument("--out", required=True, type=Path)
    allp.add_argument("--geometry-mode", choices=["vertical", "overlap", "point"], default="vertical")
    allp.add_argument("--title", default="Generated REST API")
    allp.add_argument("--version", default="0.1.0")
    allp.add_argument("--interface-dir", type=Path, help="Existing OpenAPI Generator interface output. If omitted, uses OUT/generated-backend-interface.")
    allp.add_argument("--run-openapi-generator", action="store_true", help="Run openapi-generator-cli to create interface output.")
    allp.add_argument("--base-package", default="org.openapitools")
    allp.add_argument("--force", action="store_true")
    allp.set_defaults(func=cmd_all)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
