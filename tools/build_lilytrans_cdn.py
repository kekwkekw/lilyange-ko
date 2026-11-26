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
DEFAULT_FINAL_R2_ZIP = r"D:\Downloads\LILYANGE_DELETE_SAFE_FRIDA_TRANSFER_20261115_R2.zip"
DEFAULT_CRAWLER_EXTRACTED_DIR = r"D:\lily_workspace\crawler_work\extracted"
DEFAULT_R2_HANDOFF_DIR = r"D:\lily_workspace\handoff_r2_20261126\FRIDA_AG_INTEGRATED_TRANSLATION_HANDOFF_20261126_R2"
DEFAULT_OUT_DIR = r"D:\lily_workspace\lilytrans"

FORBIDDEN_EXTENSIONS = {".unityweb", ".bundle", ".assets", ".apk", ".so", ".dll"}
MIN_EXPECTED_SCENARIO_FRAGMENTS = 86123

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


def _load_live_extracted_commands(
    extracted_dir: Path,
) -> Tuple[Dict[Tuple[str, int, int], Dict[str, Any]], Dict[Tuple[str, int, int], Dict[str, Any]]]:
    pt_map: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    ch_map: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    if not extracted_dir.is_dir():
        return pt_map, ch_map
    for p in extracted_dir.rglob("naninovel.jsonl"):
        if "apk_builtin" in p.parts:
            continue
        for raw_line in p.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            rec = json.loads(raw_line)
            cmd = rec.get("command_class")
            if cmd not in ("PrintText", "AddChoice"):
                continue
            pb = rec.get("playback_spot") or {}
            sc = str(pb.get("scriptName") or rec.get("script_m_name") or "").strip()
            if not sc:
                continue
            li = int(pb.get("lineIndex", -1))
            ii = int(pb.get("inlineIndex", 0))
            if cmd == "PrintText":
                pt_map[(sc, li, ii)] = rec
            else:
                ch_map[(sc, li, ii)] = rec
    return pt_map, ch_map


def build_scenarios(
    frida_zf: zipfile.ZipFile,
    out_dir: Path,
    *,
    final_r2_zip: Optional[Path] = None,
    crawler_extracted_dir: Optional[Path] = None,
    r2_handoff_dir: Optional[Path] = None,
    include_review_required: bool = True,
) -> Tuple[Dict[str, Dict[str, Any]], int, int, Dict[str, int]]:
    """
    Build translations/ko/scenarios/<group>/<scriptName>.json and scenario_index.json.
    Follows STAGE50_1_BASELINE_PREFERRED_WITH_DETERMINISTIC_FALLBACK:
    - Preserves all 86,123 Stage50.1 fragments (including B18 semantic coalesced splits).
    - Promotes additional approved non-HOLD fragments from FINAL 88,868 verified against
      live catalog extraction + logical master lineHash.
    - Applies deterministic zero-retranslation runtime alignment (CJK compat-kanji guard
      hash & shifted lineIndex remap where (lineHash, guard) uniquely match live bundles).
    - Optionally merges verified R2 incremental handoff scenarios (01a/01b/02_addchoice)
      without ever overwriting existing Stage50.1/FINAL 88,868 entries, normalizing
      {G_PlayerName} -> {PLAYER_NAME} for runtime Mode 1 compatibility.
    """
    # scriptName -> dict of (lineIndex, inlineIndex) -> payload
    scripts: Dict[str, Dict[Tuple[int, int], Dict[str, Any]]] = defaultdict(dict)
    promo_stats: Dict[str, int] = {
        "stage50_1_base": 0,
        "final_new_promoted": 0,
        "compat_kanji_guard_aligned": 0,
        "shifted_keys_remapped": 0,
        "r2_01a_promoted": 0,
        "r2_01b_promoted": 0,
        "r2_review_required_included": 0,
        "r2_choices_promoted": 0,
        "r2_skipped_existing": 0,
        "r2_excluded_skipped": 0,
        "r2_player_token_normalized": 0,
    }

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
        scripts[script_name][(line_idx, inline_idx)] = payload
        promo_stats["stage50_1_base"] += 1

    ext_map: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    ext_choice_map: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    if crawler_extracted_dir and crawler_extracted_dir.is_dir():
        ext_map, ext_choice_map = _load_live_extracted_commands(crawler_extracted_dir)

    # Promote approved FINAL 88,868 delta fragments (zero translation edits)
    if final_r2_zip and final_r2_zip.is_file() and ext_map:
        with zipfile.ZipFile(final_r2_zip, "r") as r2_zf:
            frag_member = find_zip_member(r2_zf, "FRIDA_FINAL_RUNTIME_FRAGMENT_REGISTRY_88868.jsonl")
            logical_member = find_zip_member(r2_zf, "FRIDA_FINAL_RUNTIME_LOGICAL_MASTER_85125.jsonl")
            if frag_member and logical_member:
                logical_lh: Dict[str, str] = {}
                for lrec in iter_jsonl_from_zip(r2_zf, logical_member):
                    lk = str(lrec.get("key", "")).strip()
                    if lk:
                        logical_lh[lk] = str(lrec.get("lineHash", ""))

                for frec in iter_jsonl_from_zip(r2_zf, frag_member):
                    if frec.get("hold") is True:
                        continue
                    sc = str(frec.get("scriptName", "")).strip()
                    if not sc:
                        continue
                    li = int(frec.get("lineIndex", 0))
                    ii = int(frec.get("inlineIndex", 0))
                    if (li, ii) in scripts[sc]:
                        continue  # Stage50.1 baseline preferred
                    ex = ext_map.get((sc, li, ii))
                    if ex is None:
                        raise RuntimeError(f"FINAL fragment {(sc, li, ii)} missing in live crawler extraction")
                    live_lh = str(ex.get("line_hash", ""))
                    lk = str(frec.get("logicalKey", ""))
                    expected_lh = logical_lh.get(lk, "")
                    if expected_lh and live_lh and expected_lh != live_lh:
                        raise RuntimeError(
                            f"FINAL fragment {(sc, li, ii)} lineHash mismatch: logical={expected_lh} live={live_lh}"
                        )
                    t_ko = str(frec.get("translation_ko", ""))
                    g_tpl = str(ex.get("source_guard_sha256_player_template") or "")
                    g_exact = str(ex.get("source_guard_sha256_exact") or "")
                    if g_tpl:
                        mode_int = 1
                        guard_sha = g_tpl
                    elif g_exact:
                        mode_int = 0
                        guard_sha = g_exact
                    else:
                        raise RuntimeError(f"FINAL fragment {(sc, li, ii)} has empty live guard hash")

                    scripts[sc][(li, ii)] = {
                        "lh": live_lh or expected_lh,
                        "m": mode_int,
                        "g": guard_sha,
                        "t": t_ko,
                    }
                    promo_stats["final_new_promoted"] += 1

    # Deterministic zero-retranslation runtime alignment against live extracted bundles
    if ext_map:
        ext_by_sc: Dict[str, Dict[Tuple[int, int], Dict[str, Any]]] = defaultdict(dict)
        for (sc, li, ii), rec in ext_map.items():
            ext_by_sc[sc][(li, ii)] = rec

        for sc, frag_map in list(scripts.items()):
            live_sc = ext_by_sc.get(sc)
            if not live_sc:
                continue

            # 1) CJK compatibility ideograph 海(U+FA45) vs 海(U+6D77) guard hash alignment
            for (li, ii), payload in frag_map.items():
                ex = live_sc.get((li, ii))
                if ex is None or payload["m"] != 0:
                    continue
                if str(ex.get("line_hash", "")) != payload["lh"]:
                    continue
                ge = str(ex.get("source_guard_sha256_exact") or "")
                if not ge or ge == payload["g"]:
                    continue
                raw_val = str((ex.get("text") or {}).get("value") or "")
                if "\ufa45" in raw_val:
                    norm_sha = sha256_bytes(raw_val.replace("\ufa45", "\u6d77").encode("utf-8"))
                    if norm_sha == payload["g"]:
                        payload["g"] = ge
                        promo_stats["compat_kanji_guard_aligned"] += 1

            # 2) Shifted lineIndex remap (only for entries whose current key fails live match)
            def _matches_live(key_pair: Tuple[int, int], p: Dict[str, Any]) -> bool:
                ex = live_sc.get(key_pair)
                if ex is None or str(ex.get("line_hash", "")) != p["lh"]:
                    return False
                expected_g = str(
                    ex.get("source_guard_sha256_player_template") if p["m"] == 1 else ex.get("source_guard_sha256_exact")
                ) or ""
                return expected_g == p["g"]

            broken_keys = [k for k, p in frag_map.items() if not _matches_live(k, p)]
            if broken_keys:
                live_lookup: Dict[Tuple[str, int, str], List[Tuple[int, int]]] = defaultdict(list)
                for lk_pair, ex in live_sc.items():
                    lh = str(ex.get("line_hash", ""))
                    ge = str(ex.get("source_guard_sha256_exact") or "")
                    gt = str(ex.get("source_guard_sha256_player_template") or "")
                    if ge:
                        live_lookup[(lh, 0, ge)].append(lk_pair)
                    if gt:
                        live_lookup[(lh, 1, gt)].append(lk_pair)

                remapped: Dict[Tuple[int, int], Dict[str, Any]] = {
                    k: p for k, p in frag_map.items() if k not in set(broken_keys)
                }
                for old_k in sorted(broken_keys):
                    p = frag_map[old_k]
                    cands = [
                        ck for ck in live_lookup.get((p["lh"], p["m"], p["g"]), [])
                        if ck not in remapped
                    ]
                    if len(cands) == 1:
                        remapped[cands[0]] = p
                        promo_stats["shifted_keys_remapped"] += 1
                    else:
                        remapped[old_k] = p
                scripts[sc] = remapped

    # Promote R2 incremental handoff scenarios (01a / 01b / 02_addchoice)
    # Never overwrites existing Stage50.1 / FINAL 88,868 entries.
    if r2_handoff_dir and r2_handoff_dir.is_dir() and ext_map:
        r2_specs = [
            ("01a", r2_handoff_dir / "02_integrated_stage_b" / "01a_new_scenarios_STAGE_B_INTEGRATED_8140.jsonl", ext_map),
            ("01b", r2_handoff_dir / "02_integrated_stage_b" / "01b_updated_existing_STAGE_B_FROZEN_2935.jsonl", ext_map),
            ("choice", r2_handoff_dir / "02_integrated_stage_b" / "02_addchoice_STAGE_B_FROZEN_0023.jsonl", ext_choice_map),
        ]
        for scope, fpath, live_lookup_map in r2_specs:
            if not fpath.is_file():
                continue
            for raw_line in fpath.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                rec = json.loads(raw_line)
                st = str(rec.get("review_status", "")).strip()
                if st.startswith("EXCLUDED"):
                    promo_stats["r2_excluded_skipped"] += 1
                    continue
                if st == "REVIEW_REQUIRED" and not include_review_required:
                    continue
                if st not in {"READY_FOR_EXPLICIT_ACCEPTANCE", "REVIEWED_REUSE_READY", "REVIEW_REQUIRED"}:
                    raise RuntimeError(f"Unexpected R2 review_status {st!r} in {fpath.name}")

                sc = str(rec.get("scriptName", "")).strip()
                li = int(rec.get("lineIndex", 0))
                ii = int(rec.get("inlineIndex", 0))
                if not sc:
                    continue
                if (li, ii) in scripts[sc]:
                    promo_stats["r2_skipped_existing"] += 1
                    continue

                ex = live_lookup_map.get((sc, li, ii))
                if ex is None:
                    raise RuntimeError(f"R2 {scope} fragment {(sc, li, ii)} missing in live crawler extraction")
                live_lh = str(ex.get("line_hash", ""))
                rec_lh = str(rec.get("lineHash", ""))
                if rec_lh and live_lh and rec_lh != live_lh:
                    raise RuntimeError(
                        f"R2 {scope} fragment {(sc, li, ii)} lineHash mismatch: rec={rec_lh} live={live_lh}"
                    )

                t_ko = str(rec.get("translation_ko") or "")
                if not t_ko.strip():
                    raise RuntimeError(f"R2 {scope} fragment {(sc, li, ii)} has empty translation_ko")

                g_tpl = str(ex.get("source_guard_sha256_player_template") or "")
                g_exact = str(ex.get("source_guard_sha256_exact") or "")
                if g_tpl:
                    mode_int = 1
                    guard_sha = g_tpl
                    if "{G_PlayerName}" in t_ko:
                        t_ko = t_ko.replace("{G_PlayerName}", "{PLAYER_NAME}")
                        promo_stats["r2_player_token_normalized"] += 1
                elif g_exact:
                    mode_int = 0
                    guard_sha = g_exact
                else:
                    raise RuntimeError(f"R2 {scope} fragment {(sc, li, ii)} has empty live guard hash")

                if "{G_PlayerName}" in t_ko:
                    raise RuntimeError(f"Unnormalized {{G_PlayerName}} remained in R2 {(sc, li, ii)}")

                scripts[sc][(li, ii)] = {
                    "lh": live_lh or rec_lh,
                    "m": mode_int,
                    "g": guard_sha,
                    "t": t_ko,
                }
                if st == "REVIEW_REQUIRED":
                    promo_stats["r2_review_required_included"] += 1
                elif scope == "01a":
                    promo_stats["r2_01a_promoted"] += 1
                elif scope == "01b":
                    promo_stats["r2_01b_promoted"] += 1
                elif scope == "choice":
                    promo_stats["r2_choices_promoted"] += 1

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

        entries = sorted(scripts[script_name].items(), key=lambda x: (x[0][0], x[0][1]))

        fragments_map: Dict[str, Dict[str, Any]] = {}
        for (line_idx, inline_idx), payload in entries:
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

    return scenario_index, len(scenario_index), total_fragments, promo_stats


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

    expected_in_manifest = manifest.get("stats", {}).get("scenario_fragments", MIN_EXPECTED_SCENARIO_FRAGMENTS)
    if total_fragments < MIN_EXPECTED_SCENARIO_FRAGMENTS or total_fragments != expected_in_manifest:
        errors.append(
            f"Total scenario fragments mismatch: {total_fragments} != manifest {expected_in_manifest} (min {MIN_EXPECTED_SCENARIO_FRAGMENTS})"
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
    parser.add_argument("--frida-zip", default=DEFAULT_FRIDA_ZIP, help="Path to FRIDA Stage50.1 runtime registry zip.")
    parser.add_argument("--bridge-zip", default=DEFAULT_BRIDGE_ZIP, help="Path to UI public patch bridge zip.")
    parser.add_argument("--final-r2-zip", default=DEFAULT_FINAL_R2_ZIP, help="Optional FRIDA FINAL 88,868 transfer R2 zip.")
    parser.add_argument("--crawler-extracted-dir", default=DEFAULT_CRAWLER_EXTRACTED_DIR, help="Optional live crawler extracted dir for guard/key verification.")
    parser.add_argument("--r2-handoff-dir", default=DEFAULT_R2_HANDOFF_DIR, help="Optional FRIDA AG integrated translation handoff R2 directory.")
    parser.add_argument(
        "--include-review-required",
        dest="include_review_required",
        action="store_true",
        default=True,
        help="Include REVIEW_REQUIRED (e.g. preserved-Kanji) rows in CDN runtime (default: enabled).",
    )
    parser.add_argument(
        "--no-include-review-required",
        dest="include_review_required",
        action="store_false",
        help="Exclude REVIEW_REQUIRED rows from CDN runtime.",
    )
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

    final_r2_path = Path(args.final_r2_zip) if args.final_r2_zip else None
    crawler_ext_path = Path(args.crawler_extracted_dir) if args.crawler_extracted_dir else None
    r2_handoff_path = Path(args.r2_handoff_dir) if args.r2_handoff_dir else None

    with zipfile.ZipFile(frida_zip_path, "r") as frida_zf:
        registry_freeze_id = extract_registry_freeze_id(frida_zf, frida_zip_path)
        scenario_index, script_count, total_fragments, promo_stats = build_scenarios(
            frida_zf,
            out_dir,
            final_r2_zip=final_r2_path,
            crawler_extracted_dir=crawler_ext_path,
            r2_handoff_dir=r2_handoff_path,
            include_review_required=args.include_review_required,
        )
    if (
        promo_stats.get("r2_01a_promoted", 0)
        + promo_stats.get("r2_01b_promoted", 0)
        + promo_stats.get("r2_choices_promoted", 0)
        > 0
    ):
        registry_freeze_id = "FRIDA_REV2_20261126_FINAL88868_PLUS_R2_INCREMENTAL"
    elif promo_stats.get("final_new_promoted", 0) > 0:
        registry_freeze_id = "FRIDA_FINAL_BINARY_HOLD_MASTER_20261023_STAGE50_1_PREFERRED"

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

    # Merge R2 accepted static UI (03_ui) and Naninovel AddChoice summaries (02_addchoice)
    # into ui.json (never overwriting existing approved UI entries; LilyMod ChoiceHandlerPanel
    # translates choice summaries via LilytransStore.TryTranslateUi).
    r2_ui_merged = 0
    r2_choice_ui_merged = 0
    if r2_handoff_path and r2_handoff_path.is_dir():
        r2_ui_file = r2_handoff_path / "02_integrated_stage_b" / "03_ui_STAGE_B_FROZEN_0012.jsonl"
        if r2_ui_file.is_file():
            for raw_line in r2_ui_file.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                rec = json.loads(raw_line)
                if rec.get("review_status") != "READY_FOR_EXPLICIT_ACCEPTANCE":
                    continue
                src = str(rec.get("text") or "").strip()
                ko = str(rec.get("translation_ko") or "").strip()
                if src and ko and src not in ui_dict:
                    ui_dict[src] = ko
                    r2_ui_merged += 1

        r2_ch_file = r2_handoff_path / "02_integrated_stage_b" / "02_addchoice_STAGE_B_FROZEN_0023.jsonl"
        if r2_ch_file.is_file():
            for raw_line in r2_ch_file.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                rec = json.loads(raw_line)
                if rec.get("review_status") != "READY_FOR_EXPLICIT_ACCEPTANCE":
                    continue
                src = str(rec.get("source_jp") or "").strip()
                ko = str(rec.get("translation_ko") or "").strip()
                if src and ko and src not in ui_dict:
                    ui_dict[src] = ko
                    r2_choice_ui_merged += 1

        if r2_ui_merged or r2_choice_ui_merged:
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
            "scenario_promotion": promo_stats,
            "ui_entries": len(ui_dict),
            "ui_extra_static_merged": extra_static_count,
            "ui_r2_merged": r2_ui_merged,
            "ui_r2_choices_merged": r2_choice_ui_merged,
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
