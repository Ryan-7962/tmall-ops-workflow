#!/usr/bin/env python3
"""天猫经营数据底座：增量入库、幂等去重、数据库驱动看板导出。

千问只负责数据文件；本脚本负责固定校验、入库和 HTML 生成。
默认命令会读取 pipeline_config.json，不向千问目录写入任何内容。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "pipeline_config.json"
STORE_METRICS = (
    "uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt",
    "rivalUv", "rivalPayAmt", "rivalPayByrCnt",
)
WXT_METRICS = ("charge", "payAmt", "click", "impression", "cart", "ordCnt")
SKU_METRICS = ("payAmt", "payByr", "payItm", "cart", "crtAmt")
CATEGORY_METRICS = {
    "uv": "uv",
    "payment_amount": "amt",
    "payment_buyers": "byr",
    "successful_refund_amount": "refund",
    "cart_users": "cart",
}
STANDARD_KEYS = {
    "store_day": ("date",),
    "product_day": ("date", "product_id"),
    "promotion_channel_day": ("date", "channel"),
    "promotion_product_day": ("date", "channel", "product_id"),
    "promotion_campaign_day": ("date", "channel", "campaign_id"),
    "promotion_unit_day": ("date", "channel", "campaign_id", "unit_id"),
    "shop_flow_day": ("date", "attribution", "channel_id", "product_id"),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    source_type TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_payloads (
    source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lingyang_payloads (
    source_file_id INTEGER PRIMARY KEY REFERENCES source_files(id),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_daily_facts (
    date TEXT NOT NULL,
    scope TEXT NOT NULL,
    uv REAL, pv REAL, payAmt REAL, payByrCnt REAL, itmUv REAL, cartByrCnt REAL,
    rivalUv REAL, rivalPayAmt REAL, rivalPayByrCnt REAL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, scope)
);
CREATE TABLE IF NOT EXISTS wxt_daily_facts (
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    attribution_scope TEXT NOT NULL,
    charge REAL, payAmt REAL, click REAL, impression REAL, cart REAL, ordCnt REAL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, channel, attribution_scope)
);
CREATE TABLE IF NOT EXISTS wxt_daily_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    channel TEXT NOT NULL,
    attribution_scope TEXT NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE (date, channel, attribution_scope, source_file_id)
);
CREATE TABLE IF NOT EXISTS window_snapshots (
    window_key TEXT NOT NULL,
    window_end TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (window_key, window_end)
);
CREATE TABLE IF NOT EXISTS standard_facts (
    domain TEXT NOT NULL,
    caliber TEXT NOT NULL,
    date TEXT NOT NULL,
    business_key TEXT NOT NULL,
    row_json TEXT NOT NULL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (domain, caliber, date, business_key)
);
CREATE TABLE IF NOT EXISTS category_daily_facts (
    date TEXT NOT NULL,
    category_l1 TEXT NOT NULL,
    uv REAL, amt REAL, byr REAL, refund REAL, cart REAL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, category_l1)
);
CREATE TABLE IF NOT EXISTS sku_daily_facts (
    date TEXT NOT NULL,
    item_id TEXT NOT NULL,
    sku_id TEXT NOT NULL,
    sku_name TEXT NOT NULL,
    item_name TEXT NOT NULL,
    payAmt REAL, payByr REAL, payItm REAL, cart REAL, crtAmt REAL,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    row_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, item_id, sku_id, sku_name)
);
CREATE INDEX IF NOT EXISTS idx_standard_facts_domain_date ON standard_facts(domain, date);
CREATE INDEX IF NOT EXISTS idx_sku_daily_item ON sku_daily_facts(item_id, date);
CREATE TABLE IF NOT EXISTS product_master (
    product_id TEXT PRIMARY KEY,
    title TEXT,
    img TEXT,
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_js(path: Path, variable: str) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^[ \t]*window\.{re.escape(variable)}\s*=\s*", text, re.MULTILINE)
    if not match:
        raise ValueError(f"{path} 中未找到 window.{variable}")
    payload = json.loads(text[match.end():].strip().rstrip(";"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 根节点必须是对象")
    return payload


def dump_js(path: Path, variable: str, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        f"window.{variable} = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    return connection


def register_file(connection: sqlite3.Connection, path: Path, source_type: str) -> tuple[int, bool]:
    sha256 = file_hash(path)
    existing = connection.execute("SELECT id FROM source_files WHERE sha256 = ?", (sha256,)).fetchone()
    if existing:
        return int(existing["id"]), False
    cursor = connection.execute(
        "INSERT INTO source_files(path, file_name, sha256, size_bytes, source_type, ingested_at) VALUES (?,?,?,?,?,?)",
        (str(path), path.name, sha256, path.stat().st_size, source_type, now()),
    )
    return int(cursor.lastrowid), True


def valid_date(value: Any) -> str:
    result = str(value or "")
    dt.date.fromisoformat(result)
    return result


def number(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def upsert(
    connection: sqlite3.Connection,
    table: str,
    keys: dict[str, Any],
    values: dict[str, Any],
    source_file_id: int,
    payload: dict[str, Any],
    stats: dict[str, int],
) -> None:
    if table not in {"store_daily_facts", "wxt_daily_facts", "window_snapshots", "standard_facts", "category_daily_facts", "sku_daily_facts"}:
        raise ValueError("未允许的数据表")
    where = " AND ".join(f"{key} = ?" for key in keys)
    existing = connection.execute(
        f"SELECT row_hash FROM {table} WHERE {where}", tuple(keys.values()),
    ).fetchone()
    row_hash = digest(payload)
    if existing and existing["row_hash"] == row_hash:
        stats["unchanged"] += 1
        return
    record = {**keys, **values, "source_file_id": source_file_id, "row_hash": row_hash, "updated_at": now()}
    if existing:
        assignments = ", ".join(f"{key} = ?" for key in record)
        connection.execute(
            f"UPDATE {table} SET {assignments} WHERE {where}",
            (*record.values(), *keys.values()),
        )
        stats["updated"] += 1
    else:
        columns = ", ".join(record)
        placeholders = ",".join("?" for _ in record)
        connection.execute(
            f"INSERT INTO {table}({columns}) VALUES ({placeholders})", tuple(record.values()),
        )
        stats["inserted"] += 1


def check_aligned(block: dict[str, Any], metrics: Iterable[str], name: str) -> None:
    dates = block.get("dates") or []
    if not dates or len(dates) != len(set(dates)):
        raise ValueError(f"{name}.dates 为空或存在重复日期")
    for day in dates:
        valid_date(day)
    for metric in metrics:
        series = block.get(metric)
        if series is not None and (not isinstance(series, list) or len(series) != len(dates)):
            raise ValueError(f"{name}.{metric} 与 dates 长度不一致")


def ingest_snapshot(connection: sqlite3.Connection, path: Path, config: dict[str, Any]) -> dict[str, Any]:
    payload = load_js(path, "__DASH__")
    source_id, fresh = register_file(connection, path, "snapshot")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    connection.execute("INSERT INTO snapshot_payloads VALUES (?,?)", (source_id, canonical(payload)))
    stats = {"store": defaultdict(int), "wxt": defaultdict(int), "windows": defaultdict(int)}
    scope = config["store_scope"]
    for block_name in ("daily", "dailyLY"):
        block = payload.get(block_name) or {}
        check_aligned(block, STORE_METRICS, block_name)
        for index, day in enumerate(block["dates"]):
            row = {metric: number((block.get(metric) or [None] * len(block["dates"]))[index]) for metric in STORE_METRICS}
            upsert(connection, "store_daily_facts", {"date": day, "scope": scope}, row, source_id, row, stats["store"])
    wxt = payload.get("wxt") or {}
    check_aligned(wxt, WXT_METRICS, "wxt")
    for index, day in enumerate(wxt["dates"]):
        row = {metric: number((wxt.get(metric) or [None] * len(wxt["dates"]))[index]) for metric in WXT_METRICS}
        keys = {"date": day, "channel": config["wxt_channel"], "attribution_scope": config["wxt_attribution_scope"]}
        upsert(connection, "wxt_daily_facts", keys, row, source_id, row, stats["wxt"])
        connection.execute(
            "INSERT INTO wxt_daily_revisions(date, channel, attribution_scope, source_file_id, observed_at, payload_json) VALUES (?,?,?,?,?,?)",
            (day, config["wxt_channel"], config["wxt_attribution_scope"], source_id, now(), canonical(row)),
        )
    for window_key in config["required_windows"]:
        window = (payload.get("windows") or {}).get(window_key)
        if not isinstance(window, dict):
            raise ValueError(f"缺少窗口 {window_key}")
        end = valid_date((window.get("cur") or {}).get("end"))
        upsert(
            connection, "window_snapshots", {"window_key": window_key, "window_end": end},
            {"payload_json": canonical(window)}, source_id, window, stats["windows"],
        )
    return {
        "file": path.name, "source_file_id": source_id,
        "data_end": payload["meta"]["dataRange"]["end"],
        "counts": {key: dict(value) for key, value in stats.items()},
    }


def ingest_lingyang(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    payload = load_js(path, "__LY__")
    source_id, fresh = register_file(connection, path, "lingyang")
    if fresh:
        connection.execute("INSERT INTO lingyang_payloads VALUES (?,?)", (source_id, canonical(payload)))
    return {"file": path.name, "skipped_duplicate_file": not fresh, "source_file_id": source_id}


def ingest_categories(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    source_id, fresh = register_file(connection, path, "lingyang_category_day")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    stats = defaultdict(int)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            day = valid_date(source["date"])
            category = str(source["cate_level1_name"] or "").strip()
            if not category:
                raise ValueError(f"{path.name} 存在空一级类目")
            row = {target: number(float(source[key])) for key, target in CATEGORY_METRICS.items()}
            upsert(connection, "category_daily_facts", {"date": day, "category_l1": category}, row, source_id, row, stats)
    return {"file": path.name, "source_file_id": source_id, "counts": dict(stats)}


def normalize_id(value: Any) -> str:
    if isinstance(value, float):
        return str(int(round(value)))
    return str(value or "").strip()


def ingest_sku(connection: sqlite3.Connection, path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    source_id, fresh = register_file(connection, path, "lingyang_sku_day")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path.name} 缺少 SKU 实际数据行")
    stats = defaultdict(int)
    for source in rows:
        day = valid_date(source.get("date"))
        item_id = normalize_id(source.get("itemId"))
        if not item_id:
            raise ValueError(f"{path.name} 存在空商品 ID")
        row = {metric: number(source.get(metric)) for metric in SKU_METRICS}
        keys = {
            "date": day, "item_id": item_id, "sku_id": normalize_id(source.get("skuId")),
            "sku_name": str(source.get("skuName") or "").strip(),
        }
        values = {**row, "item_name": str(source.get("itemName") or "").strip()}
        upsert(connection, "sku_daily_facts", keys, values, source_id, {**keys, **values}, stats)
    return {"file": path.name, "source_file_id": source_id, "counts": dict(stats)}


def cell_number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ingest_incremental_store(connection: sqlite3.Connection, path: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_id, fresh = register_file(connection, path, "incremental_store_day")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    stats = defaultdict(int)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            day = valid_date(source.get("date"))
            row = {
                "uv": cell_number(source.get("uv")),
                "pv": cell_number(source.get("pv")),
                "payAmt": cell_number(source.get("payment_amount", source.get("payAmt"))),
                "payByrCnt": cell_number(source.get("payment_buyers", source.get("payByrCnt"))),
                "itmUv": cell_number(source.get("product_visitors", source.get("itmUv"))),
                "cartByrCnt": cell_number(source.get("cart_users", source.get("cartByrCnt"))),
                "rivalUv": cell_number(source.get("rival_uv", source.get("rivalUv"))),
                "rivalPayAmt": cell_number(source.get("rival_payment_amount", source.get("rivalPayAmt"))),
                "rivalPayByrCnt": cell_number(source.get("rival_payment_buyers", source.get("rivalPayByrCnt"))),
            }
            if any(row[key] is None for key in ("uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt")):
                raise ValueError(f"{path.name} {day} 缺少店铺核心指标；不得用0代替")
            scope = str(source.get("scope") or config["store_scope"])
            upsert(connection, "store_daily_facts", {"date": day, "scope": scope}, row, source_id, row, stats)
    return {"file": path.name, "counts": dict(stats)}


def ingest_incremental_wxt(connection: sqlite3.Connection, path: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_id, fresh = register_file(connection, path, "incremental_wxt_day")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    stats = defaultdict(int)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            day = valid_date(source.get("date"))
            row = {
                "charge": cell_number(source.get("ad_spend", source.get("charge"))),
                "payAmt": cell_number(source.get("attributed_payment_amount", source.get("payAmt"))),
                "click": cell_number(source.get("clicks", source.get("click"))),
                "impression": cell_number(source.get("impressions", source.get("impression"))),
                "cart": cell_number(source.get("cart_adds", source.get("cart"))),
                "ordCnt": cell_number(source.get("total_orders", source.get("ordCnt"))),
            }
            if row["charge"] is None or row["payAmt"] is None:
                raise ValueError(f"{path.name} {day} 缺少消耗或归因成交；不得用0代替")
            keys = {
                "date": day,
                "channel": str(source.get("channel") or config["wxt_channel"]),
                "attribution_scope": str(source.get("attribution_scope") or config["wxt_attribution_scope"]),
            }
            upsert(connection, "wxt_daily_facts", keys, row, source_id, row, stats)
            connection.execute(
                "INSERT INTO wxt_daily_revisions(date, channel, attribution_scope, source_file_id, observed_at, payload_json) VALUES (?,?,?,?,?,?)",
                (day, keys["channel"], keys["attribution_scope"], source_id, now(), canonical(row)),
            )
    return {"file": path.name, "counts": dict(stats)}


def ingest_incremental_window(connection: sqlite3.Connection, path: Path, window_key: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    source_id, fresh = register_file(connection, path, "incremental_window")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    end = valid_date((payload.get("cur") or {}).get("end"))
    stats = defaultdict(int)
    upsert(
        connection, "window_snapshots", {"window_key": window_key, "window_end": end},
        {"payload_json": canonical(payload)}, source_id, payload, stats,
    )
    return {"file": path.name, "window": window_key, "counts": dict(stats)}


def ingest_incremental(connection: sqlite3.Connection, inbox: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    if not inbox.is_dir():
        return []
    reports = []
    for path in sorted(inbox.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("store_day_") and path.suffix == ".csv":
            reports.append(ingest_incremental_store(connection, path, config))
        elif path.name.startswith("wxt_day_") and path.suffix == ".csv":
            reports.append(ingest_incremental_wxt(connection, path, config))
        elif path.name.startswith("window_") and path.suffix == ".json":
            match = re.match(r"window_(1d|3d|7d|14d|30d)_", path.name)
            if not match:
                raise ValueError(f"窗口文件名不符合 window_<窗口>_<截止日>.json: {path.name}")
            reports.append(ingest_incremental_window(connection, path, match.group(1)))
        elif path.name.startswith("lingyang_category_day_") and "LEAF" not in path.name and path.suffix == ".csv":
            reports.append(ingest_categories(connection, path))
        elif path.name.startswith("lingyang-sku-") and path.suffix == ".json":
            reports.append(ingest_sku(connection, path))
    return reports


def ingest_standard_package(connection: sqlite3.Connection, package: Path) -> list[dict[str, Any]]:
    manifest_path = package / "manifest.json"
    if not manifest_path.is_file():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("files") if isinstance(manifest, dict) else manifest
    if not isinstance(entries, list):
        raise ValueError("manifest.json 缺少 files 数组")
    reports = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "passed":
            continue
        domain = str(entry.get("domain") or "")
        if domain not in STANDARD_KEYS:
            continue
        relative = str(entry.get("path") or "")
        path = (package / relative).resolve()
        if package.resolve() not in path.parents or not path.is_file():
            raise ValueError(f"manifest 文件路径无效: {relative}")
        content = path.read_bytes()
        expected = str(entry.get("sha256") or "").lower()
        if expected and hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"manifest 哈希不一致: {relative}")
        source_id, fresh = register_file(connection, path, domain)
        if not fresh:
            reports.append({"file": relative, "skipped_duplicate_file": True})
            continue
        rows = list(csv.DictReader(content.decode("utf-8-sig").splitlines()))
        stats = defaultdict(int)
        seen = set()
        caliber = str(entry.get("caliber") or entry.get("source_tool") or "default")
        for row in rows:
            day = valid_date(row.get("date"))
            business_key = canonical([row.get(key) for key in STANDARD_KEYS[domain][1:]])
            unique_key = (day, business_key)
            if unique_key in seen:
                raise ValueError(f"{relative} 存在重复业务键: {day} {business_key}")
            seen.add(unique_key)
            keys = {"domain": domain, "caliber": caliber, "date": day, "business_key": business_key}
            upsert(connection, "standard_facts", keys, {"row_json": canonical(row)}, source_id, row, stats)
        reports.append({"file": relative, "domain": domain, "counts": dict(stats)})
    return reports


def ingest_product_master(connection: sqlite3.Connection, workbench: Path) -> dict[str, Any]:
    """商品主数据（id→名称/图）入库：稳定维度表，供窗口明细 items 导出时补全 title/img。

    契约：product_master/product_master_snapshot_*.csv，列 product_id/product_name/image_url。
    幂等：文件哈希去重 + 按商品逐行比对，无变化不写。
    """
    files = sorted((workbench / "product_master").glob("product_master_snapshot_*.csv"))
    if not files:
        return {"skipped": "无商品主数据文件"}
    path = files[-1]
    source_id, fresh = register_file(connection, path, "product_master")
    if not fresh:
        return {"file": path.name, "skipped_duplicate_file": True}
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    stats = defaultdict(int)
    for row in rows:
        product_id = str(row.get("product_id") or "").strip()
        if not product_id:
            continue
        title = (row.get("product_name") or "").strip() or None
        img = (row.get("image_url") or "").strip() or None
        existing = connection.execute(
            "SELECT title, img FROM product_master WHERE product_id = ?", (product_id,)
        ).fetchone()
        if existing and existing["title"] == title and existing["img"] == img:
            stats["unchanged"] += 1
            continue
        connection.execute(
            "INSERT INTO product_master (product_id, title, img, source_file_id, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(product_id) DO UPDATE SET title=excluded.title, img=excluded.img, "
            "source_file_id=excluded.source_file_id, updated_at=excluded.updated_at",
            (product_id, title, img, source_id, now()),
        )
        stats["updated" if existing else "inserted"] += 1
    return {"file": path.name, "counts": dict(stats)}


def enrich_window_items(connection: sqlite3.Connection, windows: dict[str, Any]) -> dict[str, Any]:
    """导出层维度补全：窗口 items 缺 title/img 时用商品主数据补（只补缺失，不覆盖、不改库）。

    背景：2026-11-05 千问重建快照漏做 items 与 product_master 的 join，导致品线拆分
    （按标题关键词映射）全落"其他"、商品明细无名称。数值指标不受影响。
    """
    master = {
        str(row["product_id"]): (row["title"], row["img"])
        for row in connection.execute("SELECT product_id, title, img FROM product_master")
    }
    filled = 0
    for payload in windows.values():
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = master.get(str(item.get("id")))
            if not entry:
                continue
            if not item.get("title") and entry[0]:
                item["title"] = entry[0]
                filled += 1
            if not item.get("img") and entry[1]:
                item["img"] = entry[1]
    return {"items_title_filled": filled}


def ingest_all(connection: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    source = Path(config["source_dir"])
    workbench = source / "tmall-workbench"
    category_files = [p for p in (workbench / "lingyang_category_day").glob("lingyang_category_day_*.csv") if "LEAF" not in p.name]
    sku_files = list(source.glob("lingyang-sku-*.json"))
    snapshot_path = source / "snapshot.js"
    lingyang_path = source / "lingyang.js"
    return {
        "snapshot": ingest_snapshot(connection, snapshot_path, config) if snapshot_path.is_file() else {"skipped": "无全量快照，使用增量文件"},
        "lingyang": ingest_lingyang(connection, lingyang_path) if lingyang_path.is_file() else {"skipped": "无聚合快照，使用SKU日事实"},
        "categories": ingest_categories(connection, max(category_files, key=lambda p: p.stat().st_mtime)) if category_files else {"skipped": "无新类目文件"},
        "sku": ingest_sku(connection, max(sku_files, key=lambda p: p.stat().st_mtime)) if sku_files else {"skipped": "无新SKU文件"},
        "standard_package": ingest_standard_package(connection, workbench),
        "incremental": ingest_incremental(connection, source / "incremental", config),
        "product_master": ingest_product_master(connection, workbench),
    }


def latest_payload(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    if table not in {"snapshot_payloads", "lingyang_payloads"}:
        raise ValueError("无效 payload 表")
    rows = connection.execute(f"SELECT source_file_id, payload_json FROM {table}").fetchall()
    if not rows:
        raise ValueError(f"数据库缺少 {table}")
    def sort_key(row: sqlite3.Row) -> tuple[str, int]:
        payload = json.loads(row["payload_json"])
        if table == "snapshot_payloads":
            end = payload.get("meta", {}).get("dataRange", {}).get("end", "")
        else:
            end = str(payload.get("meta", {}).get("window", "")).split(" ~ ")[-1]
        return end, int(row["source_file_id"])
    return json.loads(max(rows, key=sort_key)["payload_json"])


def fact_series(connection: sqlite3.Connection, scope: str, year: int) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT * FROM store_daily_facts WHERE scope = ? AND date LIKE ? ORDER BY date", (scope, f"{year}-%"),
    ).fetchall()
    if not rows:
        raise ValueError(f"店铺日事实缺少 {year} 年数据")
    result = {"year": year, "dates": [row["date"] for row in rows]}
    for metric in STORE_METRICS:
        if any(row[metric] is not None for row in rows):
            result[metric] = [row[metric] for row in rows]
    result["payRate"] = [
        row["payByrCnt"] / row["uv"] if row["uv"] not in (None, 0) and row["payByrCnt"] is not None else None
        for row in rows
    ]
    return result


def monthly_from_daily(daily: dict[str, Any], months: list[str] | None = None, partial_day: int | None = None) -> dict[str, Any]:
    months = months or sorted({day[:7] for day in daily["dates"]})
    metrics = ("uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt")
    result: dict[str, Any] = {"months": months, **{metric: [] for metric in (*metrics, "payRate")}}
    for month in months:
        indices = [i for i, day in enumerate(daily["dates"]) if day.startswith(month)]
        if partial_day is not None:
            indices = [i for i in indices if int(daily["dates"][i][-2:]) <= partial_day]
        if not indices:
            raise ValueError(f"月度聚合缺少 {month} 日事实")
        for metric in metrics:
            values = [daily.get(metric, [None] * len(daily["dates"]))[i] for i in indices]
            result[metric].append(sum(values) if all(value is not None for value in values) else None)
        uv, buyers = result["uv"][-1], result["payByrCnt"][-1]
        result["payRate"].append(buyers / uv if uv not in (None, 0) and buyers is not None else None)
    return result


def build_snapshot(connection: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    base = latest_payload(connection, "snapshot_payloads")
    all_dates = [row["date"] for row in connection.execute(
        "SELECT DISTINCT date FROM store_daily_facts WHERE scope = ?", (config["store_scope"],),
    )]
    if not all_dates:
        raise ValueError("数据库没有店铺日事实")
    current_year = max(all_dates)[:4]
    daily = fact_series(connection, config["store_scope"], int(current_year))
    daily_ly = fact_series(connection, config["store_scope"], int(current_year) - 1)
    data_end = daily["dates"][-1]
    months = sorted({day[:7] for day in daily["dates"]})
    monthly = monthly_from_daily(daily)
    monthly_ly = {"months": months}
    for metric in ("uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt", "payRate"):
        monthly_ly[metric] = []
    for month in months:
        year, month_number = month.split("-")
        ly_month = f"{int(year) - 1}-{month_number}"
        partial_day = int(data_end[-2:]) if month == data_end[:7] else None
        aggregate = monthly_from_daily(daily_ly, [ly_month], partial_day)
        for metric in ("uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt", "payRate"):
            monthly_ly[metric].append(aggregate[metric][0])
    wxt_rows = connection.execute(
        "SELECT * FROM wxt_daily_facts WHERE channel = ? AND attribution_scope = ? ORDER BY date",
        (config["wxt_channel"], config["wxt_attribution_scope"]),
    ).fetchall()
    if not wxt_rows:
        raise ValueError("数据库没有万相台日事实")
    wxt = {"dates": [row["date"] for row in wxt_rows]}
    for metric in WXT_METRICS:
        wxt[metric] = [row[metric] for row in wxt_rows]
    wxt["roi"] = [round(row["payAmt"] / row["charge"], 5) if row["charge"] not in (None, 0) and row["payAmt"] is not None else None for row in wxt_rows]
    if base.get("wxt", {}).get("note") is not None:
        wxt["note"] = base["wxt"]["note"]
    windows = {}
    for key in config["required_windows"]:
        rows = connection.execute("SELECT window_end, payload_json FROM window_snapshots WHERE window_key = ?", (key,)).fetchall()
        if not rows:
            raise ValueError(f"数据库缺少窗口 {key}")
        windows[key] = json.loads(max(rows, key=lambda row: row["window_end"])["payload_json"])
    meta = dict(base.get("meta") or {})
    source_generated = meta.get("generatedAt")
    latest_ingested = connection.execute("SELECT MAX(ingested_at) FROM source_files").fetchone()[0]
    if source_generated is not None:
        meta["sourceGeneratedAt"] = source_generated
    if latest_ingested:
        meta["generatedAt"] = latest_ingested
    meta["dataEndFull"] = data_end
    meta["dataRange"] = {"start": daily["dates"][0], "end": data_end, "days": len(daily["dates"])}
    meta["pipelineGeneratedAt"] = now()
    meta["pipeline"] = "SQLite deterministic daily pipeline v1"
    enrich_report = enrich_window_items(connection, windows)
    meta["windowItemsEnriched"] = enrich_report["items_title_filled"]
    return {
        "meta": meta, "daily": daily, "dailyLY": daily_ly, "monthly": monthly,
        "monthlyLy": monthly_ly, "monthlyPartial": data_end[:7], "wxt": wxt,
        "categories": base.get("categories") or {}, "windows": windows,
    }


def build_lingyang(connection: sqlite3.Connection, config: dict[str, Any], data_end: str) -> dict[str, Any]:
    base = latest_payload(connection, "lingyang_payloads")
    days = int(config["lingyang_days"])
    start = str(dt.date.fromisoformat(data_end) - dt.timedelta(days=days - 1))
    expected_dates = [str(dt.date.fromisoformat(start) + dt.timedelta(days=i)) for i in range(days)]
    category_rows = connection.execute(
        "SELECT * FROM category_daily_facts WHERE date BETWEEN ? AND ? ORDER BY date, category_l1", (start, data_end),
    ).fetchall()
    if {row["date"] for row in category_rows} != set(expected_dates):
        raise ValueError("一级类目近7天覆盖不完整")
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in category_rows:
        for metric in ("uv", "amt", "byr", "refund", "cart"):
            if row[metric] is None:
                raise ValueError(f"一级类目 {row['date']} {row['category_l1']} 缺少 {metric}")
            grouped[row["category_l1"]][metric] += row[metric]
    total = sum(values["amt"] for values in grouped.values())
    categories = [{
        "name": name, **values,
        "payRate": values["byr"] / values["uv"] if values["uv"] else None,
        "share": values["amt"] / total if total else None,
    } for name, values in grouped.items()]
    categories.sort(key=lambda row: row["amt"], reverse=True)
    sku_rows = connection.execute(
        "SELECT * FROM sku_daily_facts WHERE date BETWEEN ? AND ? ORDER BY date, item_id", (start, data_end),
    ).fetchall()
    if {row["date"] for row in sku_rows} != set(expected_dates):
        raise ValueError("SKU 近7天覆盖不完整")
    products: dict[str, dict[str, Any]] = {}
    for row in sku_rows:
        product = products.setdefault(row["item_id"], {"id": row["item_id"], "title": "", **{metric: 0.0 for metric in SKU_METRICS}})
        if row["item_name"]:
            product["title"] = row["item_name"]
        for metric in SKU_METRICS:
            if row[metric] is not None:
                product[metric] += row[metric]
    product_list = sorted(products.values(), key=lambda row: row["payAmt"], reverse=True)
    meta = dict(base.get("meta") or {})
    latest_ingested = connection.execute("SELECT MAX(ingested_at) FROM source_files").fetchone()[0]
    meta.update({
        "window": f"{start} ~ {data_end}", "categorySource": "SQLite category_daily_facts",
        "generatedAt": latest_ingested or meta.get("generatedAt"),
        "pipelineGeneratedAt": now(),
    })
    return {
        "meta": meta, "categories": categories, "catWindow": expected_dates,
        "products": product_list, "soldCount": sum(row["payAmt"] > 0 for row in product_list),
        "totalProducts": len(product_list),
    }


def validate(snapshot: dict[str, Any], lingyang: dict[str, Any], config: dict[str, Any], enforce_freshness: bool) -> tuple[list[str], dict[str, Any]]:
    errors = []
    data_end = snapshot["meta"]["dataRange"]["end"]
    if enforce_freshness and data_end != str(dt.date.today() - dt.timedelta(days=1)):
        errors.append(f"数据截止日 {data_end} 不是昨天")
    if snapshot["daily"]["dates"][-1] != data_end or snapshot["wxt"]["dates"][-1] != data_end:
        errors.append("店铺或万相台日事实未对齐截止日")
    for key in config["required_windows"]:
        if snapshot["windows"][key]["cur"]["end"] != data_end:
            errors.append(f"窗口 {key} 未更新到 {data_end}")
    months = snapshot["monthly"]["months"]
    for metric in ("uv", "pv", "payAmt", "payByrCnt", "itmUv", "cartByrCnt", "payRate"):
        if len(snapshot["monthly"][metric]) != len(months) or len(snapshot["monthlyLy"][metric]) != len(months):
            errors.append(f"月度字段 {metric} 长度不一致")
    category_total = sum(row["amt"] for row in lingyang["categories"])
    product_total = sum(row["payAmt"] for row in lingyang["products"])
    if not math.isclose(category_total, product_total, rel_tol=0, abs_tol=0.01):
        errors.append(f"一级类目与SKU商品成交不一致: {category_total:.2f} vs {product_total:.2f}")
    if not lingyang["products"]:
        errors.append("商品池为空")
    untitled = sum(not row["title"] for row in lingyang["products"])
    if untitled:
        errors.append(f"商品池有 {untitled} 款缺标题")
    return errors, {
        "data_end": data_end, "daily_days": len(snapshot["daily"]["dates"]),
        "daily_ly_days": len(snapshot["dailyLY"]["dates"]), "wxt_days": len(snapshot["wxt"]["dates"]),
        "monthly_ly_last_payAmt": snapshot["monthlyLy"]["payAmt"][-1],
        "lingyang_category_total": round(category_total, 2),
        "lingyang_product_total": round(product_total, 2),
        "lingyang_products": len(lingyang["products"]),
    }


def export_dashboard(connection: sqlite3.Connection, config: dict[str, Any], enforce_freshness: bool = True) -> dict[str, Any]:
    snapshot = build_snapshot(connection, config)
    lingyang = build_lingyang(connection, config, snapshot["meta"]["dataRange"]["end"])
    errors, details = validate(snapshot, lingyang, config, enforce_freshness)
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True)
    report = {"status": "fail" if errors else "pass", "run_at": now(), "errors": errors, **details}
    report_path = state / f"validation-{dt.date.today()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        raise ValueError("发布门禁失败: " + "；".join(errors))
    dashboard = Path(config["dashboard_dir"])
    data_dir = dashboard / "data"
    backup = state / "backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=False)
    targets = [data_dir / "snapshot.js", data_dir / "lingyang.js", dashboard / "dashboard-online.html"]
    for target in targets:
        if target.exists():
            shutil.copy2(target, backup / target.name)
    try:
        dump_js(data_dir / "snapshot.js", "__DASH__", snapshot)
        dump_js(data_dir / "lingyang.js", "__LY__", lingyang)
        from html_builder import build_online
        online = build_online(dashboard)
    except Exception:
        for target in targets:
            saved = backup / target.name
            if saved.exists():
                shutil.copy2(saved, target)
        raise
    return {
        **details, "snapshot_file": str(data_dir / "snapshot.js"),
        "lingyang_file": str(data_dir / "lingyang.js"), "online_file": str(online),
        "online_bytes": online.stat().st_size, "backup": str(backup), "validation": str(report_path),
    }


def status(connection: sqlite3.Connection) -> dict[str, Any]:
    tables = ("source_files", "store_daily_facts", "wxt_daily_facts", "standard_facts", "category_daily_facts", "sku_daily_facts", "window_snapshots")
    result = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    result["store_date_range"] = list(connection.execute("SELECT MIN(date), MAX(date) FROM store_daily_facts").fetchone())
    result["wxt_date_range"] = list(connection.execute("SELECT MIN(date), MAX(date) FROM wxt_daily_facts").fetchone())
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "ingest", "export", "status"))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--allow-stale", action="store_true", help="仅用于历史样本测试，不强制截止日为昨天")
    parser.add_argument("--publish", action="store_true", help="run/export成功后更新既有固定线上页面")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    connection = connect(Path(config["database"]))
    try:
        if args.command == "status":
            result = status(connection)
        elif args.command == "ingest":
            with connection:
                result = ingest_all(connection, config)
        elif args.command == "export":
            result = export_dashboard(connection, config, not args.allow_stale)
        else:
            with connection:
                ingested = ingest_all(connection, config)
            exported = export_dashboard(connection, config, not args.allow_stale)
            result = {"status": "ok", "database": config["database"], "ingest": ingested, "export": exported}
        if args.publish:
            if args.command not in ("run", "export"):
                raise ValueError("--publish 只能与 run 或 export 一起使用")
            from publish_dashboard import publish
            result["publish"] = publish(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, KeyError, json.JSONDecodeError, sqlite3.Error) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
