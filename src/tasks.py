"""
后台任务模块
"""

import json
import socket
import struct
import asyncio
import time
import logging
import random
import uuid
import websockets

from config import UDP_LISTEN_PORT, FUNASR_SERVERS, logger

try:
    from src.funasr_client import FunASRClient
    from src.client_session import ClientSession
    from src.audio_router import audio_router, connection_monitor
except ImportError:
    from funasr_client import FunASRClient
    from client_session import ClientSession
    from audio_router import audio_router, connection_monitor


class UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, router):
        self.router = router
        self.packet_count = 0

    def datagram_received(self, data, addr):
        self.packet_count += 1
        if len(data) >= 8:
            try:
                header_len = struct.unpack("<I", data[:4])[0]
                if len(data) >= 4 + header_len:
                    header = json.loads(
                        data[4 : 4 + header_len].decode("utf-8", errors="ignore")
                    )
                    audio_data = data[4 + header_len :]

                    call_id = header.get("call_id", "unknown")
                    caller = header.get("caller", "Unknown")
                    callee = header.get("callee", "Unknown")
                    channel = header.get("channel", "Unknown")

                    if channel not in ["CH1", "CH2"]:
                        logger.warning(
                            f"收到不明声道音频数据: call_id={call_id[:12]}, caller={caller}, callee={callee}, channel={channel}"
                        )

                    asyncio.create_task(
                        self.router.route_audio(
                            call_id, caller, callee, channel, audio_data
                        )
                    )
            except Exception as e:
                pass

    def error_received(self, exc):
        logger.error(f"UDP协议错误: {exc}")


async def udp_server_task():
    logger.info(f"UDP服务器启动在端口 {UDP_LISTEN_PORT}")
    transport = None

    try:
        transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: UDPProtocol(audio_router), local_addr=("0.0.0.0", UDP_LISTEN_PORT)
        )

        while True:
            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info("UDP服务器任务被取消")
    except Exception as e:
        logger.error(f"UDP服务器错误: {e}")
    finally:
        if transport:
            transport.close()


async def funasr_result_forwarder(session: ClientSession):
    async def forward_channel_results(channel: str, funasr_client: FunASRClient):
        channel_name = "员工声道" if channel == "CH1" else "客户声道"
        stats_key = f"asr_results_{channel.lower()}"

        max_retries = 2
        retry_delay = 0.5

        while session.is_active:
            try:
                if not funasr_client.connected:
                    if not getattr(funasr_client, "_should_reconnect", True):
                        logger.debug(
                            f"[{session.client_id}] {channel_name} 设置了不重连，停止转发"
                        )
                        break

                    if not session.is_active:
                        break
                    logger.warning(
                        f"[{session.client_id}] {channel_name} FunASR连接断开，尝试重连..."
                    )

                    if not await funasr_client.connect():
                        await asyncio.sleep(retry_delay)
                        continue

                result = await funasr_client.receive_result()
                if result:
                    text = result.get("text", "").strip()
                    session.result_index_counters[channel] += 1
                    result_index = session.result_index_counters[channel]

                    if text:
                        message = {
                            "type": "asr_result",
                            "channel": channel,
                            "channel_role": "employee"
                            if channel == "CH1"
                            else "customer",
                            "channel_name": channel_name,
                            "employee_number": session.employee_number,
                            "call_id": session.current_call_info["call_id"],
                            "caller": session.current_call_info["caller"],
                            "callee": session.current_call_info["callee"],
                            "trace_id": session.trace_id,
                            "sent_at": time.time(),
                            "text": text,
                            "mode": result.get("mode", ""),
                            "result_index": result_index,
                            "raw_data": result,
                        }

                        sent_success = False
                        for retry in range(max_retries):
                            try:
                                if await session.send_message(message):
                                    session.stats["asr_results_sent"] += 1
                                    session.stats[stats_key] += 1
                                    sent_success = True
                                    break
                            except Exception as e2:
                                logger.warning(
                                    f"[{session.client_id}] 发送消息失败，重试 {retry + 1}/{max_retries}: {e2}"
                                )
                                await asyncio.sleep(retry_delay)

                        if not sent_success:
                            logger.error(
                                f"[{session.client_id}] 多次发送失败，可能是连接已关闭"
                            )
                            break

                        if session.stats[stats_key] % 20 == 0:
                            logger.debug(
                                f"[{session.client_id}] {channel_name} 已发送 {session.stats[stats_key]} 个结果"
                            )

                await asyncio.sleep(0.0001)

            except websockets.exceptions.ConnectionClosed:
                if not session.is_active:
                    break
                logger.warning(
                    f"[{session.client_id}] {channel_name} WebSocket连接已关闭"
                )
                funasr_client.connected = False
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.debug(f"[{session.client_id}] {channel_name} 结果转发任务被取消")
                break
            except Exception as e2:
                if not session.is_active:
                    break
                logger.error(f"[{session.client_id}] 转发{channel_name}结果失败: {e2}")
                session.stats["errors"] = session.stats.get("errors", 0) + 1
                await asyncio.sleep(1)

        logger.info(f"[{session.client_id}] {channel_name} 结果转发结束")

    tasks = []
    if session.funasr_ch1:
        tasks.append(
            asyncio.create_task(forward_channel_results("CH1", session.funasr_ch1))
        )
    if session.funasr_ch2:
        tasks.append(
            asyncio.create_task(forward_channel_results("CH2", session.funasr_ch2))
        )

    if tasks:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        for task in done:
            if task.exception():
                logger.error(f"转发器任务异常: {task.exception()}")


async def test_funasr_result_forwarder(session: ClientSession, websocket):
    async def forward_channel_results(channel: str, funasr_client: FunASRClient):
        channel_name = "员工" if channel == "CH1" else "客户"
        while session.is_active and funasr_client.connected:
            try:
                result = await funasr_client.receive_result()
                if result:
                    text = result.get("text", "").strip()

                    if text:
                        try:
                            await websocket.send_json(
                                {
                                    "type": "test_asr_result",
                                    "channel": channel,
                                    "channel_role": channel_name,
                                    "employee_number": session.employee_number,
                                    "call_id": session.current_call_info["call_id"],
                                    "caller": session.current_call_info["caller"],
                                    "callee": session.current_call_info["callee"],
                                    "sent_at": time.time(),
                                    "text": text,
                                    "mode": result.get("mode", ""),
                                    "raw_data": result,
                                }
                            )
                        except websockets.exceptions.ConnectionClosed:
                            logger.debug(f"WebSocket连接已关闭，停止转发结果")
                            session.is_active = False
                            break
                        except RuntimeError as e2:
                            if "Cannot call" in str(e2) or "close message" in str(e2):
                                logger.debug(f"WebSocket已关闭，停止发送: {e2}")
                                session.is_active = False
                                break
                            else:
                                raise

            except Exception as e2:
                logger.error(f"测试转发{channel_name}结果失败: {e2}")
                await asyncio.sleep(1)

    tasks = []
    if session.funasr_ch1:
        tasks.append(
            asyncio.create_task(forward_channel_results("CH1", session.funasr_ch1))
        )
    if session.funasr_ch2:
        tasks.append(
            asyncio.create_task(forward_channel_results("CH2", session.funasr_ch2))
        )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def print_stats_periodically():
    while True:
        await asyncio.sleep(30)
        stats = audio_router.stats
        logger.info(
            f"【服务统计】UDP包={stats['udp_packets_received']}, "
            f"路由包={stats['audio_packets_routed']} "
            f"(CH1:{stats['audio_packets_routed_ch1']}/CH2:{stats['audio_packets_routed_ch2']}), "
            f"活跃订阅={stats['active_subscriptions']}, "
            f"活跃会话={stats['active_sessions']}, "
            f"路由错误={stats['routing_errors']}"
        )


async def check_resource_leaks_periodically():
    while True:
        try:
            await asyncio.sleep(300)
            leaks = await connection_monitor.check_for_leaks()
            if leaks:
                logger.warning(f"发现 {len(leaks)} 个疑似连接泄露:")
                for client_id, created_time in leaks[:10]:
                    logger.warning(
                        f"  - {client_id}: 已存活 {time.time() - created_time:.0f} 秒"
                    )

            stats = audio_router.stats
            if stats["active_sessions"] > 1200:
                logger.error(f"活跃会话数异常高: {stats['active_sessions']}")

        except Exception as e2:
            logger.error(f"资源泄露检查失败: {e2}")


async def cleanup_inactive_sessions_periodically():
    diagnosis_counter = 0
    diagnosis_interval = 6

    while True:
        try:
            await audio_router.cleanup_inactive_sessions()

            diagnosis_counter += 1
            if diagnosis_counter >= diagnosis_interval:
                logger.info("[周期任务] 开始执行定期会话诊断...")
                try:
                    await audio_router.diagnose_and_cleanup_sessions()
                except Exception as e2:
                    logger.error(f"[周期任务] 定期诊断执行失败: {e2}")
                diagnosis_counter = 0
                logger.info("[周期任务] 定期会话诊断完成。")

        except Exception as e3:
            logger.error(f"清理过期会话失败: {e3}")
        await asyncio.sleep(30)


async def monitor_system_resources():
    import psutil
    import os

    while True:
        try:
            process = psutil.Process(os.getpid())
            memory_info = process.memory_info()
            memory_mb = memory_info.rss / 1024 / 1024

            if hasattr(process, "num_fds"):
                num_fds = process.num_fds()
            else:
                num_fds = len(process.open_files())

            num_threads = process.num_threads()

            logger.info(
                f"系统资源: 内存={memory_mb:.1f}MB, 文件描述符={num_fds}, 线程={num_threads}"
            )

            if memory_mb > 1024:
                logger.warning(f"内存使用过高: {memory_mb:.1f}MB")

            if num_fds > 1000:
                logger.warning(f"文件描述符过多: {num_fds}")

            await asyncio.sleep(60)

        except Exception as e2:
            logger.error(f"系统资源监控失败: {e2}")
            await asyncio.sleep(60)
