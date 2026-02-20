"""
客户端会话管理模块
"""

import time
import asyncio
import logging
from typing import Optional
from fastapi import WebSocket

from config import logger

try:
    from src.funasr_client import FunASRClient
except ImportError:
    from funasr_client import FunASRClient


class ClientSession:
    def __init__(
        self, client_id: str, employee_number: str, trace_id: str, websocket: WebSocket
    ):
        self.client_id = client_id
        self.employee_number = employee_number
        self.trace_id = trace_id
        self.websocket = websocket
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.funasr_ch1: Optional[FunASRClient] = None
        self.funasr_ch2: Optional[FunASRClient] = None
        self.is_active = True
        self.channel_connected = {"CH1": False, "CH2": False}
        self.current_call_info = {"call_id": "", "caller": "", "callee": ""}

        self.message_buffer = []
        self.max_buffer_size = 5000
        self.buffer_task = None

        self.result_index_counters = {"CH1": 0, "CH2": 0}

        self.timeout_seconds = 30
        self.last_heartbeat_time = time.time()

        self.stats = {
            "audio_packets_received": 0,
            "audio_packets_processed_ch1": 0,
            "audio_packets_processed_ch2": 0,
            "asr_results_sent": 0,
            "asr_results_ch1": 0,
            "asr_results_ch2": 0,
            "audio_bytes_received": 0,
        }

        self._last_real_audio_time = {"CH1": time.time(), "CH2": time.time()}
        self._keepalive_interval = 10
        self._silence_packet = b"\x00" * 640

        self._last_stat_log = 0
        self._audio_tasks = set()

        logger.info(f"创建客户端会话: ID={client_id}, 员工号={employee_number}")

        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        self._send_tail_silence_task = asyncio.create_task(
            self._send_tail_silence_loop()
        )

    async def _keepalive_loop(self):
        logger.info(f"[{self.client_id}] 保活循环启动")

        async def _should_stop_keepalive() -> bool:
            if not self.is_active:
                logger.debug(f"[{self.client_id}] 停止原因: is_active=False")
                return True
            try:
                if hasattr(self.websocket, "client_state"):
                    state = self.websocket.client_state
                    if state.name != "CONNECTED":
                        logger.debug(
                            f"[{self.client_id}] WebSocket状态异常: {state.name}"
                        )
                        return True
            except (AttributeError, RuntimeError, Exception) as e3:
                logger.debug(
                    f"[{self.client_id}] 停止原因: WebSocket检查异常 {type(e3).__name__}"
                )
                return True
            return False

        ping_counter = 0

        try:
            while True:
                if await _should_stop_keepalive():
                    logger.info(f"[{self.client_id}] 复合检查判定会话失效，停止保活")
                    break

                ping_counter += 1
                if ping_counter >= 6:
                    ping_counter = 0
                    try:
                        await asyncio.wait_for(
                            self.websocket.send_json(
                                {
                                    "type": "ping",
                                    "description": "checkalive(no need to respond)",
                                    "sent_at": time.time(),
                                }
                            ),
                            timeout=2.0,
                        )
                    except Exception as e2:
                        logger.info(
                            f"[{self.client_id}] 给客户端发送ping失败，停止保活. {e2}"
                        )
                        self.is_active = False
                        break

                await asyncio.sleep(self._keepalive_interval / 2)

                for channel in ["CH1", "CH2"]:
                    if await _should_stop_keepalive():
                        break

                    funasr_client = (
                        self.funasr_ch1 if channel == "CH1" else self.funasr_ch2
                    )

                    if funasr_client and not funasr_client.closing:
                        try:
                            await asyncio.wait_for(
                                funasr_client.send_audio(
                                    self._silence_packet, sample_rate_hz=8000
                                ),
                                timeout=2.0,
                            )
                            logger.info(
                                f"[{self.client_id}] 保活-{channel}: 静音包发送成功"
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[{self.client_id}] 保活-{channel}: 发送超时"
                            )
                        except Exception as e4:
                            if (
                                funasr_client.closing
                                or funasr_client.session_shutdown_event.is_set()
                            ):
                                logger.debug(
                                    f"[{self.client_id}] 保活-{channel}: 客户端正在关闭，停止发送"
                                )
                                break
                            logger.warning(
                                f"[{self.client_id}] 保活-{channel}: 发送静音包失败: {e4}"
                            )
                    try:
                        await asyncio.sleep(self._keepalive_interval / 2)
                    except asyncio.CancelledError:
                        logger.info(f"[{self.client_id}] 保活循环被取消")
                        raise
        except asyncio.CancelledError:
            logger.info(f"[{self.client_id}] 保活循环被取消")
            raise
        except Exception as e2:
            logger.error(f"[{self.client_id}] 保活循环遇到未预期异常，退出: {e2}")
        finally:
            self.is_active = False
            logger.info(f"[{self.client_id}] 保活循环已完全停止")

    async def _send_tail_silence_loop(self):
        try:
            logger.info(f"[{self.client_id}] 留尾巴循环已启动")
            while True:
                if not self.is_active:
                    logger.info(
                        f"[{self.client_id}] 自检-会话已停止，停止发送500ms静音包"
                    )
                    break

                for channel in ["CH1", "CH2"]:
                    funasr_client = (
                        self.funasr_ch1 if channel == "CH1" else self.funasr_ch2
                    )

                    if funasr_client and not funasr_client.closing:
                        try:
                            if time.time() - self._last_real_audio_time[channel] < 0.8:
                                await asyncio.sleep(0.5)
                                continue
                            if (
                                time.time() - self._last_real_audio_time[channel]
                                > 60 * 10
                            ):
                                await asyncio.sleep(3)
                                continue

                            if not self.is_active:
                                break

                            silence_500ms = b"\x00" * 8000
                            await asyncio.wait_for(
                                funasr_client.send_audio(
                                    silence_500ms, sample_rate_hz=8000
                                ),
                                timeout=1.0,
                            )
                            logger.info(
                                f"[{self.client_id}] 留尾巴-{channel}: 500ms静音包发送成功"
                            )
                            await asyncio.sleep(1)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[{self.client_id}] 留尾巴-{channel}: 发送超时"
                            )
                            await asyncio.sleep(1)
                        except asyncio.CancelledError:
                            logger.debug(f"[{self.client_id}] 留尾巴任务正常取消")
                            raise
                        except Exception as e4:
                            if (
                                funasr_client.closing
                                or funasr_client.session_shutdown_event.is_set()
                            ):
                                logger.debug(
                                    f"[{self.client_id}] 留尾巴-{channel}: 客户端正在关闭，停止发送"
                                )
                                break
                            logger.warning(
                                f"[{self.client_id}] 留尾巴-{channel}: 发送静音包失败: {e4}"
                            )

        except Exception as e2:
            logger.error(
                f"[{self.client_id}] 留尾巴-由于会话已停止，停止发送500ms静音包: {e2}"
            )

    async def send_message(self, message: dict):
        if not self.is_active:
            return False

        if len(self.message_buffer) >= self.max_buffer_size:
            logger.warning(f"[{self.client_id}] 消息缓冲区已满，丢弃旧消息")
            self.message_buffer.pop(0)

        self.message_buffer.append((time.time(), message))
        return await self._try_send_buffered()

    async def _try_send_buffered(self):
        if not self.is_active or not self.message_buffer:
            return True

        successful_sends = 0
        failed_sends = 0

        while self.message_buffer and successful_sends < 50:
            send_time, message = self.message_buffer[0]

            try:
                await self.websocket.send_json(message)
                self.message_buffer.pop(0)
                successful_sends += 1
                self.last_activity = time.time()
            except Exception as e2:
                failed_sends += 1
                if failed_sends >= 3:
                    logger.warning(f"[{self.client_id}] 连续发送失败，暂停发送{e2}")
                    break

        return successful_sends > 0

    async def process_audio(
        self,
        channel: str,
        audio_data: bytes,
        call_id: str = "",
        caller: str = "",
        callee: str = "",
    ):
        try:
            current_time = time.time()
            self._last_real_audio_time[channel] = current_time

            if call_id:
                self.current_call_info["call_id"] = call_id
                self.current_call_info["caller"] = caller
                self.current_call_info["callee"] = callee
            self.stats["audio_packets_received"] += 1
            self.stats["audio_bytes_received"] += len(audio_data)

            if channel == "CH1" and self.funasr_ch1 and self.funasr_ch1.connected:
                success = await self.funasr_ch1.send_audio(
                    audio_data, sample_rate_hz=8000
                )
                if success:
                    self.stats["audio_packets_processed_ch1"] += 1
                    if self.stats["audio_packets_processed_ch1"] % 10000 == 0:
                        current_time = time.time()
                        if current_time - self._last_stat_log > 30:
                            logger.info(
                                f"[{self.client_id}] CH1已处理 {self.stats['audio_packets_processed_ch1']} 包"
                            )
                            self._last_stat_log = current_time
                    return True

            elif channel == "CH2" and self.funasr_ch2 and self.funasr_ch2.connected:
                success = await self.funasr_ch2.send_audio(
                    audio_data, sample_rate_hz=8000
                )
                if success:
                    self.stats["audio_packets_processed_ch2"] += 1
                    if self.stats["audio_packets_processed_ch2"] % 10000 == 0:
                        current_time = time.time()
                        if current_time - self._last_stat_log > 30:
                            logger.info(
                                f"[{self.client_id}] CH2已处理 {self.stats['audio_packets_processed_ch2']} 包"
                            )
                            self._last_stat_log = current_time
                    return True

            return False

        except Exception as e2:
            logger.error(f"处理音频失败 {self.client_id}: {e2}")
            self.stats["audio_packets_dropped"] = (
                self.stats.get("audio_packets_dropped", 0) + 1
            )
            return False

    async def close(self):
        try:
            self.is_active = False
            logger.info(f"开始关闭客户端会话，彻底停止所有活动: {self.client_id}")

            if self.funasr_ch1:
                self.funasr_ch1.session_shutdown_event.set()
                self.funasr_ch1.closing = True
                self.funasr_ch1.should_reconnect = False
            if self.funasr_ch2:
                self.funasr_ch2.session_shutdown_event.set()
                self.funasr_ch2.closing = True
                self.funasr_ch2.should_reconnect = False
            logger.info(
                f"【会话关机】已向所有FunASR连接下达禁止重连令: {self.client_id}"
            )

            if (
                hasattr(self, "_keepalive_task")
                and self._keepalive_task
                and not self._keepalive_task.done()
            ):
                self._keepalive_task.cancel()

            if (
                hasattr(self, "_send_tail_silence_task")
                and self._send_tail_silence_task
                and not self._send_tail_silence_task.done()
            ):
                self._send_tail_silence_task.cancel()

            close_tasks = []
            if self.funasr_ch1:
                close_tasks.append(self.funasr_ch1.close())
            if self.funasr_ch2:
                close_tasks.append(self.funasr_ch2.close())
            if close_tasks:
                try:
                    await asyncio.gather(*close_tasks, return_exceptions=True)
                except Exception as e3:
                    logger.error(f"[{self.client_id}] 关闭FunASR连接异常: {e3}")

            self.funasr_ch1 = None
            self.funasr_ch2 = None
            self._keepalive_task = None

            logger.info(f"客户端会话关闭完成: {self.client_id}")
        except Exception as e2:
            logger.error(f"[{self.client_id}] 关闭客户端会话时异常: {e2}")

    async def check_timeout(self):
        if time.time() - self.last_heartbeat_time > self.timeout_seconds:
            logger.info(f"会话超时，关闭: {self.client_id}")
            self.is_active = False
            return True
        return False

    def __del__(self):
        if self.is_active:
            logger.warning(f"[{self.client_id}] 会话被垃圾回收时仍为活跃状态，尝试清理")
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.close())
            except:
                pass
