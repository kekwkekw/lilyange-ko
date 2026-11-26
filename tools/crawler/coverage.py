from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_JAPANESE_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF\uFF66-\uFF9F]")

_UI_CLASS_PREFIXES = (
    "UnityEngine.UI.",
    "TMPro.",
    "UnityEngine.EventSystems.",
    "UnityEngine.Canvas",
    "UnityEngine.RectTransform",
)
_UI_CLASS_NAMES = {
    "Text",
    "TextMeshPro",
    "TextMeshProUGUI",
    "TMP_Text",
    "TMP_FontAsset",
    "TMP_SpriteAsset",
    "Button",
    "Image",
    "RawImage",
    "Canvas",
    "CanvasRenderer",
    "CanvasScaler",
    "GraphicRaycaster",
    "RectTransform",
    "ScrollRect",
    "Scrollbar",
    "Slider",
    "Toggle",
    "ToggleGroup",
    "InputField",
    "TMP_InputField",
    "Dropdown",
    "TMP_Dropdown",
    "HorizontalLayoutGroup",
    "VerticalLayoutGroup",
    "GridLayoutGroup",
    "ContentSizeFitter",
    "AspectRatioFitter",
    "LayoutElement",
    "Mask",
    "RectMask2D",
    "Shadow",
    "Outline",
}
_SPINE_MARKERS = ("Spine.", "SkeletonDataAsset", "SkeletonAnimation", "SkeletonGraphic", "AtlasAsset")
_AUDIO_MARKERS = ("AudioClip", "AudioSource", "AudioMixer", "CriAtom", "CriWare")
_NANINOVEL_SCRIPT_MARKERS = ("Naninovel.Script", "NaninovelScript")
_TABLE_HINT_MARKERS = ("Master", "Table", "Database", "Config", "Catalog", "Param", "Sheet", "DataList", "Registry")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _has_japanese(text: str) -> bool:
    return bool(_JAPANESE_RE.search(text))


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, path)


def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False))
            fh.write("\n")
    os.replace(tmp_path, path)


def _read_json_safe(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _detect_origin(file_path: Path) -> str:
    parts_lower = {p.lower() for p in file_path.parts}
    if "android_apk_builtin" in parts_lower or "apk_builtin" in parts_lower:
        return "apk_builtin"
    return "remote_bundle"


def _discover_extracted_roots(work_dir: Path, platform: str) -> list[Path]:
    extracted_base = work_dir / "extracted"
    roots: list[Path] = []
    if not extracted_base.is_dir():
        return roots

    platform_dir = extracted_base / platform
    if platform_dir.is_dir():
        roots.append(platform_dir)
    else:
        roots.append(extracted_base)

    apk_builtin_top = extracted_base / "android_apk_builtin"
    if apk_builtin_top.is_dir() and apk_builtin_top not in roots:
        if not any(apk_builtin_top.is_relative_to(r) for r in roots):
            roots.append(apk_builtin_top)

    return roots


def _collect_jsonl_files(roots: list[Path], filename: str) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob(filename)):
            resolved = p.resolve()
            if resolved not in seen and p.is_file():
                seen.add(resolved)
                found.append(p)
    return found


def _find_lilytrans_file(lilytrans_dir: Path, *rel_candidates: str) -> Path | None:
    for rel in rel_candidates:
        candidate = lilytrans_dir / rel
        if candidate.is_file():
            return candidate
    return None


def _load_flat_dict(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = _read_json_safe(path)
    if not isinstance(data, dict):
        return {}
    source = data.get("entries") if isinstance(data.get("entries"), dict) else data
    out: dict[str, str] = {}
    for k, v in source.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def _load_api_dict_flat(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    data = _read_json_safe(path)
    if not isinstance(data, dict):
        return {}
    endpoints = data.get("endpoints") if isinstance(data.get("endpoints"), dict) else data
    out: dict[str, str] = {}
    if isinstance(endpoints, dict):
        for ep_val in endpoints.values():
            if not isinstance(ep_val, dict):
                continue
            for path_val in ep_val.values():
                if not isinstance(path_val, dict):
                    continue
                for jp, ko in path_val.items():
                    if isinstance(jp, str) and isinstance(ko, str):
                        out[jp] = ko
    return out


def _load_scoped_dict(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    data = _read_json_safe(path)
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for scope, mapping in data.items():
        if isinstance(scope, str) and isinstance(mapping, dict):
            inner: dict[str, str] = {}
            for k, v in mapping.items():
                if isinstance(k, str) and isinstance(v, str):
                    inner[k] = v
            out[scope] = inner
    return out


def _load_scenario_index(scenarios_dir: Path) -> dict[str, Path]:
    index_candidates = [
        scenarios_dir.parent / "scenario_index.json",
        scenarios_dir / "scenario_index.json",
    ]
    raw = None
    for idx_path in index_candidates:
        if idx_path.is_file():
            raw = _read_json_safe(idx_path)
            if isinstance(raw, dict):
                break
    mapping: dict[str, Path] = {}
    if isinstance(raw, dict):
        entries = raw.get("scripts") if isinstance(raw.get("scripts"), dict) else raw
        if isinstance(entries, dict):
            for script_id, val in entries.items():
                if not isinstance(script_id, str):
                    continue
                rel = val if isinstance(val, str) else (
                    val.get("path") or val.get("file") or val.get("shard") if isinstance(val, dict) else None
                )
                if isinstance(rel, str):
                    p = scenarios_dir.parent.parent.parent / rel if rel.startswith("translations/") else scenarios_dir / rel
                    if not p.is_file():
                        p = scenarios_dir / rel
                    mapping[script_id] = p
    if not mapping and scenarios_dir.is_dir():
        for p in sorted(scenarios_dir.rglob("*.json")):
            if p.name == "scenario_index.json":
                continue
            mapping[p.stem] = p
    return mapping


def _extract_ko_script_entries(shard_data: Any, script_id: str) -> dict[str, dict[str, Any]]:
    if not isinstance(shard_data, dict):
        return {}
    if isinstance(shard_data.get("fragments"), dict):
        return shard_data["fragments"]
    if script_id in shard_data and isinstance(shard_data[script_id], dict):
        candidate = shard_data[script_id]
        if all(isinstance(v, dict) for v in candidate.values()):
            return candidate
    if isinstance(shard_data.get("entries"), dict):
        return shard_data["entries"]
    if isinstance(shard_data.get("lines"), dict):
        return shard_data["lines"]
    result: dict[str, dict[str, Any]] = {}
    for k, v in shard_data.items():
        if isinstance(k, str) and isinstance(v, dict) and ":" in k:
            result[k] = v
    return result


def _is_ui_class(class_name: str) -> bool:
    if not class_name:
        return False
    if class_name in _UI_CLASS_NAMES:
        return True
    return any(class_name.startswith(prefix) for prefix in _UI_CLASS_PREFIXES)


def _is_spine_class(class_name: str) -> bool:
    return any(marker in class_name for marker in _SPINE_MARKERS)


def _is_audio_class(class_name: str) -> bool:
    return any(marker in class_name for marker in _AUDIO_MARKERS)


def _is_naninovel_script_class(class_name: str) -> bool:
    return any(marker in class_name for marker in _NANINOVEL_SCRIPT_MARKERS)


def _looks_like_master_table(class_name: str, asset_name: str) -> bool:
    combined = f"{class_name} {asset_name}"
    return any(hint.lower() in combined.lower() for hint in _TABLE_HINT_MARKERS)


def _inspect_registry_zip(registry_zip: Path | None) -> dict[str, Any] | None:
    if registry_zip is None or not registry_zip.is_file():
        return None
    try:
        with zipfile.ZipFile(registry_zip, "r") as zf:
            names = zf.namelist()
            return {
                "path": str(registry_zip),
                "entry_count": len(names),
                "sample_entries": names[:20],
            }
    except (OSError, zipfile.BadZipFile) as exc:
        return {
            "path": str(registry_zip),
            "error": str(exc),
        }


def _run_scenario_diff(
    naninovel_files: list[Path],
    lilytrans_dir: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    scenarios_dir = lilytrans_dir / "translations" / "ko" / "scenarios"
    ko_index = _load_scenario_index(scenarios_dir)
    ko_script_ids = set(ko_index.keys())

    shard_cache: dict[Path, Any] = {}

    def get_ko_entries(script_id: str) -> dict[str, dict[str, Any]]:
        shard_path = ko_index.get(script_id)
        if shard_path is None:
            return {}
        if shard_path not in shard_cache:
            shard_cache[shard_path] = _read_json_safe(shard_path)
        return _extract_ko_script_entries(shard_cache[shard_path], script_id)

    extracted_script_ids: set[str] = set()
    tallies: Counter[str] = Counter()
    missing_candidates: list[dict[str, Any]] = []
    by_origin: Counter[str] = Counter()

    for nani_path in naninovel_files:
        for rec in _iter_jsonl(nani_path):
            origin = "apk_builtin" if rec.get("source_channel") == "android_apk_builtin" else _detect_origin(nani_path)
            pb = rec.get("playback_spot") if isinstance(rec.get("playback_spot"), dict) else {}
            script_id = str(
                pb.get("scriptName")
                or rec.get("script_m_name")
                or rec.get("script_id")
                or rec.get("scriptId")
                or rec.get("script_name")
                or rec.get("scriptName")
                or ""
            )
            if script_id:
                extracted_script_ids.add(script_id)

            cmd = str(rec.get("command_class") or rec.get("command") or rec.get("command_type") or rec.get("type") or "")
            cmd_norm = cmd.lower()
            is_print = "printtext" in cmd_norm or cmd_norm == "print"
            is_choice = "addchoice" in cmd_norm or cmd_norm == "choice"
            if not (is_print or is_choice):
                continue

            tallies["total_fragments"] += 1
            by_origin[origin] += 1
            if is_choice:
                tallies["choice_total"] += 1

            line_idx = pb.get("lineIndex", rec.get("line_index", rec.get("lineIndex")))
            inline_idx = pb.get("inlineIndex", rec.get("inline_index", rec.get("inlineIndex", 0)))
            key = f"{line_idx}:{inline_idx}"

            line_hash_raw = rec.get("line_hash", rec.get("lineHash"))
            line_hash = "" if line_hash_raw is None else str(line_hash_raw)

            param_obj = rec.get("text") if is_print else (rec.get("choice_summary") or rec.get("text"))
            dyn_val_text = ""
            if isinstance(param_obj, dict):
                dyn_val_text = str(param_obj.get("dynamicValue_ValueText") or "")
                source_text = str(param_obj.get("value") or dyn_val_text or "")
            else:
                source_text_raw = rec.get("source_text", param_obj or rec.get("source", ""))
                source_text = "" if source_text_raw is None else str(source_text_raw)

            guard_exact = str(rec.get("source_guard_sha256_exact") or "")
            if not guard_exact and source_text and not dyn_val_text:
                guard_exact = _sha256_text(source_text)
            guard_exact_lf = _sha256_text(source_text.replace("\r\n", "\n")) if (source_text and not dyn_val_text) else ""

            guard_template = str(rec.get("source_guard_sha256_player_template") or "")

            is_dynamic_unknown = bool(
                (dyn_val_text and not guard_template)
                or rec.get("dynamic_unknown_expr")
                or rec.get("has_dynamic_unknown_expr")
                or rec.get("expression_status") == "dynamic_unknown"
            )
            if is_dynamic_unknown:
                tallies["dynamic_unknown_expr"] += 1

            ko_entries = get_ko_entries(script_id) if script_id else {}
            ko_entry = ko_entries.get(key)

            if ko_entry is None:
                tallies["key_missing_in_ko"] += 1
                missing_candidates.append(
                    {
                        "reason": "key_missing_in_ko",
                        "origin": origin,
                        "script_id": script_id,
                        "key": key,
                        "command": cmd,
                        "line_hash": line_hash,
                        "source_text": source_text,
                        "source_guard_sha256_exact": guard_exact,
                        "source_guard_sha256_player_template": guard_template,
                        "dynamic_unknown_expr": is_dynamic_unknown,
                        "bundle": rec.get("bundle") or rec.get("bundle_name") or nani_path.parent.name,
                        "path_id": str(rec.get("path_id", "")),
                    }
                )
                continue

            if is_choice:
                tallies["choice_in_ko"] += 1

            ko_lh_raw = ko_entry.get("lh")
            ko_lh = "" if ko_lh_raw is None else str(ko_lh_raw)
            if ko_lh and line_hash and ko_lh != line_hash:
                tallies["linehash_mismatch"] += 1
                missing_candidates.append(
                    {
                        "reason": "linehash_mismatch",
                        "origin": origin,
                        "script_id": script_id,
                        "key": key,
                        "command": cmd,
                        "line_hash": line_hash,
                        "ko_line_hash": ko_lh,
                        "source_text": source_text,
                        "bundle": rec.get("bundle") or rec.get("bundle_name") or nani_path.parent.name,
                        "path_id": str(rec.get("path_id", "")),
                    }
                )
                continue

            ko_mode = ko_entry.get("m", 0)
            ko_guard = str(ko_entry.get("g") or "")

            if ko_mode == 1:
                if ko_guard and guard_template and ko_guard == guard_template:
                    tallies["template_guard_match"] += 1
                else:
                    tallies["guard_mismatch"] += 1
                    missing_candidates.append(
                        {
                            "reason": "guard_mismatch",
                            "mode": 1,
                            "origin": origin,
                            "script_id": script_id,
                            "key": key,
                            "command": cmd,
                            "line_hash": line_hash,
                            "ko_guard": ko_guard,
                            "source_guard_sha256_player_template": guard_template,
                            "source_text": source_text,
                            "bundle": rec.get("bundle") or rec.get("bundle_name") or nani_path.parent.name,
                            "path_id": str(rec.get("path_id", "")),
                        }
                    )
            else:
                if ko_guard and (ko_guard == guard_exact or (guard_exact_lf and ko_guard == guard_exact_lf)):
                    tallies["exact_guard_match"] += 1
                else:
                    tallies["guard_mismatch"] += 1
                    missing_candidates.append(
                        {
                            "reason": "guard_mismatch",
                            "mode": 0,
                            "origin": origin,
                            "script_id": script_id,
                            "key": key,
                            "command": cmd,
                            "line_hash": line_hash,
                            "ko_guard": ko_guard,
                            "source_guard_sha256_exact": guard_exact,
                            "source_text": source_text,
                            "bundle": rec.get("bundle") or rec.get("bundle_name") or nani_path.parent.name,
                            "path_id": str(rec.get("path_id", "")),
                        }
                    )

    in_both = sorted(extracted_script_ids & ko_script_ids)
    catalog_only = sorted(extracted_script_ids - ko_script_ids)
    ko_only = sorted(ko_script_ids - extracted_script_ids)

    diff_report: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "scripts": {
            "extracted_count": len(extracted_script_ids),
            "ko_index_count": len(ko_script_ids),
            "in_both_count": len(in_both),
            "catalog_only_count": len(catalog_only),
            "ko_only_count": len(ko_only),
            "in_both": in_both,
            "catalog_only": catalog_only,
            "ko_only": ko_only,
        },
        "fragments": {
            "total_fragments": tallies["total_fragments"],
            "exact_guard_match": tallies["exact_guard_match"],
            "template_guard_match": tallies["template_guard_match"],
            "key_missing_in_ko": tallies["key_missing_in_ko"],
            "linehash_mismatch": tallies["linehash_mismatch"],
            "guard_mismatch": tallies["guard_mismatch"],
            "dynamic_unknown_expr": tallies["dynamic_unknown_expr"],
            "choice_total": tallies["choice_total"],
            "choice_in_ko": tallies["choice_in_ko"],
            "by_origin": dict(by_origin),
        },
        "missing_candidates_count": len(missing_candidates),
    }

    _atomic_write_json(reports_dir / "scenario_diff.json", diff_report)
    _atomic_write_jsonl(reports_dir / "scenario_missing_candidates.jsonl", missing_candidates)
    return diff_report


def _run_ui_diff(
    string_files: list[Path],
    lilytrans_dir: Path,
    reports_dir: Path,
) -> tuple[dict[str, Any], Counter[str]]:
    ui_dict = _load_flat_dict(
        _find_lilytrans_file(lilytrans_dir, "translations/ko/ui.json", "ui.json")
    )
    names_dict = _load_flat_dict(
        _find_lilytrans_file(lilytrans_dir, "translations/ko/names.json", "names.json")
    )
    api_dict = _load_api_dict_flat(
        _find_lilytrans_file(lilytrans_dir, "translations/ko/api_dict.json", "api_dict.json")
    )
    ui_scoped = _load_scoped_dict(
        _find_lilytrans_file(lilytrans_dir, "translations/ko/ui_scoped.json", "ui_scoped.json")
    )

    combined_ui_names: dict[str, str] = {}
    combined_ui_names.update(names_dict)
    combined_ui_names.update(ui_dict)

    candidate_categories = {
        "ui_candidate",
        "structured_data_candidate",
        "textasset_candidate",
    }

    all_strings_by_origin: dict[str, set[str]] = {
        "remote_bundle": set(),
        "apk_builtin": set(),
    }
    category_counts: Counter[str] = Counter()
    dedup: dict[str, dict[str, Any]] = {}

    for spath in string_files:
        for rec in _iter_jsonl(spath):
            origin = "apk_builtin" if rec.get("source_channel") == "android_apk_builtin" else _detect_origin(spath)
            text_raw = rec.get("text", rec.get("value", rec.get("source_text")))
            if not isinstance(text_raw, str) or text_raw == "":
                continue
            category = str(rec.get("classification") or rec.get("category") or rec.get("kind") or "uncategorized")
            category_counts[category] += 1
            all_strings_by_origin[origin].add(text_raw)

            if category not in candidate_categories:
                continue

            entry = dedup.get(text_raw)
            if entry is None:
                entry = {
                    "text": text_raw,
                    "has_japanese": _has_japanese(text_raw),
                    "occurrences": 0,
                    "categories": Counter(),
                    "origins": Counter(),
                    "sample_provenance": [],
                }
                dedup[text_raw] = entry

            entry["occurrences"] += 1
            entry["categories"][category] += 1
            entry["origins"][origin] += 1
            if len(entry["sample_provenance"]) < 5:
                entry["sample_provenance"].append(
                    {
                        "origin": origin,
                        "category": category,
                        "bundle": rec.get("bundle") or rec.get("bundle_name") or spath.parent.name,
                        "asset_name": rec.get("asset_name") or rec.get("name") or "",
                        "path_id": str(rec.get("path_id", "")),
                        "component_type": rec.get("component_type") or rec.get("class_name") or "",
                        "field_path": rec.get("field_path") or "",
                    }
                )

    classification_tallies: Counter[str] = Counter()
    missing_records: list[dict[str, Any]] = []

    for text, entry in dedup.items():
        has_jp = entry["has_japanese"]
        if text in combined_ui_names:
            translated = combined_ui_names[text]
            if translated and translated != text:
                status = "present"
            else:
                status = "source_equal"
            classification_tallies[status] += 1
        else:
            status = "missing_japanese" if has_jp else "missing_non_japanese"
            classification_tallies["missing"] += 1
            classification_tallies[status] += 1
            missing_records.append(
                {
                    "text": text,
                    "has_japanese": has_jp,
                    "occurrences": entry["occurrences"],
                    "categories": dict(entry["categories"]),
                    "origins": dict(entry["origins"]),
                    "sample_provenance": entry["sample_provenance"],
                }
            )

    def _check_dict_presence(source_dict: dict[str, str]) -> dict[str, int]:
        remote_set = all_strings_by_origin["remote_bundle"]
        apk_set = all_strings_by_origin["apk_builtin"]
        in_remote_only = 0
        in_apk_only = 0
        in_both = 0
        not_found_statically = 0
        for key in source_dict:
            in_r = key in remote_set
            in_a = key in apk_set
            if in_r and in_a:
                in_both += 1
            elif in_r:
                in_remote_only += 1
            elif in_a:
                in_apk_only += 1
            else:
                not_found_statically += 1
        return {
            "total_unique_keys": len(source_dict),
            "found_in_remote_only": in_remote_only,
            "found_in_apk_builtin_only": in_apk_only,
            "found_in_both_remote_and_apk": in_both,
            "not_found_statically": not_found_statically,
        }

    scoped_unique_keys: set[str] = set()
    for mapping in ui_scoped.values():
        scoped_unique_keys.update(mapping.keys())
    scoped_presence = _check_dict_presence({k: "" for k in scoped_unique_keys})

    ui_report: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "extracted_candidates": {
            "unique_total": len(dedup),
            "present": classification_tallies["present"],
            "source_equal": classification_tallies["source_equal"],
            "missing_total": classification_tallies["missing"],
            "missing_has_japanese_true": classification_tallies["missing_japanese"],
            "missing_has_japanese_false": classification_tallies["missing_non_japanese"],
        },
        "dictionary_static_presence": {
            "api_dict": _check_dict_presence(api_dict),
            "ui_json": _check_dict_presence(ui_dict),
            "names_json": _check_dict_presence(names_dict),
            "ui_scoped": {
                "scope_count": len(ui_scoped),
                **scoped_presence,
            },
        },
        "missing_candidates_count": len(missing_records),
    }

    _atomic_write_json(reports_dir / "ui_coverage.json", ui_report)
    _atomic_write_jsonl(reports_dir / "ui_missing_candidates.jsonl", missing_records)
    return ui_report, category_counts


def _run_master_discovery(
    object_files: list[Path],
    reports_dir: Path,
) -> tuple[dict[str, Any], int, Counter[str]]:
    total_objects = 0
    object_type_counts: Counter[str] = Counter()
    class_summary: dict[tuple[str, str, str], dict[str, Any]] = {}
    master_like_remote = 0
    master_like_apk = 0

    for opath in object_files:
        origin = _detect_origin(opath)
        for rec in _iter_jsonl(opath):
            total_objects += 1
            unity_type = str(rec.get("type") or rec.get("unity_type") or rec.get("class_name") or "Unknown")
            object_type_counts[unity_type] += 1

            if unity_type not in {"TextAsset", "MonoBehaviour"}:
                continue

            script_class = str(
                rec.get("script_class")
                or rec.get("mono_script_class")
                or rec.get("class_name")
                or unity_type
            )
            asset_name = str(rec.get("name") or rec.get("asset_name") or "")

            if (
                _is_ui_class(script_class)
                or _is_spine_class(script_class)
                or _is_audio_class(script_class)
                or _is_naninovel_script_class(script_class)
            ):
                continue

            is_master_like = _looks_like_master_table(script_class, asset_name)
            if is_master_like:
                if origin == "apk_builtin":
                    master_like_apk += 1
                else:
                    master_like_remote += 1

            key = (origin, unity_type, script_class)
            bucket = class_summary.get(key)
            if bucket is None:
                bucket = {
                    "origin": origin,
                    "unity_type": unity_type,
                    "script_class": script_class,
                    "count": 0,
                    "master_like_hint": is_master_like,
                    "sample_bundles": [],
                    "sample_assets": [],
                }
                class_summary[key] = bucket

            bucket["count"] += 1
            if is_master_like:
                bucket["master_like_hint"] = True
            bundle_name = str(rec.get("bundle") or rec.get("bundle_name") or opath.parent.name)
            if bundle_name and bundle_name not in bucket["sample_bundles"] and len(bucket["sample_bundles"]) < 5:
                bucket["sample_bundles"].append(bundle_name)
            if asset_name and asset_name not in bucket["sample_assets"] and len(bucket["sample_assets"]) < 5:
                bucket["sample_assets"].append(asset_name)

    entries = sorted(class_summary.values(), key=lambda x: (-x["count"], x["origin"], x["script_class"]))
    master_report: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "master_data_tables_detected_remote": master_like_remote > 0,
        "master_data_tables_detected_apk_builtin": master_like_apk > 0,
        "master_like_object_count_remote": master_like_remote,
        "master_like_object_count_apk_builtin": master_like_apk,
        "non_ui_non_spine_non_audio_class_count": len(entries),
        "classes": entries,
    }
    _atomic_write_json(reports_dir / "master_discovery.json", master_report)
    return master_report, total_objects, object_type_counts


def run_coverage_diff(
    work_dir: str | Path,
    platform: str,
    lilytrans_dir: str | Path,
    registry_zip: str | Path | None = None,
) -> dict[str, Any]:
    work_path = Path(work_dir)
    lilytrans_path = Path(lilytrans_dir)
    registry_path = Path(registry_zip) if registry_zip is not None else None
    reports_dir = work_path / "reports" / platform
    reports_dir.mkdir(parents=True, exist_ok=True)

    extracted_roots = _discover_extracted_roots(work_path, platform)
    naninovel_files = _collect_jsonl_files(extracted_roots, "naninovel.jsonl")
    string_files = _collect_jsonl_files(extracted_roots, "strings.jsonl")
    object_files = _collect_jsonl_files(extracted_roots, "objects.jsonl")
    failure_files = _collect_jsonl_files(extracted_roots, "failures.jsonl")

    bundle_dirs_remote: set[Path] = set()
    bundle_dirs_apk: set[Path] = set()
    for p in naninovel_files + string_files + object_files + failure_files:
        if _detect_origin(p) == "apk_builtin":
            bundle_dirs_apk.add(p.parent)
        else:
            bundle_dirs_remote.add(p.parent)

    failure_count = 0
    failure_by_type: Counter[str] = Counter()
    for fpath in failure_files:
        for rec in _iter_jsonl(fpath):
            failure_count += 1
            err_type = str(rec.get("error_type") or rec.get("stage") or "unknown")
            failure_by_type[err_type] += 1

    scenario_diff = _run_scenario_diff(naninovel_files, lilytrans_path, reports_dir)
    ui_diff, string_category_counts = _run_ui_diff(string_files, lilytrans_path, reports_dir)
    master_discovery, total_objects, object_type_counts = _run_master_discovery(object_files, reports_dir)

    acquisition_report = _read_json_safe(reports_dir / "acquisition_summary.json")
    registry_info = _inspect_registry_zip(registry_path)

    api_presence = ui_diff["dictionary_static_presence"]["api_dict"]
    ui_presence = ui_diff["dictionary_static_presence"]["ui_json"]

    summary: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "platform": platform,
        "work_dir": str(work_path),
        "acquisition_status": {
            "extracted_remote_bundle_dirs": len(bundle_dirs_remote),
            "extracted_apk_builtin_dirs": len(bundle_dirs_apk),
            "acquisition_summary": acquisition_report,
            "registry_zip": registry_info,
        },
        "extraction_status": {
            "naninovel_jsonl_files": len(naninovel_files),
            "strings_jsonl_files": len(string_files),
            "objects_jsonl_files": len(object_files),
            "failures_jsonl_files": len(failure_files),
            "total_objects": total_objects,
            "total_naninovel_fragments": scenario_diff["fragments"]["total_fragments"],
            "total_failures": failure_count,
            "failures_by_type": dict(failure_by_type),
        },
        "classification_counts": {
            "string_categories": dict(string_category_counts),
            "object_unity_types": dict(object_type_counts),
        },
        "scenario_diff": {
            "scripts": {
                "extracted_count": scenario_diff["scripts"]["extracted_count"],
                "ko_index_count": scenario_diff["scripts"]["ko_index_count"],
                "in_both_count": scenario_diff["scripts"]["in_both_count"],
                "catalog_only_count": scenario_diff["scripts"]["catalog_only_count"],
                "ko_only_count": scenario_diff["scripts"]["ko_only_count"],
            },
            "fragments": scenario_diff["fragments"],
            "missing_candidates_count": scenario_diff["missing_candidates_count"],
        },
        "ui_diff": {
            "extracted_candidates": ui_diff["extracted_candidates"],
            "dictionary_static_presence": ui_diff["dictionary_static_presence"],
            "missing_candidates_count": ui_diff["missing_candidates_count"],
        },
        "master_discovery": {
            "master_data_tables_detected_remote": master_discovery["master_data_tables_detected_remote"],
            "master_data_tables_detected_apk_builtin": master_discovery["master_data_tables_detected_apk_builtin"],
            "master_like_object_count_remote": master_discovery["master_like_object_count_remote"],
            "master_like_object_count_apk_builtin": master_discovery["master_like_object_count_apk_builtin"],
            "non_ui_non_spine_non_audio_class_count": master_discovery["non_ui_non_spine_non_audio_class_count"],
        },
        "remaining_non_remote_supply_channels": {
            "api_dict_not_found_statically": api_presence["not_found_statically"],
            "api_dict_total": api_presence["total_unique_keys"],
            "ui_json_not_found_statically": ui_presence["not_found_statically"],
            "ui_json_total": ui_presence["total_unique_keys"],
            "ko_only_scenarios_count": scenario_diff["scripts"]["ko_only_count"],
            "apk_builtin_included": len(bundle_dirs_apk) > 0,
        },
    }

    _atomic_write_json(reports_dir / "coverage_summary.json", summary)
    return summary