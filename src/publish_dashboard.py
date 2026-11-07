#!/usr/bin/env python3
"""将数据库生成的数据安全发布到既有 WorkBuddy 固定 Page 节点。

只替换 snapshot/lingyang 两段业务数据，保留线上 app、平台注入和页面壳。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SKILL_DIR = Path(os.environ.get("WORKBUDDY_LIBRARY_SKILL_DIR", "library-skill-not-configured"))
NODE_ID = ""
ENTRY_HTML = "dashboard-online.html"
SCRIPT_PATTERN = re.compile(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>")


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True)
    output = result.stdout.strip()
    if result.returncode != 0 or not output or output.startswith('{"error"'):
        message = output or result.stderr.strip() or "命令执行失败"
        raise RuntimeError(message)
    return output


def script_content(html: str, marker: str) -> str:
    matches = [match.group(1) for match in SCRIPT_PATTERN.finditer(html) if marker in match.group(1)]
    if len(matches) != 1:
        raise ValueError(f"脚本标记不唯一: {marker}，实际 {len(matches)} 段")
    return matches[0]


def replace_script(html: str, marker: str, content: str) -> str:
    matches = [match for match in SCRIPT_PATTERN.finditer(html) if marker in match.group(1)]
    if len(matches) != 1:
        raise ValueError(f"线上脚本标记不唯一: {marker}")
    match = matches[0]
    return html[:match.start(1)] + "\n" + content.strip() + "\n" + html[match.end(1):]


def mask_business_scripts(html: str) -> str:
    for marker, placeholder in (("window.__DASH__ =", "__SNAPSHOT__"), ("window.__LY__ =", "__LINGYANG__")):
        matches = [match for match in SCRIPT_PATTERN.finditer(html) if marker in match.group(1)]
        if len(matches) != 1:
            raise ValueError(f"页面壳检查失败: {marker}")
        match = matches[0]
        html = html[:match.start(1)] + placeholder + html[match.end(1):]
    return html


def payload_from_script(script: str, variable: str) -> dict[str, Any]:
    match = re.search(rf"window\.{re.escape(variable)}\s*=\s*", script)
    if not match:
        raise ValueError(f"缺少 window.{variable}")
    return json.loads(script[match.end():].strip().rstrip(";"))


def business_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(payload))
    if isinstance(result.get("meta"), dict):
        result["meta"].pop("pipelineGeneratedAt", None)
    return result


def merge(baseline: str, local: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    snapshot_script = script_content(local, "window.__DASH__ =")
    lingyang_script = script_content(local, "window.__LY__ =")
    snapshot = payload_from_script(snapshot_script, "__DASH__")
    lingyang = payload_from_script(lingyang_script, "__LY__")
    old_snapshot = payload_from_script(script_content(baseline, "window.__DASH__ ="), "__DASH__")
    old_lingyang = payload_from_script(script_content(baseline, "window.__LY__ ="), "__LY__")
    unchanged = (
        business_payload(snapshot) == business_payload(old_snapshot)
        and business_payload(lingyang) == business_payload(old_lingyang)
    )
    merged = replace_script(baseline, "window.__DASH__ =", snapshot_script)
    merged = replace_script(merged, "window.__LY__ =", lingyang_script)
    if mask_business_scripts(merged) != mask_business_scripts(baseline):
        raise ValueError("双向差异检查失败：页面壳或线上功能脚本发生变化")
    for marker in ("data-page-node-id", "aovCrc", "renderUserBreak"):
        if marker in baseline and marker not in merged:
            raise ValueError(f"线上功能丢失: {marker}")
    return merged, snapshot, {"unchanged": unchanged, "lingyang_products": len(lingyang.get("products") or [])}


def publish(config: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    node_id = str(config.get("page_node_id") or NODE_ID).strip()
    if not node_id or node_id.startswith("REPLACE_"):
        raise ValueError("请在本地 pipeline_config.json 配置既有 page_node_id")
    if not SKILL_DIR.is_dir():
        raise ValueError("请通过 WORKBUDDY_LIBRARY_SKILL_DIR 指定 WorkBuddy library 技能目录")
    local_path = Path(config["dashboard_dir"]) / ENTRY_HTML
    local = local_path.read_text(encoding="utf-8")
    run_root = Path(config["state_dir"]) / "publish-runs"
    run_root.mkdir(parents=True, exist_ok=True)
    work_dir = run_root / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    baseline_dir = work_dir / ".baseline"
    baseline_dir.mkdir(parents=True, exist_ok=False)
    opened = run([sys.executable, str(SKILL_DIR / "page/create_page_transaction.py"), "--node-id", node_id])
    if not opened.startswith("KS_PAGE_TX_OPEN "):
        raise RuntimeError("未能开启页面事务")
    transaction = json.loads(opened[len("KS_PAGE_TX_OPEN "):])
    if transaction.get("html") != [ENTRY_HTML]:
        raise ValueError(f"固定页面入口异常: {transaction.get('html')}")
    artifact_url = transaction["url"] + ENTRY_HTML
    with urllib.request.urlopen(artifact_url, timeout=60) as response:
        baseline_bytes = response.read()
    baseline_path = baseline_dir / ENTRY_HTML
    baseline_path.write_bytes(baseline_bytes)
    working_path = work_dir / ENTRY_HTML
    working_path.write_bytes(baseline_bytes)
    merged, snapshot, merge_report = merge(baseline_bytes.decode("utf-8"), local)
    data_end = snapshot["meta"]["dataRange"]["end"]
    if merge_report["unchanged"]:
        return {"status": "skipped_no_change", "node_id": node_id, "data_end": data_end}
    working_path.write_text(merged, encoding="utf-8")
    if dry_run:
        return {
            "status": "dry_run_pass", "node_id": node_id, "data_end": data_end,
            "base_version": transaction["baseVersion"], **merge_report,
        }
    run([
        sys.executable, str(SKILL_DIR / "page/upload_page_artifact.py"),
        "--transaction-id", transaction["transactionId"], "--path", ENTRY_HTML,
        "--file", str(working_path),
    ])
    run([
        sys.executable, str(SKILL_DIR / "page/commit_page_transaction.py"),
        "--transaction-id", transaction["transactionId"],
        "--message", f"同步数据库经营数据至{data_end}", "--cleanup-dir", str(work_dir),
    ])
    published = run([sys.executable, str(SKILL_DIR / "page/publish_page.py"), "--node-id", node_id])
    publish_url = ""
    for line in published.splitlines():
        if line.startswith("KS_PAGE_PUBLISH\t"):
            publish_url = line.split("\t", 1)[1].strip()
    verify_dir = run_root / ("verify-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    run([
        sys.executable, str(SKILL_DIR / "page/download_page_artifacts.py"),
        "--node-id", node_id, "--source", "publish", "--out-dir", str(verify_dir),
        "--path", ENTRY_HTML,
    ])
    published_html = (verify_dir / ENTRY_HTML).read_text(encoding="utf-8")
    published_snapshot = payload_from_script(script_content(published_html, "window.__DASH__ ="), "__DASH__")
    if published_snapshot["meta"]["dataRange"]["end"] != data_end:
        raise ValueError("发布回读截止日不一致")
    for marker in ("data-page-node-id", "aovCrc", "renderUserBreak"):
        if marker not in published_html:
            raise ValueError(f"发布回读缺少功能标记: {marker}")
    return {
        "status": "published", "node_id": node_id, "data_end": data_end,
        "publish_url": publish_url, "edit_url": f"https://www.workbuddy.cn/space/d/{node_id}",
        "verified": True, **merge_report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "pipeline_config.json"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        result = publish(config, args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, KeyError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
