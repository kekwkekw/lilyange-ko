#!/usr/bin/env python3
"""Unified CLI for the Lilianze (LilyAnge) Full-Asset Addressables & Naninovel Crawler.

Subcommands:
  catalog         Fetch & parse official Addressables catalog (WebGL or Android) and save snapshot/inventory.
  crawl           Download & extract remote bundles (--mode full|scenarios|ui|samples|pattern).
  extract-apk     Extract built-in Unity assets from an Android base.apk (non-remote UI/config supply channel).
  coverage        Compare extracted Naninovel/UI/structured assets against lilyange-ko CDN dictionaries.
  verify-samples  Verify exact reproduction of the 2026-11-26 handoff sample bundles.

All raw bundles, state, extractions, and reports stay under --work-dir (outside Git repos)
to preserve the lilyange-ko Zero Game Assets policy.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import catalog_parser as cp
import coverage as cov
import downloader as dl
import extractor as ext


DEFAULT_WORK_DIR = Path("D:/lily_workspace/crawler_work")
DEFAULT_LILYTRANS_DIR = Path("D:/lily_workspace/lilytrans")
DEFAULT_HANDOFF_REF = Path(
    "D:/lily_workspace/handoff_20261126/LilyAnge_AG_Handoff_20261126/metadata/catalog_graph_summary.json"
)


def _find_latest_snapshot_dir(work_dir: Path, platform: str) -> Path:
    base = work_dir / "snapshots" / platform
    if not base.exists():
        raise FileNotFoundError(f"No snapshots found under {base}. Run 'catalog' first.")
    candidates = [p for p in base.iterdir() if p.is_dir() and (p / "all_remote_bundles.json").exists()]
    if not candidates:
        raise FileNotFoundError(f"No parsed snapshot with all_remote_bundles.json under {base}.")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _load_bundles_from_snapshot(snap_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw_bundles = json.loads((snap_dir / "all_remote_bundles.json").read_text(encoding="utf-8"))
    bundles = raw_bundles["bundles"] if isinstance(raw_bundles, dict) and "bundles" in raw_bundles else raw_bundles
    summary = json.loads((snap_dir / "catalog_graph_summary.json").read_text(encoding="utf-8"))
    return bundles, summary


def _select_bundles(bundles: list[dict[str, Any]], mode: str, patterns: list[str] | None) -> list[dict[str, Any]]:
    if mode == "full":
        selected = list(bundles)
    elif mode == "scenarios":
        selected = [b for b in bundles if b.get("group_guess") == "naninovelseparate_scripts"]
    elif mode == "ui":
        selected = [b for b in bundles if "naninovelui" in str(b.get("internal_id", "")).lower()]
    elif mode == "samples":
        selected = [
            b for b in bundles
            if any(
                tok in str(b.get("internal_id", ""))
                for tok in (
                    "/scripts/10101_",
                    "/scripts/chara1601_101_",
                    "/scripts/adv_init_",
                    "naninovel_assets_naninovelui_",
                    "10050.asset_",
                    "ギャグ_",
                    "systemvoice1013_",
                )
            )
        ]
    elif mode == "pattern":
        selected = list(bundles)
    else:
        raise ValueError(f"Unknown mode: {mode}")

    if patterns:
        low_pats = [p.lower() for p in patterns]
        selected = [
            b for b in selected
            if any(p in str(b.get("internal_id", "")).lower() or p in str(b.get("group_guess", "")).lower() for p in low_pats)
        ]
    return selected


def cmd_catalog(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    snap = cp.fetch_snapshot(
        work_dir,
        args.platform,
        offline_catalog=args.catalog_file,
        cdn_base=args.cdn_base,
    )
    snap_dir = Path(snap["snapshot_dir"])
    raw_path = Path(snap["catalog_path"])
    parsed = cp.parse_catalog_bytes(raw_path.read_bytes(), snap["cdn_base"], args.platform)
    cp.write_inventory_outputs(snap_dir, parsed, snap)

    result: dict[str, Any] = {
        "snapshot_dir": str(snap_dir),
        "platform": args.platform,
        "observed_at_utc": snap.get("observed_at_utc"),
        "catalog_hash_token": snap.get("catalog_hash_token"),
        "summary": parsed["summary"],
    }
    if args.reference_summary and Path(args.reference_summary).exists():
        result["reference_diff"] = cp.compare_with_reference(parsed["summary"], Path(args.reference_summary))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_crawl(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    if args.refresh_catalog:
        snap = cp.fetch_snapshot(work_dir, args.platform, cdn_base=args.cdn_base)
        snap_dir = Path(snap["snapshot_dir"])
        parsed = cp.parse_catalog_bytes(Path(snap["catalog_path"]).read_bytes(), snap["cdn_base"], args.platform)
        cp.write_inventory_outputs(snap_dir, parsed, snap)
    else:
        snap_dir = _find_latest_snapshot_dir(work_dir, args.platform)

    bundles, summary = _load_bundles_from_snapshot(snap_dir)
    selected = _select_bundles(bundles, args.mode, args.pattern)
    declared_total = sum(int((b.get("metadata") or {}).get("m_BundleSize") or 0) for b in selected)

    print(
        json.dumps(
            {
                "phase": "selection",
                "platform": args.platform,
                "mode": args.mode,
                "snapshot_dir": str(snap_dir),
                "total_catalog_bundles": len(bundles),
                "selected_bundles": len(selected),
                "selected_declared_bytes": declared_total,
                "full_catalog_scope": (args.mode == "full" and not args.pattern and args.max_items == 0),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    if not args.acquire and not args.extract_only:
        return 0

    dl_report: dict[str, Any] | None = None
    state_items: list[dict[str, Any]] = []

    if args.extract_only:
        state_dir = work_dir / "state" / args.platform / "assets"
        selected_ids = {str(b.get("internal_id") or "") for b in selected}
        if state_dir.exists():
            for p in state_dir.rglob("*.json"):
                if p.parent.name == "history":
                    continue
                try:
                    st = json.loads(p.read_text(encoding="utf-8"))
                    if st.get("internal_id") in selected_ids and st.get("status") == "ACQUIRED":
                        state_items.append(st)
                except Exception:
                    continue
    else:
        dl_report = dl.download_inventory(
            work_dir=work_dir,
            platform=args.platform,
            bundles=selected,
            snapshot_meta={"catalog_sha256": summary.get("catalog_sha256"), "platform": args.platform},
            workers=args.workers,
            max_retries=args.max_retries,
            timeout=args.timeout,
            cancel_file=args.cancel_file,
            integrity=args.integrity,
            max_items=args.max_items,
        )
        print(
            json.dumps(
                {
                    "phase": "download",
                    "status": dl_report.get("status"),
                    "elapsed_seconds": dl_report.get("elapsed_seconds"),
                    "summary": dl_report.get("summary"),
                    "size_mismatches": len(dl_report.get("size_mismatches") or []),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        # Load acquired state items for selected bundles
        state_dir = work_dir / "state" / args.platform / "assets"
        selected_ids = {str(b.get("internal_id") or "") for b in selected}
        if state_dir.exists():
            for p in state_dir.rglob("*.json"):
                if p.parent.name == "history":
                    continue
                try:
                    st = json.loads(p.read_text(encoding="utf-8"))
                    if st.get("internal_id") in selected_ids and st.get("status") == "ACQUIRED":
                        state_items.append(st)
                except Exception:
                    continue

    if args.no_extract:
        return 0 if (dl_report is None or dl_report.get("status") == "COMPLETED") else 2

    ext_report = ext.extract_all(
        work_dir=work_dir,
        platform=args.platform,
        state_items=state_items,
        workers=args.extract_workers,
        force=args.force_extract,
        retry_failures=args.retry_extraction_failures,
        cancel_file=args.cancel_file,
    )
    print(
        json.dumps(
            {
                "phase": "extract",
                "selected_acquired": len(state_items),
                "report": {k: v for k, v in ext_report.items() if k != "items"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


def cmd_extract_apk(args: argparse.Namespace) -> int:
    rep = ext.extract_apk_builtin(args.apk, args.work_dir)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    rep = cov.run_coverage_diff(
        work_dir=args.work_dir,
        platform=args.platform,
        lilytrans_dir=args.lilytrans_dir,
        registry_zip=args.registry_zip,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_samples(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir)
    snap_dir = _find_latest_snapshot_dir(work_dir, args.platform)
    bundles, summary = _load_bundles_from_snapshot(snap_dir)
    selected = _select_bundles(bundles, "samples", None)
    dl_rep = dl.download_inventory(
        work_dir=work_dir,
        platform=args.platform,
        bundles=selected,
        snapshot_meta={"catalog_sha256": summary.get("catalog_sha256")},
        workers=4,
        integrity=True,
    )
    state_dir = work_dir / "state" / args.platform / "assets"
    selected_ids = {str(b.get("internal_id") or "") for b in selected}
    state_items = []
    for p in state_dir.rglob("*.json"):
        if p.parent.name == "history":
            continue
        st = json.loads(p.read_text(encoding="utf-8"))
        if st.get("internal_id") in selected_ids and st.get("status") == "ACQUIRED":
            state_items.append(st)

    ext_rep = ext.extract_all(
        work_dir=work_dir,
        platform=args.platform,
        state_items=state_items,
        workers=2,
        force=args.force_extract,
    )
    details = []
    for st in sorted(state_items, key=lambda x: x["internal_id"]):
        sha = st["raw_sha256"]
        edir = work_dir / "extracted" / sha[:2] / sha / ext.EXTRACTOR_VERSION
        meta = json.loads((edir / "meta.json").read_text(encoding="utf-8")) if (edir / "meta.json").exists() else {}
        nani_lines = (
            [json.loads(line) for line in (edir / "naninovel.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if (edir / "naninovel.jsonl").exists()
            else []
        )
        str_lines = (
            [json.loads(line) for line in (edir / "strings.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if (edir / "strings.jsonl").exists()
            else []
        )
        obj_lines = (
            [json.loads(line) for line in (edir / "objects.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            if (edir / "objects.jsonl").exists()
            else []
        )
        cls_counts: dict[str, int] = {}
        for o in obj_lines:
            c = o.get("classification") or "unknown"
            cls_counts[c] = cls_counts.get(c, 0) + 1
        print_cmds = [n for n in nani_lines if "PrintText" in str(n.get("command_class", ""))]
        choice_cmds = [n for n in nani_lines if "AddChoice" in str(n.get("command_class", ""))]
        details.append(
            {
                "internal_id": st["internal_id"],
                "actual_bytes": st["actual_bytes"],
                "raw_sha256": sha,
                "extract_status": meta.get("status"),
                "object_classifications": cls_counts,
                "naninovel_total": len(nani_lines),
                "print_text_total": len(print_cmds),
                "print_text_dynamic": sum(
                    1 for n in print_cmds
                    if bool((n.get("text") or {}).get("dynamicValue_ValueText"))
                ),
                "add_choice_total": len(choice_cmds),
                "strings_total": len(str_lines),
                "ui_candidates": sum(1 for s in str_lines if s.get("classification") == "ui_candidate" and not s.get("is_empty", False)),
                "ui_candidates_jp": sum(1 for s in str_lines if s.get("classification") == "ui_candidate" and s.get("has_japanese")),
                "failures": meta.get("failure_count", 0),
            }
        )
    print(json.dumps({"download": dl_rep["summary"], "samples": details}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Lilianze (LilyAnge) Full-Asset Crawler")
    p.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR), help="Local non-Git workspace for raw/state/extracted/reports")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("catalog", help="Fetch and parse official Addressables catalog")
    pc.add_argument("--platform", choices=["WebGL", "Android"], default="WebGL")
    pc.add_argument("--catalog-file", default=None, help="Offline catalog JSON path for replay")
    pc.add_argument("--cdn-base", default="https://dok6uc0hyhl8f.cloudfront.net")
    pc.add_argument("--reference-summary", default=str(DEFAULT_HANDOFF_REF))
    pc.set_defaults(func=cmd_catalog)

    pcr = sub.add_parser("crawl", help="Download and extract remote bundles")
    pcr.add_argument("--platform", choices=["WebGL", "Android"], default="WebGL")
    pcr.add_argument("--mode", choices=["full", "scenarios", "ui", "samples", "pattern"], default="full")
    pcr.add_argument("--pattern", action="append", default=[], help="Substring filter on internal_id/group")
    pcr.add_argument("--acquire", action="store_true", help="Actually download raw bundles (omit for dry-run selection)")
    pcr.add_argument("--extract-only", action="store_true", help="Skip network download; extract already acquired raw bundles")
    pcr.add_argument("--no-extract", action="store_true", help="Download raw only without running UnityPy extraction")
    pcr.add_argument("--refresh-catalog", action="store_true", help="Fetch fresh catalog before crawling")
    pcr.add_argument("--cdn-base", default="https://dok6uc0hyhl8f.cloudfront.net")
    pcr.add_argument("--workers", type=int, default=4, help="Download workers")
    pcr.add_argument("--extract-workers", type=int, default=2, help="Extraction workers")
    pcr.add_argument("--max-retries", type=int, default=3)
    pcr.add_argument("--timeout", type=int, default=60)
    pcr.add_argument("--max-items", type=int, default=0, help="0 = unlimited")
    pcr.add_argument("--cancel-file", default=None)
    pcr.add_argument("--integrity", action="store_true")
    pcr.add_argument("--force-extract", action="store_true")
    pcr.add_argument("--retry-extraction-failures", action="store_true")
    pcr.set_defaults(func=cmd_crawl)

    pa = sub.add_parser("extract-apk", help="Extract built-in Unity assets from an Android base.apk")
    pa.add_argument("--apk", required=True)
    pa.set_defaults(func=cmd_extract_apk)

    pv = sub.add_parser("coverage", help="Compare extracted assets against lilyange-ko CDN")
    pv.add_argument("--platform", choices=["WebGL", "Android"], default="WebGL")
    pv.add_argument("--lilytrans-dir", default=str(DEFAULT_LILYTRANS_DIR))
    pv.add_argument("--registry-zip", default=None)
    pv.set_defaults(func=cmd_coverage)

    ps = sub.add_parser("verify-samples", help="Verify exact reproduction of handoff samples")
    ps.add_argument("--platform", choices=["WebGL", "Android"], default="WebGL")
    ps.add_argument("--force-extract", action="store_true")
    ps.set_defaults(func=cmd_verify_samples)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
