#!/usr/bin/env python3
# rtp_forwarder_v3.py
"""
RTP转发：将VoIP电话信号整合成统一的语音推流，实时定向推送到指定IP指定端口
"""
import socket
import json
import struct
import time
import threading
import re
import ipaddress
from collections import defaultdict
import logging
import scapy.all as scapy
from scapy.layers.inet import IP, UDP
# from scapy.all import sniff

# ==================== 配置文件区域 ====================
FORWARD_TARGETS = [("xx.xx.xx.xx", 8850)]
# 网口查看：ip addr show
MONITOR_INTERFACES = ["eno2", "eno3"]
SIP_PORTS = [5060]

# E1网关IP段 (客户侧 CH2) - 请根据运维给的网络环境配置
CUSTOMER_IP_RANGES = [
    "xx.xx.xx.xx-xx",
    "xx.xx.xx.xx-xx"
]
# 座席/CTI服务器IP段 (员工侧 CH1) - 请根据运维给的网络环境配置
EMPLOYEE_IP_RANGES = [
    "xx.xx.xx.xx-xx",
    "xx.xx.xx.xx-xx"
]
# ====================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - RTPForwarder - %(levelname)s - %(message)s'
)
logger = logging.getLogger()


class IPRangeMatcher:
    """IP范围匹配器，用于动态判断声道"""
    def __init__(self, customer_ranges, employee_ranges):
        self.customer_networks = self._parse_ranges(customer_ranges)
        self.employee_networks = self._parse_ranges(employee_ranges)

    def _parse_ranges(self, ip_ranges):
        networks = []
        for r in ip_ranges:
            if '-' in r:
                start_str, end_str = r.split('-')
                if end_str.count('.') == 0:
                    base = '.'.join(start_str.split('.')[:3])
                    end_str = f"{base}.{end_str}"
                start_ip = ipaddress.ip_address(start_str)
                end_ip = ipaddress.ip_address(end_str)
                # 将连续IP范围转换为CIDR列表（简化处理）
                networks.append((int(start_ip), int(end_ip)))
            else:
                ip = ipaddress.ip_address(r)
                networks.append((int(ip), int(ip)))
        return networks

    def get_channel(self, ip_str: str) -> str:
        """根据IP判断声道，CH2=客户，CH1=员工"""
        try:
            ip_int = int(ipaddress.ip_address(ip_str))
            # 匹配员工侧IP
            for start, end in self.employee_networks:
                if start <= ip_int <= end:
                    return "CH1"
            # 匹配客户侧IP
            for start, end in self.customer_networks:
                if start <= ip_int <= end:
                    return "CH2"
        except:
            pass
        # 添加日志记录未知IP （用于后续完善配置）
        logger.warning(f"未知IP无法判断声道: {ip_str}")
        # 返回"Unknown"而不是默认CH1
        return "Unknown"



# ==================== SIP解析器 ====================

# 预编译所有正则表达式
_PATTERN_CALL_ID = re.compile(r'Call-ID:\s*([^\s\r\n]+)')
_PATTERN_PAI = re.compile(r'P-Asserted-Identity:\s*(?:[^<]*<)?sip:([^:@>\s]+)')
_PATTERN_FROM = re.compile(r'From:\s*(?:[^<]*<)?sip:([^:@>\s]+)')
_PATTERN_TO = re.compile(r'To:\s*(?:[^<]*<)?sip:([^:@>\s]+)')
_PATTERN_CONTACT = re.compile(r'Contact:\s*(?:[^<]*<)?sip:([^:@>\s]+)')
_PATTERN_SDP_CONNECTION = re.compile(r'c=IN IP4 (\d+\.\d+\.\d+\.\d+)')


class SIPParser:
    """使用Scapy增强的SIP解析器，支持SDP解析"""

    def __init__(self):
        # 缓存Call-ID到号码的映射，应对多次INVITE/200 OK
        self.callid_to_numbers = {}

    def parse_with_scapy(self, udp_payload: bytes, src_ip: str, src_port: int):
        """
        使用Scapy解析SIP/UDP数据包
        返回: (call_id, caller, callee, sdp_info)
        """
        try:
            # 解码SIP消息文本
            text = udp_payload.decode('utf-8', errors='ignore')

            call_id = caller = callee = None
            sdp_info = {}

            # 使用正则表达式提取关键信息）
            callid_match = _PATTERN_CALL_ID.search(text)
            if callid_match:
                call_id = callid_match.group(1).strip()

            # 提取主叫号码 (优先P-Asserted-Identity，然后From)
            pai_match = _PATTERN_PAI.search(text)
            if pai_match:
                caller = pai_match.group(1)
            else:
                from_match = _PATTERN_FROM.search(text)
                if from_match:
                    if from_match:
                        caller = from_match.group(1)
                # 如果到这里caller还是None，就设为"Unknown"
                if caller is None:
                    caller = "Unknown"

            # 提取被叫号码 (To头)
            to_match = _PATTERN_TO.search(text)
            if to_match:
                callee = to_match.group(1)
            if callee is None:
                callee = "Unknown"

            # 尝试从Contact头部获取
            if (not caller or caller == "Unknown") or (not callee or callee == "Unknown"):
                contact_match = _PATTERN_CONTACT.search(text)
                if contact_match:
                    contact_num = contact_match.group(1)
                    if not caller or caller == "Unknown":
                        caller = contact_num
                    elif not callee or callee == "Unknown":
                        callee = contact_num

            # 解析SDP信息 (查找媒体IP和端口)
            sdp_info = self._parse_sdp_from_text(text)

            # 清理号码格式
            caller = self._clean_number(caller) if caller else "Unknown"
            callee = self._clean_number(callee) if callee else "Unknown"

            # 缓存号码信息
            if call_id:
                self.callid_to_numbers[call_id] = (caller, callee, time.time())

            logger.debug(f"增强SIP解析: call_id={call_id[:16] if call_id else 'Unknown'} "
                         f"主叫={caller} 被叫={callee} SDP端口={sdp_info.get('audio_port', 'N/A')}")

            return call_id, caller, callee, sdp_info

        except Exception as e:
            logger.debug(f"增强SIP解析异常: {e}, 使用降级解析")
            return self._fallback_parse(udp_payload, src_ip, src_port)

    def _parse_sdp_from_text(self, text: str):
        """从SIP消息文本中解析SDP信息"""
        sdp_info = {'media': []}
        try:
            # 查找SDP部分（通常在消息体后）
            sdp_start = text.find('\r\n\r\n')
            if sdp_start == -1:
                return sdp_info

            sdp_text = text[sdp_start:].strip()

            # 解析连接信息（媒体IP）
            for line in sdp_text.split('\r\n'):
                line = line.strip()
                if line.startswith('c=IN IP4 '):
                    ip_match = _PATTERN_SDP_CONNECTION.search(line)
                    if ip_match:
                        sdp_info['media_ip'] = ip_match.group(1)

                # 解析音频媒体行: m=audio <port> ...
                elif line.startswith('m=audio '):
                    parts = line.split()
                    if len(parts) >= 2:
                        sdp_info['audio_port'] = int(parts[1])
                        media_info = {
                            'type': 'audio',
                            'port': int(parts[1]),
                            'proto': parts[2] if len(parts) > 2 else 'RTP/AVP'
                        }
                        sdp_info['media'].append(media_info)

        except Exception as e:
            logger.debug(f"SDP解析异常: {e}")

        return sdp_info

    def _clean_number(self, number):
        """清理号码格式"""
        if not number or number == "Unknown":
            return "Unknown"

        # 移除分号后的参数，如 8001;user=phone
        if ';' in number:
            number = number.split(';')[0]

        # 移除冒号后的端口，如 8001:5060
        if ':' in number:
            # 检查是否是IP:端口格式
            ip_part = number.split(':')[0]
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip_part):
                # 如果是IP地址，只保留IP部分
                return ip_part
            else:
                # 不是IP，可能是号码:端口，只取号码部分
                number = ip_part

        # 过滤纯IP地址
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', number):
            return "Unknown"

        return number

    def _fallback_parse(self, data: bytes, src_ip: str, src_port: int):
        """降级解析"""
        try:
            text = data.decode('utf-8', errors='ignore')

            # 预编译所有正则表达式
            callid_match = _PATTERN_CALL_ID.search(text)
            from_match = _PATTERN_FROM.search(text)
            to_match = _PATTERN_TO.search(text)
            pai_match = _PATTERN_PAI.search(text)

            call_id = callid_match.group(1) if callid_match else f"unknown_{int(time.time())}"
            caller = callee = "Unknown"

            if pai_match:
                caller = pai_match.group(1)
            elif from_match:
                caller = from_match.group(1)

            if to_match:
                callee = to_match.group(1)

            # 尝试从Contact获取
            if caller == "Unknown":
                contact_match = _PATTERN_CONTACT.search(text)
                if contact_match:
                    caller = contact_match.group(1)

            caller = self._clean_number(caller)
            callee = self._clean_number(callee)

            return call_id, caller, callee, {}

        except Exception as e:
            logger.debug(f"降级解析异常: {e}")
            return f"unknown_{int(time.time())}", "Unknown", "Unknown", {}


class CallTracker:
    """呼叫跟踪器"""

    def __init__(self, ip_matcher: IPRangeMatcher):
        self.ip_matcher = ip_matcher
        self.sip_parser = SIPParser()

        # 呼叫状态跟踪
        self.active_calls = {}  # call_id -> (caller, callee, last_update_time)
        self.call_id_numbers = defaultdict(set)  # call_id -> 所有出现过的号码集合

        # RTP端口映射
        self.rtp_to_call = {}  # (src_ip, src_port) -> call_id
        self.rtp_port_mapping = {}  # (ip, port) -> call_id (来自SDP)
        self.call_rtp_ports = defaultdict(list)  # call_id -> [(ip, port), ...]

        self._lock = threading.Lock()

        self.channel_stats = {"CH1": 0, "CH2": 0, "Unknown": 0}

    def process_sip(self, data: bytes, src_ip: str, src_port: int):
        """处理SIP数据包，提取号码和SDP信息"""
        call_id, caller, callee, sdp_info = self.sip_parser.parse_with_scapy(data, src_ip, src_port)

        if not call_id:
            return

        with self._lock:
            # 更新呼叫信息
            self.active_calls[call_id] = (caller, callee, time.time())

            # 记录历史号码
            if caller != "Unknown":
                self.call_id_numbers[call_id].add(caller)
            if callee != "Unknown":
                self.call_id_numbers[call_id].add(callee)

            # 处理SDP媒体信息
            if sdp_info and 'media' in sdp_info:
                # 优先使用SDP中的媒体IP，否则使用源IP
                media_ip = sdp_info.get('media_ip', src_ip)

                for media in sdp_info['media']:
                    if media['type'] == 'audio':
                        rtp_port = media['port']
                        # 建立端口映射
                        key = (media_ip, rtp_port)
                        self.rtp_port_mapping[key] = call_id
                        self.call_rtp_ports[call_id].append(key)

                        # logger.info(f"SDP媒体映射: {media_ip}:{rtp_port} -> {call_id[:12]} " f"(主叫={caller}, 被叫={callee})")

        # 记录日志
        if caller != "Unknown" or callee != "Unknown":
            pass
            # logger.info(f"呼叫更新: {call_id[:16]} 主叫:{caller} 被叫:{callee}")

    def get_call_info(self, rtp_src_ip: str, rtp_src_port: int):
        """根据RTP流获取呼叫信息"""
        key = (rtp_src_ip, rtp_src_port)

        with self._lock:
            # 检查SDP建立的映射
            if key in self.rtp_port_mapping:
                call_id = self.rtp_port_mapping[key]
                if call_id in self.active_calls:
                    caller, callee, _ = self.active_calls[call_id]
                    return call_id, caller, callee

            # 检查已有的RTP到呼叫的映射
            if key in self.rtp_to_call:
                call_id = self.rtp_to_call[key]
                if call_id in self.active_calls:
                    caller, callee, _ = self.active_calls[call_id]
                    return call_id, caller, callee

            # 检查同一IP的其他端口映射
            for (ip, port), cid in self.rtp_port_mapping.items():
                if ip == rtp_src_ip and cid in self.active_calls:
                    # 同一IP的不同端口，很可能是同一呼叫
                    self.rtp_to_call[key] = cid
                    caller, callee, _ = self.active_calls[cid]
                    return cid, caller, callee

            # 关联到最新的活跃呼叫
            if self.active_calls:
                latest_call_id = next(reversed(self.active_calls))
                self.rtp_to_call[key] = latest_call_id
                caller, callee, _ = self.active_calls[latest_call_id]
                return latest_call_id, caller, callee

        return f"unknown_{int(time.time())}", "Unknown", "Unknown"

    def _cleanup_old_calls(self):
        """每5分钟清理一次30分钟前的旧呼叫"""
        cutoff = time.time() - 1800  # 30分钟
        with self._lock:
            to_delete = [cid for cid, (_, _, ts) in self.active_calls.items() if ts < cutoff]
            for cid in to_delete:
                del self.active_calls[cid]
                if cid in self.call_id_numbers:
                    del self.call_id_numbers[cid]
                if cid in self.call_rtp_ports:
                    del self.call_rtp_ports[cid]
                # 清理相关映射
                self.rtp_to_call = {k: v for k, v in self.rtp_to_call.items() if v != cid}
                self.rtp_port_mapping = {k: v for k, v in self.rtp_port_mapping.items() if v != cid}

    def update_channel_stats(self, channel):
        if channel in self.channel_stats:
            self.channel_stats[channel] += 1
        else:
            self.channel_stats["Unknown"] += 1

    def log_stats(self):
        logger.info(f"声道统计: {self.channel_stats}")


class AudioForwarder:
    """音频转发器，转发数据包含call_id"""

    def __init__(self):
        self.sockets = []
        self._lock = threading.Lock()
        for target in FORWARD_TARGETS:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                self.sockets.append((sock, target))
                logger.info(f"初始化转发到 {target}")
            except Exception as e:
                logger.error(f"初始化socket失败 {target}: {e}")

    def forward(self, call_id: str, caller: str, callee: str, channel: str, audio_data: bytes, iface: str = None):
        # 控制日志频率的参数（单位：秒），例如设为5表示每5秒记录一次
        LOG_INTERVAL = 5
        # 计算当前时间间隔块
        current_interval = int(time.time() // LOG_INTERVAL)
        if not hasattr(self, 'last_log_time'):
            self.last_log_time = {}
        log_key = f"{call_id}_{channel}"
        if current_interval != self.last_log_time.get(log_key, 0):
            # 打印完整的call_id
            logger.info(f"转发音频: 数据来源={iface} call_id={call_id} 主叫={caller} 被叫={callee} "
                        f"声道={channel} 长度={len(audio_data)}字节")
            self.last_log_time[log_key] = current_interval

        header = json.dumps({
            "call_id": call_id,
            "caller": caller,
            "callee": callee,
            "channel": channel,
            "ts": int(time.time() * 1000),
            "audio_len": len(audio_data)  # 添加长度字段便于调试
        }).encode('utf-8')

        packet = struct.pack('<I', len(header)) + header + audio_data

        with self._lock:  # 添加线程锁
            for sock, target in self.sockets[:]:  # 遍历副本
                try:
                    sock.sendto(packet, target)
                except (socket.error, OSError) as e:
                    logger.error(f"转发失败 {target}: {e}, 移除该目标")
                    try:
                        sock.close()
                    except:
                        pass
                    self.sockets.remove((sock, target))  # 移除故障目标

    def add_target(self, target):
        """动态添加转发目标"""
        with self._lock:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 100 * 1024 * 1024)
                self.sockets.append((sock, target))
                logger.info(f"添加转发目标: {target}")
            except Exception as e:
                logger.error(f"添加目标失败 {target}: {e}")


# ==================== G.711解码函数 ====================
def decode_g711(g711_data: bytes, codec: str = 'ulaw') -> bytes:
    """
    解码G.711音频 (支持A-law和μ-law)
    Args:
        g711_data: G.711编码的字节数据
        codec: 'alaw' 或 'ulaw' (默认)
    Returns:
        PCM 16bit 单声道字节数据 (小端字节序)
    """
    # ==================== μ-law (u-law) 解码表 ====================
    # 完整的μ-law解码表 (256项)，根据ITU-T G.711标准
    ULAW_TABLE = [
        -32124, -31100, -30076, -29052, -28028, -27004, -25980, -24956,
        -23932, -22908, -21884, -20860, -19836, -18812, -17788, -16764,
        -15996, -15484, -14972, -14460, -13948, -13436, -12924, -12412,
        -11900, -11388, -10876, -10364, -9852, -9340, -8828, -8316,
        -7932, -7676, -7420, -7164, -6908, -6652, -6396, -6140,
        -5884, -5628, -5372, -5116, -4860, -4604, -4348, -4092,
        -3900, -3772, -3644, -3516, -3388, -3260, -3132, -3004,
        -2876, -2748, -2620, -2492, -2364, -2236, -2108, -1980,
        -1884, -1820, -1756, -1692, -1628, -1564, -1500, -1436,
        -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
        -876, -844, -812, -780, -748, -716, -684, -652,
        -620, -588, -556, -524, -492, -460, -428, -396,
        -372, -356, -340, -324, -308, -292, -276, -260,
        -244, -228, -212, -196, -180, -164, -148, -132,
        -120, -112, -104, -96, -88, -80, -72, -64,
        -56, -48, -40, -32, -24, -16, -8, 0,
        32124, 31100, 30076, 29052, 28028, 27004, 25980, 24956,
        23932, 22908, 21884, 20860, 19836, 18812, 17788, 16764,
        15996, 15484, 14972, 14460, 13948, 13436, 12924, 12412,
        11900, 11388, 10876, 10364, 9852, 9340, 8828, 8316,
        7932, 7676, 7420, 7164, 6908, 6652, 6396, 6140,
        5884, 5628, 5372, 5116, 4860, 4604, 4348, 4092,
        3900, 3772, 3644, 3516, 3388, 3260, 3132, 3004,
        2876, 2748, 2620, 2492, 2364, 2236, 2108, 1980,
        1884, 1820, 1756, -1692, -1628, -1564, -1500, -1436,
        -1372, -1308, -1244, -1180, -1116, -1052, -988, -924,
        -876, -844, -812, -780, -748, -716, -684, -652,
        -620, -588, -556, -524, -492, -460, -428, -396,
        -372, -356, -340, -324, -308, -292, -276, -260,
        -244, -228, -212, -196, -180, -164, -148, -132,
        -120, -112, -104, -96, -88, -80, -72, -64,
        -56, -48, -40, -32, -24, -16, -8, 0
    ]

    # ==================== A-law 解码表 ====================
    # 完整的A-law解码表 (256项)，根据ITU-T G.711标准
    ALAW_TABLE = [
        -5504, -5248, -6016, -5760, -4480, -4224, -4992, -4736,
        -7552, -7296, -8064, -7808, -6528, -6272, -7040, -6784,
        -2752, -2624, -3008, -2880, -2240, -2112, -2496, -2368,
        -3776, -3648, -4032, -3904, -3264, -3136, -3520, -3392,
        -22016, -20992, -24064, -23040, -17920, -16896, -19968, -18944,
        -30208, -29184, -32256, -31232, -26112, -25088, -28160, -27136,
        -11008, -10496, -12032, -11520, -8960, -8448, -9984, -9472,
        -15104, -14592, -16128, -15616, -13056, -12544, -14080, -13568,
        -344, -328, -376, -360, -280, -264, -312, -296,
        -472, -456, -504, -488, -408, -392, -440, -424,
        -88, -72, -120, -104, -24, -8, -56, -40,
        -216, -200, -248, -232, -152, -136, -184, -168,
        -1376, -1312, -1504, -1440, -1120, -1056, -1248, -1184,
        -1888, -1824, -2016, -1952, -1632, -1568, -1760, -1696,
        -688, -656, -752, -720, -560, -528, -624, -592,
        -944, -912, -1008, -976, -816, -784, -880, -848,
        5504, 5248, 6016, 5760, 4480, 4224, 4992, 4736,
        7552, 7296, 8064, 7808, 6528, 6272, 7040, 6784,
        2752, 2624, 3008, 2880, 2240, 2112, 2496, 2368,
        3776, 3648, 4032, 3904, 3264, 3136, 3520, 3392,
        22016, 20992, 24064, 23040, 17920, 16896, 19968, 18944,
        30208, 29184, 32256, 31232, 26112, 25088, 28160, 27136,
        11008, 10496, 12032, 11520, 8960, 8448, 9984, 9472,
        15104, 14592, 16128, 15616, 13056, 12544, 14080, 13568,
        344, 328, 376, 360, 280, 264, 312, 296,
        472, 456, 504, 488, 408, 392, 440, 424,
        88, 72, 120, 104, 24, 8, 56, 40,
        216, 200, 248, 232, 152, 136, 184, 168,
        1376, 1312, 1504, 1440, 1120, 1056, 1248, 1184,
        1888, 1824, 2016, 1952, 1632, 1568, 1760, 1696,
        688, 656, 752, 720, 560, 528, 624, 592,
        944, 912, 1008, 976, 816, 784, 880, 848
    ]

    # 选择解码表
    if codec.lower() == 'alaw':
        decode_table = ALAW_TABLE
    elif codec.lower() == 'ulaw':
        decode_table = ULAW_TABLE
    else:
        raise ValueError(f"不支持的编码格式: {codec}，支持 'alaw' 或 'ulaw'")

    # 预分配PCM缓冲区（每个G.711样本解码为2字节）
    pcm_size = len(g711_data) * 2
    pcm_buffer = bytearray(pcm_size)

    # 高效解码：使用查表法，避免条件判断
    for i, g711_byte in enumerate(g711_data):
        # 查表获取16位PCM值
        pcm_value = decode_table[g711_byte]

        # 小端字节序存储（低字节在前）
        # 使用位操作代替struct.pack以提高性能
        pcm_buffer[i * 2] = pcm_value & 0xFF  # 低字节
        pcm_buffer[i * 2 + 1] = (pcm_value >> 8) & 0xFF  # 高字节

    return bytes(pcm_buffer)


# ==================== 捕获线程 ====================
def capture_thread(iface: str, tracker: CallTracker, forwarder: AudioForwarder, ip_matcher: IPRangeMatcher):
    """
    使用Scapy增强的网络捕获线程[citation:1][citation:6]
    可以更可靠地解析协议栈
    """
    logger.info(f'{iface}: 线程启动，准备绑定抓包')
    try:
        # 添加详细的启动日志
        logger.info(f"{iface}: 开始捕获线程，SIP端口={SIP_PORTS}")

        # 添加网卡绑定调试信息
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            sock.bind((iface, 0))
            sock.settimeout(1.0)
            logger.info(f"{iface}: 成功绑定到网卡")
        except Exception as e:
            logger.error(f"{iface}: 绑定网卡失败: {e}")
            return

        logger.info(f"{iface}: 开始捕获 (增强模式)")

        stats = {'packets': 0, 'sip': 0, 'rtp': 0, 'forwarded': 0}
        last_stat_time = time.time()
        last_cleanup_time = time.time()

        # 为每个网卡维护独立的活跃呼叫集合
        local_active_calls = set()

        # 使用Scapy的sniff函数
        # 备用的Scapy捕获方式
        use_scapy_sniff = False  # 设置为True尝试Scapy嗅探

        if use_scapy_sniff:
            # Scapy嗅探模式[citation:6]
            def packet_callback(pkt):
                nonlocal stats, last_stat_time, last_cleanup_time
                stats['packets'] += 1

                # 检查IP和UDP层
                if IP in pkt and UDP in pkt:
                    src_ip = pkt[IP].src
                    dst_ip = pkt[IP].dst
                    src_port = pkt[UDP].sport
                    dst_port = pkt[UDP].dport
                    udp_payload = bytes(pkt[UDP].payload)

                    # 处理SIP
                    if src_port in SIP_PORTS or dst_port in SIP_PORTS or len(udp_data) > 200:
                        stats['sip'] += 1
                        tracker.process_sip(udp_payload, src_ip, src_port)
                        # 新增：记录本地呼叫ID
                        try:
                            text = udp_data.decode('utf-8', errors='ignore')
                            callid_match = _PATTERN_CALL_ID.search(text)
                            if callid_match:
                                local_active_calls.add(callid_match.group(1).strip())
                        except:
                            pass

                    # 处理RTP
                    elif len(udp_payload) >= 12:
                        rtp_version = (udp_payload[0] >> 6) & 0x03
                        if rtp_version == 2:
                            payload_type = udp_payload[1] & 0x7F
                            if payload_type in (0, 8, 9, 18, 3, 4, 97, 101):
                                stats['rtp'] += 1
                                call_id, caller, callee = tracker.get_call_info(src_ip, src_port)
                                channel = ip_matcher.get_channel(src_ip)
                                audio_payload = udp_payload[12:]
                                pcm_data = decode_g711(audio_payload, 'ulaw' if payload_type == 0 else 'alaw')
                                forwarder.forward(call_id, caller, callee, channel, pcm_data, iface)
                                stats['forwarded'] += 1

                # 定期清理和统计
                current_time = time.time()
                if current_time - last_cleanup_time > 300:
                    tracker._cleanup_old_calls()
                    last_cleanup_time = current_time

                if current_time - last_stat_time > 10:
                    logger.info(f"{iface}: 包={stats['packets']}, SIP={stats['sip']}, "
                                f"RTP={stats['rtp']}, 转发={stats['forwarded']}, "
                                f"活跃呼叫={len(local_active_calls)}, "
                                f"总活跃呼叫={len(tracker.active_calls)}")
                    stats = {'packets': 0, 'sip': 0, 'rtp': 0, 'forwarded': 0}
                    last_stat_time = current_time

            scapy.sniff(iface=iface, prn=packet_callback, store=0,
                        filter=f"udp port {SIP_PORTS} or udp portrange 10000-20000")

        else:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            sock.bind((iface, 0))
            sock.settimeout(1.0)

            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    stats['packets'] += 1

                    # 调试：定期显示收到的包数
                    if stats['packets'] % 1000 == 0 and iface == MONITOR_INTERFACES[0]:
                        logger.debug(f"{iface}: 已收到 {stats['packets']} 个包")
                    if stats['packets'] % 1000 == 0 and iface == MONITOR_INTERFACES[1]:
                        logger.debug(f"{iface}: 已收到 {stats['packets']} 个包")
                    '''
                    if stats['packets'] % 1000 == 0 and iface == MONITOR_INTERFACES[2]:
                        logger.debug(f"{iface}: 已收到 {stats['packets']} 个包")
                    '''

                    # 解析以太网帧
                    if len(data) < 14:
                        continue

                    eth_type = (data[12] << 8) | data[13]
                    if eth_type != 0x0800:
                        continue

                    # 解析IP头
                    if len(data) < 34:
                        continue

                    ip_header = data[14:34]
                    ip_ihl = (ip_header[0] & 0x0F) * 4
                    protocol = ip_header[9]
                    src_ip = socket.inet_ntoa(ip_header[12:16])
                    dst_ip = socket.inet_ntoa(ip_header[16:20])

                    if protocol != 17:  # UDP
                        continue

                    # 解析UDP头
                    if len(data) < 14 + ip_ihl + 8:
                        continue

                    udp_header = data[14 + ip_ihl:14 + ip_ihl + 8]
                    src_port = (udp_header[0] << 8) | udp_header[1]
                    dst_port = (udp_header[2] << 8) | udp_header[3]
                    udp_data = data[14 + ip_ihl + 8:]

                    # 处理SIP信令
                    if src_port in SIP_PORTS or dst_port in SIP_PORTS or len(udp_data) > 200:
                        stats['sip'] += 1
                        # 调试：显示SIP包信息
                        if iface == "eno4" and stats['sip'] % 100 == 0:
                            logger.info(
                                f"{iface}: SIP包 src={src_ip}:{src_port} -> dst={dst_ip}:{dst_port}, len={len(udp_data)}"
                            )
                            # 尝试显示SIP消息类型
                            try:
                                text = udp_data[:200].decode('utf-8', errors='ignore')  # 只显示前200字符
                                first_line = text.split('\r\n')[0] if '\r\n' in text else text[:50]
                                logger.info(f"{iface}: SIP消息: {first_line}")
                            except:
                                pass
                        tracker.process_sip(udp_data, src_ip, src_port)
                        # 新增：记录本地呼叫ID
                        try:
                            text = udp_data.decode('utf-8', errors='ignore')
                            callid_match = _PATTERN_CALL_ID.search(text)
                            if callid_match:
                                local_active_calls.add(callid_match.group(1).strip())
                                # 调试信息
                                if iface == MONITOR_INTERFACES[0]:
                                    logger.debug(f"{iface}: 提取到Call-ID: {call_id[:20]}...")
                                if iface == MONITOR_INTERFACES[1]:
                                    logger.debug(f"{iface}: 提取到Call-ID: {call_id[:20]}...")
                        except:
                            pass
                        continue

                    # RTP包判断
                    if len(udp_data) >= 12:
                        rtp_version = (udp_data[0] >> 6) & 0x03
                        if rtp_version == 2:
                            payload_type = udp_data[1] & 0x7F
                            if payload_type in (0, 8, 9, 18, 3, 4, 97, 101):
                                payload_len = len(udp_data) - 12
                                if 100 <= payload_len <= 500:
                                    stats['rtp'] += 1
                                    # 获取目的IP用于调试
                                    dst_ip = socket.inet_ntoa(ip_header[16:20])
                                    # 调试
                                    if iface == MONITOR_INTERFACES[0] and stats['rtp'] % 100 == 0:
                                        logger.debug(f"{iface}: RTP包: {src_ip}:{src_port} -> {dst_ip}:{dst_port}, "
                                                     f"payload_type={payload_type}, payload_len={payload_len}")
                                    if iface == MONITOR_INTERFACES[1] and stats['rtp'] % 100 == 0:
                                        logger.debug(f"{iface}: RTP包: {src_ip}:{src_port} -> {dst_ip}:{dst_port}, "
                                                     f"payload_type={payload_type}, payload_len={payload_len}")

                                    call_id, caller, callee = tracker.get_call_info(src_ip, src_port)

                                    channel = ip_matcher.get_channel(src_ip)

                                    if channel == "Unknown":
                                        dst_side = ip_matcher.get_channel(dst_ip)
                                        if dst_side == "CH1":
                                            channel = "CH2"
                                        elif dst_side == "CH2":
                                            channel = "CH1"


                                    audio_payload = udp_data[12:]

                                    if payload_type == 0:
                                        pcm_data = decode_g711(audio_payload, 'ulaw')
                                    elif payload_type == 8:
                                        pcm_data = decode_g711(audio_payload, 'alaw')
                                    else:
                                        continue

                                    forwarder.forward(call_id, caller, callee, channel, pcm_data, iface)
                                    stats['forwarded'] += 1

                    # 定期清理和统计
                    current_time = time.time()
                    if current_time - last_cleanup_time > 300:
                        tracker._cleanup_old_calls()
                        last_cleanup_time = current_time

                    if current_time - last_stat_time > 10:
                        logger.info(f"来源={iface}: 包={stats['packets']}, SIP={stats['sip']}, "
                                    f"RTP={stats['rtp']}, 转发={stats['forwarded']}, "
                                    f"活跃呼叫={len(local_active_calls)}, "
                                    f"总活跃呼叫={len(tracker.active_calls)}")
                        stats = {'packets': 0, 'sip': 0, 'rtp': 0, 'forwarded': 0}
                        last_stat_time = current_time

                except socket.timeout:
                    continue
                except Exception as e:
                    logger.error(f"{iface}处理异常: {e}")
                    continue

    except Exception as e:
        logger.error(f"{iface}捕获线程异常: {e}")


def validate_config():
    """验证配置有效性"""
    errors = []
    for iface in MONITOR_INTERFACES:
        try:
            socket.if_nametoindex(iface)
        except OSError:
            errors.append(f"网卡 {iface} 不存在")

    if not FORWARD_TARGETS:
        errors.append("未配置转发目标")

    for target in FORWARD_TARGETS:
        if not isinstance(target, tuple) or len(target) != 2:
            errors.append(f"转发目标格式错误: {target}")

    return errors


def main():
    config_errors = validate_config()
    if config_errors:
        logger.error("配置错误:")
        for err in config_errors:
            logger.error(f"  - {err}")
        exit(1)

    logger.info("RTP转发服务启动")
    logger.info(f"监控网卡: {MONITOR_INTERFACES}")
    logger.info(f"转发目标: {FORWARD_TARGETS}")

    # 初始化IP匹配器
    ip_matcher = IPRangeMatcher(CUSTOMER_IP_RANGES, EMPLOYEE_IP_RANGES)

    # 呼叫跟踪器
    tracker = CallTracker(ip_matcher)
    forwarder = AudioForwarder()

    threads = []
    for iface in MONITOR_INTERFACES:
        thread = threading.Thread(
            target=capture_thread,
            args=(iface, tracker, forwarder, ip_matcher),
            name=f"Capture-{iface}",
            daemon=True
        )
        thread.start()
        threads.append(thread)
        logger.info(f"启动 {iface} 线程")

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        logger.info("收到停止信号")
    finally:
        logger.info("服务停止")


if __name__ == "__main__":
    import os
    import sys

    # 检查Scapy是否可用
    try:
        import scapy.all

        logger.info("Scapy库可用")
    except ImportError:
        logger.error("错误: 未安装Scapy库，请运行: pip install scapy")
        sys.exit(1)

    if os.geteuid() != 0:
        logger.error("错误: 需要root权限运行")
        sys.exit(1)

    main()



