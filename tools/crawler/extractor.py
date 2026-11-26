from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import tempfile
import traceback
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import UnityPy

EXTRACTOR_VERSION = "lily-extractor-v1"

UI_TEXT_CLASSES = frozenset(
    {
        "Text",
        "TextMeshProUGUI",
        "NaninovelTMProText",
        "RevealableTMProText",
        "RevealableUIText",
        "TMP_Text",
    }
)

MEDIA_VISUAL_TYPES = frozenset({"Texture2D", "Sprite"})
ENGINE_ASSET_TYPES = frozenset({"Material", "Shader", "Font"})

EXCLUDED_STRUCTURED_KEYS = frozenset(
    {
        "m_Name",
        "m_Script",
        "m_Shader",
        "m_FileID",
        "m_PathID",
        "m_AssetGUID",
        "m_ClassName",
        "m_Namespace",
        "m_AssemblyName",
    }
)

JP_CHAR_RE = re.compile(
    r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF\uFF66-\uFF9F]"
)
ASSET_PATH_RE = re.compile(
    r"^(Assets/|Packages/|Resources/|Shaders/|Hidden/|UI/|Sprites/|Fonts/|Audio/|Prefabs/|Scenes/)"
    r"|(\.(png|jpg|jpeg|tga|psd|bmp|gif|webp|wav|ogg|mp3|acb|awb|mat|shader|prefab|asset|unity|fbx|anim|controller|ttf|otf|bytes|skel|atlas|json|txt|cs|dll)$)",
    re.IGNORECASE,
)

MAX_STRING_FIELD_BYTES = 20 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_str(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_japanese(text: str) -> bool:
    return bool(JP_CHAR_RE.search(text))


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with tmp_path.open("wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _safe_raw_bytes(obj: Any) -> bytes:
    try:
        raw = obj.get_raw_data()
        if isinstance(raw, (bytes, bytearray, memoryview)):
            return bytes(raw)
    except Exception:
        pass
    return b""


def _extract_textasset_raw_bytes(obj: Any) -> tuple[bytes, str]:
    name = ""
    try:
        tree = obj.read_typetree()
        if isinstance(tree, dict):
            name = str(tree.get("m_Name") or "")
            m_script = tree.get("m_Script")
            if isinstance(m_script, (bytes, bytearray, memoryview)):
                return bytes(m_script), name
            if isinstance(m_script, str):
                return m_script.encode("utf-8", errors="surrogateescape"), name
    except Exception:
        pass

    try:
        parsed = obj.read()
        name = str(getattr(parsed, "m_Name", "") or getattr(parsed, "name", "") or name)
        script_attr = getattr(parsed, "script", None)
        if isinstance(script_attr, (bytes, bytearray, memoryview)):
            return bytes(script_attr), name
        m_script = getattr(parsed, "m_Script", None)
        if isinstance(m_script, (bytes, bytearray, memoryview)):
            return bytes(m_script), name
        if isinstance(m_script, str):
            return m_script.encode("utf-8", errors="surrogateescape"), name
    except Exception:
        pass

    return _safe_raw_bytes(obj), name


def _detect_spine_textasset(name: str, raw_bytes: bytes) -> tuple[bool, str | None]:
    lower_name = name.lower()
    if lower_name.endswith(".atlas") or lower_name.endswith(".atlas.txt"):
        return True, "filename_suffix:.atlas"
    if lower_name.endswith(".skel") or lower_name.endswith(".skel.bytes"):
        return True, "filename_suffix:.skel"
    stripped = raw_bytes.lstrip()
    if stripped.startswith(b"{"):
        try:
            text = raw_bytes.decode("utf-8")
            parsed = json.loads(text)
            if (
                isinstance(parsed, dict)
                and "skeleton" in parsed
                and ("bones" in parsed or "slots" in parsed)
            ):
                return True, "json_keys:skeleton+(bones|slots)"
        except Exception:
            pass
    return False, None


def _build_ref_map(references: Any) -> dict[Any, dict[str, Any]]:
    ref_map: dict[Any, dict[str, Any]] = {}
    if not isinstance(references, dict):
        return ref_map
    ref_ids = references.get("RefIds")
    if isinstance(ref_ids, list):
        for entry in ref_ids:
            if isinstance(entry, dict) and "rid" in entry:
                ref_map[entry["rid"]] = entry
                ref_map[str(entry["rid"])] = entry
    elif isinstance(ref_ids, dict):
        for k, entry in ref_ids.items():
            if isinstance(entry, dict):
                rid = entry.get("rid", k)
                ref_map[rid] = entry
                ref_map[str(rid)] = entry
                ref_map[k] = entry
                ref_map[str(k)] = entry
    return ref_map


def _lookup_ref(ref_map: dict[Any, dict[str, Any]], rid: Any) -> dict[str, Any] | None:
    if rid is None:
        return None
    entry = ref_map.get(rid)
    if entry is not None:
        return entry
    return ref_map.get(str(rid))


def _ref_class_info(ref_entry: dict[str, Any]) -> tuple[str, str]:
    type_info = ref_entry.get("type")
    if isinstance(type_info, dict):
        cls = str(type_info.get("class") or "")
        ns = str(type_info.get("ns") or "")
        return cls, ns
    return "", ""


def _extract_param_info(param: Any) -> dict[str, Any] | None:
    if not isinstance(param, dict):
        return None
    has_value = bool(param.get("hasValue", False))
    value = param.get("value")
    dyn = param.get("dynamicValue")
    dyn_value_text: str | None = None
    dyn_expressions: list[Any] | None = None
    if isinstance(dyn, dict):
        vt = dyn.get("ValueText")
        if vt is None:
            vt = dyn.get("valueText")
        if vt is not None:
            dyn_value_text = str(vt)
        exprs = dyn.get("Expressions")
        if exprs is None:
            exprs = dyn.get("expressions")
        if isinstance(exprs, list):
            dyn_expressions = exprs
    return {
        "hasValue": has_value,
        "value": value,
        "dynamicValue_ValueText": dyn_value_text,
        "dynamicValue_Expressions": dyn_expressions,
    }


def _compute_player_template_hash(dyn_value_text: str | None) -> str | None:
    if not dyn_value_text or "{G_PlayerName}" not in dyn_value_text:
        return None
    normalized = dyn_value_text.replace("{G_PlayerName}", "{PLAYER_NAME}")
    return _sha256_str(normalized)


def _find_unknown_command_strings(
    data: Any, prefix: str = ""
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if "hasValue" in data and ("value" in data or "dynamicValue" in data):
            info = _extract_param_info(data)
            if info:
                val = info["value"]
                dvt = info["dynamicValue_ValueText"]
                if (isinstance(val, str) and val != "") or (
                    isinstance(dvt, str) and dvt != ""
                ):
                    found.append({"field": prefix or "root", "param": info})
            return found
        for k, v in data.items():
            if k in ("playbackSpot", "PlaybackSpot", "ConditionalExpression"):
                continue
            sub_prefix = f"{prefix}.{k}" if prefix else str(k)
            found.extend(_find_unknown_command_strings(v, sub_prefix))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            sub_prefix = f"{prefix}[{idx}]"
            found.extend(_find_unknown_command_strings(item, sub_prefix))
    elif isinstance(data, str) and data != "":
        found.append({"field": prefix or "root", "raw_string": data})
    return found


def _extract_naninovel_script(
    tree: dict[str, Any],
    base_provenance: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    records: list[dict[str, Any]] = []
    line_stats: dict[str, int] = Counter()

    script_m_name = str(tree.get("m_Name") or "")
    ref_map = _build_ref_map(tree.get("references"))
    lines = tree.get("lines")
    if not isinstance(lines, list):
        return records, dict(line_stats)

    def _process_command_ref(
        cmd_rid: Any,
        parent_line_type: str,
        parent_line_index: Any,
        parent_line_hash: Any,
        fallback_inline_index: int,
    ) -> None:
        cmd_ref = _lookup_ref(ref_map, cmd_rid)
        if not cmd_ref:
            return
        cmd_class, cmd_ns = _ref_class_info(cmd_ref)
        cmd_data = cmd_ref.get("data")
        if not isinstance(cmd_data, dict):
            return

        spot = cmd_data.get("playbackSpot")
        if not isinstance(spot, dict):
            spot = cmd_data.get("PlaybackSpot")
        spot_script_name = (
            str(spot.get("scriptName"))
            if isinstance(spot, dict) and spot.get("scriptName") is not None
            else None
        )
        spot_line_index = (
            spot.get("lineIndex") if isinstance(spot, dict) else None
        )
        spot_inline_index = (
            spot.get("inlineIndex")
            if isinstance(spot, dict) and spot.get("inlineIndex") is not None
            else fallback_inline_index
        )

        consistency = {
            "line_index_matches": (
                spot_line_index == parent_line_index
                if spot_line_index is not None and parent_line_index is not None
                else None
            ),
            "script_name_matches": (
                spot_script_name == script_m_name
                if spot_script_name is not None and script_m_name != ""
                else None
            ),
        }

        common = {
            **base_provenance,
            "script_m_name": script_m_name,
            "parent_line_type": parent_line_type,
            "line_index": parent_line_index,
            "line_hash": (
                str(parent_line_hash) if parent_line_hash is not None else None
            ),
            "command_class": cmd_class,
            "command_namespace": cmd_ns,
            "playback_spot": {
                "scriptName": spot_script_name,
                "lineIndex": spot_line_index,
                "inlineIndex": spot_inline_index,
            },
            "consistency": consistency,
        }

        if cmd_class == "PrintText":
            text_param = _extract_param_info(cmd_data.get("Text"))
            author_param = _extract_param_info(cmd_data.get("AuthorId"))
            static_val = (
                text_param["value"]
                if text_param and isinstance(text_param["value"], str)
                else None
            )
            dyn_text = (
                text_param["dynamicValue_ValueText"] if text_param else None
            )
            records.append(
                {
                    **common,
                    "is_known_text_command": True,
                    "text": text_param,
                    "author_id": author_param,
                    "source_guard_sha256_exact": (
                        _sha256_str(static_val)
                        if static_val is not None
                        and (text_param and text_param.get("hasValue"))
                        else None
                    ),
                    "source_guard_sha256_player_template": _compute_player_template_hash(
                        dyn_text
                    ),
                }
            )
        elif cmd_class == "AddChoice":
            summary_param = _extract_param_info(cmd_data.get("ChoiceSummary"))
            goto_param = _extract_param_info(cmd_data.get("GotoPath"))
            set_expr_param = _extract_param_info(cmd_data.get("SetExpression"))
            static_val = (
                summary_param["value"]
                if summary_param and isinstance(summary_param["value"], str)
                else None
            )
            dyn_text = (
                summary_param["dynamicValue_ValueText"]
                if summary_param
                else None
            )
            records.append(
                {
                    **common,
                    "is_known_text_command": True,
                    "choice_summary": summary_param,
                    "goto_path": goto_param,
                    "set_expression": set_expr_param,
                    "source_guard_sha256_exact": (
                        _sha256_str(static_val)
                        if static_val is not None
                        and (summary_param and summary_param.get("hasValue"))
                        else None
                    ),
                    "source_guard_sha256_player_template": _compute_player_template_hash(
                        dyn_text
                    ),
                }
            )
        else:
            extra_strings = _find_unknown_command_strings(cmd_data)
            if extra_strings:
                records.append(
                    {
                        **common,
                        "is_known_text_command": False,
                        "extracted_fields": extra_strings,
                    }
                )

    for line_ref_holder in lines:
        line_rid = (
            line_ref_holder.get("rid")
            if isinstance(line_ref_holder, dict)
            else line_ref_holder
        )
        line_ref = _lookup_ref(ref_map, line_rid)
        if not line_ref:
            line_stats["UnresolvedLineRef"] += 1
            continue
        line_class, _ = _ref_class_info(line_ref)
        line_stats[line_class or "UnknownLine"] += 1
        line_data = line_ref.get("data")
        if not isinstance(line_data, dict):
            continue

        line_index = line_data.get("lineIndex")
        line_hash = line_data.get("lineHash")

        if line_class == "GenericTextScriptLine":
            inlined = line_data.get("inlinedCommands")
            if isinstance(inlined, list):
                for idx, cmd_holder in enumerate(inlined):
                    cmd_rid = (
                        cmd_holder.get("rid")
                        if isinstance(cmd_holder, dict)
                        else cmd_holder
                    )
                    _process_command_ref(
                        cmd_rid=cmd_rid,
                        parent_line_type=line_class,
                        parent_line_index=line_index,
                        parent_line_hash=line_hash,
                        fallback_inline_index=idx,
                    )
        elif line_class == "CommandScriptLine":
            cmd_holder = line_data.get("command")
            cmd_rid = (
                cmd_holder.get("rid")
                if isinstance(cmd_holder, dict)
                else cmd_holder
            )
            _process_command_ref(
                cmd_rid=cmd_rid,
                parent_line_type=line_class,
                parent_line_index=line_index,
                parent_line_hash=line_hash,
                fallback_inline_index=0,
            )

    return records, dict(line_stats)


def _scan_structured_strings(
    node: Any,
    path: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            key_str = str(k)
            if key_str in EXCLUDED_STRUCTURED_KEYS:
                continue
            sub_path = f"{path}.{key_str}" if path else key_str
            results.extend(_scan_structured_strings(v, sub_path))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            sub_path = f"{path}[{idx}]"
            results.extend(_scan_structured_strings(item, sub_path))
    elif isinstance(node, str):
        if node == "":
            return results
        if ASSET_PATH_RE.search(node):
            return results
        raw_utf8 = node.encode("utf-8", errors="replace")
        truncated = False
        text_out = node
        if len(raw_utf8) > MAX_STRING_FIELD_BYTES:
            truncated = True
            text_out = raw_utf8[:MAX_STRING_FIELD_BYTES].decode(
                "utf-8", errors="ignore"
            )
        results.append(
            {
                "field_path": path,
                "text": text_out,
                "truncated": truncated,
                "original_byte_len": len(raw_utf8),
                "text_sha256": _sha256_bytes(raw_utf8),
                "has_japanese": _has_japanese(node),
            }
        )
    return results


def _build_asset_maps(
    env: Any,
) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    monoscript_map: dict[str, dict[str, str]] = {}
    gameobject_map: dict[str, str] = {}

    for obj in env.objects:
        tname = obj.type.name
        pid_str = str(obj.path_id)
        if tname == "MonoScript":
            try:
                parsed = obj.read()
                cls = str(getattr(parsed, "m_ClassName", "") or "")
                ns = str(getattr(parsed, "m_Namespace", "") or "")
                asm = str(getattr(parsed, "m_AssemblyName", "") or "")
                full = f"{ns}.{cls}" if ns else cls
                monoscript_map[pid_str] = {
                    "class_name": cls,
                    "namespace": ns,
                    "assembly": asm,
                    "full_name": full,
                }
            except Exception:
                try:
                    tree = obj.read_typetree()
                    if isinstance(tree, dict):
                        cls = str(tree.get("m_ClassName") or "")
                        ns = str(tree.get("m_Namespace") or "")
                        asm = str(tree.get("m_AssemblyName") or "")
                        full = f"{ns}.{cls}" if ns else cls
                        monoscript_map[pid_str] = {
                            "class_name": cls,
                            "namespace": ns,
                            "assembly": asm,
                            "full_name": full,
                        }
                except Exception:
                    pass
        elif tname == "GameObject":
            try:
                parsed = obj.read()
                go_name = str(
                    getattr(parsed, "m_Name", "")
                    or getattr(parsed, "name", "")
                    or ""
                )
                gameobject_map[pid_str] = go_name
            except Exception:
                try:
                    tree = obj.read_typetree()
                    if isinstance(tree, dict):
                        gameobject_map[pid_str] = str(tree.get("m_Name") or "")
                except Exception:
                    pass

    return monoscript_map, gameobject_map


def _resolve_monobehaviour_class(
    obj: Any,
    tree: dict[str, Any] | None,
    monoscript_map: dict[str, dict[str, str]],
) -> tuple[str, str, str]:
    if isinstance(tree, dict):
        m_script = tree.get("m_Script")
        if isinstance(m_script, dict):
            script_pid = m_script.get("m_PathID")
            if script_pid is not None:
                info = monoscript_map.get(str(script_pid))
                if info:
                    return (
                        info["class_name"],
                        info["namespace"],
                        info["full_name"],
                    )
    try:
        parsed = obj.read()
        m_script_pptr = getattr(parsed, "m_Script", None)
        if m_script_pptr is not None:
            script_pid = getattr(m_script_pptr, "path_id", None)
            if script_pid is not None and str(script_pid) in monoscript_map:
                info = monoscript_map[str(script_pid)]
                return info["class_name"], info["namespace"], info["full_name"]
            if hasattr(m_script_pptr, "read"):
                ms = m_script_pptr.read()
                cls = str(getattr(ms, "m_ClassName", "") or "")
                ns = str(getattr(ms, "m_Namespace", "") or "")
                full = f"{ns}.{cls}" if ns else cls
                return cls, ns, full
    except Exception:
        pass
    return "", "", ""


def _resolve_gameobject_name(
    tree: dict[str, Any] | None,
    gameobject_map: dict[str, str],
) -> str:
    if isinstance(tree, dict):
        m_go = tree.get("m_GameObject")
        if isinstance(m_go, dict):
            go_pid = m_go.get("m_PathID")
            if go_pid is not None:
                return gameobject_map.get(str(go_pid), "")
    return ""


def _extract_unity_env(
    env: Any,
    out_dir: Path,
    bundle_provenance: dict[str, Any],
) -> dict[str, Any]:
    objects_records: list[dict[str, Any]] = []
    naninovel_records: list[dict[str, Any]] = []
    strings_records: list[dict[str, Any]] = []
    failure_records: list[dict[str, Any]] = []

    type_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    naninovel_line_stats: Counter[str] = Counter()

    monoscript_map, gameobject_map = _build_asset_maps(env)
    unsupported_raw_dir = out_dir / "unsupported_raw"

    for obj in env.objects:
        path_id_str = str(obj.path_id)
        obj_type = obj.type.name
        container = obj.container
        type_counts[obj_type] += 1

        raw_bytes = _safe_raw_bytes(obj)
        serialized_len = len(raw_bytes) if raw_bytes else int(getattr(obj, "byte_size", 0) or 0)
        payload_sha256 = _sha256_bytes(raw_bytes) if raw_bytes else None

        class_name = obj_type
        obj_name = ""
        classification = "engine_asset"
        error_msg: str | None = None

        base_prov = {
            "source_channel": bundle_provenance.get("source_channel", "addressables_bundle"),
            "bundle_raw_sha256": bundle_provenance.get("raw_sha256"),
            "bundle_internal_id": bundle_provenance.get("internal_id"),
            "container": container,
            "path_id": path_id_str,
        }

        try:
            if obj_type == "AudioClip":
                classification = "media_audio"
                try:
                    parsed = obj.read()
                    obj_name = str(getattr(parsed, "m_Name", "") or getattr(parsed, "name", "") or "")
                except Exception:
                    pass

            elif obj_type in MEDIA_VISUAL_TYPES:
                classification = "media_visual"
                try:
                    parsed = obj.read()
                    obj_name = str(getattr(parsed, "m_Name", "") or getattr(parsed, "name", "") or "")
                except Exception:
                    pass

            elif obj_type in ENGINE_ASSET_TYPES:
                classification = "engine_asset"
                try:
                    parsed = obj.read()
                    obj_name = str(getattr(parsed, "m_Name", "") or getattr(parsed, "name", "") or "")
                except Exception:
                    pass

            elif obj_type == "TextAsset":
                ta_bytes, obj_name = _extract_textasset_raw_bytes(obj)
                ta_sha256 = _sha256_bytes(ta_bytes)
                if ta_bytes.startswith(b"@UTF"):
                    classification = "media_audio_cri_acb"
                elif ta_bytes.startswith(b"AFS2"):
                    classification = "media_audio_cri_awb"
                else:
                    is_spine, spine_evidence = _detect_spine_textasset(obj_name, ta_bytes)
                    if is_spine:
                        classification = "media_spine"
                        strings_records.append(
                            {
                                **base_prov,
                                "classification": "media_spine",
                                "object_type": obj_type,
                                "name": obj_name,
                                "is_translatable": False,
                                "evidence": spine_evidence,
                                "byte_len": len(ta_bytes),
                                "payload_sha256": ta_sha256,
                            }
                        )
                    else:
                        try:
                            decoded_text = ta_bytes.decode("utf-8")
                            classification = "textasset_candidate"
                            truncated = False
                            text_out = decoded_text
                            if len(ta_bytes) > MAX_STRING_FIELD_BYTES:
                                truncated = True
                                text_out = ta_bytes[:MAX_STRING_FIELD_BYTES].decode(
                                    "utf-8", errors="ignore"
                                )
                            strings_records.append(
                                {
                                    **base_prov,
                                    "classification": "textasset_candidate",
                                    "object_type": obj_type,
                                    "name": obj_name,
                                    "text": text_out,
                                    "truncated": truncated,
                                    "original_byte_len": len(ta_bytes),
                                    "text_sha256": ta_sha256,
                                    "has_japanese": _has_japanese(decoded_text),
                                    "is_empty": decoded_text == "",
                                }
                            )
                        except UnicodeDecodeError as dec_err:
                            classification = "binary_textasset"
                            raw_save_path = unsupported_raw_dir / f"{path_id_str}.bin"
                            _atomic_write_bytes(raw_save_path, ta_bytes)
                            failure_records.append(
                                {
                                    **base_prov,
                                    "object_type": obj_type,
                                    "class_name": class_name,
                                    "name": obj_name,
                                    "reason": "binary_textasset_utf8_decode_failed",
                                    "error": str(dec_err),
                                    "raw_dump": str(raw_save_path.relative_to(out_dir)),
                                    "byte_len": len(ta_bytes),
                                    "payload_sha256": ta_sha256,
                                }
                            )

            elif obj_type == "MonoBehaviour":
                tree: dict[str, Any] | None = None
                tree_err: Exception | None = None
                try:
                    t = obj.read_typetree()
                    if isinstance(t, dict):
                        tree = t
                except Exception as exc:
                    tree_err = exc

                cls, ns, full_cls = _resolve_monobehaviour_class(
                    obj, tree, monoscript_map
                )
                class_name = full_cls or cls or "MonoBehaviour"
                if isinstance(tree, dict):
                    obj_name = str(tree.get("m_Name") or "")

                if tree is None:
                    classification = "unsupported_monobehaviour"
                    error_msg = f"read_typetree_failed: {tree_err}"
                    raw_save_path = unsupported_raw_dir / f"{path_id_str}.bin"
                    if raw_bytes:
                        _atomic_write_bytes(raw_save_path, raw_bytes)
                    failure_records.append(
                        {
                            **base_prov,
                            "object_type": obj_type,
                            "class_name": class_name,
                            "name": obj_name,
                            "reason": "monobehaviour_typetree_unavailable",
                            "error": error_msg,
                            "raw_dump": (
                                str(raw_save_path.relative_to(out_dir))
                                if raw_bytes
                                else None
                            ),
                            "byte_len": serialized_len,
                            "payload_sha256": payload_sha256,
                        }
                    )
                elif full_cls == "Naninovel.Script" or (
                    cls == "Script" and "Naninovel" in ns
                ):
                    classification = "naninovel_script"
                    nani_recs, lstats = _extract_naninovel_script(tree, base_prov)
                    naninovel_records.extend(nani_recs)
                    naninovel_line_stats.update(lstats)
                elif cls in UI_TEXT_CLASSES or full_cls.split(".")[-1] in UI_TEXT_CLASSES:
                    classification = "ui_candidate"
                    go_name = _resolve_gameobject_name(tree, gameobject_map)
                    ui_text = tree.get("m_Text")
                    if ui_text is None:
                        ui_text = tree.get("m_text")
                    text_str = str(ui_text) if ui_text is not None else ""
                    strings_records.append(
                        {
                            **base_prov,
                            "classification": "ui_candidate",
                            "object_type": obj_type,
                            "class_name": class_name,
                            "name": obj_name,
                            "game_object_name": go_name,
                            "text": text_str,
                            "text_sha256": _sha256_str(text_str),
                            "has_japanese": _has_japanese(text_str),
                            "is_empty": text_str == "",
                        }
                    )
                else:
                    go_name = _resolve_gameobject_name(tree, gameobject_map)
                    scanned = _scan_structured_strings(tree)
                    if scanned:
                        classification = "structured_data_candidate"
                        for item in scanned:
                            strings_records.append(
                                {
                                    **base_prov,
                                    "classification": "structured_data_candidate",
                                    "object_type": obj_type,
                                    "class_name": class_name,
                                    "name": obj_name,
                                    "game_object_name": go_name,
                                    **item,
                                }
                            )
                    else:
                        classification = "monobehaviour_no_text"

            else:
                classification = "engine_asset"
                try:
                    parsed = obj.read()
                    obj_name = str(
                        getattr(parsed, "m_Name", "")
                        or getattr(parsed, "name", "")
                        or ""
                    )
                except Exception:
                    pass

        except Exception as exc:
            classification = "extraction_error"
            error_msg = f"{type(exc).__name__}: {exc}"
            raw_save_path = unsupported_raw_dir / f"{path_id_str}.bin"
            if raw_bytes:
                _atomic_write_bytes(raw_save_path, raw_bytes)
            failure_records.append(
                {
                    **base_prov,
                    "object_type": obj_type,
                    "class_name": class_name,
                    "name": obj_name,
                    "reason": "object_extraction_exception",
                    "error": error_msg,
                    "traceback": traceback.format_exc(),
                    "raw_dump": (
                        str(raw_save_path.relative_to(out_dir))
                        if raw_bytes
                        else None
                    ),
                    "byte_len": serialized_len,
                    "payload_sha256": payload_sha256,
                }
            )

        classification_counts[classification] += 1
        objects_records.append(
            {
                "path_id": path_id_str,
                "type": obj_type,
                "class_name": class_name,
                "container": container,
                "name": obj_name,
                "classification": classification,
                "serialized_bytes_len": serialized_len,
                "payload_sha256": payload_sha256,
                "error": error_msg,
            }
        )

    total_objects = len(objects_records)
    total_failures = len(failure_records)
    if total_objects == 0 and total_failures > 0:
        status = "FAILED"
    elif total_failures == 0:
        status = "EXTRACTED"
    elif total_failures < total_objects:
        status = "PARTIAL"
    else:
        status = "UNSUPPORTED"

    meta = {
        "extractor_version": EXTRACTOR_VERSION,
        "extracted_at_utc": _utc_now_iso(),
        "status": status,
        "bundle_provenance": bundle_provenance,
        "object_type_counts": dict(type_counts),
        "classification_counts": dict(classification_counts),
        "naninovel_line_stats": dict(naninovel_line_stats),
        "counts": {
            "objects": total_objects,
            "naninovel_records": len(naninovel_records),
            "string_records": len(strings_records),
            "failures": total_failures,
        },
    }

    _atomic_write_jsonl(out_dir / "objects.jsonl", objects_records)
    _atomic_write_jsonl(out_dir / "naninovel.jsonl", naninovel_records)
    _atomic_write_jsonl(out_dir / "strings.jsonl", strings_records)
    _atomic_write_jsonl(out_dir / "failures.jsonl", failure_records)
    _atomic_write_json(out_dir / "meta.json", meta)
    return meta


def extract_bundle(
    bundle_path: str | Path,
    bundle_meta: dict[str, Any],
    work_dir: str | Path,
    force: bool = False,
) -> dict[str, Any]:
    bpath = Path(bundle_path)
    wdir = Path(work_dir)
    raw_sha256 = str(bundle_meta.get("raw_sha256") or "")
    if not raw_sha256:
        raw_sha256 = _sha256_file(bpath)

    out_dir = wdir / "extracted" / raw_sha256[:2] / raw_sha256 / EXTRACTOR_VERSION
    meta_path = out_dir / "meta.json"

    if not force and meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if existing.get("extractor_version") == EXTRACTOR_VERSION and existing.get(
                "status"
            ) in ("EXTRACTED", "PARTIAL"):
                return existing
        except Exception:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)
    provenance = {
        **bundle_meta,
        "raw_sha256": raw_sha256,
        "bundle_path": str(bpath),
        "source_channel": bundle_meta.get("source_channel", "addressables_bundle"),
    }

    try:
        env = UnityPy.load(str(bpath))
        return _extract_unity_env(env, out_dir, provenance)
    except Exception as exc:
        meta = {
            "extractor_version": EXTRACTOR_VERSION,
            "extracted_at_utc": _utc_now_iso(),
            "status": "FAILED",
            "bundle_provenance": provenance,
            "object_type_counts": {},
            "classification_counts": {},
            "naninovel_line_stats": {},
            "counts": {
                "objects": 0,
                "naninovel_records": 0,
                "string_records": 0,
                "failures": 1,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }
        _atomic_write_jsonl(out_dir / "objects.jsonl", [])
        _atomic_write_jsonl(out_dir / "naninovel.jsonl", [])
        _atomic_write_jsonl(out_dir / "strings.jsonl", [])
        _atomic_write_jsonl(
            out_dir / "failures.jsonl",
            [
                {
                    "source_channel": provenance["source_channel"],
                    "bundle_raw_sha256": raw_sha256,
                    "bundle_internal_id": provenance.get("internal_id"),
                    "reason": "unitypy_load_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            ],
        )
        _atomic_write_json(meta_path, meta)
        return meta


def extract_apk_builtin(apk_path: str | Path, work_dir: str | Path) -> dict[str, Any]:
    apk = Path(apk_path)
    wdir = Path(work_dir)
    apk_sha256 = _sha256_file(apk)

    out_dir = wdir / "extracted" / "apk_builtin" / apk_sha256 / EXTRACTOR_VERSION
    out_dir.mkdir(parents=True, exist_ok=True)

    extracted_files: list[str] = []
    with tempfile.TemporaryDirectory(prefix="lily_apk_builtin_", ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(apk, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename
                if name.startswith("assets/bin/Data/") or (
                    name.startswith("assets/aa/Android/") and name.endswith(".bundle")
                ):
                    target = tmp_path / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_files.append(name)

        provenance = {
            "source_channel": "android_apk_builtin",
            "raw_sha256": apk_sha256,
            "apk_path": str(apk),
            "internal_id": apk.name,
            "builtin_file_count": len(extracted_files),
        }

        try:
            env = UnityPy.load(str(tmp_path))
            return _extract_unity_env(env, out_dir, provenance)
        except Exception as exc:
            meta = {
                "extractor_version": EXTRACTOR_VERSION,
                "extracted_at_utc": _utc_now_iso(),
                "status": "FAILED",
                "bundle_provenance": provenance,
                "object_type_counts": {},
                "classification_counts": {},
                "naninovel_line_stats": {},
                "counts": {
                    "objects": 0,
                    "naninovel_records": 0,
                    "string_records": 0,
                    "failures": 1,
                },
                "error": f"{type(exc).__name__}: {exc}",
            }
            _atomic_write_json(out_dir / "meta.json", meta)
            return meta


def _extract_single_item_worker(
    args: tuple[str, dict[str, Any], str, bool],
) -> tuple[str, dict[str, Any]]:
    bundle_path, bundle_meta, work_dir, force = args
    raw_sha256 = str(bundle_meta.get("raw_sha256") or "")
    meta = extract_bundle(bundle_path, bundle_meta, work_dir, force=force)
    return raw_sha256, meta


def extract_all(
    work_dir: str | Path,
    platform: str,
    state_items: list[dict[str, Any]],
    workers: int = 2,
    force: bool = False,
    retry_failures: bool = False,
    cancel_file: str | Path | None = None,
) -> dict[str, Any]:
    wdir = Path(work_dir)
    cfile = Path(cancel_file) if cancel_file else None

    tasks: list[tuple[str, dict[str, Any], str, bool]] = []
    skipped = 0

    for item in state_items:
        raw_sha256 = str(item.get("raw_sha256") or "")
        if not raw_sha256:
            skipped += 1
            continue

        raw_path_val = item.get("raw_path")
        if raw_path_val:
            bundle_path = Path(raw_path_val)
        else:
            bundle_path = wdir / "raw" / raw_sha256[:2] / f"{raw_sha256}.bundle"

        if not bundle_path.exists():
            skipped += 1
            continue

        out_meta = (
            wdir / "extracted" / raw_sha256[:2] / raw_sha256 / EXTRACTOR_VERSION / "meta.json"
        )
        should_force = force
        if not force and out_meta.exists():
            try:
                existing = json.loads(out_meta.read_text(encoding="utf-8"))
                st = existing.get("status")
                if st in ("EXTRACTED", "PARTIAL"):
                    skipped += 1
                    continue
                if st in ("FAILED", "UNSUPPORTED") and not retry_failures:
                    skipped += 1
                    continue
                if retry_failures:
                    should_force = True
            except Exception:
                should_force = True

        bundle_meta = {
            **item,
            "platform": platform,
            "raw_sha256": raw_sha256,
        }
        tasks.append((str(bundle_path), bundle_meta, str(wdir), should_force))

    status_counts: Counter[str] = Counter()
    cancelled = False
    processed = 0

    if workers <= 1:
        for task in tasks:
            if cfile and cfile.exists():
                cancelled = True
                break
            _, meta = _extract_single_item_worker(task)
            status_counts[str(meta.get("status", "UNKNOWN"))] += 1
            processed += 1
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
            futures: list[concurrent.futures.Future[tuple[str, dict[str, Any]]]] = []
            for task in tasks:
                if cfile and cfile.exists():
                    cancelled = True
                    break
                futures.append(executor.submit(_extract_single_item_worker, task))

            for fut in concurrent.futures.as_completed(futures):
                if cfile and cfile.exists():
                    cancelled = True
                try:
                    _, meta = fut.result()
                    status_counts[str(meta.get("status", "UNKNOWN"))] += 1
                except Exception:
                    status_counts["FAILED"] += 1
                processed += 1

    return {
        "extractor_version": EXTRACTOR_VERSION,
        "platform": platform,
        "total_items": len(state_items),
        "queued": len(tasks),
        "processed": processed,
        "skipped": skipped,
        "cancelled": cancelled,
        "status_counts": dict(status_counts),
    }