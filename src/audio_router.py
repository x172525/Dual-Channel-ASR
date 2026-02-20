"""
音频路由管理器模块
"""

import time
import asyncio
import logging
from typing import Dict, Set, Optional, List, Tuple
from collections import defaultdict

from config import logger

try:
    from src.client_session import ClientSession
except ImportError:
    from client_session import ClientSession


class AudioRouterManager:
    def __init__(self):
        self.subscriptions: Dict[str, Set[ClientSession]] = defaultdict(set)
        self.number_to_employees: Dict[str, Set[str]] = defaultdict(set)
        self.recent_active_numbers: Dict[str, float] = {}
        self.recent_numbers_ttl = 30

        self._last_cleanup_time = time.time()
        self._cleanup_interval = 30

        self.stats = {
            "udp_packets_received": 0,
            "audio_packets_routed": 0,
            "audio_packets_routed_ch1": 0,
            "audio_packets_routed_ch2": 0,
            "active_subscriptions": 0,
            "active_sessions": 0,
            "routing_errors": 0,
        }

        self._lock = asyncio.Lock()
        self._number_cache_ttl = 10
        self._number_cache: Dict[str, Tuple[float, Set[str]]] = {}

        logger.info("音频路由管理器初始化完成 (高性能无队列版)")

    async def subscribe(self, employee_number: str, client_session: ClientSession):
        async with self._lock:
            self.subscriptions[employee_number].add(client_session)
            self.number_to_employees[employee_number].add(employee_number)

            self.stats["active_subscriptions"] = len(self.subscriptions)
            self.stats["active_sessions"] = sum(
                len(sessions) for sessions in self.subscriptions.values()
            )
            self._clear_cache()

        logger.info(
            f"新订阅: 员工号={employee_number}, 客户端={client_session.client_id}"
        )
        return True

    async def unsubscribe(self, employee_number: str, client_session: ClientSession):
        async with self._lock:
            if employee_number in self.subscriptions:
                self.subscriptions[employee_number].discard(client_session)
                if client_session in self.subscriptions[employee_number]:
                    self.subscriptions[employee_number].remove(client_session)

                if not self.subscriptions[employee_number]:
                    del self.subscriptions[employee_number]

                for num, employees in list(self.number_to_employees.items()):
                    if employee_number in employees:
                        employees.discard(employee_number)
                        if not employees:
                            del self.number_to_employees[num]

                self.stats["active_subscriptions"] = len(self.subscriptions)
                self.stats["active_sessions"] = sum(
                    len(sessions) for sessions in self.subscriptions.values()
                )
                self._clear_cache()

        logger.info(
            f"取消订阅: 员工号={employee_number}, 客户端={client_session.client_id}"
        )

    def _get_employees_for_number(self, number: str) -> Set[str]:
        current_time = time.time()

        if number in self._number_cache:
            cache_time, employees = self._number_cache[number]
            if current_time - cache_time < self._number_cache_ttl:
                return employees

        employees = set()

        if number in self.subscriptions:
            employees.add(number)

        if number in self.number_to_employees:
            employees.update(self.number_to_employees[number])

        for emp_num in self.subscriptions.keys():
            if emp_num.endswith(number) or number.endswith(emp_num):
                employees.add(emp_num)

        self._number_cache[number] = (current_time, employees)
        return employees

    async def route_audio(
        self, call_id: str, caller: str, callee: str, channel: str, audio_data: bytes
    ):
        try:
            await self.cleanup_inactive_sessions()
        except Exception as e2:
            logger.error(f"清理不活跃会话出错: {e2}")

        self.stats["udp_packets_received"] += 1

        current_time = time.time()

        if caller != "Unknown":
            self.recent_active_numbers[caller] = current_time
        if callee != "Unknown":
            self.recent_active_numbers[callee] = current_time

        self._cleanup_recent_numbers(current_time)

        if self.stats["udp_packets_received"] % 100000 == 0:
            logger.info(
                f"【路由统计】总包数={self.stats['udp_packets_received']}, "
                f"路由成功={self.stats['audio_packets_routed']}, "
                f"活跃订阅={self.stats['active_subscriptions']}, "
                f"活跃会话={self.stats['active_sessions']}"
            )

        if caller == "Unknown" and callee == "Unknown":
            if self.stats["udp_packets_received"] % 10000 == 0:
                logger.debug(f"音频无有效号码: call_id={call_id[:12]}")
            return

        target_employees = set()
        if caller != "Unknown":
            target_employees.update(self._get_employees_for_number(caller))
        if callee != "Unknown":
            target_employees.update(self._get_employees_for_number(callee))

        if not target_employees:
            if self.stats["udp_packets_received"] % 50000 == 0:
                logger.debug(
                    f"音频无目标订阅者: caller={caller}, callee={callee}, call_id={call_id[:12]}"
                )
            return

        tasks = []
        for employee in target_employees:
            async with self._lock:
                sessions = list(self.subscriptions.get(employee, []))

            for session in sessions:
                if session.is_active:
                    task = asyncio.create_task(
                        self._process_audio_for_session(
                            session,
                            channel,
                            audio_data,
                            call_id=call_id,
                            caller=caller,
                            callee=callee,
                        )
                    )
                    tasks.append(task)

        if tasks:
            batch_size = min(50, len(tasks))
            for i in range(0, len(tasks), batch_size):
                batch = tasks[i : i + batch_size]
                try:
                    await asyncio.gather(*batch, return_exceptions=True)
                except Exception as e2:
                    logger.error(f"批量处理音频任务失败: {e2}")
                finally:
                    del batch

        if tasks:
            self.stats["audio_packets_routed"] += 1
            if channel == "CH1":
                self.stats["audio_packets_routed_ch1"] += 1
            else:
                self.stats["audio_packets_routed_ch2"] += 1

    async def _process_audio_for_session(
        self,
        session: ClientSession,
        channel: str,
        audio_data: bytes,
        call_id: str = "",
        caller: str = "",
        callee: str = "",
    ):
        try:
            success = await session.process_audio(
                channel, audio_data, call_id, caller, callee
            )
            if not success and session.is_active:
                if (
                    channel == "CH1"
                    and session.funasr_ch1
                    and not session.funasr_ch1.connected
                ):
                    await session.funasr_ch1.connect()
                elif (
                    channel == "CH2"
                    and session.funasr_ch2
                    and not session.funasr_ch2.connected
                ):
                    await session.funasr_ch2.connect()
        except Exception as e2:
            self.stats["routing_errors"] += 1
            logger.error(f"处理音频会话失败 {session.client_id}: {e2}")

    def _cleanup_recent_numbers(self, current_time: float):
        expired = [
            num
            for num, ts in self.recent_active_numbers.items()
            if current_time - ts > self.recent_numbers_ttl
        ]
        for num in expired:
            del self.recent_active_numbers[num]

    def _clear_cache(self):
        self._number_cache.clear()

    def get_recent_active_numbers(self) -> List[str]:
        current_time = time.time()
        self._cleanup_recent_numbers(current_time)
        return list(self.recent_active_numbers.keys())

    async def cleanup_inactive_sessions(self):
        current_time = time.time()
        if current_time - self._last_cleanup_time < self._cleanup_interval:
            return

        self._last_cleanup_time = current_time

        async with self._lock:
            sessions_to_remove = []

            for employee_number, sessions in list(self.subscriptions.items()):
                for session in list(sessions):
                    remove_session = False
                    if not session.is_active:
                        remove_session = True
                    elif current_time - session.last_activity > 300:
                        logger.info(f"会话 [{session.client_id}] 因超时被清理")
                        remove_session = True
                    elif hasattr(session, "websocket") and session.websocket:
                        try:
                            state = session.websocket.client_state
                            if state.name != "CONNECTED":
                                remove_session = True
                                logger.info(
                                    f"清理: 会话 [{session.client_id}] WebSocket断开"
                                )
                        except Exception as e5:
                            remove_session = True
                            logger.info(
                                f"清理: 会话 [{session.client_id}] WebSocket异常: {e5}"
                            )

                    if remove_session:
                        sessions_to_remove.append((employee_number, session))

            for employee_number, session in sessions_to_remove:
                if session in self.subscriptions.get(employee_number, set()):
                    self.subscriptions[employee_number].remove(session)
                    logger.info(
                        f"清理不活跃会话: 员工号={employee_number}, 客户端={session.client_id}"
                    )

                for num, employees in list(self.number_to_employees.items()):
                    if employee_number in employees:
                        employees.discard(employee_number)
                        if not employees:
                            del self.number_to_employees[num]

            for employee_number in list(self.subscriptions.keys()):
                if not self.subscriptions[employee_number]:
                    del self.subscriptions[employee_number]

            self.stats["active_subscriptions"] = len(self.subscriptions)
            self.stats["active_sessions"] = sum(
                len(sessions) for sessions in self.subscriptions.values()
            )

    async def diagnose_and_cleanup_sessions(self):
        logger.info("=== 开始会话诊断 ===")
        async with self._lock:
            for emp_num, sessions in self.subscriptions.items():
                logger.info(f"员工号 [{emp_num}] 有 {len(sessions)} 个会话:")
                for session in list(sessions):
                    is_healthy = (
                        session.is_active
                        and hasattr(session, "websocket")
                        and session.websocket
                        and session.websocket.client_state.name == "CONNECTED"
                    )
                    logger.info(
                        f"  - 会话 [{session.client_id}], 活跃: {session.is_active}, 健康: {is_healthy}, "
                        f"最后活动: {time.time() - session.last_activity:.0f}秒前"
                    )

                    if not is_healthy:
                        logger.warning(
                            f"检测到不健康会话 [{session.client_id}]，开始清理..."
                        )
                        session.is_active = False
                        await asyncio.sleep(0.1)
                        sessions.discard(session)
                        await session.close()
                        logger.info(f"    不健康会话 [{session.client_id}] 清理完成。")

            for emp_num in list(self.subscriptions.keys()):
                if not self.subscriptions[emp_num]:
                    del self.subscriptions[emp_num]

        logger.info("=== 会话诊断结束 ===")


audio_router = AudioRouterManager()


class ConnectionMonitor:
    def __init__(self):
        self.active_connections = {}
        self.closed_connections = {}
        self._lock = asyncio.Lock()

    async def connection_created(self, client_id: str):
        async with self._lock:
            self.active_connections[client_id] = time.time()

    async def connection_closed(self, client_id: str, reason: str = "unknown"):
        async with self._lock:
            if client_id in self.active_connections:
                created_time = self.active_connections.pop(client_id)
                duration = time.time() - created_time
                self.closed_connections[client_id] = (time.time(), reason, duration)
                self._cleanup_old_records()

    def _cleanup_old_records(self):
        cutoff = time.time() - 24 * 3600
        to_remove = []
        for client_id, (closed_time, _, _) in self.closed_connections.items():
            if closed_time < cutoff:
                to_remove.append(client_id)

        for client_id in to_remove:
            del self.closed_connections[client_id]

    async def check_for_leaks(self):
        current_time = time.time()
        async with self._lock:
            leaks = []
            for client_id, created_time in self.active_connections.items():
                if current_time - created_time > 3600:
                    leaks.append((client_id, created_time))
            return leaks


connection_monitor = ConnectionMonitor()
