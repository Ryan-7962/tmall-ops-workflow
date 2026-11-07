#!/usr/bin/env python3
"""同一 SQLite 事实库驱动的群聊问数与企业微信 CLI 回复。

默认只生成答案；只有显式传入 --send 才会调用企业微信 CLI。
企业微信 CLI 负责发送，不负责监听群消息；实时提问由宿主触发本程序。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "pipeline_config.json"
STORE_METRICS = ("payAmt", "payByrCnt", "uv")
WXT_METRICS = ("charge", "payAmt", "ordCnt", "cart")


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    database = Path(path).expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"数据库不存在：{database}")
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def latest_date(connection: sqlite3.Connection, domain: str, config: dict[str, Any]) -> str | None:
    if domain == "store":
        row = connection.execute(
            "SELECT MAX(date) AS value FROM store_daily_facts WHERE scope = ?",
            (config["store_scope"],),
        ).fetchone()
    elif domain == "wxt":
        row = connection.execute(
            "SELECT MAX(date) AS value FROM wxt_daily_facts WHERE channel = ? AND attribution_scope = ?",
            (config["wxt_channel"], config["wxt_attribution_scope"]),
        ).fetchone()
    else:
        raise ValueError("未知数据域")
    return row["value"] if row else None


def explicit_date(question: str) -> str | None:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", question)
    if not match:
        return None
    return dt.date(*map(int, match.groups())).isoformat()


def resolve_date(question: str, latest: str | None, today: dt.date) -> str | None:
    explicit = explicit_date(question)
    if explicit:
        return explicit
    if "今天" in question:
        return today.isoformat()
    if "昨天" in question:
        return (today - dt.timedelta(days=1)).isoformat()
    return latest


def period_days(question: str, default: int = 1) -> int:
    if explicit_date(question):
        return 1
    if re.search(r"近?\s*30\s*天|30\s*日", question):
        return 30
    if re.search(r"近?\s*7\s*天|7\s*日|一周|本周", question):
        return 7
    if explicit_date(question) or "昨天" in question or "今天" in question:
        return 1
    return default


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def complete_totals(rows: list[sqlite3.Row], metrics: tuple[str, ...]) -> dict[str, float | None]:
    """任一已覆盖行的指标缺失时，该指标不做看似完整的区间汇总。"""
    return {
        metric: sum(row[metric] for row in rows)
        if rows and all(is_number(row[metric]) for row in rows) else None
        for metric in metrics
    }


def amount(value: Any) -> str:
    return f"{value / 1000:,.1f} 千元" if is_number(value) else "—"


def yuan(value: Any) -> str:
    return f"¥{value:,.2f}" if is_number(value) else "—"


def integer(value: Any) -> str:
    return f"{value:,.0f}" if is_number(value) else "—"


def percent(value: Any, signed: bool = False) -> str:
    if not is_number(value):
        return "—"
    return f"{value:+.1%}" if signed else f"{value:.1%}"


def divide(numerator: Any, denominator: Any) -> float | None:
    if not is_number(numerator) or not is_number(denominator) or not denominator:
        return None
    return numerator / denominator


def dashboard_url(config: dict[str, Any]) -> str | None:
    value = str(config.get("dashboard_url") or "").strip()
    if not value or "REPLACE_" in value or "example.com" in value:
        return None
    return value


def with_dashboard(lines: list[str], config: dict[str, Any]) -> str:
    url = dashboard_url(config)
    if url:
        lines.extend(["", f"[打开数据看板]({url})"])
    return "\n".join(lines)


def previous_year(value: str) -> str:
    day = dt.date.fromisoformat(value)
    try:
        return day.replace(year=day.year - 1).isoformat()
    except ValueError:
        return day.replace(year=day.year - 1, day=28).isoformat()


def missing_message(domain: str, date: str | None, latest: str | None) -> str:
    label = "店铺日事实" if domain == "store" else "万相台日事实"
    requested = date or "所问日期"
    latest_text = latest or "暂无"
    return f"暂无 {requested} 的{label}；当前最新数据到 {latest_text}。缺失没有填 0。"


def store_answer(
    connection: sqlite3.Connection,
    question: str,
    config: dict[str, Any],
    today: dt.date,
) -> str:
    scope = config["store_scope"]
    latest = latest_date(connection, "store", config)
    end = resolve_date(question, latest, today)
    days = period_days(question)
    if not end:
        return "店铺事实库还没有数据，请先运行增量入库。"
    if days == 1:
        row = connection.execute(
            "SELECT * FROM store_daily_facts WHERE date = ? AND scope = ?",
            (end, scope),
        ).fetchone()
        if not row:
            return missing_message("store", end, latest)
        lines = [
            f"**店铺经营 · {end}**",
            f"GMV **{amount(row['payAmt'])}**｜支付买家 **{integer(row['payByrCnt'])}**｜UV **{integer(row['uv'])}**",
            f"支付转化率 **{percent(divide(row['payByrCnt'], row['uv']))}**｜客单价 **{yuan(divide(row['payAmt'], row['payByrCnt']))}**",
        ]
        last_year = connection.execute(
            "SELECT payAmt FROM store_daily_facts WHERE date = ? AND scope = ?",
            (previous_year(end), scope),
        ).fetchone()
        if last_year and is_number(last_year["payAmt"]) and last_year["payAmt"]:
            growth = divide(row["payAmt"], last_year["payAmt"])
            lines.append(f"GMV 同比 **{percent(growth - 1 if growth is not None else None, True)}**")
        return with_dashboard(lines, config)

    end_date = dt.date.fromisoformat(end)
    start = (end_date - dt.timedelta(days=days - 1)).isoformat()
    rows = connection.execute(
        "SELECT * FROM store_daily_facts WHERE scope = ? AND date BETWEEN ? AND ? ORDER BY date",
        (scope, start, end),
    ).fetchall()
    if not rows:
        return missing_message("store", end, latest)
    totals = complete_totals(rows, STORE_METRICS)
    coverage_label = "" if len(rows) == days else "已覆盖日期 "
    lines = [
        f"**近 {days} 天经营 · {start} ~ {end}**",
        f"{coverage_label}GMV **{amount(totals['payAmt'])}**｜支付买家 **{integer(totals['payByrCnt'])}**｜UV **{integer(totals['uv'])}**",
        f"日均 GMV **{amount(divide(totals['payAmt'], len(rows)))}**｜支付转化率 **{percent(divide(totals['payByrCnt'], totals['uv']))}**",
    ]
    incomplete_metrics = [metric for metric in STORE_METRICS if any(not is_number(row[metric]) for row in rows)]
    if len(rows) < days or incomplete_metrics:
        lines.append(f"覆盖提示：区间内有 {len(rows)}/{days} 天店铺事实；缺失指标及派生指标显示为 —，不按 0 计算。")
    return with_dashboard(lines, config)


def wxt_answer(
    connection: sqlite3.Connection,
    question: str,
    config: dict[str, Any],
    today: dt.date,
) -> str:
    channel = config["wxt_channel"]
    attribution = config["wxt_attribution_scope"]
    latest = latest_date(connection, "wxt", config)
    end = resolve_date(question, latest, today)
    days = period_days(question, default=7)
    if not end:
        return "万相台事实库还没有数据，请先运行增量入库。"
    if days == 1:
        rows = connection.execute(
            "SELECT * FROM wxt_daily_facts WHERE date = ? AND channel = ? AND attribution_scope = ?",
            (end, channel, attribution),
        ).fetchall()
        start = end
    else:
        end_date = dt.date.fromisoformat(end)
        start = (end_date - dt.timedelta(days=days - 1)).isoformat()
        rows = connection.execute(
            "SELECT * FROM wxt_daily_facts WHERE channel = ? AND attribution_scope = ? AND date BETWEEN ? AND ? ORDER BY date",
            (channel, attribution, start, end),
        ).fetchall()
    if not rows:
        return missing_message("wxt", end, latest)
    totals = complete_totals(rows, WXT_METRICS)
    label = end if days == 1 else f"近 {days} 天 · {start} ~ {end}"
    coverage_label = "" if len(rows) == days else "已覆盖日期 "
    roi = divide(totals["payAmt"], totals["charge"])
    lines = [
        f"**万相台 · {label}**",
        f"{coverage_label}消耗 **{amount(totals['charge'])}**｜归因成交 **{amount(totals['payAmt'])}**｜ROI **{roi:.2f}**" if roi is not None else f"{coverage_label}消耗 **{amount(totals['charge'])}**｜归因成交 **{amount(totals['payAmt'])}**｜ROI **—**",
        f"归因订单 **{integer(totals['ordCnt'])}**｜加购 **{integer(totals['cart'])}**",
        f"口径：{attribution}。近端归因会后验回补，同一业务主键覆盖，不累加。",
    ]
    incomplete_metrics = [metric for metric in WXT_METRICS if any(not is_number(row[metric]) for row in rows)]
    if len(rows) < days or incomplete_metrics:
        lines.append(f"覆盖提示：区间内有 {len(rows)}/{days} 天事实；缺失指标及派生指标显示为 —，不按 0 计算。")
    return with_dashboard(lines, config)


def category_answer(connection: sqlite3.Connection, question: str, today: dt.date) -> str:
    latest_row = connection.execute("SELECT MAX(date) AS value FROM category_daily_facts").fetchone()
    latest = latest_row["value"] if latest_row else None
    end = resolve_date(question, latest, today)
    if not end:
        return "类目事实库还没有数据，请先运行增量入库。"
    end_date = dt.date.fromisoformat(end)
    start = (end_date - dt.timedelta(days=6)).isoformat()
    rows = connection.execute(
        "SELECT date, category_l1, amt FROM category_daily_facts WHERE date BETWEEN ? AND ? ORDER BY category_l1, date",
        (start, end),
    ).fetchall()
    if not rows:
        return f"暂无 {start} ~ {end} 的一级类目事实，缺失没有填 0。"
    expected_dates = {(dt.date.fromisoformat(start) + dt.timedelta(days=offset)).isoformat() for offset in range(7)}
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        grouped.setdefault(row["category_l1"], {})[row["date"]] = row["amt"]
    complete = {
        name: sum(by_date.values())
        for name, by_date in grouped.items()
        if set(by_date) == expected_dates and all(is_number(value) for value in by_date.values())
    }
    incomplete = len(grouped) - len(complete)
    ranking = sorted(complete.items(), key=lambda item: item[1], reverse=True)[:5]
    if not ranking:
        return "一级类目成交指标均不完整，未生成排行，也没有把缺失按 0 计算。"
    lines = [f"**一级类目成交 TOP5 · {start} ~ {end}**"]
    lines.extend(f"{index}. {name}：**{amount(value)}**" for index, (name, value) in enumerate(ranking, 1))
    lines.append("仅统计 7 天日期与成交字段均完整的一级类目，不跨层级重复汇总。")
    coverage = len({row["date"] for row in rows})
    if coverage < 7 or incomplete:
        lines.append(f"覆盖提示：全局有 {coverage}/7 天类目事实；{incomplete} 个缺日或成交缺失的类目未参与排名。")
    return "\n".join(lines)


def status_answer(connection: sqlite3.Connection, config: dict[str, Any]) -> str:
    store = latest_date(connection, "store", config) or "暂无"
    wxt = latest_date(connection, "wxt", config) or "暂无"
    return with_dashboard([
        "**数据底座状态**",
        f"店铺日事实最新到 **{store}**",
        f"万相台日事实最新到 **{wxt}**",
        "看板、日报和群聊问数读取同一数据库。",
    ], config)


def help_answer() -> str:
    return "\n".join([
        "**我可以直接查同一数据底座：**",
        "- 昨天销售怎么样？",
        "- 近 7 天 GMV、买家和转化率",
        "- 万相台近 7 天 ROI",
        "- 近 7 天一级类目 TOP5",
        "- 数据更新到哪一天？",
        "- 看板链接",
        "未入库的数据会明确说缺失，不会填 0 或让 AI 猜数。",
    ])


def answer_question(
    connection: sqlite3.Connection,
    question: str,
    config: dict[str, Any],
    today: dt.date | None = None,
) -> str:
    question = question.strip()
    today = today or dt.date.today()
    if not question or "帮助" in question:
        return help_answer()
    if "看板" in question and any(word in question for word in ("链接", "地址", "在哪", "打开")):
        url = dashboard_url(config)
        return f"[打开数据看板]({url})" if url else "尚未配置 dashboard_url，请先在本地配置中填入固定看板链接。"
    if any(word in question for word in ("更新到", "数据到", "最新日期", "数据状态")):
        return status_answer(connection, config)
    if any(word in question for word in ("万相台", "ROI", "roi", "广告消耗", "推广消耗")):
        return wxt_answer(connection, question, config, today)
    if "类目" in question:
        return category_answer(connection, question, today)
    if any(word in question for word in ("销售", "经营", "GMV", "gmv", "成交", "昨天", "今天", "近 7 天", "近7天", "近 30 天", "近30天")) or explicit_date(question):
        return store_answer(connection, question, config, today)
    return help_answer()


def command(args: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("企业微信 CLI 调用失败，请检查安装、授权和错误输出。")
    return result


def json_output(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("企业微信 CLI 未返回有效 JSON。") from error
    if not isinstance(payload, dict):
        raise RuntimeError("企业微信 CLI 返回的 JSON 顶层必须是对象。")
    if payload.get("errcode") not in (None, 0):
        raise RuntimeError(f"企业微信 CLI 返回错误：{payload.get('errmsg') or payload['errcode']}")
    return payload


def send_wecom(chat_name: str, markdown: str, config: dict[str, Any]) -> dict[str, str]:
    allowed = config.get("chat_allowed_groups") or []
    if chat_name not in allowed:
        raise ValueError("目标群不在 chat_allowed_groups 允许清单中；未发送。")
    cli = str(config.get("wecom_cli") or "wecom-cli")
    version = command([cli, "--version"]).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < (1, 2, 1):
        raise RuntimeError("企业微信 CLI 版本需不低于 1.2.1。")
    auth_status = command([cli, "auth", "show", "--status"]).stdout
    if not re.search(r"(?<![A-Za-z])authorized(?![A-Za-z])", auth_status):
        raise RuntimeError("企业微信 CLI 尚未授权；未发送。")
    sessions = json_output(command([cli, "message", "aibot", "sessions", "list"])).get("sessions") or []
    matches = [item for item in sessions if item.get("chat_type") == "group" and item.get("chat_name") == chat_name]
    if len(matches) != 1:
        raise ValueError("目标群不在本次可发送会话中，或存在同名群；未发送。")
    payload = {
        "chat_id": matches[0]["chat_id"],
        "msg_type": "markdown",
        "markdown": {"content": markdown},
    }
    json_output(command([cli, "message", "aibot", "send", "--json", json.dumps(payload, ensure_ascii=False)]))
    return {"status": "sent", "chat_name": chat_name, "msg_type": "markdown"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    ask = subparsers.add_parser("ask", help="查询事实库，可选发送到企业微信群")
    ask.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ask.add_argument("--question", required=True)
    ask.add_argument("--send", action="store_true")
    ask.add_argument("--chat-name", default="")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        connection = connect_readonly(config["database"])
        try:
            answer = answer_question(connection, args.question, config)
        finally:
            connection.close()
        if args.send:
            if not args.chat_name:
                raise ValueError("发送群消息时必须提供 --chat-name。")
            send_wecom(args.chat_name, answer, config)
            print(f"已发送到群「{args.chat_name}」（Markdown）。")
        else:
            print(answer)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error, json.JSONDecodeError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
