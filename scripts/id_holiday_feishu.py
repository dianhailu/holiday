#!/usr/bin/env python3
"""Send Feishu reminders for Indonesian public holidays.

Audience: China-based teammates monitoring Indonesia business and data.
Reminder offsets (relative to holiday date):
  +30, +7, +3, +1, 0 (today), -1 (day after holiday).

Timezone: Asia/Jakarta (WIB). Default schedule: 08:00 WIB via cron.
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

# Fixed WIB offset so CI does not depend on tzdata / ZoneInfo databases.
TZ = timezone(timedelta(hours=7))
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

# Monday=0 ... Sunday=6
WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
WEEKDAY_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")

# day_diff = holiday_date - today
REMINDER_META = {
    30: {"zh_title": "印尼业务假期 · 提前 30 天", "id_title": "Libur bisnis ID · H-30"},
    7: {"zh_title": "印尼业务假期 · 提前 7 天", "id_title": "Libur bisnis ID · H-7"},
    3: {"zh_title": "印尼业务假期 · 提前 3 天", "id_title": "Libur bisnis ID · H-3"},
    1: {"zh_title": "印尼业务假期 · 明天", "id_title": "Libur bisnis ID · H-1"},
    0: {"zh_title": "印尼业务假期 · 今天", "id_title": "Libur bisnis ID · hari ini"},
    -1: {"zh_title": "印尼业务假期 · 已结束", "id_title": "Libur bisnis ID · selesai"},
}


@dataclass(frozen=True)
class Holiday:
    date: date
    name_id: str
    name_zh: str
    type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--today",
        help="Override today as YYYY-MM-DD (Jakarta calendar date). Useful for dry runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages instead of posting to Feishu.",
    )
    parser.add_argument(
        "--include-joint-leave",
        action="store_true",
        help="Also remind for cuti bersama (joint leave), not only libur nasional.",
    )
    return parser.parse_args()


def resolve_today(override: str | None) -> date:
    if override:
        return date.fromisoformat(override)
    return datetime.now(TZ).date()


def load_holidays_for_years(years: set[int], include_joint_leave: bool) -> list[Holiday]:
    holidays: list[Holiday] = []
    for year in sorted(years):
        path = DATA_DIR / f"id-holidays-{year}.json"
        if not path.exists():
            print(f"warn: missing holiday file {path}", file=sys.stderr)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("holidays", []):
            holiday_type = item.get("type", "national")
            if holiday_type != "national" and not include_joint_leave:
                continue
            holidays.append(
                Holiday(
                    date=date.fromisoformat(item["date"]),
                    name_id=item["name_id"],
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
    if holiday_type == "joint_leave":
        return "集体假期", "cuti bersama"
    return "法定假期", "hari libur nasional"


def weekday_zh(d: date) -> str:
    return WEEKDAY_ZH[d.weekday()]


def weekday_id(d: date) -> str:
    return WEEKDAY_ID[d.weekday()]


def build_bodies(day_diff: int, holiday: Holiday) -> tuple[str, str]:
    """Return (zh_body, id_body) for the reminder stage."""
    d = holiday.date.isoformat()
    name = holiday.name_zh
    wd = weekday_zh(holiday.date)
    wd_id = weekday_id(holiday.date)

    if day_diff == 30:
        zh = f"{d}，印尼放假（{name}），请提前安排工作及关注业务变化。"
        idn = (
            f"{d}, Indonesia libur ({holiday.name_id}). "
            "Mohon siapkan pekerjaan lebih awal dan pantau perubahan bisnis."
        )
    elif day_diff == 7:
        zh = f"下周{wd}（{d}），印尼放假（{name}），请提前安排工作及关注业务变化。"
        idn = (
            f"Minggu depan {wd_id} ({d}), Indonesia libur ({holiday.name_id}). "
            "Mohon siapkan pekerjaan lebih awal dan pantau perubahan bisnis."
        )
    elif day_diff == 3:
        zh = f"3天后（{wd}，{d}），印尼放假（{name}），请提前安排工作及关注业务变化。"
        idn = (
            f"3 hari lagi ({wd_id}, {d}), Indonesia libur ({holiday.name_id}). "
            "Mohon siapkan pekerjaan lebih awal dan pantau perubahan bisnis."
        )
    elif day_diff == 1:
        zh = f"明天（{wd}），印尼放假（{name}）；请关注业务变化及数据情况。"
        idn = (
            f"Besok ({wd_id}), Indonesia libur ({holiday.name_id}). "
            "Mohon pantau perubahan bisnis dan kondisi data."
        )
    elif day_diff == 0:
        zh = f"今天印尼放假（{name}）；请各部门关注当前业务变化及数据情况。"
        idn = (
            f"Hari ini Indonesia libur ({holiday.name_id}). "
            "Mohon seluruh tim pantau perubahan bisnis dan kondisi data saat ini."
        )
    elif day_diff == -1:
        zh = f"假期结束；请各部门关注昨天业务变化及数据情况。（假期：{name}，{d}）"
        idn = (
            f"Libur telah berakhir; mohon seluruh tim pantau perubahan bisnis "
            f"dan kondisi data kemarin. (Libur: {holiday.name_id}, {d})"
        )
    else:
        raise ValueError(f"unsupported day_diff: {day_diff}")
    return zh, idn


def build_message(today: date, day_diff: int, holiday: Holiday) -> str:
    meta = REMINDER_META[day_diff]
    type_zh, type_id = type_label(holiday.type)
    zh_body, id_body = build_bodies(day_diff, holiday)
    return "\n".join(
        [
            f"🇮🇩 {meta['zh_title']} / {meta['id_title']}",
            "",
            f"假期：{holiday.name_zh}（{type_zh}）",
            f"Tanggal: {holiday.date.isoformat()} · {holiday.name_id} ({type_id})",
            "",
            f"【中文】{zh_body}",
            "",
            f"【Indonesia】{id_body}",
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

    # Feishu custom bots usually return {"code":0,"msg":"success"}
    code = result.get("code", result.get("StatusCode"))
    if code not in (0, None):
        raise RuntimeError(explain_feishu_error(result))


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    args = parse_args()
    include_joint_leave = args.include_joint_leave or env_flag("INCLUDE_JOINT_LEAVE")
    dry_run = args.dry_run or env_flag("DRY_RUN")

    today_raw = (args.today or os.getenv("TODAY_OVERRIDE") or "").strip() or None
    today = resolve_today(today_raw)
    years = {
        today.year,
        (today + timedelta(days=30)).year,
        (today - timedelta(days=1)).year,
    }
    holidays = load_holidays_for_years(years, include_joint_leave=include_joint_leave)
    matches = matched_reminders(today, holidays)

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    secret = os.getenv("FEISHU_SECRET", "").strip() or None
    print(
        f"debug: today={today.isoformat()} dry_run={dry_run} "
        f"webhook_set={bool(webhook_url)} secret_set={bool(secret)} "
        f"matches={len(matches)} holidays_loaded={len(holidays)}"
    )

    if not matches:
        print(f"No holiday reminders for {today.isoformat()} (WIB).")
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
