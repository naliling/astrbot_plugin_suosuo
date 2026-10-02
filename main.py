"""嗦蹄子：@群友「嗦」TA 的蹄子，并统计战绩。

## 为什么是这个结构

v1.0 用 `event_message_type(ALL)` + 在函数体内判断命令，这个骨架**是能跑的**。
v2.0 把它换成自定义 filter + 路由层之后，反而引入了「指令全部失灵」
「管理页看不到指令」「空前缀打不开」一连串问题——复杂度就是 bug 的来源。

所以这里保留 v1.0 的骨架，只修三个确实是 bug 的地方：

1. **双回复**：命中后调 `event.stop_event()`。
   AstrBot 在「有结果但没停止」时会再走一次 LLM，wake_prefix 配成 `.` 时
   用户会收到两份回复。

2. **误触发**：v1.0 是 `startswith(prefix + cmd)`，所以 `.嗦粉` 会被当成 `.嗦`。
   现在要求命令名后面必须跟空白或结束。

3. **@ 解析**：v1.0 写死 `message_chain[1]`，前面插进任何一段（表情、图片）
   就取不到人。现在遍历整条消息链找第一个 At。

数据仍然按群隔离，v1.0 的存档只读继承——这两点 v1.0 没有，是真需求，保留。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_ID = "astrbot_plugin_suosuo"
VERSION = "2.0.0"

# 插件名一直是 astrbot_plugin_suosuo（v1.0 就是这个），所以 v1.0 的存档
# 就在**同一个目录**下——不需要跨目录去找。
# LEGACY_FILE 只读、不写：保留它是为了随时能回退到 v1.0。
LEGACY_FILE = "suo_list.json"
GROUPED_FILE = "suo_list_by_group.json"

DEFAULT_SUO = "嗦"
DEFAULT_STATS = "嗦蹄战绩"


class SuosuoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_ID

        prefix_group = config.get("命令前缀", {})
        raw = prefix_group.get("prefix", ["."]) if isinstance(prefix_group, dict) else ["."]
        # 空串必须丢掉：startswith("") 恒真，任何消息都会进到这里。
        self.prefixes = [p.strip() for p in raw if isinstance(p, str) and p.strip()]
        if not self.prefixes:
            self.prefixes = ["."]

        name_group = config.get("命令名称", {})
        if not isinstance(name_group, dict):
            name_group = {}
        # 命令名同样不能是空串，否则 startswith(prefix + "") 恒真。
        self.suo_command = _clean(name_group.get("suo_command"), DEFAULT_SUO)
        self.stats_command = _clean(name_group.get("stats_command"), DEFAULT_STATS)
        # 存量配置里可能存着 v1.0 的错字「嗦啼战绩」。配置项的值优先于默认值，
        # 所以光改 DEFAULT_* 不够——用户配置里的旧值照样生效。
        # 这里把它就地纠正并记一笔，用户不必手动去配置页改。
        if name_group.get("stats_command") != self.stats_command:
            try:
                name_group["stats_command"] = self.stats_command
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                logger.info(
                    f"嗦蹄子：配置里的战绩命令「{name_group.get('stats_command')}」已自动更正为"
                    f"「{self.stats_command}」"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"嗦蹄子：自动更正配置里的命令名失败（不影响使用）：{exc}")

        self.data = self._load()

    # ---------- 数据 ----------

    def _legacy_path(self) -> Path:
        return self.data_dir / LEGACY_FILE

    def _load(self) -> dict[str, dict[str, int]]:
        """读两处：本群的新档，以及 v1.0 的全局档。

        返回的结构是 {群号: {QQ: {"suo": n, "suoed": n}}}。
        老档只在某个 QQ 第一次出现在本群时，作为它的初始值被取用。
        """
        grouped = _read_json(self.data_dir / GROUPED_FILE)
        legacy = _read_json(self._legacy_path())
        if legacy:
            logger.info(f"嗦蹄子：已载入 {len(legacy)} 条 v1.0 历史战绩（只读继承）")
        return {"grouped": grouped, "legacy": legacy}

    def _bucket(self, group_id: str) -> dict[str, dict[str, int]]:
        return self.data["grouped"].setdefault(str(group_id or "private"), {})

    def _record(self, group_id: str, qq: str) -> dict[str, int]:
        """取某人在某群的记录。没有就用 v1.0 的数字作起点。"""
        bucket = self._bucket(group_id)
        key = str(qq)
        rec = bucket.get(key)
        if rec is None:
            old = self.data["legacy"].get(key) or {}
            rec = {"suo": _as_int(old.get("suo")), "suoed": _as_int(old.get("suoed"))}
            bucket[key] = rec
        return rec

    def get_record(self, group_id: str, qq: str) -> dict[str, int]:
        return dict(self._record(group_id, qq))

    def _save(self) -> None:
        """原子写。临时文件 + rename，半路崩了也不会留下半个坏文件。"""
        path = self.data_dir / GROUPED_FILE
        temp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp"
            ) as tf:
                json.dump(self.data["grouped"], tf, ensure_ascii=False, indent=2)
                tf.flush()
                os.fsync(tf.fileno())
                temp = Path(tf.name)
            temp.replace(path)
            temp = None
        except OSError as exc:
            logger.error(f"嗦蹄子保存失败：{exc}")
        finally:
            if temp is not None and temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    # ---------- 入口 ----------
    #
    # 保持 v1.0 的注册方式：event_message_type(ALL)。
    # 它让每条消息都进来，所以下面的判断必须够严——
    # 不匹配就 return，绝不停事件、绝不产生回复。

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def command(self, event: AstrMessageEvent):
        hit = self._match(event)
        if hit is None:
            return
        handler, _arg = hit

        # 命中后立刻停事件。不停的话 AstrBot 会因为「有结果但没停止」
        # 再调一次 LLM，用户会收到两份回复。
        event.stop_event()

        async for result in handler(event):
            yield result

    def _match(self, event: AstrMessageEvent):
        """返回 (处理函数, 参数)，不匹配返回 None。

        ## 判「后面带的是不是 @ 人」靠消息链，不靠 message_str

        不同平台把 @ 渲染成文本的方式不一样：有的是 `@小王`，
        有的渲染成 `[At:222]`。猜错了就是「命令失灵」——
        所以直接问消息链「有没有 At 组件」，那才是权威答案，
        与平台、与 message_str 怎么拼都无关。

        命令名后面必须跟空白/结束符/At，所以 `.嗦粉` 不会被当成 `.嗦`。
        """
        text = (event.get_message_str() or "").strip()
        if not text:
            return None

        has_at = _first_at(event) is not None
        # 长命令优先：`嗦` 是 `嗦蹄战绩` 的前缀，反过来就会被短命令吃掉。
        commands = [
            (self.stats_command, self._stats),
            (self.suo_command, self._suo),
        ]
        commands.sort(key=lambda x: len(x[0]), reverse=True)

        for prefix in self.prefixes:
            if not text.startswith(prefix):
                continue
            rest = text[len(prefix):]
            hit = _match_command(rest, commands, has_at)
            # 前缀对上了但没匹配到命令：不再退回裸命令，
            # 否则 `.嗦粉` 会被当成裸的 `嗦`。
            return hit

        # 没带前缀的形态。AstrBot 会先剥掉全局 wake_prefix，
        # 所以用户敲 `.嗦` 到这里时 text 已经变成 `嗦` 了。
        return _match_command(text, commands, has_at)

    # ---------- 指令 ----------

    async def _suo(self, event: AstrMessageEvent):
        """嗦 @群友。"""
        target = _first_at(event)
        if target is None:
            yield event.plain_result("你需要@一名群友哦")
            return

        actor = str(event.get_sender_id())
        group_id = _group_id(event)
        if target == actor:
            yield event.plain_result("自己嗦自己，格局小了")
            return

        target_rec = self._record(group_id, target)
        target_rec["suoed"] += 1
        self._record(group_id, actor)["suo"] += 1
        self._save()

        chain: list[Any] = []
        url = _avatar(_group(self.config, "话术").get("avatar_template"), target)
        if url:
            chain.append(Comp.Image.fromURL(url))
        chain.append(Comp.At(qq=target))
        chain.append(Comp.Plain("的蹄子被嗦了"))
        chain.append(Comp.Plain(f"ta 被嗦了 {target_rec['suoed']} 次"))
        yield event.chain_result(chain)

    async def _stats(self, event: AstrMessageEvent):
        """查战绩：@ 指定别人，不 @ 查自己。

        排版上做成**一条** Plain 段里用换行分隔，而不是每行一个 Plain：
        QQ 客户端会把多个 Plain 段渲染成多条消息，看着很散。
        """
        group_id = _group_id(event)
        target = _first_at(event) or str(event.get_sender_id())
        rec = self.get_record(group_id, target)

        phrases = _group(self.config, "话术")
        tiers = _group(self.config, "档位")

        title = _tier(tiers.get("title_tiers"), rec["suoed"])
        comment = _tier(tiers.get("comment_tiers"), rec["suo"])
        rank = self._rank_in_group(group_id, rec["suoed"])

        vals = {"name": target, "rank": rank or ""}
        header = _fill(_render(phrases.get("stats_header"), "嗦蹄战绩"), **vals)
        title_line = _fill(_render(phrases.get("stats_title"), "称号：{text}"),
                           text=title, **vals)
        suo_line = _fill(_render(phrases.get("stats_suo"), "嗦人：{count} 次"),
                         count=rec["suo"], **vals)
        suoed_line = _fill(_render(phrases.get("stats_suoed"), "被嗦：{count} 次"),
                           count=rec["suoed"], **vals)
        comment_line = _fill(_render(phrases.get("stats_comment"), "评价：{text}"),
                             text=comment, **vals)
        rank_line = f"排行：本群第 {rank} 名被嗦" if rank else ""

        # 三行排版，比之前每行一个 Plain 段紧凑得多——QQ 客户端会把多个
        # Plain 段渲染成多条消息，看着很散。
        lines = [
            f"{header}　{title_line}",
            f"{suo_line}　{suoed_line}",
            f"{comment_line}　{rank_line}",
        ]
        body = "\n".join(x for x in lines if x.strip())

        chain: list[Any] = []
        url = _avatar(phrases.get("avatar_template"), target)
        if url:
            chain.append(Comp.Image.fromURL(url))
        chain.append(Comp.At(qq=target))
        chain.append(Comp.Plain(body))
        yield event.chain_result(chain)

    def _rank_in_group(self, group_id: str, value: int) -> int:
        """本群按被嗦次数排第几（从 1 起）。同分同名次。

        做法是数「比自己次数高的人数」再 +1：
            9 次、5 次、1 次 → 9 那个人是第 1，5 那个人是第 2
        """
        bucket = self._bucket(group_id)
        ahead = sum(1 for r in bucket.values() if int(r.get("suoed", 0) or 0) > value)
        return ahead + 1


    # ---------- 生命周期 ----------

    async def terminate(self) -> None:
        self._save()


def _match_command(rest: str, commands, has_at: bool):
    """在剥掉前缀（或没前缀）的文本里找命令。

    放行条件：命令名后面是结束符、空白、@，或消息链里确实带了 @。
    拦下来的是「命令词 + 中文」那类日常聊天（嗦粉、嗦一口）。
    """
    for name, handler in commands:
        if rest == name:
            return handler, ""
        # 命令名后面立刻跟 @（平台可能渲染成 `[At:222]` 或 `@小王`）
        if rest.startswith(name + "@") or rest.startswith(name + "["):
            return handler, ""
        if rest.startswith(name + " "):
            tail = rest[len(name):].strip()
            # 后面是空白：消息链里有 At 就放行（@ 已被平台渲染成文本了）
            if has_at:
                return handler, tail
            # 没 At 的话，只放行常见的 @ 写法；其余当日常聊天
            if tail.startswith("@") or tail.startswith("["):
                return handler, tail
    return None


# ---------- 小工具 ----------


def _clean(value: Any, fallback: str) -> str:
    """命令名清洗。

    1) 空串会让 startswith 恒真（任何消息都命中），必须回退；
    2) v1.0 的默认值带错字（啼 vs 蹄），存量配置里存的就是那个错字，
       用户敲的却是「嗦蹄」——所以顺手纠过来。
    """
    if not isinstance(value, str):
        return fallback
    name = value.strip()
    if not name:
        return fallback
    return name.replace("\u5617", "\u8e44")   # 啼 -> 蹄


def _group(config: Any, name: str) -> dict[str, Any]:
    value = config.get(name, {}) if isinstance(config, dict) else {}
    return value if isinstance(value, dict) else {}


def _group_id(event: AstrMessageEvent) -> str:
    return str(event.get_group_id() or "")


def _first_at(event: AstrMessageEvent) -> str | None:
    """消息里第一个 @ 的人。

    v1.0 写死 `message_chain[1]`，前面插进表情/图片就取不到。
    这里遍历整条链，而且用鸭子类型而非 isinstance——
    At 类在重装/多次 import 后可能是不同的类对象，isinstance 会静默失效。
    """
    for comp in event.message_obj.message:
        qq = getattr(comp, "qq", None)
        if qq is None:
            continue
        if type(comp).__name__.lower().startswith("at"):
            return str(qq) or None
    return None


def _avatar(template: Any, qq: str) -> str:
    """头像地址。协议不对就退掉——AstrBot 的 Image.fromURL 只接受
    http/https 开头的地址，不合格会抛异常，把整个命令打挂。"""
    if not isinstance(template, str) or not template:
        return ""
    url = template.replace("{qq}", str(qq)).replace("{id}", str(qq))
    return url if url.startswith(("http://", "https://")) else ""


def _fill(template: str, **values) -> str:
    """替换文案里的 {count} / {text} 这类占位符。

    配置里写「{count} 次」，实际发出去必须把 {count} 换成数字——
    之前直接把配置串原样发出去了，用户看到的是一串「{count}」。
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def _render(template: Any, fallback: str) -> str:
    return template if isinstance(template, str) and template else fallback


def _tier(tiers: Any, value: int) -> str:
    """按阈值查档位，取命中数值的最高档。"""
    if not isinstance(tiers, list):
        return ""
    usable = []
    for item in tiers:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            continue
        if "min" not in item:
            continue
        usable.append((_as_int(item.get("min")), item["text"]))
    if not usable:
        return ""
    usable.sort(key=lambda p: p[0], reverse=True)
    for threshold, text in usable:
        if value >= threshold:
            return text
    return usable[-1][1]


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error(f"嗦蹄子读取 {path.name} 失败：{exc}，按空数据处理")
        return {}
    return payload if isinstance(payload, dict) else {}