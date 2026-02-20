# RTPForward

RTP转发工具 - 将VoIP电话RTP音频流整合后实时推送到指定目标。

## 功能

- 监听网卡抓取SIP信令和RTP媒体流
- 解析SIP消息提取主叫、被叫、Call-ID等呼叫信息
- 根据源IP自动判断声道(CH1员工侧 / CH2客户侧)
- G.711解码(支持A-law和μ-law)
- 转发包含呼叫元数据的PCM音频流

## 配置

编辑 `rtp_forwarder.py` 配置文件区域：

```python
# 转发目标 (IP, 端口)
FORWARD_TARGETS = [("xx.xx.xx.xx", 8850)]

# 监控网卡
# 网口查看：ip addr show，确定电话线路口
MONITOR_INTERFACES = ["eno2", "eno3"]

# SIP端口，通常是5060，但有可能还有其他SIP端口
SIP_PORTS = [5060]

# 客户侧IP段 (CH2)，根据实际填写
CUSTOMER_IP_RANGES = ["xx.xx.xx.xx-xx"]

# 员工侧IP段 (CH1)，根据实际填写
EMPLOYEE_IP_RANGES = ["xx.xx.xx.xx-xx"]
```

## 使用

```bash
# 安装依赖
pip install scapy

# 单独运行rtp_forwarder.py程序(需要root权限)
sudo python rtp_forwarder.py
```

## 转发数据格式

每个UDP包包含:
- 4字节Header长度(小端)
- JSON元数据(call_id, caller, callee, channel, ts, audio_len)
- PCM音频数据
