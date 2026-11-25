#!/usr/bin/env python3
"""
build_lilytrans_cdn.py

Standalone stdlib-only builder for the Lilianze (jp.co.fanzagames.lilyange)
gctrans-inspired GitHub CDN repository.

Strict Zero-Game-Asset Policy:
- Never copies .unityweb, .bundle, .assets, .apk, .so, .dll, or any game binary.
- Never writes source_jp plaintext into translations/ko/scenarios/**/*.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_FRIDA_ZIP = r"D:\lily_workspace\android_research\refs\FRIDA_STAGE50_1_FULL_RUNTIME_REGISTRY_20261019.zip"
DEFAULT_BRIDGE_ZIP = r"D:\lily_workspace\android_research\refs\LILYANGE_UI_PUBLIC_PATCH_BRIDGE_20261027.zip"
DEFAULT_OUT_DIR = r"D:\lily_workspace\lilytrans"

FORBIDDEN_EXTENSIONS = {".unityweb", ".bundle", ".assets", ".apk", ".so", ".dll"}
EXPECTED_SCENARIO_FRAGMENTS = 86123

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

UI_SOURCE_PRIORITY: Dict[str, int] = {
    "ui_review": 10,
    "legacy_decisions": 20,
    "triaged": 30,
    "units": 40,
    "settings_overlay": 45,
    "settings": 46,
    "runtime_wasm_label_targets": 50,
    "runtime_wasm_skill_targets": 51,
    "runtime_wasm_b03c_targets": 52,
    "runtime_wasm_global_skill_targets": 53,
    "runtime_common_dictionary": 60,
    "runtime_quest_dictionary": 70,
}

NAME_PATH_SUFFIXES = (".name", "character_name", "chara_name", "speaker")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def write_json_file(path: Path, obj: Any, *, compact: bool = False) -> Tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    raw = text.encode("utf-8")
    path.write_bytes(raw)
    return sha256_bytes(raw), len(raw)


def find_zip_member(zf: zipfile.ZipFile, suffix: str) -> Optional[str]:
    norm_suffix = suffix.replace("\\", "/").lstrip("/")
    exact_matches: List[str] = []
    suffix_matches: List[str] = []
    for name in zf.namelist():
        if name.endswith("/"):
            continue
        norm = name.replace("\\", "/")
        if norm == norm_suffix:
            exact_matches.append(name)
        elif norm.endswith("/" + norm_suffix):
            suffix_matches.append(name)
    if exact_matches:
        return sorted(exact_matches)[0]
    if suffix_matches:
        return sorted(suffix_matches)[0]
    return None


def find_zip_members_matching(zf: zipfile.ZipFile, pattern: str) -> List[str]:
    rx = re.compile(pattern)
    matched = [
        n for n in zf.namelist()
        if not n.endswith("/") and rx.search(n.replace("\\", "/"))
    ]
    return sorted(matched)


def read_json_from_zip(zf: zipfile.ZipFile, member: str) -> Any:
    with zf.open(member, "r") as f:
        return json.loads(f.read().decode("utf-8-sig"))


def iter_jsonl_from_zip(zf: zipfile.ZipFile, member: str) -> Iterable[Dict[str, Any]]:
    with zf.open(member, "r") as f:
        for raw_line in f:
            line = raw_line.decode("utf-8-sig").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def scenario_group_for(script_name: str) -> str:
    if script_name.isdigit():
        return "main"
    lower = script_name.lower()
    if lower.startswith("chara"):
        return "chara"
    if lower.startswith("event"):
        return "event"
    return "other"


def sanitize_filename(name: str) -> str:
    cleaned = INVALID_FILENAME_RE.sub("_", name).strip(" .")
    if not cleaned:
        cleaned = "unnamed"
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def source_priority(source_name: str) -> int:
    s = (source_name or "").strip().lower()
    if s in UI_SOURCE_PRIORITY:
        return UI_SOURCE_PRIORITY[s]
    if s.startswith("runtime_wasm"):
        return 55
    return 80


def extract_registry_freeze_id(zf: zipfile.ZipFile, zip_path: Path) -> str:
    for name in sorted(zf.namelist()):
        if name.endswith("/") or not name.lower().endswith(".json"):
            continue
        base = os.path.basename(name).lower()
        if any(k in base for k in ("manifest", "freeze", "metadata", "summary", "registry_info")):
            try:
                data = read_json_from_zip(zf, name)
            except Exception:
                continue
            if isinstance(data, dict):
                for key in ("registry_freeze_id", "freeze_id", "build_id", "snapshot_id", "version"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
    return zip_path.stem


def locate_frida_fragments_members(zf: zipfile.ZipFile) -> List[str]:
    candidates: List[str] = []
    for name in sorted(zf.namelist()):
        if name.endswith("/"):
            continue
        low = name.lower()
        if not (low.endswith(".jsonl") or low.endswith(".json")):
            continue
        try:
            with zf.open(name, "r") as f:
                head = f.read(4096).decode("utf-8-sig", errors="ignore")
        except Exception:
            continue
        if "source_guard_sha256" in head and "replacement_ko" in head and "scriptName" in head:
            candidates.append(name)
    return candidates


def iter_frida_fragments(zf: zipfile.ZipFile) -> Iterable[Dict[str, Any]]:
    members = locate_frida_fragments_members(zf)
    if not members:
        raise RuntimeError("Could not locate scenario fragment registry inside frida zip.")
    # Prefer jsonl files if both jsonl and summary json exist
    jsonl_members = [m for m in members if m.lower().endswith(".jsonl")]
    chosen = jsonl_members if jsonl_members else members
    for member in chosen:
        if member.lower().endswith(".jsonl"):
            yield from iter_jsonl_from_zip(zf, member)
        else:
            data = read_json_from_zip(zf, member)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "scriptName" in item and "replacement_ko" in item:
                        yield item
            elif isinstance(data, dict):
                for key in ("fragments", "entries", "records", "items"):
                    val = data.get(key)
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, dict) and "scriptName" in item and "replacement_ko" in item:
                                yield item


def build_scenarios(
    frida_zf: zipfile.ZipFile,
    out_dir: Path,
) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
    """
    Build translations/ko/scenarios/<group>/<scriptName>.json and scenario_index.json.
    Returns (scenario_index, script_count, total_fragment_count).
    """
    # scriptName -> list of (lineIndex, inlineIndex, payload)
    scripts: Dict[str, List[Tuple[int, int, Dict[str, Any]]]] = defaultdict(list)

    for rec in iter_frida_fragments(frida_zf):
        script_name = str(rec.get("scriptName", "")).strip()
        if not script_name:
            continue
        try:
            line_idx = int(rec.get("lineIndex", 0))
            inline_idx = int(rec.get("inlineIndex", 0))
        except (TypeError, ValueError):
            continue

        mode_str = str(rec.get("source_guard_mode", "EXACT_UTF8")).strip().upper()
        mode_int = 1 if mode_str == "PLAYER_NAME_TEMPLATE" else 0

        payload = {
            "lh": str(rec.get("lineHash", "")),
            "m": mode_int,
            "g": str(rec.get("source_guard_sha256", "")),
            "t": str(rec.get("replacement_ko", "")),
        }
        scripts[script_name].append((line_idx, inline_idx, payload))

    scenarios_root = out_dir / "translations" / "ko" / "scenarios"
    scenario_index: Dict[str, Dict[str, Any]] = {}
    used_rel_paths: Dict[str, str] = {}
    total_fragments = 0

    for script_name in sorted(scripts.keys()):
        group = scenario_group_for(script_name)
        safe_base = sanitize_filename(script_name)
        rel_path = f"translations/ko/scenarios/{group}/{safe_base}.json"

        # Guard against rare filename collision after sanitization
        if rel_path.lower() in used_rel_paths and used_rel_paths[rel_path.lower()] != script_name:
            suffix = hashlib.sha256(script_name.encode("utf-8")).hexdigest()[:8]
            rel_path = f"translations/ko/scenarios/{group}/{safe_base}_{suffix}.json"
        used_rel_paths[rel_path.lower()] = script_name

        entries = scripts[script_name]
        entries.sort(key=lambda x: (x[0], x[1]))

        fragments_map: Dict[str, Dict[str, Any]] = {}
        for line_idx, inline_idx, payload in entries:
            fragments_map[f"{line_idx}:{inline_idx}"] = payload

        doc = {
            "script": script_name,
            "fragments": fragments_map,
        }

        out_path = out_dir / Path(rel_path)
        digest, size = write_json_file(out_path, doc, compact=True)
        count = len(fragments_map)
        total_fragments += count

        scenario_index[script_name] = {
            "path": rel_path,
            "sha256": digest,
            "count": count,
            "bytes": size,
        }

    return scenario_index, len(scenario_index), total_fragments


def _extract_string(d: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v != "":
            return v
    return ""


def _is_approved_candidate(cand: Dict[str, Any]) -> bool:
    if cand.get("approved") is True:
        return True
    status = str(cand.get("approval_status", "") or cand.get("status", "")).strip().upper()
    if status in {"APPROVED", "ACCEPTED", "REVIEWED"}:
        return True
    return False


def _is_primary_candidate(cand: Dict[str, Any]) -> bool:
    role = str(cand.get("variant_role", "") or cand.get("role", "")).strip().lower()
    if not role:
        return True
    return role == "primary"


def _collect_settings_pairs(obj: Any, out: List[Tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        # Direct source/translation object
        src = _extract_string(obj, "source_jp", "source", "jp", "original")
        ko = _extract_string(obj, "translation_ko", "translation", "ko", "replacement_ko")
        if src and ko:
            out.append((src, ko))
            return
        # Nested containers or flat string->string dictionary
        for k, v in obj.items():
            if isinstance(v, str) and v != "" and isinstance(k, str) and k != "":
                # Heuristic: ignore obvious metadata keys
                if k.lower() not in {
                    "schema", "version", "game", "language", "lang", "updated_at",
                    "generated_at", "description", "notes", "id", "name", "type",
                }:
                    out.append((k, v))
            elif isinstance(v, (dict, list)):
                _collect_settings_pairs(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_settings_pairs(item, out)


def build_ui_and_dictionaries(
    bridge_zf: zipfile.ZipFile,
) -> Tuple[
    Dict[str, str],
    Dict[str, str],
    Dict[str, List[Dict[str, Any]]],
    Dict[str, str],
    Dict[str, Dict[str, Dict[str, str]]],
    Any,
]:
    """
    Compile:
    - ui.json
    - ui_scoped.json
    - names.json
    - api_dict.json
    - api_templates.json
    """
    # source_jp -> (priority, source_name, translation_ko)
    ui_best: Dict[str, Tuple[int, str, str]] = {}

    def register_ui(source_jp: str, translation_ko: str, source_name: str) -> None:
        if not source_jp or not translation_ko:
            return
        prio = source_priority(source_name)
        existing = ui_best.get(source_jp)
        if existing is None:
            ui_best[source_jp] = (prio, source_name, translation_ko)
        else:
            old_prio, old_src, old_ko = existing
            if (prio, source_name, translation_ko) < (old_prio, old_src, old_ko):
                ui_best[source_jp] = (prio, source_name, translation_ko)

    # 1. inventory/merged_ui_candidates.jsonl
    merged_member = find_zip_member(bridge_zf, "inventory/merged_ui_candidates.jsonl")
    if merged_member:
        for rec in iter_jsonl_from_zip(bridge_zf, merged_member):
            source_jp = _extract_string(rec, "source_jp", "source", "text_jp")
            if not source_jp:
                continue

            has_approved = bool(rec.get("has_approved"))
            candidates: List[Dict[str, Any]] = []
            for list_key in (
                "approved_translations",
                "translation_candidates",
                "candidates",
                "translations",
            ):
                val = rec.get(list_key)
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            candidates.append(item)

            if has_approved or any(_is_approved_candidate(c) for c in candidates):
                found_in_list = False
                for cand in candidates:
                    if not _is_approved_candidate(cand):
                        continue
                    if not _is_primary_candidate(cand):
                        continue
                    ko = _extract_string(cand, "translation_ko", "translation", "ko", "text_ko")
                    src_name = _extract_string(cand, "source", "origin", "provider") or "merged_ui_candidates"
                    if ko:
                        register_ui(source_jp, ko, src_name)
                        found_in_list = True
                if not found_in_list:
                    ko = _extract_string(rec, "translation_ko", "approved_translation_ko", "translation")
                    src_name = _extract_string(rec, "approved_source", "source_name", "source") or "merged_ui_candidates"
                    if ko:
                        register_ui(source_jp, ko, src_name)

    # 2. runtime_wasm_*_targets.json (for ui.json and names.json)
    names_map: Dict[str, str] = {}
    wasm_members = find_zip_members_matching(
        bridge_zf, r"source/existing_ui_sources/runtime_wasm_.*_targets\.json$"
    )
    for member in wasm_members:
        source_name = Path(member).stem
        data = read_json_from_zip(bridge_zf, member)
        items = data if isinstance(data, list) else (data.get("entries", []) if isinstance(data, dict) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            src = _extract_string(item, "source", "source_jp")
            ko = _extract_string(item, "translation", "translation_ko")
            if src and ko:
                register_ui(src, ko, source_name)

            # Extract names
            owner_jp = _extract_string(item, "owner_source_jp")
            owner_ko = _extract_string(item, "owner_translation_ko")
            if owner_jp and owner_ko and owner_jp not in names_map:
                names_map[owner_jp] = owner_ko

            src_name_jp = _extract_string(item, "source_name_jp")
            disp_name_ko = _extract_string(item, "display_name_ko")
            if src_name_jp and disp_name_ko and src_name_jp not in names_map:
                names_map[src_name_jp] = disp_name_ko

            owners = item.get("owners")
            if isinstance(owners, list):
                for owner in owners:
                    if not isinstance(owner, dict):
                        continue
                    o_jp = _extract_string(owner, "source_name_jp", "owner_source_jp")
                    o_ko = _extract_string(owner, "display_name_ko", "owner_translation_ko")
                    if o_jp and o_ko and o_jp not in names_map:
                        names_map[o_jp] = o_ko

    # 3. settings.json and settings_overlay.json
    for cfg_name in ("settings_overlay.json", "settings.json"):
        member = find_zip_member(bridge_zf, f"source/existing_ui_sources/{cfg_name}")
        if not member:
            continue
        data = read_json_from_zip(bridge_zf, member)
        pairs: List[Tuple[str, str]] = []
        _collect_settings_pairs(data, pairs)
        src_tag = Path(cfg_name).stem
        for src, ko in pairs:
            register_ui(src, ko, src_tag)

    # 4. runtime_common_dictionary.json and runtime_quest_dictionary.json
    #    -> api_dict.json, names.json, and fallback ui.json
    api_dict: Dict[str, Dict[str, Dict[str, str]]] = {}

    for dict_file in ("runtime_common_dictionary.json", "runtime_quest_dictionary.json"):
        member = find_zip_member(bridge_zf, f"source/existing_ui_sources/{dict_file}")
        if not member:
            continue
        source_name = Path(dict_file).stem
        data = read_json_from_zip(bridge_zf, member)
        items = data if isinstance(data, list) else (data.get("entries", []) if isinstance(data, dict) else [])
        for item in items:
            if not isinstance(item, dict):
                continue
            src = _extract_string(item, "source_jp", "source")
            ko = _extract_string(item, "translation_ko", "translation")
            if not src or not ko:
                continue

            register_ui(src, ko, source_name)

            endpoint = _extract_string(item, "endpoint") or "*"
            path_pat = _extract_string(item, "json_path_pattern", "path_pattern", "path") or "*"
            ep_map = api_dict.setdefault(endpoint, {})
            pat_map = ep_map.setdefault(path_pat, {})
            if src not in pat_map:
                pat_map[src] = ko

            if dict_file == "runtime_common_dictionary.json":
                low_pat = path_pat.lower()
                if any(low_pat.endswith(suf) for suf in NAME_PATH_SUFFIXES):
                    if src not in names_map:
                        names_map[src] = ko

    # Finalize sorted ui.json
    ui_dict: Dict[str, str] = {
        k: ui_best[k][2] for k in sorted(ui_best.keys())
    }

    # 5. audit/approved_source_collisions.jsonl -> ui_scoped.json
    ui_scoped: Dict[str, List[Dict[str, Any]]] = {}
    collisions_member = find_zip_member(bridge_zf, "audit/approved_source_collisions.jsonl")
    if collisions_member:
        for rec in iter_jsonl_from_zip(bridge_zf, collisions_member):
            src = _extract_string(rec, "source_jp", "source")
            if not src:
                continue
            raw_cands = rec.get("translation_candidates")
            if not isinstance(raw_cands, list):
                continue
            variants: List[Dict[str, Any]] = []
            for cand in raw_cands:
                if not isinstance(cand, dict):
                    continue
                if not _is_approved_candidate(cand):
                    continue
                ko = _extract_string(cand, "translation_ko", "translation")
                if not ko:
                    continue
                variant: Dict[str, Any] = {
                    "translation_ko": ko,
                    "source": _extract_string(cand, "source"),
                    "variant_role": _extract_string(cand, "variant_role") or "primary",
                }
                ctx = cand.get("context")
                if isinstance(ctx, dict):
                    variant["context"] = ctx
                variants.append(variant)
            if variants:
                variants.sort(
                    key=lambda v: (
                        source_priority(v.get("source", "")),
                        v.get("variant_role", ""),
                        v.get("translation_ko", ""),
                    )
                )
                ui_scoped[src] = variants

    ui_scoped_sorted = {k: ui_scoped[k] for k in sorted(ui_scoped.keys())}
    names_sorted = {k: names_map[k] for k in sorted(names_map.keys())}

    api_dict_sorted: Dict[str, Dict[str, Dict[str, str]]] = {}
    for ep in sorted(api_dict.keys()):
        api_dict_sorted[ep] = {}
        for pat in sorted(api_dict[ep].keys()):
            api_dict_sorted[ep][pat] = {
                k: api_dict[ep][pat][k] for k in sorted(api_dict[ep][pat].keys())
            }

    # 6. runtime_api_runtime_templates.json -> api_templates.json
    templates_member = find_zip_member(
        bridge_zf, "source/existing_ui_sources/runtime_api_runtime_templates.json"
    )
    api_templates: Any = []
    if templates_member:
        api_templates = read_json_from_zip(bridge_zf, templates_member)

    return (
        ui_dict,
        ui_scoped_sorted,
        names_sorted,
        api_dict_sorted,
        api_templates,
    )


def verify_output(out_dir: Path) -> Dict[str, Any]:
    errors: List[str] = []

    # 1. Forbidden file extensions check
    forbidden_found: List[str] = []
    for root, _, files in os.walk(out_dir):
        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext in FORBIDDEN_EXTENSIONS:
                forbidden_found.append(str(Path(root) / fname))
    if forbidden_found:
        errors.append(f"Forbidden file extensions found: {forbidden_found}")

    # 2. Check manifest.json
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.is_file():
        errors.append("manifest.json is missing")
        return {"ok": False, "errors": errors}

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Verify core files
    core = manifest.get("core", {})
    for name, meta in core.items():
        rel = meta.get("path", "")
        fpath = out_dir / Path(rel)
        if not fpath.is_file():
            errors.append(f"Missing core file: {rel}")
            continue
        actual_sha, actual_bytes = sha256_file(fpath)
        if actual_sha != meta.get("sha256"):
            errors.append(f"SHA-256 mismatch for core.{name} ({rel})")
        if actual_bytes != meta.get("bytes"):
            errors.append(f"Byte size mismatch for core.{name} ({rel})")

    # Verify images.json
    images_meta = manifest.get("images", {})
    if isinstance(images_meta, dict):
        img_rel = images_meta.get("path", "translations/ko/images.json")
        img_path = out_dir / Path(img_rel)
        if not img_path.is_file():
            errors.append(f"Missing images file: {img_rel}")
        else:
            actual_sha, actual_bytes = sha256_file(img_path)
            if actual_sha != images_meta.get("sha256"):
                errors.append(f"SHA-256 mismatch for images ({img_rel})")
            if "bytes" in images_meta and actual_bytes != images_meta.get("bytes"):
                errors.append(f"Byte size mismatch for images ({img_rel})")

    # 3. Verify scenario_index.json and all scenario files
    idx_path = out_dir / "translations" / "ko" / "scenario_index.json"
    if not idx_path.is_file():
        errors.append("scenario_index.json is missing")
        return {"ok": False, "errors": errors}

    scenario_index = json.loads(idx_path.read_text(encoding="utf-8"))
    total_fragments = 0
    source_jp_leaks = 0

    for script_name, meta in scenario_index.items():
        rel = meta.get("path", "")
        fpath = out_dir / Path(rel)
        if not fpath.is_file():
            errors.append(f"Missing scenario file: {rel}")
            continue
        actual_sha, actual_bytes = sha256_file(fpath)
        if actual_sha != meta.get("sha256"):
            errors.append(f"SHA-256 mismatch for scenario {script_name} ({rel})")
        if actual_bytes != meta.get("bytes"):
            errors.append(f"Byte size mismatch for scenario {script_name} ({rel})")

        doc = json.loads(fpath.read_text(encoding="utf-8"))
        frags = doc.get("fragments", {})
        if not isinstance(frags, dict):
            errors.append(f"Invalid fragments object in {rel}")
            continue
        if len(frags) != meta.get("count"):
            errors.append(f"Fragment count mismatch in {rel}: {len(frags)} != {meta.get('count')}")
        total_fragments += len(frags)
        for frag in frags.values():
            if isinstance(frag, dict) and "source_jp" in frag:
                source_jp_leaks += 1

    if source_jp_leaks > 0:
        errors.append(f"Found {source_jp_leaks} source_jp keys inside scenarios/")

    if total_fragments != EXPECTED_SCENARIO_FRAGMENTS:
        errors.append(
            f"Total scenario fragments mismatch: {total_fragments} != {EXPECTED_SCENARIO_FRAGMENTS}"
        )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "checked_scenarios": len(scenario_index),
        "total_scenario_fragments": total_fragments,
        "forbidden_files": len(forbidden_found),
        "source_jp_leaks": source_jp_leaks,
        "stats": manifest.get("stats", {}),
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Lilianze (lilyange) gctrans-inspired CDN repository."
    )
    parser.add_argument("--frida-zip", default=DEFAULT_FRIDA_ZIP, help="Path to FRIDA runtime registry zip.")
    parser.add_argument("--bridge-zip", default=DEFAULT_BRIDGE_ZIP, help="Path to UI public patch bridge zip.")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="Output CDN repository directory.")
    parser.add_argument(
        "--verify",
        dest="verify",
        action="store_true",
        default=True,
        help="Run built-in verification after build (default: enabled).",
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Skip built-in verification.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    frida_zip_path = Path(args.frida_zip)
    bridge_zip_path = Path(args.bridge_zip)
    out_dir = Path(args.out_dir)

    if not frida_zip_path.is_file():
        print(f"ERROR: --frida-zip not found: {frida_zip_path}", file=sys.stderr)
        return 1
    if not bridge_zip_path.is_file():
        print(f"ERROR: --bridge-zip not found: {bridge_zip_path}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    ko_dir = out_dir / "translations" / "ko"
    ko_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(frida_zip_path, "r") as frida_zf:
        registry_freeze_id = extract_registry_freeze_id(frida_zf, frida_zip_path)
        scenario_index, script_count, total_fragments = build_scenarios(frida_zf, out_dir)

    with zipfile.ZipFile(bridge_zip_path, "r") as bridge_zf:
        (
            ui_dict,
            ui_scoped_dict,
            names_dict,
            api_dict,
            api_templates,
        ) = build_ui_and_dictionaries(bridge_zf)

    # Merge clean-room independent static UI translations (never overwriting approved entries)
    extra_static_path = ko_dir / "ui_static_ko.json"
    extra_static_count = 0
    if extra_static_path.is_file():
        extra_static = json.loads(extra_static_path.read_text(encoding="utf-8"))
        if isinstance(extra_static, dict):
            for k, v in extra_static.items():
                if k and isinstance(v, str) and v.strip() and k not in ui_dict:
                    ui_dict[k] = v.strip()
                    extra_static_count += 1
            ui_dict = {k: ui_dict[k] for k in sorted(ui_dict.keys())}

    core_specs: List[Tuple[str, str, Any, bool]] = [
        ("scenario_index", "translations/ko/scenario_index.json", scenario_index, True),
        ("ui", "translations/ko/ui.json", ui_dict, False),
        ("ui_scoped", "translations/ko/ui_scoped.json", ui_scoped_dict, False),
        ("names", "translations/ko/names.json", names_dict, False),
        ("api_dict", "translations/ko/api_dict.json", api_dict, False),
        ("api_templates", "translations/ko/api_templates.json", api_templates, False),
    ]

    core_manifest: Dict[str, Dict[str, Any]] = {}
    for logical_name, rel_path, payload, is_compact in core_specs:
        digest, size = write_json_file(out_dir / Path(rel_path), payload, compact=is_compact)
        core_manifest[logical_name] = {
            "path": rel_path,
            "sha256": digest,
            "bytes": size,
        }

    images_rel = "translations/ko/images.json"
    images_doc = {"schema": 1, "images": {}}
    img_sha, img_bytes = write_json_file(out_dir / Path(images_rel), images_doc, compact=False)

    font_meta: Any = None
    font_rel = "fonts/NotoSansKR-Regular.ttf"
    font_file = out_dir / Path(font_rel)
    if font_file.is_file():
        font_bytes = font_file.read_bytes()
        font_meta = {
            "path": font_rel,
            "sha256": hashlib.sha256(font_bytes).hexdigest(),
            "bytes": len(font_bytes),
            "license": "OFL-1.1",
        }

    api_dict_entries = sum(
        len(entries)
        for pat_map in api_dict.values()
        for entries in pat_map.values()
    )

    manifest = {
        "schema": 1,
        "game": "lilyange",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "registry_freeze_id": registry_freeze_id,
        "stats": {
            "scenario_scripts": script_count,
            "scenario_fragments": total_fragments,
            "ui_entries": len(ui_dict),
            "ui_extra_static_merged": extra_static_count,
            "ui_scoped_entries": len(ui_scoped_dict),
            "names_entries": len(names_dict),
            "api_dict_endpoints": len(api_dict),
            "api_dict_entries": api_dict_entries,
            "api_templates_entries": len(api_templates) if isinstance(api_templates, list) else 0,
        },
        "core": core_manifest,
        "images": {
            "path": images_rel,
            "sha256": img_sha,
            "bytes": img_bytes,
        },
        "font": font_meta,
    }

    write_json_file(out_dir / "manifest.json", manifest, compact=False)

    if args.verify:
        result = verify_output(out_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2

    print(json.dumps({"ok": True, "verified": False, "stats": manifest["stats"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
