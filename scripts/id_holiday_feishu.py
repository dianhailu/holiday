#!/usr/bin/env python3
"""Send Feishu reminders for Indonesian public holidays.

Reminder offsets (relative to holiday date):
  +7 days, +1 day, 0 (today), -1 day (day after holiday).

Timezone: Asia/Jakarta. Default run time is expected to be 08:00 WIB via cron.
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

# day_diff = holiday_date - today
REMINDER_COPY = {
    7: {
        "zh_title": "提前 7 天提醒",
        "id_title": "Pengingat 7 hari sebelumnya",
        "zh_body": "下周将迎来印尼法定假期，请提前安排工作与排班。",
        "id_body": "Minggu depan ada hari libur nasional Indonesia. Mohon siapkan jadwal kerja lebih awal.",
    },
    1: {
        "zh_title": "明天放假提醒",
        "id_title": "Pengingat sehari sebelumnya",
        "zh_body": "明天是印尼法定假期，请今天完成收尾并设置自动回复。",
        "id_body": "Besok adalah hari libur nasional Indonesia. Selesaikan pekerjaan hari ini dan aktifkan auto-reply.",
    },
    0: {
        "zh_title": "今天放假提醒",
        "id_title": "Pengingat hari ini",
        "zh_body": "今天是印尼法定假期，请勿安排紧急会议或关键交付。",
        "id_body": "Hari ini adalah hari libur nasional Indonesia. Hindari rapat mendesak atau pengiriman kritis.",
    },
    -1: {
        "zh_title": "假期后一天提醒",
        "id_title": "Pengingat sehari setelah libur",
        "zh_body": "印尼法定假期已结束，请恢复正常工作与考勤。",
        "id_body": "Hari libur nasional Indonesia telah berakhir. Silakan kembali ke ritme kerja normal.",
    },
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
    wanted = set(REMINDER_COPY)
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


def build_message(today: date, day_diff: int, holiday: Holiday) -> str:
    copy = REMINDER_COPY[day_diff]
    type_zh, type_id = type_label(holiday.type)
    return "\n".join(
        [
            f"🇮🇩 {copy['zh_title']} / {copy['id_title']}",
            "",
            f"【中文】{holiday.name_zh}（{type_zh}）",
            f"日期：{holiday.date.isoformat()}",
            copy["zh_body"],
            "",
            f"【Indonesia】{holiday.name_id} ({type_id})",
            f"Tanggal: {holiday.date.isoformat()}",
            copy["id_body"],
            "",
            f"检查日期 / Tanggal cek: {today.isoformat()} (WIB)",
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
    years = {today.year, (today + timedelta(days=7)).year, (today - timedelta(days=1)).year}
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
