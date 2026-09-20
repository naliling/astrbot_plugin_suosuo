from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star
from astrbot.api import logger # 使用 astrbot 提供的 logger 接口
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.api.message_components import At
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig
import json
import asyncio
import tempfile
import os

class MyPlugin(Star):
    def __init__(self, context: Context,config: AstrBotConfig):
        super().__init__(context)
        self.suo_list_path = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "suo_list.json"
        # 命令前缀分组配置（支持列表和字符串两种配置格式）
        prefix_group = config.get("命令前缀", {})
        raw_prefix = prefix_group.get("prefix", ["."])
        self.prefixes = raw_prefix if isinstance(raw_prefix, list) else [raw_prefix]
        # 空前缀开关：开启时额外加一个空字符到列表中
        if prefix_group.get("allow_empty_prefix", False) and "" not in self.prefixes:
            self.prefixes.append("")
        # 命令名称分组配置（可自定义两个命令的触发词）
        command_name_group = config.get("命令名称", {})
        self.suo_command = command_name_group.get("suo_command", "嗦")
        self.stats_command = command_name_group.get("stats_command", "嗦啼战绩")
        # 数据结构: {"QQ号": {"suo": 嗦人次数, "suoed": 被嗦次数}}
        self.suo_list = {}
        # 自动保存间隔（秒），默认 60 秒
        self._auto_save_interval = config.get("auto_save_interval", 60)
        # 标记是否需要自动保存任务
        self._auto_save_task = None
        # 标记数据是否被修改过（有脏数据）
        self._dirty = False
        if self.suo_list_path.exists():
            try:
                with open(self.suo_list_path, "r", encoding="utf-8") as f:
                    self.suo_list = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"读取 suo_list.json 失败: {e}，将使用空数据")
                self.suo_list = {}
        else:
            # 自动创建目录和文件
            self.suo_list_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_to_disk()
        # 启动自动保存任务
        self._auto_save_task = asyncio.create_task(self._auto_save_loop())

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def command(self, event: AstrMessageEvent):
        message_str = event.message_obj.message_str
        # 按命令名长度降序排列，避免短命令名吞掉长命令名（如 "嗦" 是 "嗦啼战绩" 的前缀）
        commands = [
            (self.stats_command, self.suo_stats),
            (self.suo_command, self.suo),
        ]
        commands.sort(key=lambda x: len(x[0]), reverse=True)
        # 遍历所有前缀，找到匹配的
        for prefix in self.prefixes:
            if message_str.startswith(prefix):
                for cmd, handler in commands:
                    if message_str.startswith(prefix + cmd):
                        async for ret in handler(event):
                            yield ret
                        break
                break

    def _save_to_disk(self):
        '''原子写入 JSON 文件，防止写入损坏导致数据丢失。'''
        temp_path = None
        try:
            self.suo_list_path.parent.mkdir(parents=True, exist_ok=True)
            # 先写入临时文件，再重命名，保证原子性
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False,
                dir=self.suo_list_path.parent, suffix=".tmp"
            ) as tf:
                json.dump(self.suo_list, tf, ensure_ascii=False, indent=2)
                tf.flush()
                os.fsync(tf.fileno())  # 确保数据刷到磁盘
                temp_path = Path(tf.name)
            # 临时文件替换原文件（原子操作，Windows 下 close 后才能 rename）
            temp_path.replace(self.suo_list_path)
            temp_path = None  # 替换成功，不再需要清理
            self._dirty = False
        except OSError as e:
            logger.error(f"保存 suo_list.json 失败: {e}")
        finally:
            # 如果写入或替换过程中出错/崩溃，清理残留的临时文件
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    async def _auto_save_loop(self):
        '''定期将脏数据写入磁盘，防止进程崩溃时数据丢失。'''
        try:
            while True:
                await asyncio.sleep(self._auto_save_interval)
                if self._dirty:
                    self._save_to_disk()
                    logger.debug(f"自动保存 suo_list.json (间隔 {self._auto_save_interval}s)")
        except asyncio.CancelledError:
            # 任务被取消时，做最后一次保存
            if self._dirty:
                self._save_to_disk()
            raise

    async def suo(self, event: AstrMessageEvent):
        '''嗦嗦群友 使用"嗦 [@群友]"'''
        message_chain = event.message_obj.message
        send_user = event.get_sender_id()
        if len(message_chain) > 1 and isinstance(message_chain[1], At):
            at_qq = str(message_chain[1].qq)
        else:
            yield event.plain_result("你需要@一名群友哦")
            return
        if at_qq not in self.suo_list:
            self.suo_list[at_qq] = {"suo": 0, "suoed": 0}
        if send_user not in self.suo_list:
            self.suo_list[send_user] = {"suo": 0, "suoed": 0}

        self.suo_list[send_user]["suo"] += 1
        self.suo_list[at_qq]["suoed"] += 1
        self._dirty = True

        # 每次操作后立即持久化，防止因崩溃丢失
        self._save_to_disk()

        chain = [
            Comp.At(qq=at_qq),
            Comp.Plain("的蹄子被嗦了"),
            Comp.Image.fromURL(f"https://q1.qlogo.cn/g?b=qq&nk={at_qq}&s=40"),
            Comp.Plain(f"ta被嗦了 {self.suo_list[at_qq]['suoed']} 次"),
        ]
        yield event.chain_result(chain)

    async def suo_stats(self, event: AstrMessageEvent):
        message_chain = event.message_obj.message
        if len(message_chain) > 1 and isinstance(message_chain[1], At):
            qq = str(message_chain[1].qq)
        else:
            qq = event.get_sender_id()

        # 只读查询：不因查询而产生数据条目
        record = self.suo_list.get(qq, {"suo": 0, "suoed": 0})

        suo = record["suo"]
        suoed = record["suoed"]
        chain = [
            Comp.At(qq=qq),
            Comp.Plain("嗦啼战绩:\n\u200b"),
            Comp.Plain(f"嗦人:{suo}次\n\u200b"),
        ]
        if suo < 10:
            chain.append(Comp.Plain("评价:没有人来过这片地方\n\u200b"))
        elif suo < 50:
            chain.append(Comp.Plain("评价:有点水渍 但不多\n\u200b"))
        elif suo < 80:
            chain.append(Comp.Plain("评价:这里常年湿润\n\u200b"))
        elif suo < 100:
            chain.append(Comp.Plain("评价:热带雨林\n\u200b"))
        elif suo < 120:
            chain.append(Comp.Plain("评价:似乎很美味?\n\u200b"))
        else:
            chain.append(Comp.Plain("评价:这到底有多美味\n\u200b"))
        chain.append(Comp.Plain(f"被嗦:{suoed}次\n\u200b"))
        if suoed < 10:
            chain.append(Comp.Plain("称号:嗦啼小白"))
        elif suoed < 50:
            chain.append(Comp.Plain("称号:嗦啼白银"))
        elif suoed < 100:
            chain.append(Comp.Plain("称号:嗦啼高手"))
        elif suoed < 150:
            chain.append(Comp.Plain("称号:嗦啼大神"))
        else:
            chain.append(Comp.Plain("称号:嗦啼大手子 [评价:口干舌燥?]"))

        yield event.chain_result(chain)

    async def terminate(self):
        '''当插件被卸载/停用时调用，执行最终保存并清理后台任务。'''
        # 取消自动保存任务
        if self._auto_save_task and not self._auto_save_task.done():
            self._auto_save_task.cancel()
            try:
                await self._auto_save_task
            except (asyncio.CancelledError, Exception):
                pass
        # 执行最终写入
        self._save_to_disk()
