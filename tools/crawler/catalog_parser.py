from __future__ import annotations

import base64
import collections
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
from typing import Any
import urllib.error
import urllib.request


DEFAULT_CDN_BASE = "https://dok6uc0hyhl8f.cloudfront.net"

PLATFORM_CONFIGS: dict[str, dict[str, str | None]] = {
    "WebGL": {
        "settings_url": "https://lilyange.saikyo.biz/app_data/StreamingAssets/aa/settings.json",
        "cdn_base": DEFAULT_CDN_BASE,
        "catalog_hash_url": f"{DEFAULT_CDN_BASE}/WebGL/catalog_0.0.1.hash",
        "catalog_json_url": f"{DEFAULT_CDN_BASE}/WebGL/catalog_0.0.1.json",
    },
    "Android": {
        "settings_url": None,
        "cdn_base": DEFAULT_CDN_BASE,
        "catalog_hash_url": f"{DEFAULT_CDN_BASE}/Android/catalog_0.0.1.hash",
        "catalog_json_url": f"{DEFAULT_CDN_BASE}/Android/catalog_0.0.1.json",
    },
}

_PREFIX_PATTERN = re.compile(r"^(\d+)#(.*)$")
_DEFAULT_USER_AGENT = "lily-crawler/1.0 (+UnityAddressables)"


class CatalogParseError(ValueError):
    """Raised when an Addressables catalog binary payload is malformed."""


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes atomically using a sibling .tmp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int | None = 2) -> None:
    """Serialize JSON with UTF-8 encoding and write atomically."""
    encoded = json.dumps(payload, ensure_ascii=False, indent=indent).encode("utf-8")
    atomic_write_bytes(path, encoded)


def _http_get_bytes(url: str, timeout: float = 60.0) -> tuple[bytes, dict[str, str]]:
    """Fetch a URL and return (raw_bytes, lowercased_headers)."""
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return data, headers


def _resolve_platform_urls(platform: str, cdn_base: str | None = None) -> dict[str, str | None]:
    if platform not in PLATFORM_CONFIGS:
        raise ValueError(f"Unsupported platform {platform!r}; expected one of {sorted(PLATFORM_CONFIGS)}")
    cfg = dict(PLATFORM_CONFIGS[platform])
    base = (cdn_base or cfg["cdn_base"] or DEFAULT_CDN_BASE).rstrip("/")
    cfg["cdn_base"] = base
    cfg["catalog_hash_url"] = f"{base}/{platform}/catalog_0.0.1.hash"
    cfg["catalog_json_url"] = f"{base}/{platform}/catalog_0.0.1.json"
    return cfg


def fetch_snapshot(
    work_dir: Path | str,
    platform: str,
    offline_catalog: Path | str | None = None,
    cdn_base: str | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Fetch (or replay offline) a platform catalog snapshot and save raw files atomically."""
    work_path = Path(work_dir)
    cfg = _resolve_platform_urls(platform, cdn_base=cdn_base)
    resolved_cdn = str(cfg["cdn_base"])
    observed_at_utc = datetime.now(timezone.utc).isoformat()

    settings_raw: bytes | None = None
    settings_headers: dict[str, str] = {}
    hash_raw: bytes | None = None
    hash_headers: dict[str, str] = {}
    catalog_headers: dict[str, str] = {}
    offline_mode = offline_catalog is not None

    if offline_mode:
        offline_path = Path(offline_catalog)  # type: ignore[arg-type]
        if not offline_path.is_file():
            raise FileNotFoundError(f"Offline catalog file not found: {offline_path}")
        catalog_raw = offline_path.read_bytes()
        source_url = str(offline_path.resolve())
    else:
        settings_url = cfg.get("settings_url")
        if settings_url:
            try:
                settings_raw, settings_headers = _http_get_bytes(settings_url, timeout=timeout)
            except (urllib.error.URLError, TimeoutError, OSError):
                settings_raw = None

        hash_url = cfg.get("catalog_hash_url")
        if hash_url:
            try:
                hash_raw, hash_headers = _http_get_bytes(hash_url, timeout=timeout)
            except (urllib.error.URLError, TimeoutError, OSError):
                hash_raw = None

        catalog_url = str(cfg["catalog_json_url"])
        catalog_raw, catalog_headers = _http_get_bytes(catalog_url, timeout=timeout)
        source_url = catalog_url

    catalog_sha256 = hashlib.sha256(catalog_raw).hexdigest()
    catalog_md5 = hashlib.md5(catalog_raw).hexdigest()
    sha12 = catalog_sha256[:12]

    snapshot_dir = work_path / "snapshots" / platform / sha12
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = snapshot_dir / "catalog_0.0.1.json"
    atomic_write_bytes(catalog_path, catalog_raw)

    settings_path: Path | None = None
    if settings_raw is not None:
        settings_path = snapshot_dir / "settings.json"
        atomic_write_bytes(settings_path, settings_raw)

    hash_path: Path | None = None
    catalog_hash_token: str | None = None
    if hash_raw is not None:
        hash_path = snapshot_dir / "catalog_0.0.1.hash"
        atomic_write_bytes(hash_path, hash_raw)
        catalog_hash_token = hash_raw.decode("utf-8", errors="replace").strip()

    meta: dict[str, Any] = {
        "platform": platform,
        "cdn_base": resolved_cdn,
        "offline": offline_mode,
        "source_url": source_url,
        "observed_at_utc": observed_at_utc,
        "catalog_sha256": catalog_sha256,
        "catalog_md5": catalog_md5,
        "catalog_bytes": len(catalog_raw),
        "sha12": sha12,
        "http_last_modified": catalog_headers.get("last-modified"),
        "http_etag": catalog_headers.get("etag"),
        "catalog_hash_token": catalog_hash_token,
        "hash_http_last_modified": hash_headers.get("last-modified"),
        "settings_present": settings_raw is not None,
        "settings_http_last_modified": settings_headers.get("last-modified"),
        "snapshot_dir": str(snapshot_dir),
        "catalog_path": str(catalog_path),
        "settings_path": str(settings_path) if settings_path else None,
        "hash_path": str(hash_path) if hash_path else None,
    }
    atomic_write_json(snapshot_dir / "snapshot_meta.json", meta)
    return meta


def _check_bounds(buf: bytes, pos: int, size: int, label: str) -> None:
    if pos < 0 or size < 0 or pos + size > len(buf):
        raise CatalogParseError(
            f"Out-of-bounds read in {label}: pos={pos}, size={size}, len={len(buf)}"
        )


def _read_i32(buf: bytes, pos: int, label: str = "i32") -> int:
    _check_bounds(buf, pos, 4, label)
    return int(struct.unpack_from("<i", buf, pos)[0])


def _read_object(buf: bytes, pos: int, label: str = "object") -> Any:
    _check_bounds(buf, pos, 1, f"{label}.tag")
    tag = buf[pos]
    pos += 1

    if tag in (0, 1):
        length = _read_i32(buf, pos, f"{label}.str_len")
        pos += 4
        _check_bounds(buf, pos, length, f"{label}.str_body")
        encoding = "ascii" if tag == 0 else "utf-16-le"
        return buf[pos : pos + length].decode(encoding, errors="strict")

    if tag == 2:
        _check_bounds(buf, pos, 2, f"{label}.u16")
        return int(struct.unpack_from("<H", buf, pos)[0])

    if tag == 3:
        _check_bounds(buf, pos, 4, f"{label}.u32")
        return int(struct.unpack_from("<I", buf, pos)[0])

    if tag == 4:
        _check_bounds(buf, pos, 4, f"{label}.i32")
        return int(struct.unpack_from("<i", buf, pos)[0])

    if tag in (5, 6):
        _check_bounds(buf, pos, 1, f"{label}.short_len")
        length = buf[pos]
        pos += 1
        _check_bounds(buf, pos, length, f"{label}.short_body")
        return buf[pos : pos + length].decode("ascii", errors="strict")

    if tag == 7:
        _check_bounds(buf, pos, 1, f"{label}.asm_len")
        alen = buf[pos]
        pos += 1
        _check_bounds(buf, pos, alen, f"{label}.asm")
        assembly = buf[pos : pos + alen].decode("ascii", errors="strict")
        pos += alen

        _check_bounds(buf, pos, 1, f"{label}.cls_len")
        clen = buf[pos]
        pos += 1
        _check_bounds(buf, pos, clen, f"{label}.cls")
        cls_name = buf[pos : pos + clen].decode("ascii", errors="strict")
        pos += clen

        jlen = _read_i32(buf, pos, f"{label}.json_len")
        pos += 4
        _check_bounds(buf, pos, jlen, f"{label}.json_body")
        raw_json = buf[pos : pos + jlen].decode("utf-16-le", errors="strict")
        return {
            "assembly": assembly,
            "class": cls_name,
            "data": json.loads(raw_json),
        }

    raise CatalogParseError(f"Unknown Addressables object tag {tag} at offset {pos - 1} in {label}")


def _expand_internal_id(raw_id: str, prefixes: list[str]) -> str:
    if not prefixes:
        return raw_id
    match = _PREFIX_PATTERN.match(raw_id)
    if not match:
        return raw_id
    idx = int(match.group(1))
    suffix = match.group(2)
    if 0 <= idx < len(prefixes):
        return f"{prefixes[idx]}{suffix}"
    return raw_id


def _resolve_bundle_url(internal_id: str, cdn_base: str) -> str:
    base = cdn_base.rstrip("/")
    if "DefineResourcesURLAddressable" in internal_id:
        return internal_id.replace("DefineResourcesURLAddressable", base)
    if internal_id.startswith("http://") or internal_id.startswith("https://"):
        return internal_id
    return f"{base}/{internal_id.lstrip('/')}"


def _guess_bundle_group(internal_id: str) -> str:
    low = internal_id.lower()
    if "unitybuiltinshaders" in low:
        return "unity_builtin_shaders"
    if "/battleaudio" in low or "battleaudio_assets_" in low:
        return "battleaudio"
    if "/systemvoice" in low or "systemvoice_assets_" in low:
        return "systemvoice"
    if "remotegroup_assets_skeletondataasset/" in low:
        return "remotegroup_skeletondataasset"
    if "naninovelseparate_assets_naninovel/scripts/" in low:
        return "naninovelseparate_scripts"
    if "naninovelseparate_assets_naninovel/audio/" in low:
        return "naninovelseparate_audio"
    if "naninovelseparate_assets_naninovel/backgrounds/" in low:
        return "naninovelseparate_backgrounds"
    if "naninovelseparate_" in low:
        return "naninovelseparate_other"
    if "naninovel_assets_" in low:
        return "naninovel_bundle"
    name = PurePosixPath(internal_id).name
    if name.endswith(".bundle"):
        name = name[:-7]
    for marker in ("_assets_", "_scenes_"):
        if marker in name:
            return name.split(marker, 1)[0] or "unknown"
    return "other"


def _extract_bundle_metadata(extra: Any) -> dict[str, Any] | None:
    if not isinstance(extra, dict):
        return None
    data = extra.get("data")
    if not isinstance(data, dict):
        return None
    return {
        "m_Hash": data.get("m_Hash"),
        "m_Crc": data.get("m_Crc"),
        "m_BundleSize": data.get("m_BundleSize"),
        "m_Timeout": data.get("m_Timeout"),
        "m_RetryCount": data.get("m_RetryCount"),
        "m_BundleName": data.get("m_BundleName"),
        "raw": data,
    }


def _collect_transitive_deps(
    start_deps: list[int],
    locations: list[dict[str, Any]],
) -> set[int]:
    visited: set[int] = set()
    queue: collections.deque[int] = collections.deque(start_deps)
    total = len(locations)
    while queue:
        dep = queue.popleft()
        if dep in visited:
            continue
        if dep < 0 or dep >= total:
            continue
        visited.add(dep)
        for child in locations[dep]["dependencies"]:
            if child not in visited:
                queue.append(child)
    return visited


def parse_catalog_bytes(raw: bytes, cdn_base: str, platform: str) -> dict[str, Any]:
    """Parse an Addressables 1.19.19 / 1.21.20 catalog_0.0.1.json binary payload."""
    try:
        cat = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise CatalogParseError(f"Failed to decode catalog JSON: {exc}") from exc

    required_fields = (
        "m_InternalIds",
        "m_ProviderIds",
        "m_resourceTypes",
        "m_BucketDataString",
        "m_KeyDataString",
        "m_EntryDataString",
        "m_ExtraDataString",
    )
    for field in required_fields:
        if field not in cat:
            raise CatalogParseError(f"Catalog missing required field: {field}")

    prefixes_raw = cat.get("m_InternalIdPrefixes") or []
    prefixes = [str(p) for p in prefixes_raw]
    addressables_layout = "1.21+" if prefixes else "1.19"

    internal_ids = [_expand_internal_id(str(x), prefixes) for x in cat["m_InternalIds"]]
    provider_ids = [str(x) for x in cat["m_ProviderIds"]]
    resource_types = cat["m_resourceTypes"]

    try:
        bdata = base64.b64decode(cat["m_BucketDataString"])
        kdata = base64.b64decode(cat["m_KeyDataString"])
        edata = base64.b64decode(cat["m_EntryDataString"])
        xdata = base64.b64decode(cat["m_ExtraDataString"])
    except Exception as exc:
        raise CatalogParseError(f"Failed to base64-decode catalog binary tables: {exc}") from exc

    bucket_count = _read_i32(bdata, 0, "buckets.count")
    if bucket_count < 0:
        raise CatalogParseError(f"Negative bucket count: {bucket_count}")

    buckets: list[list[int]] = []
    keys: list[Any] = []
    pos = 4
    for b_idx in range(bucket_count):
        _check_bounds(bdata, pos, 8, f"buckets[{b_idx}].header")
        offset, count = struct.unpack_from("<ii", bdata, pos)
        pos += 8
        if count < 0:
            raise CatalogParseError(f"Negative entry count {count} in bucket {b_idx}")
        _check_bounds(bdata, pos, 4 * count, f"buckets[{b_idx}].entries")
        entries = list(struct.unpack_from(f"<{count}i", bdata, pos)) if count else []
        pos += 4 * count
        buckets.append(entries)
        keys.append(_read_object(kdata, offset, f"keys[{b_idx}]"))

    if pos != len(bdata):
        raise CatalogParseError(f"Bucket table trailing bytes: consumed {pos}, total {len(bdata)}")

    declared_key_count = _read_i32(kdata, 0, "keys.count")
    if len(keys) != declared_key_count:
        raise CatalogParseError(
            f"Key count mismatch: parsed {len(keys)}, header declares {declared_key_count}"
        )

    entry_count = _read_i32(edata, 0, "entries.count")
    if entry_count < 0:
        raise CatalogParseError(f"Negative entry count: {entry_count}")
    expected_edata_len = 4 + entry_count * 28
    if len(edata) != expected_edata_len:
        raise CatalogParseError(
            f"Entry table size mismatch: expected {expected_edata_len}, got {len(edata)}"
        )

    locations: list[dict[str, Any]] = []
    unresolved_locations: list[dict[str, Any]] = []

    for n in range(entry_count):
        iid, pid, dep, dh, extra, pk, typ = struct.unpack_from("<7i", edata, 4 + n * 28)
        issues: list[str] = []

        if 0 <= iid < len(internal_ids):
            internal_id = internal_ids[iid]
        else:
            internal_id = f"<unresolved_iid:{iid}>"
            issues.append(f"invalid_internal_id_index:{iid}")

        if 0 <= pid < len(provider_ids):
            provider = provider_ids[pid]
        else:
            provider = f"<unresolved_pid:{pid}>"
            issues.append(f"invalid_provider_index:{pid}")

        if dep >= 0:
            if dep < len(buckets):
                dependency_key = keys[dep]
                dependencies = list(buckets[dep])
            else:
                dependency_key = None
                dependencies = []
                issues.append(f"invalid_dependency_bucket_index:{dep}")
        else:
            dependency_key = None
            dependencies = []

        if 0 <= pk < len(keys):
            primary_key = keys[pk]
        else:
            primary_key = None
            issues.append(f"invalid_primary_key_index:{pk}")

        if 0 <= typ < len(resource_types) and isinstance(resource_types[typ], dict):
            res_type = str(resource_types[typ].get("m_ClassName", ""))
        else:
            res_type = f"<unresolved_type:{typ}>"
            issues.append(f"invalid_resource_type_index:{typ}")

        extra_obj: Any = None
        if extra >= 0:
            try:
                extra_obj = _read_object(xdata, extra, f"extra[{n}]")
            except CatalogParseError as exc:
                issues.append(f"invalid_extra_offset:{extra}:{exc}")

        loc: dict[str, Any] = {
            "entry": n,
            "internal_id": internal_id,
            "provider": provider,
            "dependency_key": dependency_key,
            "dependencies": dependencies,
            "dependency_hash": dh,
            "primary_key": primary_key,
            "type": res_type,
            "extra": extra_obj,
        }
        locations.append(loc)
        if issues:
            unresolved_locations.append(
                {
                    "entry": n,
                    "internal_id": internal_id,
                    "provider": provider,
                    "issues": issues,
                }
            )

    remote_bundles_by_id: dict[str, dict[str, Any]] = {}
    group_counter: collections.Counter[str] = collections.Counter()
    group_bytes: collections.Counter[str] = collections.Counter()

    for loc in locations:
        iid_str = loc["internal_id"]
        if not iid_str.endswith(".bundle"):
            continue
        if iid_str in remote_bundles_by_id:
            continue
        metadata = _extract_bundle_metadata(loc["extra"])
        group_guess = _guess_bundle_group(iid_str)
        bundle_record = {
            "entry": loc["entry"],
            "internal_id": iid_str,
            "resolved_url": _resolve_bundle_url(iid_str, cdn_base),
            "group_guess": group_guess,
            "provider": loc["provider"],
            "primary_key": loc["primary_key"],
            "metadata": metadata,
        }
        remote_bundles_by_id[iid_str] = bundle_record
        group_counter[group_guess] += 1
        declared_size = (metadata or {}).get("m_BundleSize")
        if isinstance(declared_size, int) and declared_size > 0:
            group_bytes[group_guess] += declared_size

    remote_bundles = [remote_bundles_by_id[k] for k in sorted(remote_bundles_by_id)]

    script_locs = [
        loc
        for loc in locations
        if loc["internal_id"].startswith("Assets/Naninovel/Scripts/")
        and loc["internal_id"].endswith(".nani")
    ]

    scenarios: list[dict[str, Any]] = []
    script_bundles_by_id: dict[str, dict[str, Any]] = {}

    for path in sorted({loc["internal_id"] for loc in script_locs}):
        ls = [loc for loc in script_locs if loc["internal_id"] == path]
        direct_deps = sorted({dep for loc in ls for dep in loc["dependencies"]})
        all_deps = _collect_transitive_deps(direct_deps, locations)
        dep_bundles: list[dict[str, Any]] = []
        for dep_idx in sorted(all_deps):
            dep_loc = locations[dep_idx]
            dep_iid = dep_loc["internal_id"]
            if not dep_iid.endswith(".bundle"):
                continue
            bundle_entry = remote_bundles_by_id.get(dep_iid)
            if bundle_entry is None:
                bundle_entry = {
                    "entry": dep_loc["entry"],
                    "internal_id": dep_iid,
                    "resolved_url": _resolve_bundle_url(dep_iid, cdn_base),
                    "group_guess": _guess_bundle_group(dep_iid),
                    "metadata": _extract_bundle_metadata(dep_loc["extra"]),
                }
            dep_bundles.append(bundle_entry)
            script_bundles_by_id[dep_iid] = bundle_entry

        scenarios.append(
            {
                "script_id": PurePosixPath(path).stem,
                "asset_path": path,
                "location_entries": [loc["entry"] for loc in ls],
                "primary_keys": list(dict.fromkeys(loc["primary_key"] for loc in ls)),
                "types": list(dict.fromkeys(loc["type"] for loc in ls)),
                "direct_dependencies": direct_deps,
                "bundle_dependencies": dep_bundles,
            }
        )

    legacy_by_path: dict[str, dict[str, Any]] = {}
    for loc in locations:
        if "LegacyResourcesProvider" in loc["provider"]:
            iid_str = loc["internal_id"]
            if iid_str not in legacy_by_path:
                legacy_by_path[iid_str] = {
                    "entry": loc["entry"],
                    "internal_id": iid_str,
                    "provider": loc["provider"],
                    "primary_key": loc["primary_key"],
                    "type": loc["type"],
                }
    legacy_resources = [legacy_by_path[k] for k in sorted(legacy_by_path)]

    builtin_scenes: list[dict[str, Any]] = []
    seen_scenes: set[str] = set()
    for loc in locations:
        if "SceneInstance" not in loc["type"] and not loc["internal_id"].endswith(".unity"):
            continue
        all_deps = _collect_transitive_deps(loc["dependencies"], locations)
        has_remote_bundle = any(
            locations[i]["internal_id"].endswith(".bundle") for i in all_deps
        )
        if not has_remote_bundle and loc["internal_id"] not in seen_scenes:
            seen_scenes.add(loc["internal_id"])
            builtin_scenes.append(
                {
                    "entry": loc["entry"],
                    "internal_id": loc["internal_id"],
                    "provider": loc["provider"],
                    "primary_key": loc["primary_key"],
                    "type": loc["type"],
                }
            )

    remote_bundle_declared_bytes = sum(
        int((b["metadata"] or {}).get("m_BundleSize") or 0) for b in remote_bundles
    )
    script_bundle_declared_bytes = sum(
        int((b["metadata"] or {}).get("m_BundleSize") or 0)
        for b in script_bundles_by_id.values()
    )
    group_breakdown = {
        group: {
            "bundle_count": group_counter[group],
            "declared_bytes": group_bytes[group],
        }
        for group in sorted(group_counter)
    }

    summary: dict[str, Any] = {
        "platform": platform,
        "catalog_sha256": hashlib.sha256(raw).hexdigest(),
        "catalog_bytes": len(raw),
        "internal_ids": len(internal_ids),
        "keys": len(keys),
        "locations": len(locations),
        "unique_remote_bundles": len(remote_bundles),
        "remote_bundle_declared_bytes": remote_bundle_declared_bytes,
        "group_breakdown": group_breakdown,
        "script_paths": len(scenarios),
        "script_locations": len(script_locs),
        "unique_script_bundles": len(script_bundles_by_id),
        "script_bundle_declared_bytes": script_bundle_declared_bytes,
        "unmapped_scripts": [s["script_id"] for s in scenarios if not s["bundle_dependencies"]],
        "legacy_unique_paths": len(legacy_resources),
        "builtin_scene_count": len(builtin_scenes),
        "unresolved_count": len(unresolved_locations),
        "addressables_layout": addressables_layout,
    }

    return {
        "summary": summary,
        "remote_bundles": remote_bundles,
        "scenarios": scenarios,
        "legacy_resources": legacy_resources,
        "builtin_scenes": builtin_scenes,
        "unresolved_locations": unresolved_locations,
    }


def compare_with_reference(summary: dict[str, Any], ref_summary_path: Path | str) -> dict[str, Any]:
    """Compute exact numeric and hash deltas against a reference catalog summary JSON."""
    ref_path = Path(ref_summary_path)
    ref = json.loads(ref_path.read_text(encoding="utf-8"))

    numeric_keys = (
        "catalog_bytes",
        "internal_ids",
        "keys",
        "locations",
        "unique_remote_bundles",
        "remote_bundle_declared_bytes",
        "script_paths",
        "script_locations",
        "unique_script_bundles",
        "script_bundle_declared_bytes",
        "legacy_unique_paths",
        "builtin_scene_count",
        "unresolved_count",
    )

    deltas: dict[str, dict[str, int | None]] = {}
    for key in numeric_keys:
        cur_val = summary.get(key)
        ref_val = ref.get(key)
        if isinstance(cur_val, int) and isinstance(ref_val, int):
            delta: int | None = cur_val - ref_val
        else:
            delta = None
        deltas[key] = {
            "current": cur_val if isinstance(cur_val, int) else None,
            "reference": ref_val if isinstance(ref_val, int) else None,
            "delta": delta,
        }

    return {
        "reference_path": str(ref_path),
        "sha256_match": summary.get("catalog_sha256") == ref.get("catalog_sha256"),
        "current_sha256": summary.get("catalog_sha256"),
        "reference_sha256": ref.get("catalog_sha256"),
        "deltas": deltas,
    }


def write_inventory_outputs(
    snapshot_dir: Path | str,
    parsed: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Path]:
    """Write inventory and catalog graph outputs atomically into snapshot_dir."""
    out_dir = Path(snapshot_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "catalog_graph_summary.json"
    bundles_path = out_dir / "all_remote_bundles.json"
    scenarios_path = out_dir / "scenario_bundle_index.json"
    inventory_path = out_dir / "inventory.json"

    inventory_payload = {
        "meta": meta,
        "summary": parsed["summary"],
        "legacy_resources": parsed["legacy_resources"],
        "builtin_scenes": parsed["builtin_scenes"],
        "unresolved_locations": parsed["unresolved_locations"],
    }

    atomic_write_json(summary_path, parsed["summary"], indent=2)
    atomic_write_json(bundles_path, parsed["remote_bundles"], indent=2)
    atomic_write_json(scenarios_path, parsed["scenarios"], indent=2)
    atomic_write_json(inventory_path, inventory_payload, indent=2)

    return {
        "inventory": inventory_path,
        "catalog_graph_summary": summary_path,
        "all_remote_bundles": bundles_path,
        "scenario_bundle_index": scenarios_path,
    }