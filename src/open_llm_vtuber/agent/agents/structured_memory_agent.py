"""Structured two-tier memory agent.

Adds a table-based memory system on top of BasicMemoryAgent, without modifying
it. Select it with:

    conversation_agent_choice: 'structured_memory_agent'

What it does that BasicMemoryAgent does not:

1. Injects a memory block (LTM + STM tables) into the system prompt before
   every turn, so the character genuinely "remembers".
2. Asks the model to append an invisible tag to each reply:

       <mem>{"scene":"工作","event":"主人说项目要上线了"}</mem>

   The tag is stripped before the text reaches TTS or the UI, so it is never
   spoken or displayed.
3. Records one short-term row per turn.
4. When STM reaches the configured threshold, consolidates (固化) the short-term
   rows into long-term rows via one extra LLM call, then clears STM.

If the model forgets the tag, the turn simply is not recorded - no error.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

from loguru import logger

from .basic_memory_agent import BasicMemoryAgent
from ...memory import MemoryConfig, MemoryManager
from ..output_types import SentenceOutput
from ..input_types import BatchInput

# Matches the memory tag, tolerant of whitespace and an optional code fence.
MEM_TAG_RE = re.compile(r"<mem>\s*(\{.*?\})\s*</mem>", re.DOTALL | re.IGNORECASE)
# Anything that looks like a partially streamed memory tag must never reach the
# user, so we also hide a lone opening tag.
MEM_OPEN_RE = re.compile(r"<mem>", re.IGNORECASE)

FIXATION_SYSTEM = (
    "你是一个记忆整理器。只输出 JSON，不要输出任何解释、前后缀或代码块标记。"
)

EXTRACT_SYSTEM = (
    "你是一个客观的记录员。只输出 JSON，不要输出任何解释、前后缀或代码块标记。"
)

_OPEN = "<mem>"
_CLOSE = "</mem>"


class MemoryTagStreamFilter:
    """Incrementally strips <mem>...</mem> from a token stream.

    Token chunks arrive at arbitrary boundaries, so a tag may be split across
    chunks ("<me" + "m>{...}"). Text is emitted as early as possible; only an
    ambiguous tail is held back:

      * a complete "<mem>" starts a tag -> everything until "</mem>" is dropped
      * a trailing partial prefix ("<", "<m", "<me") is held until resolved

    During a tag we still scan for a complete "open...close" pair, so several
    tags inside one chunk are all stripped (not just the first).

    Usage:
        f = MemoryTagStreamFilter()
        for chunk in stream:
            out = f.feed(chunk)
            if out:
                yield out
        tail = f.flush()
        raw  = f.raw      # untouched original text, for parsing
    """

    def __init__(self) -> None:
        self._pending = ""
        self._raw = ""
        self._in_tag = False

    @staticmethod
    def _find_tag(text: str) -> int:
        """Index of a complete <mem>...</mem> pair, or -1."""
        low = text.lower()
        start = low.find(_OPEN)
        if start == -1:
            return -1
        end = low.find(_CLOSE, start + len(_OPEN))
        return start if end != -1 else -1

    @staticmethod
    def _partial_prefix_len(text: str) -> int:
        """Length of a trailing fragment that could grow into '<mem>'."""
        low = text.lower()
        for n in range(min(len(_OPEN) - 1, len(text)), 0, -1):
            if low.endswith(_OPEN[:n]):
                return n
        return 0

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._raw += chunk
        self._pending += chunk
        out: list[str] = []

        while self._pending:
            pair = self._find_tag(self._pending)
            if pair != -1:
                # Emit text before the tag, drop the tag, keep scanning.
                if pair:
                    out.append(self._pending[:pair])
                close = self._pending.lower().find(_CLOSE, pair + len(_OPEN))
                self._pending = self._pending[close + len(_CLOSE):]
                self._in_tag = False
                continue

            if self._in_tag:
                # Inside an unterminated tag: hold everything.
                break

            low = self._pending.lower()
            idx = low.find(_OPEN)
            if idx != -1:
                out.append(self._pending[:idx])
                self._pending = self._pending[idx + len(_OPEN):]
                self._in_tag = True
                continue

            # No tag: emit all but a possible partial prefix at the very end.
            hold = self._partial_prefix_len(self._pending)
            cut = len(self._pending) - hold
            if cut > 0:
                out.append(self._pending[:cut])
                self._pending = self._pending[cut:]
            break

        return "".join(out)

    def flush(self) -> str:
        """Release anything still buffered (the model finished talking)."""
        if self._in_tag:
            # Unterminated tag: discard the fragment rather than leak it.
            self._pending = ""
            self._in_tag = False
            return ""
        out = self._pending
        self._pending = ""
        return out

    @property
    def raw(self) -> str:
        """The complete original text, tag included (used for parsing)."""
        return self._raw


class StructuredMemoryAgent(BasicMemoryAgent):
    """BasicMemoryAgent + table-based short/long term memory."""

    def __init__(
        self,
        *args,
        memory_config: Optional[dict] = None,
        project_root: Optional[str] = None,
        memory_llm: Any = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self._mem_cfg = MemoryConfig.from_dict(memory_config)
        root = Path(project_root) if project_root else Path.cwd()
        self._mem = MemoryManager(root, self._mem_cfg)
        self._base_system = self._system
        self._last_user_text: str = ""
        self._pending_tasks: set = set()
        self._all_tasks: set = set()

        # A dedicated (usually much smaller, non-reasoning) model for memory
        # work. Extraction and consolidation are structured JSON tasks that do
        # not need the big model, and a reasoning model can take ~100s to
        # consolidate. Falls back to the main LLM when not configured.
        self._mem_llm = memory_llm or self._llm

        logger.info(
            f"StructuredMemoryAgent ready - dir={self._mem.root}, "
            f"mode={self._mem_cfg.memory_mode}, "
            f"threshold={self._mem_cfg.stm_fix_threshold}, "
            f"ltm={len(self._mem.read_ltm())}, stm={len(self._mem.read_stm())}, "
            f"archived_days={len(self._mem.archive.available_dates())}, "
            f"memory_llm={'dedicated' if memory_llm is not None else 'main model'}"
        )

    # ------------------------------------------------------------------ system
    def set_system(self, system: str):
        """Keep the persona and remember it as the base for prompt composition."""
        super().set_system(system)
        self._base_system = self._system

    def _compose_system(self, user_text: str = "") -> str:
        """Base persona + memory tables + archive recall + recording instruction.

        `user_text` is used to detect date references ("昨天", "上周三"…) so the
        matching daily archive can be pulled in on demand.
        """
        try:
            return (
                self._base_system
                + self._mem.prompt_block(user_text)
                + self._mem.record_instruction()
            )
        except Exception as exc:  # never break a turn because of memory
            logger.error(f"Memory prompt composition failed: {exc}")
            return self._base_system

    @staticmethod
    def _extract_display_text(item: Any) -> str:
        """Pull user-visible text out of an arbitrary pipeline output object.

        Depending on which decorator produced it, the item may be a
        SentenceOutput (.display_text.text), a SentenceWithTags (.text),
        a plain str, or a dict.
        """
        if isinstance(item, str):
            return item

        display = getattr(item, "display_text", None)
        if display is not None:
            text = getattr(display, "text", None)
            if isinstance(text, str) and text:
                return text
            if isinstance(display, str) and display:
                return display

        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            return text

        if isinstance(item, dict):
            for key in ("text", "content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    return value

        return ""

    # -------------------------------------------------------------------- chat
    async def chat(
        self, input_data: BatchInput
    ) -> AsyncIterator[Union[SentenceOutput, Dict[str, Any]]]:
        """Run the pipeline.

        Recording itself happens in the `_on_assistant_message` hook (installed
        in __init__), because `_add_message` is the one place every turn is
        guaranteed to pass through. This override only (a) refreshes the system
        prompt with the latest memory and (b) strips any memory tag the model
        may have emitted, so it never reaches TTS or the UI.
        """
        # remember what the user said, for the hook to pair with the reply
        try:
            self._last_user_text = self._to_text_prompt(input_data)
        except Exception:
            self._last_user_text = ""

        # refresh the system prompt (with date-archive recall for this input)
        try:
            self._system = self._compose_system(self._last_user_text)
        except Exception as exc:
            logger.error(f"Memory prompt composition failed: {exc}")
            self._system = self._base_system

        tag_filter = MemoryTagStreamFilter()

        async for item in super().chat(input_data):
            text = self._extract_display_text(item)
            if not text:
                yield item
                continue

            visible = tag_filter.feed(text)
            if visible != text:
                # A tag was (partly) present: rewrite this output's display text
                # and the TTS text so neither speaks nor shows the tag.
                try:
                    self._strip_tag_from_output(item, visible)
                except Exception as exc:
                    logger.debug(f"tag strip on output failed: {exc}")
            yield item

        tail = tag_filter.flush()
        if tail:
            logger.debug(f"Memory filter flush: {tail!r}")

    @staticmethod
    def _strip_tag_from_output(item: Any, visible: str) -> None:
        """Replace the tag-bearing text on a pipeline output object in place."""
        display = getattr(item, "display_text", None)
        if display is not None and hasattr(display, "text"):
            try:
                display.text = visible
            except Exception:
                pass
        if hasattr(item, "tts_text"):
            try:
                item.tts_text = visible
            except Exception:
                pass

    # ------------------------------------------------------------------- hook
    def _on_user_message(self, text: str) -> None:
        """Remember what the user said, so the reply hook can pair with it.

        Captured here rather than in chat(): _add_message is the guaranteed
        path, and chat() turned out not to be the entry point actually used
        (observed: _last_user_text stayed empty).
        """
        self._last_user_text = text or ""

    def _on_assistant_message(self, text: str) -> None:
        """Called by BasicMemoryAgent._add_message after each assistant turn.

        Schedules the memory write as an independent task. Two details matter:

        1. create_task (not an inline await): the conversation task is cancelled
           right after the audio finishes playing, and anything awaited inline
           would be killed with it.
        2. the user text is snapshotted NOW and passed by value. The extraction
           takes a few seconds, and the next turn overwrites _last_user_text in
           the meantime - reading it later inside the task made every recorded
           row describe the *previous* turn (observed).
        """
        if not self._mem_cfg.enabled:
            return

        user_snapshot = self._last_user_text or ""
        asst_snapshot = text or ""

        try:
            task = asyncio.create_task(
                self._record_turn(user_snapshot, asst_snapshot)
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)
            # Keep a strong reference on the loop too: a task with no reference
            # can be garbage-collected before it finishes.
            self._all_tasks.add(task)
            task.add_done_callback(self._all_tasks.discard)
        except RuntimeError as exc:
            # no running loop (shouldn't happen inside a conversation)
            logger.warning(f"No event loop; memory turn not recorded: {exc}")

    async def flush_pending(self, timeout: float = 60.0) -> int:
        """Wait for in-flight memory writes. Returns how many were pending.

        Called when the session ends so a turn is not lost just because the
        client disconnected while its extraction was still running.
        """
        pending = [t for t in self._all_tasks if not t.done()]
        if not pending:
            return 0
        logger.info(f"Memory: waiting for {len(pending)} pending write(s)")
        try:
            await asyncio.wait(pending, timeout=timeout)
        except Exception as exc:
            logger.warning(f"Memory flush interrupted: {exc}")
        return len(pending)

    def _diag(self, message: str) -> None:
        """Append a diagnostic line to memory/diag.log (never raises).

        Written straight to a file on purpose: loguru output can be swallowed
        when the server is started through a pipe, which made earlier attempts
        at debugging this invisible.
        """
        try:
            path = self._mem.root / "diag.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H:%M:%S")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp} {message}\n")
        except Exception:
            pass

    async def _record_turn(self, user_text: str, assistant_text: str) -> None:
        """Extract this turn's memory and store it. Never raises."""
        # Remove any tag the model may have appended (inline mode leftovers).
        assistant_text = self._tidy_text(MEM_TAG_RE.sub("", assistant_text))

        # keep conversation memory clean for the next turn / history file
        try:
            if self._memory and self._memory[-1].get("role") == "assistant":
                self._memory[-1]["content"] = self._tidy_text(
                    MEM_TAG_RE.sub("", self._memory[-1].get("content", ""))
                )
        except Exception:
            pass

        scene = event = ""
        wants_fix = False
        try:
            if self._mem_cfg.memory_mode == "inline":
                scene, event, wants_fix = self._extract_tag(assistant_text)
            else:
                scene, event = await asyncio.wait_for(
                    self._extract_memory(user_text, assistant_text),
                    timeout=self._mem_cfg.internal_timeout,
                )
        except asyncio.TimeoutError:
            self._diag("extraction TIMED OUT")
            logger.warning("Memory extraction timed out; turn not recorded")
        except Exception as exc:
            self._diag(f"extraction ERROR: {type(exc).__name__}: {exc}")
            logger.error(f"Memory extraction failed: {exc}")

        self._diag(f"extraction result scene={scene!r} event={event!r}")

        if event:
            try:
                self._mem.add_stm(scene, event)
                self._diag(f"STM written: [{scene}] {event}")
                logger.info(f"Memory recorded: [{scene}] {event}")
            except Exception as exc:
                self._diag(f"STM write ERROR: {exc}")
                logger.error(f"Failed to record STM: {exc}")
        else:
            self._diag("nothing recorded (empty event)")

        if wants_fix or self._mem.need_fixation():
            logger.info("Memory: threshold reached, consolidating STM -> LTM")
            try:
                await asyncio.wait_for(
                    self._consolidate(),
                    timeout=self._mem_cfg.consolidation_timeout,
                )
                logger.info("Memory consolidation finished")
            except asyncio.TimeoutError:
                logger.warning(
                    f"Memory consolidation timed out after "
                    f"{self._mem_cfg.consolidation_timeout:.0f}s; STM kept intact"
                )
            except Exception as exc:
                logger.error(f"Memory consolidation failed: {exc}")

    async def _call_memory_llm(self, prompt: str, system: str, budget: int) -> str:
        """Run one internal call on the memory LLM, falling back to the main LLM.

        Why the fallback matters: LM Studio can only keep so many models
        resident. When the dedicated memory model and the character's main model
        are both being requested in the same turn (the main model also runs the
        tool-calling loop), LM Studio may abort one load with
        "Engine protocol startup was aborted". Rather than silently dropping the
        memory, retry once on the main LLM (or wait briefly and retry).
        """
        async def _run(llm) -> str:
            text = ""
            previous = self._bump_token_budget(budget, llm)
            try:
                async for chunk in llm.chat_completion(
                    [{"role": "user", "content": prompt}], system
                ):
                    if isinstance(chunk, str):
                        text += chunk
            finally:
                self._restore_token_budget(previous, llm)
            return text

        # 1) preferred model
        try:
            text = await _run(self._mem_llm)
            if text.strip():
                return text
            logger.debug("Memory LLM returned empty output")
        except Exception as exc:
            logger.warning(f"Memory LLM call failed ({type(exc).__name__}); retrying")

        # 2) brief pause, then retry the same model (transient load contention)
        await asyncio.sleep(2.0)
        try:
            text = await _run(self._mem_llm)
            if text.strip():
                return text
        except Exception as exc:
            logger.warning(f"Memory LLM retry failed ({type(exc).__name__})")

        # 3) last resort: the main model, which is certainly loaded already
        if self._mem_llm is not self._llm:
            try:
                self._diag("falling back to main LLM for memory")
                text = await _run(self._llm)
                if text.strip():
                    return text
            except Exception as exc:
                logger.error(f"Memory fallback to main LLM failed: {exc}")
        return ""

    def _bump_token_budget(self, target: int | None = None, llm: Any = None) -> int | None:
        """Temporarily raise an LLM's max_tokens for an internal call.

        Reasoning models spend most of their budget on hidden "thinking", so a
        small budget yields an empty answer. Returns the previous value so the
        caller can restore it.
        """
        target_llm = llm if llm is not None else self._mem_llm
        wanted = target or self._mem_cfg.internal_max_tokens
        previous = getattr(target_llm, "max_tokens", None)
        if previous is None or previous < wanted:
            try:
                target_llm.max_tokens = wanted
            except Exception:
                return previous
        return previous

    def _restore_token_budget(self, previous: int | None, llm: Any = None) -> None:
        target_llm = llm if llm is not None else self._mem_llm
        if hasattr(target_llm, "max_tokens"):
            target_llm.max_tokens = previous

    async def _extract_memory(self, user_text: str, assistant_text: str) -> tuple[str, str]:
        """Separate small LLM call that reports this turn's memory (extract mode).

        This is the reliable path: the model never has to remember to emit a
        tag, because it is asked directly with a short, focused prompt.
        """
        if not user_text.strip():
            return "", ""

        prompt = self._mem.extract_prompt(user_text, assistant_text)
        text = await self._call_memory_llm(
            prompt, EXTRACT_SYSTEM, self._mem_cfg.internal_max_tokens
        )

        data = self._parse_json_block(text)
        if not isinstance(data, dict):
            if text:
                logger.warning(f"Extraction returned unparseable output: {text[:160]}")
            return "", ""
        return (
            str(data.get("scene") or "").strip(),
            str(data.get("event") or "").strip(),
        )

    @staticmethod
    def _tidy_text(text: str) -> str:
        """Remove leftovers after tag stripping (empty code fences, stray lines)."""
        cleaned = re.sub(r"```[a-zA-Z]*\s*```", "", text)      # emptied fence
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _extract_tag(text: str) -> tuple[str, str, bool]:
        """Pull scene/event/consolidation flag out of the model output."""
        scene, event, wants_fix = "", "", False
        for match in MEM_TAG_RE.finditer(text):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                logger.warning(f"Memory tag was not valid JSON: {match.group(1)[:120]}")
                continue
            scene = str(data.get("scene") or "").strip() or scene
            event = str(data.get("event") or "").strip() or event
            if data.get("consolidation") is True:
                wants_fix = True
        return scene, event, wants_fix

    async def _consolidate(self) -> None:
        """Merge STM rows into LTM rows using one extra LLM call."""
        rows = self._mem.fixation_payload()
        if not rows:
            return

        scenes = " / ".join(self._mem_cfg.scene_options)
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
        prompt = (
            "把下面的短期记忆归并成长期记忆。\n\n"
            f"短期记忆（JSON 数组）：\n{payload}\n\n"
            "规则：\n"
            "1. 按【主题】归并，不要按条数。同一件事或紧密相关的几件事合成一条。\n"
            "2. 目标 1~3 条。有明显不同的主题才拆开；不要为了凑数把一条拆成多条。\n"
            "3. 每条 event 用一句话概括，只写客观事实，不写角色感受。\n"
            f"4. scene_start / scene_end 只能从这些词里选，必须完全一致，不要自己造词：{scenes}\n"
            "5. time_start / time_end 原样复制输入里的时间字符串。\n\n"
            "只输出 JSON，不要解释、不要代码块：\n"
            '{"items":[{"time_start":"...","time_end":"...",'
            '"scene_start":"...","scene_end":"...","event":"..."}]}\n\n'
            "示例（3 条工作相关 + 1 条搬家合成 2 条）：\n"
            '{"items":['
            '{"time_start":"2026-10-14 09:00 第1天","time_end":"2026-10-14 23:00 第1天",'
            '"scene_start":"工作","scene_end":"工作",'
            '"event":"用户全天开会并准备次日项目上线，期间表示紧张。"},'
            '{"time_start":"2026-10-14 20:00 第1天","time_end":"2026-10-14 20:10 第1天",'
            '"scene_start":"日常","scene_end":"日常",'
            '"event":"用户下周搬去杭州，母亲将帮忙收拾，新家离公司较近。"}'
            "]}"
        )

        text = await self._call_memory_llm(
            prompt, FIXATION_SYSTEM, self._mem_cfg.consolidation_max_tokens
        )

        data = self._parse_json_block(text)
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not items:
            logger.warning(
                f"Consolidation returned unusable output, STM left intact: {text[:200]}"
            )
            return

        written = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            event = str(item.get("event") or "").strip()
            if not event:
                continue
            self._mem.add_ltm(
                time_start=str(item.get("time_start") or rows[0]["time"]),
                time_end=str(item.get("time_end") or ""),
                scene_start=self._mem.normalize_scene(str(item.get("scene_start") or "")),
                scene_end=self._mem.normalize_scene(str(item.get("scene_end") or "")),
                event=event,
            )
            written += 1

        if written:
            self._mem.clear_stm()
            logger.info(
                f"Memory consolidated: {len(rows)} STM row(s) -> {written} LTM row(s)"
            )
        else:
            logger.warning("Consolidation produced no usable rows; STM kept.")

    @staticmethod
    def _parse_json_block(text: str) -> Optional[dict]:
        """Best-effort JSON extraction (handles stray prose or code fences)."""
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = re.sub(r"```\s*$", "", cleaned).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # fall back to the outermost {...}
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
