# UDP/RTP 推流格式说明

本文档详细说明向本服务推送音频数据的 UDP/RTP 推流格式要求。

## 协议概述

服务通过 UDP 协议监听音频数据，端口号为 `8850`（可在 config.py 中修改）。

## 数据包格式

### 基础格式

```
+------------------+---------------------------+
| 4 字节 (Header长度) | JSON Header | 音频数据    |
+------------------+---------------------------+
```

- **Header 长度**：4 字节无符号整数（小端序，uint32），表示 JSON Header 的字节长度
- **JSON Header**：变长，JSON 格式的元数据
- **音频数据**：原始 PCM 音频数据

### 示例

```
# 假设 Header 为 {"call_id": "123", "caller": "10086", "callee": "10010", "channel": "CH1"}
# JSON 字符串长度 = 65 字节

# 完整数据包结构：
[0x41 0x00 0x00 0x00]  [{"call_id":"123",...}]  [PCM音频数据...]
     ↑                        ↑                        ↑
   Header长度              JSON Header              音频数据
   (65 = 0x41)
```

## Header JSON 字段说明

| 字段 | 类型 | 必填 | 说明 | 示例 |
|------|------|------|------|------|
| `call_id` | string | 是 | 通话唯一标识 | `"call_123456"` |
| `caller` | string | 是 | 主叫号码 | `"10086"` |
| `callee` | string | 是 | 被叫号码 | `"10010"` |
| `channel` | string | 是 | 声道标识 | `"CH1"` 或 `"CH2"` |

### channel 字段说明

- `CH1`：员工声道（主叫，通常是客服/坐席）
- `CH2`：客户声道（被叫，通常是用户/客户）

## 音频数据格式要求

### 采样率

- **支持采样率**：8000 Hz（推荐）、16000 Hz
- 生产环境建议使用 **8000 Hz**

### 音频格式

- **编码**：PCM 16-bit 有符号整数（小端序）
- **声道**：单声道
- **字节序**：小端序（Little Endian）

### 音频帧大小

| 采样率 | 帧时长 | 帧大小 |
|--------|--------|--------|
| 8000 Hz | 40 ms | 640 字节 |
| 16000 Hz | 40 ms | 1280 字节 |

计算公式：`帧大小 = 采样率 × 声道数 × 2(16bit) × 帧时长(秒)`

## 完整示例

### Python 发送示例

```python
import socket
import json
import struct

def send_audio_packet(sock, call_id, caller, callee, channel, audio_data):
    """发送音频数据包"""
    
    # 构建 Header
    header = {
        "call_id": call_id,
        "caller": caller,
        "callee": callee,
        "channel": channel  # "CH1" 或 "CH2"
    }
    
    # 序列化为 JSON
    header_json = json.dumps(header, ensure_ascii=False)
    header_bytes = header_json.encode('utf-8')
    
    # 打包 Header 长度 (4 字节, 小端序)
    header_len = struct.pack('<I', len(header_bytes))
    
    # 发送: [长度][Header][音频数据]
    packet = header_len + header_bytes + audio_data
    sock.sendto(packet, ('127.0.0.1', 8850))


# 使用示例
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 发送 CH1 (员工/客服) 音频
with open('employee_audio.pcm', 'rb') as f:
    audio_data = f.read()
    send_audio_packet(sock, "call_001", "10086", "10010", "CH1", audio_data)

# 发送 CH2 (客户) 音频
with open('customer_audio.pcm', 'rb') as f:
    audio_data = f.read()
    send_audio_packet(sock, "call_001", "10086", "10010", "CH2", audio_data)

sock.close()
```

### Golang 发送示例

```go
package main

import (
    "encoding/binary"
    "encoding/json"
    "net"
)

type AudioHeader struct {
    CallID  string `json:"call_id"`
    Caller  string `json:"caller"`
    Callee  string `json:"callee"`
    Channel string `json:"channel"` // "CH1" or "CH2"
}

func SendAudioPacket(conn *net.UDPConn, callID, caller, callee, channel string, audioData []byte) {
    header := AudioHeader{
        CallID:  callID,
        Caller:  caller,
        Callee:  callee,
        Channel: channel,
    }
    
    headerJSON, _ := json.Marshal(header)
    
    // 打包 Header 长度 (4 bytes, little-endian)
    headerLen := make([]byte, 4)
    binary.LittleEndian.PutUint32(headerLen, uint32(len(headerJSON)))
    
    // 拼接数据包
    packet := append(headerLen, headerJSON...)
    packet = append(packet, audioData...)
    
    conn.WriteToUDP(packet, &net.UDPAddr{IP: net.IP{127, 0, 0, 1}, Port: 8850})
}

func main() {
    conn, _ := net.DialUDP("udp", nil, &net.UDPAddr{IP: net.IP{127, 0, 0, 1}, Port: 8850})
    defer conn.Close()
    
    // 读取音频文件
    audioData := make([]byte, 640) // 40ms @ 8kHz
    // ... 读取音频数据 ...
    
    SendAudioPacket(conn, "call_001", "10086", "10010", "CH1", audioData)
}
```

### Java 发送示例

```java
import java.io.*;
import java.net.*;

public class AudioSender {
    
    public static void sendAudioPacket(DatagramSocket socket, String callId, 
            String caller, String callee, String channel, byte[] audioData) 
            throws Exception {
        
        // 构建 JSON Header
        String json = String.format(
            "{\"call_id\":\"%s\",\"caller\":\"%s\",\"callee\":\"%s\",\"channel\":\"%s\"}",
            callId, caller, callee, channel
        );
        
        byte[] headerBytes = json.getBytes("UTF-8");
        
        // 打包 Header 长度 (4 bytes, little-endian)
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        DataOutputStream dos = new DataOutputStream(baos);
        dos.writeInt(headerBytes.length);  // 4 bytes
        
        // 添加 Header 和音频数据
        baos.write(headerBytes);
        baos.write(audioData);
        
        byte[] packet = baos.toByteArray();
        
        // 发送
        DatagramPacket dp = new DatagramPacket(
            packet, packet.length,
            InetAddress.getByName("127.0.0.1"), 8850
        );
        socket.send(dp);
    }
    
    public static void main(String[] args) throws Exception {
        DatagramSocket socket = new DatagramSocket();
        byte[] audioData = new byte[640]; // 40ms @ 8kHz
        
        sendAudioPacket(socket, "call_001", "10086", "10010", "CH1", audioData);
    }
}
```

## C/C++ 发送示例

```c
#include <stdio.h>
#include <string.h>
#include <arpa/inet.h>

#pragma pack(push, 1)
typedef struct {
    uint32_t call_id;
    uint32_t caller;
    uint32_t callee;
    uint32_t channel;  // 1=CH1, 2=CH2
} audio_header_t;
#pragma pack(pop)

int send_audio_packet(int sockfd, const char* call_id, const char* caller, 
                      const char* callee, int channel, const uint8_t* audio_data, int audio_len) {
    
    // 构建 JSON
    char json[256];
    int json_len = snprintf(json, sizeof(json), 
        "{\"call_id\":\"%s\",\"caller\":\"%s\",\"callee\":\"%s\",\"channel\":\"CH%d\"}",
        call_id, caller, callee, channel);
    
    // 分配缓冲区: 4 + json_len + audio_len
    uint8_t packet[4 + 256 + 4096];
    
    // 写入 Header 长度 (little-endian)
    uint32_t len = json_len;
    packet[0] = len & 0xFF;
    packet[1] = (len >> 8) & 0xFF;
    packet[2] = (len >> 16) & 0xFF;
    packet[3] = (len >> 24) & 0xFF;
    
    // 写入 JSON
    memcpy(packet + 4, json, json_len);
    
    // 写入音频数据
    memcpy(packet + 4 + json_len, audio_data, audio_len);
    
    // 发送
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(8850);
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
    
    int total_len = 4 + json_len + audio_len;
    return sendto(sockfd, packet, total_len, 0, (struct sockaddr*)&addr, sizeof(addr));
}
```

## 常见问题

### Q: 如何判断音频是 CH1 还是 CH2？

- **CH1**：员工/客服声道（主动发起呼叫的一方）
- **CH2**：客户/用户声道（接听的一方）

通常根据业务需求定义，确保发送端和本服务的定义一致即可。

### Q: 可以发送 16kHz 音频吗？

可以，但需要在 `config.py` 中将 `RESAMPLE_MODE` 设置为支持重采样，并在发送时确保音频格式正确。

### Q: 音频帧时长必须是 40ms 吗？

建议使用 40ms，这是较为理想的帧长。也可以使用其他帧长，但可能影响识别效果。

### Q: 如何处理多路通话？

每个通话使用不同的 `call_id`，服务会根据 `call_id` + `caller` + `callee` 自动路由到对应的会话。

### Q: 主叫/被叫号码可以是空吗？

可以，但建议填入有效号码以便追踪。空号码会显示为 `"Unknown"`，可能会影响路由。

## 性能优化建议

1. **批量发送**：可以一次性发送多个帧的数据，减少网络往返
2. **缓冲区复用**：避免频繁分配内存，复用发送缓冲区
3. **异步发送**：使用异步 I/O 提高发送效率
4. **监控丢包**：监控 UDP 丢包情况，确保音频质量
