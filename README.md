**# Charles MCP Server**



**这是一个基于 \[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 的服务器，允许 AI 直接操作 Charles Proxy 进行抓包、过滤流量和设置弱网环境。**



**## 功能特点**

**- 🚀 \*\*自动化抓包\*\*：通过 `proxy\_by\_time` 录制特定时长的流量。**

**- 🔍 \*\*智能搜索\*\*：支持正则表达式在流量包中定位关键字及其行号。**

**- 🌐 \*\*弱网模拟\*\*：一键切换 3G/4G/Fibre 等网络预设。**

**- 🛡️ \*\*安全隔离\*\*：退出时自动物理清空流量数据并还原 Charles 配置。**



**## 快速开始**



**### 前提条件**

**1. 安装 Python 3.10+**

**2. 安装并运行 \[Charles Proxy](https://www.charlesproxy.com/)**

**3. 在 Charles 中开启 Web Interface: `Proxy -> Web Interface Settings` (用户名: `tower`, 密码: `123456`)**



**### 安装**

**```bash**

**pip install -r requirements.txt**



mcp.json：

```json
{
  "mcpServers": {
    "charles": {
      "command": "python",
      "args": ["/绝对路径/到/charles_mcp_server.py"]
    }
  }
}
```

