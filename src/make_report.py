#!/usr/bin/env python3
"""千问经营全景看板 · 企微日报生成脚本
用法: python3 make_report.py
读取 snapshot.js + 校验状态 → 生成 state/daily-report-YYYY-MM-DD.md（企微 markdown 格式）
退出码: 0=成功  2=数据未就绪(推送告警版)
"""
import datetime
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("PIPELINE_CONFIG", ROOT / "pipeline_config.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
SNAPSHOT = str(Path(CONFIG["dashboard_dir"]) / "data" / "snapshot.js")
STATE_DIR = str(Path(CONFIG["state_dir"]))
VALIDATION_DIR = str(Path(CONFIG["state_dir"]) / "daily-pipeline")


def load_snapshot():
    with open(SNAPSHOT, encoding="utf-8") as f:
        s = f.read()
    m = re.search(r"window\.__DASH__\s*=\s*", s)
    return json.loads(s[m.end():].rstrip().rstrip(";"))


def agg(o, start, end, keys):
    idx = {dt: i for i, dt in enumerate(o.get("dates", []))}
    out = {k: 0.0 for k in keys}
    for dt, i in idx.items():
        if start <= dt <= end:
            for k in keys:
                v = o.get(k, [None] * len(idx))[i]
                if isinstance(v, (int, float)):
                    out[k] += v
    return out


def yoy(cur_v, ly_v):
    if not ly_v:
        return None
    return cur_v / ly_v - 1


def fmt_pct(v):
    return f"{v*100:+.1f}%" if v is not None else "—"


def fmt_k(v):
    """元 → 千元"""
    return f"{v/1000:,.1f}"


def fmt_kn(v):
    return f"{v/1000:,.1f}"


def main():
    today = datetime.date.today()
    os.makedirs(STATE_DIR, exist_ok=True)

    # 校验状态
    val = None
    vpath = os.path.join(VALIDATION_DIR, f"validation-{today}.json")
    if os.path.exists(vpath):
        with open(vpath, encoding="utf-8") as f:
            val = json.load(f)

    d = load_snapshot()
    meta = d.get("meta", {})
    cur, ly = d["daily"], d["dailyLY"]
    end = (meta.get("dataRange") or {}).get("end", cur["dates"][-1])
    edate = datetime.date.fromisoformat(end)
    last_day_label = f"{edate.month:02d}-{edate.day:02d}（{'周' + '一二三四五六日'[edate.weekday()]}）"

    def window(s_days, label):
        cs = (edate - datetime.timedelta(days=s_days - 1)).isoformat()
        ce = end
        ls = (edate.replace(year=edate.year - 1) - datetime.timedelta(days=s_days - 1)).isoformat()
        le = ce.replace(str(edate.year), str(edate.year - 1))
        rc = agg(cur, cs, ce, ["payAmt", "payByrCnt", "uv"])
        rl = agg(ly, ls, le, ["payAmt", "payByrCnt", "uv"])
        return label, rc, rl, cs, ce

    # 万相台近7天
    wxt = d.get("wxt", {})
    wxt_dates = wxt.get("dates", [])
    w7 = {"charge": 0.0, "payAmt": 0.0}
    if wxt_dates:
        wstart = wxt_dates[max(0, len(wxt_dates) - 7)]
        for i, dt in enumerate(wxt_dates):
            if dt >= wstart:
                for k in w7:
                    v = wxt.get(k, [None] * len(wxt_dates))[i]
                    if isinstance(v, (int, float)):
                        w7[k] += v

    lines = []
    lines.append(f"**{CONFIG.get('report_title', '天猫旗舰店·经营日报')} {end}** {last_day_label}")
    lines.append(f"数据源：SQLite 数据底座 {meta.get('generatedAt','?')[:16]}")
    lines.append("")

    # 昨日
    rc = agg(cur, end, end, ["payAmt", "payByrCnt", "uv", "cartByrCnt"])
    rl = agg(ly, end.replace(str(edate.year), str(edate.year - 1)), end.replace(str(edate.year), str(edate.year - 1)), ["payAmt", "payByrCnt", "uv"])
    lines.append(f"**昨日** GMV {fmt_k(rc['payAmt'])}千元({fmt_pct(yoy(rc['payAmt'], rl['payAmt']))})"
                 f" | 买家 {fmt_kn(rc['payByrCnt'])}千({fmt_pct(yoy(rc['payByrCnt'], rl['payByrCnt']))})"
                 f" | UV {fmt_kn(rc['uv'])}千({fmt_pct(yoy(rc['uv'], rl['uv']))})")

    # 近7天 / 30天 / 月 / YTD
    for days, label in [(7, "近7天"), (30, "近30天")]:
        lb, rc, rl, cs, ce = window(days, label)
        lines.append(f"**{lb}** GMV {fmt_k(rc['payAmt'])}千元({fmt_pct(yoy(rc['payAmt'], rl['payAmt']))})"
                     f" | 买家 {fmt_kn(rc['payByrCnt'])}千({fmt_pct(yoy(rc['payByrCnt'], rl['payByrCnt']))})")
    # 月至今
    ms = f"{edate.year}-{edate.month:02d}-01"
    ls_ = ms.replace(str(edate.year), str(edate.year - 1))
    le_ = end.replace(str(edate.year), str(edate.year - 1))
    rc = agg(cur, ms, end, ["payAmt"]); rl = agg(ly, ls_, le_, ["payAmt"])
    lines.append(f"**{edate.month}月至今** GMV {fmt_k(rc['payAmt'])}千元({fmt_pct(yoy(rc['payAmt'], rl['payAmt']))})")
    # YTD
    rc = agg(cur, f"{edate.year}-01-01", end, ["payAmt"]); rl = agg(ly, f"{edate.year-1}-01-01", le_, ["payAmt"])
    lines.append(f"**年至今** GMV {fmt_k(rc['payAmt'])}千元({fmt_pct(yoy(rc['payAmt'], rl['payAmt']))})")

    # 万相台
    if w7["charge"]:
        roi = w7["payAmt"] / w7["charge"] if w7["charge"] else None
        lines.append(f"**万相台近7天** 消耗 {fmt_k(w7['charge'])}千元 | ROI {roi:.2f}" if roi else "")

    # 校验结论
    lines.append("")
    if val and val.get("status") == "fail":
        lines.append("⚠ **校验未通过，以下同比数字可能失真**")
        for e in val.get("errors", [])[:3]:
            lines.append(f"⚠ {e}")
    elif val and val.get("status") == "warn":
        lines.append(f"⚠ {len(val.get('warnings', []))} 项警告，详见校验报告")
    else:
        lines.append("✅ 数据校验通过")

    report = "\n".join(x for x in lines if x is not None)
    out = os.path.join(STATE_DIR, f"daily-report-{today}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print()
    print(f"[已生成] {out}")
    sys.exit(2 if (val and val.get("status") == "fail") else 0)


if __name__ == "__main__":
    main()
