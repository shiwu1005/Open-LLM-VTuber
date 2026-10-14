"""Structured two-tier memory: short-term table + long-term table.

Storage format is deliberately human-readable Markdown tables so the user can
open, read and hand-edit the files.

    memory/
    ├── long_term.md      # LTM table
    ├── short_term.md     # STM table
    ├── day_counter.json  # "day N" counter
    └── memory.log        # operation log

Design notes
------------
* Two tiers:
    STM = raw per-turn records, short
    LTM = consolidated, spans a time range and possibly a scene transition
* Fixation ("固化"): when STM reaches `stm_fix_threshold` entries, the agent
  asks the LLM to merge them into 1..N LTM rows, then STM is cleared and
  renumbered from S001.
* "day N" counts from the first conversation day by default.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .archive import ArchiveStore, DateResolver

LTM_TABLE_RE = re.compile(r"^\|\s*L\d+\s*\|", re.MULTILINE)
STM_TABLE_RE = re.compile(r"^\|\s*S\d+\s*\|", re.MULTILINE)

LTM_HEADER = """# 长期记忆（LTM）

> 由记忆系统自动维护，也可以手动编辑。
> 格式：编号 | 时间（起 第n天 → 止 第n天）| 场景（起 → 止）| 事件（客观事实）

| 编号 | 时间 | 场景 | 事件（客观事实） |
| --- | --- | --- | --- |
"""

STM_HEADER = """# 短期记忆（STM）

> 由记忆系统自动维护。条数达到阈值后会被固化进 long_term.md 并清空。
> 格式：编号 | 时间 | 场景 | 事件（客观事实）

| 编号 | 时间 | 场景 | 事件（客观事实） |
| --- | --- | --- | --- |
"""


@dataclass
class MemoryRow:
    """One row of either table."""

    index: int                      # numeric part, e.g. 1 for L001
    time_start: str                 # "2026-10-14 21:30 第1天"
    time_end: str = ""              # LTM only, "" means same as start
    scene_start: str = "日常"
    scene_end: str = ""             # LTM only, "" means same as start
    event: str = ""

    # ---- formatting -------------------------------------------------------
    @property
    def time_cell(self) -> str:
        if self.time_end and self.time_end != self.time_start:
            return f"{self.time_start} → {self.time_end}"
        return self.time_start

    @property
    def scene_cell(self) -> str:
        if self.scene_end and self.scene_end != self.scene_start:
            return f"{self.scene_start} → {self.scene_end}"
        return self.scene_start

    def to_ltm_line(self) -> str:
        return f"| L{self.index:03d} | {self.time_cell} | {self.scene_cell} | {self.event} |"

    def to_stm_line(self) -> str:
        return f"| S{self.index:03d} | {self.time_start} | {self.scene_start} | {self.event} |"

    # ---- parsing ----------------------------------------------------------
    @staticmethod
    def _split_pair(cell: str) -> tuple[str, str]:
        """Split a 'A → B' cell into (A, B); B is '' when there is no arrow."""
        if "→" in cell:
            left, right = cell.split("→", 1)
            return left.strip(), right.strip()
        if "->" in cell:
            left, right = cell.split("->", 1)
            return left.strip(), right.strip()
        return cell.strip(), ""

    @classmethod
    def from_ltm_line(cls, line: str) -> "MemoryRow | None":
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            return None
        m = re.match(r"L?(\d+)", cells[0])
        if not m:
            return None
        t_start, t_end = cls._split_pair(cells[1])
        s_start, s_end = cls._split_pair(cells[2])
        return cls(
            index=int(m.group(1)),
            time_start=t_start,
            time_end=t_end,
            scene_start=s_start or "日常",
            scene_end=s_end,
            event=cells[3],
        )

    @classmethod
    def from_stm_line(cls, line: str) -> "MemoryRow | None":
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            return None
        m = re.match(r"S?(\d+)", cells[0])
        if not m:
            return None
        return cls(
            index=int(m.group(1)),
            time_start=cells[1],
            scene_start=cells[2] or "日常",
            event=cells[3],
        )


@dataclass
class MemoryConfig:
    """Runtime configuration for the memory system."""

    enabled: bool = True
    dir: str = "memory"
    stm_fix_threshold: int = 7
    max_ltm_in_prompt: int = 40
    max_stm_in_prompt: int = 7
    day_start_date: str = ""
    scene_options: list[str] = field(
        default_factory=lambda: ["工作", "学习", "游戏", "影音", "深夜闲聊", "日常"]
    )
    # "extract" = a separate LLM call reads the turn and reports the memory
    #             (reliable, costs one extra small call)
    # "inline"  = the model appends an invisible <mem> tag to its own reply
    #             (free, but we measured it being dropped on long prompts)
    memory_mode: str = "extract"
    # Which entry of llm_configs to use for memory extraction/consolidation.
    # Empty = reuse the character's main LLM. Point this at a small
    # non-reasoning model for much faster memory writes.
    memory_llm_provider: str = ""
    # Token budget for the extraction / consolidation calls. Reasoning models
    # spend most of their budget "thinking", so this must be generous or the
    # JSON never appears (observed: empty output at 300 tokens).
    internal_max_tokens: int = 2048
    # Consolidation merges 7+ rows into 1..3, so it needs more room than a
    # single-turn extraction (reasoning models especially).
    consolidation_max_tokens: int = 4096
    # Seconds to wait for an internal extraction/consolidation call.
    # Consolidation is a much bigger ask than a single-turn extraction: it has
    # to read 7+ rows and emit 1..3 merged ones, and a reasoning model spends a
    # lot of that budget thinking. Observed: 90s was not enough, which left STM
    # growing past the threshold with nothing written to LTM.
    internal_timeout: float = 90.0
    consolidation_timeout: float = 300.0

    @classmethod
    def from_dict(cls, data: dict | None) -> "MemoryConfig":
        data = data or {}
        mode = str(data.get("memory_mode", "extract")).strip().lower()
        if mode not in ("extract", "inline"):
            mode = "extract"
        return cls(
            enabled=bool(data.get("enabled", True)),
            dir=str(data.get("dir", "memory")),
            stm_fix_threshold=int(data.get("stm_fix_threshold", 7)),
            max_ltm_in_prompt=int(data.get("max_ltm_in_prompt", 40)),
            max_stm_in_prompt=int(data.get("max_stm_in_prompt", 7)),
            day_start_date=str(data.get("day_start_date", "") or ""),
            scene_options=list(
                data.get("scene_options")
                or ["工作", "学习", "游戏", "影音", "深夜闲聊", "日常"]
            ),
            memory_mode=mode,
            memory_llm_provider=str(data.get("memory_llm_provider", "") or "").strip(),
            internal_max_tokens=int(data.get("internal_max_tokens", 2048)),
            consolidation_max_tokens=int(data.get("consolidation_max_tokens", 4096)),
            internal_timeout=float(data.get("internal_timeout", 90.0)),
            consolidation_timeout=float(data.get("consolidation_timeout", 300.0)),
        )


class MemoryManager:
    """Reads/writes the two Markdown tables and tracks the day counter."""

    def __init__(self, project_root: Path | str, config: MemoryConfig):
        self.config = config
        self.root = Path(project_root) / config.dir
        self.ltm_path = self.root / "long_term.md"
        self.stm_path = self.root / "short_term.md"
        self.day_path = self.root / "day_counter.json"
        self.log_path = self.root / "memory.log"
        self._ensure_files()

        # daily archive + date index (see memory/archive.py)
        self.archive = ArchiveStore(self.root, self._start_date())
        self.last_recall: list[str] = []

    # ------------------------------------------------------------------ setup
    def _ensure_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.ltm_path.exists():
            self.ltm_path.write_text(LTM_HEADER, encoding="utf-8")
        if not self.stm_path.exists():
            self.stm_path.write_text(STM_HEADER, encoding="utf-8")
        if not self.day_path.exists():
            start = self.config.day_start_date or date.today().isoformat()
            self.day_path.write_text(
                json.dumps(
                    {"start_date": start, "today": date.today().isoformat(), "day": 1},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    def log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {message}\n")
        except Exception:
            pass  # logging must never break a conversation

    # ------------------------------------------------------------- day counter
    def _start_date(self) -> date:
        """The date that counts as day 1."""
        raw = self.config.day_start_date
        if not raw:
            try:
                raw = json.loads(self.day_path.read_text(encoding="utf-8")).get(
                    "start_date", ""
                )
            except Exception:
                raw = ""
        try:
            return date.fromisoformat(str(raw)[:10])
        except (ValueError, TypeError):
            return date.today()

    def current_day(self) -> int:
        """Return the current 'day N', advancing it when the date changed."""
        try:
            data = json.loads(self.day_path.read_text(encoding="utf-8"))
        except Exception:
            data = {"start_date": date.today().isoformat(), "day": 1}

        start_raw = self.config.day_start_date or data.get("start_date") or date.today().isoformat()
        try:
            start = date.fromisoformat(str(start_raw)[:10])
        except ValueError:
            start = date.today()

        day = (date.today() - start).days + 1
        if day < 1:
            day = 1

        if data.get("day") != day or str(data.get("start_date")) != start.isoformat():
            data = {
                "start_date": start.isoformat(),
                "today": date.today().isoformat(),
                "day": day,
            }
            try:
                self.day_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception:
                pass
        return day

    def stamp(self) -> str:
        """e.g. '2026-10-14 21:30 第1天'"""
        return f"{datetime.now().strftime('%Y-%m-%d %H:%M')} 第{self.current_day()}天"

    # ----------------------------------------------------------------- reading
    def read_ltm(self) -> list[MemoryRow]:
        rows: list[MemoryRow] = []
        if not self.ltm_path.exists():
            return rows
        for line in self.ltm_path.read_text(encoding="utf-8").splitlines():
            if LTM_TABLE_RE.match(line):
                row = MemoryRow.from_ltm_line(line)
                if row:
                    rows.append(row)
        return rows

    def read_stm(self) -> list[MemoryRow]:
        rows: list[MemoryRow] = []
        if not self.stm_path.exists():
            return rows
        for line in self.stm_path.read_text(encoding="utf-8").splitlines():
            if STM_TABLE_RE.match(line):
                row = MemoryRow.from_stm_line(line)
                if row:
                    rows.append(row)
        return rows

    # ----------------------------------------------------------------- writing
    def _append_line(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def add_stm(self, scene: str, event: str) -> MemoryRow:
        """Append one short-term row, and archive it under today's date."""
        rows = self.read_stm()
        next_index = (max((r.index for r in rows), default=0)) + 1
        stamp = self.stamp()
        row = MemoryRow(
            index=next_index,
            time_start=stamp,
            scene_start=(scene or "日常").strip(),
            event=(event or "").strip(),
        )
        self._append_line(self.stm_path, row.to_stm_line())
        self.log(f"STM+ S{row.index:03d} [{row.scene_start}] {row.event}")

        # daily archive: same fact, filed under its date
        try:
            self.archive.append_entry(
                date.today(), row.scene_start, row.event, stamp
            )
        except Exception as exc:
            self.log(f"archive failed: {exc}")

        return row

    def add_ltm(
        self,
        time_start: str,
        time_end: str,
        scene_start: str,
        scene_end: str,
        event: str,
    ) -> MemoryRow:
        """Append one consolidated long-term row and return it.

        Scene labels are normalised here rather than by the caller, so any code
        path (and any hand-written call) is guaranteed clean labels.
        """
        rows = self.read_ltm()
        next_index = (max((r.index for r in rows), default=0)) + 1
        scene_start = self.normalize_scene(scene_start)
        scene_end = self.normalize_scene(scene_end) if (scene_end or "").strip() else ""
        row = MemoryRow(
            index=next_index,
            time_start=time_start.strip(),
            time_end=(time_end or "").strip(),
            scene_start=scene_start,
            scene_end=scene_end,
            event=(event or "").strip(),
        )
        self._append_line(self.ltm_path, row.to_ltm_line())
        self.log(f"LTM+ L{row.index:03d} [{row.scene_cell}] {row.event}")
        return row

    def clear_stm(self) -> None:
        """Clear the short-term table (used after fixation)."""
        self.stm_path.write_text(STM_HEADER, encoding="utf-8")
        self.log("STM cleared after fixation")

    # ------------------------------------------------------- scene normalising
    def normalize_scene(self, scene: str) -> str:
        """Snap a model-supplied scene onto one of the configured options.

        Small models like to invent variants ("日常工作", "日常交流"). Left alone
        these break the scene-transition logic, because "日常工作" != "工作" and
        every row then looks like a transition. This is deliberately
        deterministic - no extra LLM call.
        """
        raw = (scene or "").strip().strip("，。、,.。:：")
        if not raw:
            return "日常"

        options = list(self.config.scene_options or [])
        if not options:
            return raw

        # 1) exact
        for opt in options:
            if raw == opt:
                return opt

        # 2) An option appearing inside the model's text is a strong signal:
        #    "日常工作" -> "工作", "游戏娱乐" -> "游戏", "工作相关" -> "工作".
        #    Take the LONGEST match so "深夜闲聊" wins over "闲聊".
        #
        #    We deliberately do NOT match the other direction (model text being
        #    a prefix of an option): a bare "闲聊" would then snap to "深夜闲聊",
        #    and a bare "深夜" likewise, which reads as a scene change when the
        #    user just meant small talk.
        contained = [opt for opt in options if opt in raw]
        if contained:
            return max(contained, key=len)

        # 3) fall back to the default wording
        for fallback in ("日常",):
            if fallback in options:
                return fallback
        return options[0]

    # ------------------------------------------------------------- fixation
    def need_fixation(self) -> bool:
        """True when STM count has reached the threshold."""
        return len(self.read_stm()) >= self.config.stm_fix_threshold

    def fixation_payload(self) -> list[dict]:
        """The STM rows to hand to the LLM for merging."""
        return [
            {
                "id": f"S{r.index:03d}",
                "time": r.time_start,
                "scene": r.scene_start,
                "event": r.event,
            }
            for r in self.read_stm()
        ]

    # ------------------------------------------------------- prompt injection
    def prompt_block(self, user_text: str = "") -> str:
        """The memory section injected into the system prompt.

        If `user_text` mentions a date ("昨天", "上周三", "10月14日", "第3天"…),
        the matching daily archives are pulled in automatically.
        """
        ltm = self.read_ltm()
        stm = self.read_stm()

        ltm_shown = ltm[-self.config.max_ltm_in_prompt :]
        stm_shown = stm[-self.config.max_stm_in_prompt :]

        lines = [
            "",
            "=============== 你的记忆 ===============",
            f"（第 {self.current_day()} 天）",
            "",
        ]

        if ltm_shown:
            lines.append("【长期记忆】编号 | 时间 | 场景 | 事件")
            for r in ltm_shown:
                lines.append(f"{r.to_ltm_line().strip('| ').replace(' | ', ' | ')}")
        else:
            lines.append("【长期记忆】（还什么都没有）")

        lines.append("")

        if stm_shown:
            lines.append("【短期记忆】编号 | 时间 | 场景 | 事件")
            for r in stm_shown:
                lines.append(f"{r.to_stm_line().strip('| ').replace(' | ', ' | ')}")
        else:
            lines.append("【短期记忆】（本轮之前还没有记录）")

        if ltm and len(ltm) > len(ltm_shown):
            lines.append(f"（更早的 {len(ltm) - len(ltm_shown)} 条长期记忆已省略）")

        # ---- date archive: index + on-demand recall ----
        index = ""
        try:
            index = self.archive.index_summary()
        except Exception as exc:
            self.log(f"archive index failed: {exc}")

        if index:
            lines.append("")
            lines.append(f"【记忆归档索引】{index}")

            recalled_dates: list[date] = []
            if user_text:
                try:
                    recalled_dates = self.archive.resolver.resolve(user_text)
                except Exception as exc:
                    self.log(f"date resolve failed: {exc}")

            if recalled_dates:
                self.last_recall = [d.isoformat() for d in recalled_dates]
                try:
                    recall_text = self.archive.recall(recalled_dates)
                except Exception as exc:
                    recall_text = ""
                    self.log(f"archive recall failed: {exc}")
                if recall_text:
                    lines.append("")
                    lines.append("【你提到的日期，以下是那天的详细记忆】")
                    lines.append(recall_text)

        lines.append("")
        lines.append("以上是你真实的记忆。自然地使用它们，不要逐条复述，也不要说“根据我的记忆”。")

        if self.need_fixation():
            lines.append("")
            lines.append(
                f"【重要】短期记忆已达 {len(stm)} 条，本轮结束后需要固化。"
                "请在回复末尾的记忆标签里把 consolidation 设为 true。"
            )

        lines.append("=======================================")
        return "\n".join(lines)

    def record_instruction(self) -> str:
        """Instruction appended to the system prompt (inline mode only).

        In "extract" mode nothing is appended: the model talks normally and a
        separate call reads the turn afterwards.
        """
        if self.config.memory_mode != "inline":
            return ""
        scenes = " / ".join(self.config.scene_options)
        return (
            "\n\n"
            "=============== 记忆记录要求 ===============\n"
            "每次回复的最后，你必须附加一行记忆标签（用户看不到它，不要解释、不要提及）：\n"
            '<mem>{"scene":"场景","event":"本轮客观事实"}</mem>\n'
            f"- scene：从这些里选最贴近的一个：{scenes}\n"
            "- event：一句话客观事实（谁做了什么、发生了什么），不要写你的主观感受\n"
            "- 这一行必须放在回复的最末尾，格式严格如上\n"
            "=========================================="
        )

    def extract_prompt(self, user_text: str, assistant_text: str) -> str:
        """Prompt for the separate extraction call (extract mode)."""
        scenes = " / ".join(self.config.scene_options)
        return (
            "下面是一轮对话。请客观记录本轮发生了什么，供长期档案使用。\n\n"
            f"【用户说】{user_text}\n"
            f"【角色回】{assistant_text}\n\n"
            "要求：\n"
            "- event：一句话概括本轮发生的客观事实（用户说了什么、做了什么、\n"
            "  有什么决定或状态变化、达成了什么）。不要写角色的主观感受。\n"
            "- 如果本轮只是寒暄、没有值得长期记住的事实，event 填空字符串。\n"
            f"- scene：从这些里选最贴近当前情境的一个：{scenes}\n\n"
            "只输出如下 JSON，不要任何其它文字：\n"
            '{"scene":"场景","event":"客观事实"}'
        )
