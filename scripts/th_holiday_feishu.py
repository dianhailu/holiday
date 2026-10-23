#!/usr/bin/env python3
"""Send Feishu reminders for Thai public holidays.

Audience: China-based teammates monitoring Thailand business and data.
Reminder offsets (relative to holiday date):
  +30, +7, +3, +1, 0 (today), -1 (day after holiday).

Languages: Chinese and Thai.
Timezone: Asia/Bangkok (ICT, UTC+7). Default schedule: 08:00 ICT via cron.
Uses the same Feishu bot secrets as the Indonesia reminder.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Fixed ICT offset so CI does not depend on tzdata / ZoneInfo databases.
TZ = timezone(timedelta(hours=7))
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

# Monday=0 ... Sunday=6
WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAY_TH = (
    "วันจันทร์",
    "วันอังคาร",
    "วันพุธ",
    "วันพฤหัสบดี",
    "วันศุกร์",
    "วันเสาร์",
    "วันอาทิตย์",
)

# day_diff = holiday_date - today
REMINDER_META = {
    30: {"zh_title": "泰国业务假期 · 提前 30 天", "th_title": "วันหยุดธุรกิจไทย · ล่วงหน้า 30 วัน"},
    7: {"zh_title": "泰国业务假期 · 提前 7 天", "th_title": "วันหยุดธุรกิจไทย · ล่วงหน้า 7 วัน"},
    3: {"zh_title": "泰国业务假期 · 提前 3 天", "th_title": "วันหยุดธุรกิจไทย · ล่วงหน้า 3 วัน"},
    1: {"zh_title": "泰国业务假期 · 明天", "th_title": "วันหยุดธุรกิจไทย · พรุ่งนี้"},
    0: {"zh_title": "泰国业务假期 · 今天", "th_title": "วันหยุดธุรกิจไทย · วันนี้"},
    -1: {"zh_title": "泰国业务假期 · 已结束", "th_title": "วันหยุดธุรกิจไทย · สิ้นสุดแล้ว"},
}

# nationwide closures are on by default; Bangkok-only days need a flag
DEFAULT_TYPES = {"national", "substitution", "private", "government"}


@dataclass(frozen=True)
class Holiday:
    date: date
    name_th: str
    name_zh: str
    type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today",
        help="Override today as YYYY-MM-DD (Bangkok calendar date). Useful for dry runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages instead of posting to Feishu.",
    )
    parser.add_argument(
        "--include-bangkok-special",
        action="store_true",
        help="Also remind for Bangkok-only special holidays.",
    )
    return parser.parse_args()


def resolve_today(override: str | None) -> date:
    if override:
        return date.fromisoformat(override)
    return datetime.now(TZ).date()


def load_holidays_for_years(years: set[int], include_bangkok: bool) -> list[Holiday]:
    holidays: list[Holiday] = []
    allowed = set(DEFAULT_TYPES)
    if include_bangkok:
        allowed.add("bangkok")
    for year in sorted(years):
        path = DATA_DIR / f"th-holidays-{year}.json"
        if not path.exists():
            print(f"warn: missing holiday file {path}", file=sys.stderr)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("holidays", []):
            holiday_type = item.get("type", "national")
            if holiday_type not in allowed:
                continue
            holidays.append(
                Holiday(
                    date=date.fromisoformat(item["date"]),
                    name_th=item["name_th"],
                    name_zh=item["name_zh"],
                    type=holiday_type,
                )
            )
    return holidays


def matched_reminders(today: date, holidays: list[Holiday]) -> list[tuple[int, Holiday]]:
    wanted = set(REMINDER_META)
    matches: list[tuple[int, Holiday]] = []
    for holiday in holidays:
        day_diff = (holiday.date - today).days
        if day_diff in wanted:
            matches.append((day_diff, holiday))
    matches.sort(key=lambda row: (-row[0], row[1].date, row[1].name_zh))
    return matches


def type_label(holiday_type: str) -> tuple[str, str]:
    labels = {
        "national": ("法定假期", "วันหยุดราชการ"),
        "substitution": ("补假", "วันหยุดชดเชย"),
        "private": ("私营部门及银行假期", "วันหยุดภาคเอกชนและธนาคาร"),
        "government": ("政府机构假期", "วันหยุดเฉพาะส่วนราชการ"),
        "bangkok": ("曼谷特别假期", "วันหยุดพิเศษเฉพาะกรุงเทพฯ"),
    }
    return labels.get(holiday_type, ("假期", "วันหยุด"))


def scope_phrase(holiday_type: str) -> tuple[str, str]:
    phrases = {
        "national": ("泰国放假", "ประเทศไทยหยุด"),
        "substitution": ("泰国补假", "ประเทศไทยเป็นวันหยุดชดเชย"),
        "private": ("泰国私营部门及银行放假", "ภาคเอกชนและธนาคารในประเทศไทยหยุด"),
        "government": ("泰国政府机构放假", "ส่วนราชการของประเทศไทยหยุด"),
        "bangkok": ("曼谷放假", "กรุงเทพมหานครหยุด"),
    }
    return phrases.get(holiday_type, ("泰国放假", "ประเทศไทยหยุด"))


def weekday_zh(d: date) -> str:
    return WEEKDAY_ZH[d.weekday()]


def weekday_th(d: date) -> str:
    return WEEKDAY_TH[d.weekday()]


def build_bodies(day_diff: int, holiday: Holiday) -> tuple[str, str]:
    """Return (zh_body, th_body) for the reminder stage."""
    d = holiday.date.isoformat()
    name = holiday.name_zh
    name_th = holiday.name_th
    wd = weekday_zh(holiday.date)
    wd_th = weekday_th(holiday.date)
    scope_zh, scope_th = scope_phrase(holiday.type)

    if day_diff == 30:
        zh = f"{d}，{scope_zh}（{name}），请提前安排工作及关注业务变化。"
        thai = (
            f"{d} {scope_th} ({name_th}) "
            "กรุณาจัดเตรียมงานล่วงหน้า และติดตามการเปลี่ยนแปลงของธุรกิจ"
        )
    elif day_diff == 7:
        zh = f"下周{wd}（{d}），{scope_zh}（{name}），请提前安排工作及关注业务变化。"
        thai = (
            f"สัปดาห์หน้า{wd_th} ({d}) {scope_th} ({name_th}) "
            "กรุณาจัดเตรียมงานล่วงหน้า และติดตามการเปลี่ยนแปลงของธุรกิจ"
        )
    elif day_diff == 3:
        zh = f"3天后（{wd}，{d}），{scope_zh}（{name}），请提前安排工作及关注业务变化。"
        thai = (
            f"อีก 3 วัน ({wd_th}, {d}) {scope_th} ({name_th}) "
            "กรุณาจัดเตรียมงานล่วงหน้า และติดตามการเปลี่ยนแปลงของธุรกิจ"
        )
    elif day_diff == 1:
        zh = f"明天（{wd}），{scope_zh}（{name}）；请关注业务变化及数据情况。"
        thai = (
            f"พรุ่งนี้ ({wd_th}) {scope_th} ({name_th}) "
            "กรุณาติดตามการเปลี่ยนแปลงของธุรกิจ และสถานะข้อมูล"
        )
    elif day_diff == 0:
        zh = f"今天{scope_zh}（{name}）；请各部门关注当前业务变化及数据情况。"
        thai = (
            f"วันนี้ {scope_th} ({name_th}) "
            "ขอให้ทุกฝ่ายติดตามการเปลี่ยนแปลงของธุรกิจ และสถานะข้อมูลในปัจจุบัน"
        )
    elif day_diff == -1:
        zh = f"假期结束；请各部门关注昨天业务变化及数据情况。（假期：{name}，{d}）"
        thai = (
            "วันหยุดสิ้นสุดแล้ว ขอให้ทุกฝ่ายติดตามการเปลี่ยนแปลงของธุรกิจ "
            f"และสถานะข้อมูลเมื่อวาน (วันหยุด: {name_th}, {d})"
        )
    else:
        raise ValueError(f"unsupported day_diff: {day_diff}")
    return zh, thai


def build_message(today: date, day_diff: int, holiday: Holiday) -> str:
    meta = REMINDER_META[day_diff]
    type_zh, type_th = type_label(holiday.type)
    zh_body, th_body = build_bodies(day_diff, holiday)
    return "\n".join(
        [
            f"🇹🇭 {meta['zh_title']} / {meta['th_title']}",
            "",
            f"假期：{holiday.name_zh}（{type_zh}）",
            f"วันที่: {holiday.date.isoformat()} · {holiday.name_th} ({type_th})",
            "",
            f"【中文】{zh_body}",
            "",
            f"【ไทย】{th_body}",
        ]
    )


def feishu_sign(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def validate_webhook_url(webhook_url: str) -> None:
    if not webhook_url.startswith("https://"):
        raise RuntimeError("FEISHU_WEBHOOK_URL must start with https://")
    if "hook" not in webhook_url:
        raise RuntimeError(
            "FEISHU_WEBHOOK_URL does not look like a Feishu/Lark bot webhook "
            "(expected .../bot/v2/hook/...)."
        )


def explain_feishu_error(result: dict) -> str:
    code = result.get("code", result.get("StatusCode"))
    hints = {
        19021: "签名校验失败：机器人若开启了签名，请把密钥配到 FEISHU_SECRET；若未开启，请删除该 Secret。",
        19022: "时间戳无效或过期：请确认 Actions runner 时间正常，并重试。",
        19024: "Webhook 地址无效或机器人已被移除：请重新复制群机器人 Webhook。",
        11232: "消息内容被拦截：请检查文案是否触发了飞书安全关键词。",
    }
    hint = hints.get(code, "请检查 FEISHU_WEBHOOK_URL / FEISHU_SECRET 是否配置正确。")
    return f"Feishu API error: {result}. Hint: {hint}"


def post_feishu(text: str, webhook_url: str, secret: str | None) -> None:
    validate_webhook_url(webhook_url)
    payload: dict = {
        "msg_type": "text",
        "content": {"text": text},
    }
    if secret:
        timestamp = str(int(datetime.now(TZ).timestamp()))
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Feishu HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Feishu request failed: {exc}") from exc

    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Feishu returned non-JSON body: {body}") from exc

    code = result.get("code", result.get("StatusCode"))
    if code not in (0, None):
        raise RuntimeError(explain_feishu_error(result))


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    args = parse_args()
    include_bangkok = args.include_bangkok_special or env_flag("INCLUDE_BANGKOK_SPECIAL")
    dry_run = args.dry_run or env_flag("DRY_RUN")

    today_raw = (args.today or os.getenv("TODAY_OVERRIDE") or "").strip() or None
    today = resolve_today(today_raw)
    years = {
        today.year,
        (today + timedelta(days=30)).year,
        (today - timedelta(days=1)).year,
    }
    holidays = load_holidays_for_years(years, include_bangkok=include_bangkok)
    matches = matched_reminders(today, holidays)

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.getenv("FEISHU_SECRET", "").strip() or None
    print(
        f"debug: today={today.isoformat()} dry_run={dry_run} "
        f"webhook_set={bool(webhook_url)} secret_set={bool(secret)} "
        f"matches={len(matches)} holidays_loaded={len(holidays)}"
    )

    if not matches:
        print(f"No holiday reminders for {today.isoformat()} (ICT).")
        return 0

    if not dry_run and not webhook_url:
        print("FEISHU_WEBHOOK_URL is required unless --dry-run is set.", file=sys.stderr)
        return 2

    for day_diff, holiday in matches:
        text = build_message(today, day_diff, holiday)
        print("---")
        print(text)
        if dry_run:
            continue
        try:
            post_feishu(text, webhook_url, secret)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print("posted: ok")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
