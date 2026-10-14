"""Daily archive with a date index, plus date-reference resolution.

Layout
------
    memory/
    ├── long_term.md            # consolidated LTM table
    ├── short_term.md           # current STM table
    ├── day_counter.json
    ├── archive/
    │   ├── INDEX.md            # human-readable date index
    │   ├── index.json          # machine-readable index
    │   ├── 2026-10-14.md       # that day's memory, in the same table format
    │   └── 2026-10-15.md
    └── memory.log

Why a separate archive rather than only LTM/STM:
  * LTM is consolidated and lossy - good for "what do I know about him"
  * the archive is exact   - good for "what did we talk about on the 14th"

Retrieval is deliberately deterministic: date expressions in the user's message
are resolved with regex + calendar arithmetic and the matching archive files are
loaded. No extra LLM call is needed to decide *which* day to open.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

ARCHIVE_INDEX_HEADER = """# 记忆归档索引

> 每天一个归档文件，按日期列出当天发生的事情。
> 提到某天时，记忆系统会自动调取对应归档。

| 日期 | 星期 | 第n天 | 条数 | 摘要 |
| --- | --- | --- | --- | --- |
"""

# ---------------------------------------------------------------- date parsing

_WEEKDAY_CN = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_WEEKDAY_NAME = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_RE_TODAY = re.compile(r"今天|今日|当天")
_RE_YESTERDAY = re.compile(r"昨天|昨日|前一晚|昨晚")
_RE_DAY_BEFORE = re.compile(r"前天")
_RE_TOMORROW = re.compile(r"明天|明日|第二天")
_RE_N_DAYS_AGO = re.compile(r"(\d{1,3})\s*天前")
_RE_LAST_WEEK = re.compile(r"上周|上个?星期|上星期")
_RE_THIS_WEEK = re.compile(r"这周|本周|这个?星期")
_RE_LAST_WEEKDAY = re.compile(r"上周([一二三四五六日天1-7])")
_RE_DAY_N = re.compile(r"第\s*(\d{1,4})\s*天")
_RE_ISO = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
_RE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_RE_MONTH = re.compile(r"(20\d{2})年(\d{1,2})月")


class DateResolver:
    """Turns natural-language date references into concrete dates."""

    def __init__(self, start_date: date, today: date | None = None):
        self.start_date = start_date
        self.today = today or date.today()

    def day_number(self, d: date) -> int:
        return max(1, (d - self.start_date).days + 1)

    def date_of_day_number(self, n: int) -> date:
        return self.start_date + timedelta(days=max(1, n) - 1)

    def resolve(self, text: str, max_results: int = 3) -> list[date]:
        """Return the dates referenced in `text`, most specific first."""
        if not text:
            return []
        found: list[date] = []

        def add(d: date) -> None:
            if d not in found:
                found.append(d)

        # explicit ISO-ish: 2026-10-14 / 2026年10月14日
        for m in _RE_ISO.finditer(text):
            try:
                add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            except ValueError:
                pass

        # 10月14日 / 10月14号  (assume current year)
        for m in _RE_MD.finditer(text):
            try:
                add(date(self.today.year, int(m.group(1)), int(m.group(2))))
            except ValueError:
                pass

        # "第 12 天"
        for m in _RE_DAY_N.finditer(text):
            add(self.date_of_day_number(int(m.group(1))))

        # relative days
        if _RE_TODAY.search(text):
            add(self.today)
        if _RE_YESTERDAY.search(text):
            add(self.today - timedelta(days=1))
        if _RE_DAY_BEFORE.search(text):
            add(self.today - timedelta(days=2))
        if _RE_TOMORROW.search(text):
            add(self.today + timedelta(days=1))
        for m in _RE_N_DAYS_AGO.finditer(text):
            add(self.today - timedelta(days=int(m.group(1))))

        # 上周三
        for m in _RE_LAST_WEEKDAY.finditer(text):
            target = _WEEKDAY_CN.get(m.group(1))
            if target is not None:
                add(self._weekday_of_last_week(target))

        # a bare "上周" -> the whole previous week, Monday..Sunday
        if _RE_LAST_WEEK.search(text) and not _RE_LAST_WEEKDAY.search(text):
            monday = self.today - timedelta(days=self.today.weekday() + 7)
            for i in range(7):
                add(monday + timedelta(days=i))

        if _RE_THIS_WEEK.search(text):
            monday = self.today - timedelta(days=self.today.weekday())
            for i in range(7):
                d = monday + timedelta(days=i)
                if d <= self.today:
                    add(d)

        # never return future dates (nothing archived there yet)
        found = [d for d in found if d <= self.today]
        return found[:max_results]

    def _weekday_of_last_week(self, target: int) -> date:
        """'上周三' relative to today."""
        monday_this = self.today - timedelta(days=self.today.weekday())
        monday_last = monday_this - timedelta(days=7)
        return monday_last + timedelta(days=target)


class ArchiveStore:
    """Daily archive files plus their index."""

    def __init__(self, memory_root: Path, start_date: date):
        self.root = memory_root
        self.dir = memory_root / "archive"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_md = self.dir / "INDEX.md"
        self.index_json = self.dir / "index.json"
        self.resolver = DateResolver(start_date)

    # ------------------------------------------------------------------ paths
    def path_for(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.md"

    # ------------------------------------------------------------------ write
    def append_entry(self, d: date, scene: str, event: str, stamp: str) -> None:
        """Append one row to that day's archive, creating it if needed."""
        path = self.path_for(d)
        if not path.exists():
            day_no = self.resolver.day_number(d)
            path.write_text(
                f"# {d.isoformat()}（{_WEEKDAY_NAME[d.weekday()]}，第{day_no}天）记忆归档\n\n"
                "| 编号 | 时间 | 场景 | 事件（客观事实） |\n"
                "| --- | --- | --- | --- |\n",
                encoding="utf-8",
            )

        rows = self._count_rows(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"| A{rows + 1:03d} | {stamp} | {scene} | {event} |\n")
        self._rebuild_index()

    @staticmethod
    def _count_rows(path: Path) -> int:
        try:
            return sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if re.match(r"^\|\s*A\d+\s*\|", line)
            )
        except Exception:
            return 0

    # ------------------------------------------------------------------- read
    def available_dates(self) -> list[date]:
        out: list[date] = []
        for p in sorted(self.dir.glob("20*.md")):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return out

    def read_day(self, d: date) -> list[dict]:
        """Rows of one archived day."""
        path = self.path_for(d)
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not re.match(r"^\|\s*A\d+\s*\|", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4:
                rows.append(
                    {"id": cells[0], "time": cells[1], "scene": cells[2], "event": cells[3]}
                )
        return rows

    def read_day_text(self, d: date) -> str:
        path = self.path_for(d)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ------------------------------------------------------------------ index
    def _rebuild_index(self) -> None:
        dates = self.available_dates()
        entries: list[dict] = []
        lines = [ARCHIVE_INDEX_HEADER]

        for d in dates:
            rows = self.read_day(d)
            summary = rows[-1]["event"] if rows else ""
            if len(summary) > 40:
                summary = summary[:40] + "…"
            day_no = self.resolver.day_number(d)
            weekday = _WEEKDAY_NAME[d.weekday()]
            entries.append(
                {
                    "date": d.isoformat(),
                    "weekday": weekday,
                    "day_number": day_no,
                    "rows": len(rows),
                    "summary": summary,
                }
            )
            lines.append(f"| {d.isoformat()} | {weekday} | 第{day_no}天 | {len(rows)} | {summary} |")

        self.index_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.index_json.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def index_summary(self, max_days: int = 14) -> str:
        """Compact list of archived days, for prompt injection."""
        dates = self.available_dates()
        if not dates:
            return ""
        shown = dates[-max_days:]
        parts = []
        for d in shown:
            rows = self.read_day(d)
            day_no = self.resolver.day_number(d)
            parts.append(
                f"{d.isoformat()}({_WEEKDAY_NAME[d.weekday()]} 第{day_no}天, {len(rows)}条)"
            )
        head = f"已有归档共 {len(dates)} 天"
        if len(dates) > len(shown):
            head += f"，以下是最近 {len(shown)} 天"
        return head + "：" + "、".join(parts)

    def recall(self, targets: Iterable[date], max_rows_per_day: int = 12) -> str:
        """Build a recall block for specific dates."""
        blocks: list[str] = []
        for d in targets:
            rows = self.read_day(d)
            if not rows:
                continue
            day_no = self.resolver.day_number(d)
            lines = [f"【{d.isoformat()}（{_WEEKDAY_NAME[d.weekday()]}，第{day_no}天）】"]
            for r in rows[-max_rows_per_day:]:
                lines.append(f"{r['time']} | {r['scene']} | {r['event']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)
