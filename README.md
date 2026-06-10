# Charles MCP Server

**专为 AI agent 逆向分析设计的 Charles Proxy MCP 服务器。**

设计原则：MCP 只做数据通道，不替 agent 做分析判断。工具职责是取数据、过滤、呈现，推理交给 agent。

## 核心机制

### Checkpoint 时间线

每次调用 `harvest_data()` 都会：

1. 从 Charles 导出当前 session
2. 取上次收割后产生的新条目（按 `times.start` 毫秒时间戳增量判断）
3. 将新条目写入内存 **ARCHIVE**
4. 在时间线上记录一个 **checkpoint**
5. 清空 Charles session，重启录制（保持导出始终轻量）

agent 可通过 `load_checkpoint(n)` 随时回到任意历史时间窗口，所有过滤工具自动在那个范围内工作。

### 资源文件占位符

图片、JS、CSS、二进制等资源的 body 不传给 agent，替换为 `[image/png 45.2KB]` 形式的占位符。agent 知道类型和大小，按需调 `get_raw_data` 获取实际内容。

### 关键词互锁

`filter_by_keyword` 在匹配数 > 30 时要求先调 `check_keyword_exists`，避免一次性拉取大量 body 撑爆上下文。

---

## 工具总览

### 收割

| 工具 | 说明 |
| --- | --- |
| `harvest_data(fresh_start=False)` | 增量收割，创建 checkpoint。`fresh_start=True` 插入重置点，之后只看新流量 |

### 时间线

| 工具 | 说明 |
| --- | --- |
| `list_checkpoints()` | 查看所有 checkpoint，了解收割时间线 |
| `load_checkpoint(id)` | 切换到指定 checkpoint 的数据窗口 |

### 概览

| 工具 | 说明 |
| --- | --- |
| `summarize_traffic()` | 统计当前窗口的 host / path / 状态码 / 方法分布，不拉取 body |

### 过滤（均返回精简视图，含 body 预览）

| 工具 | 说明 |
| --- | --- |
| `filter_by_host(keyword)` | 按域名过滤，只匹配顶层 host 字段 |
| `filter_by_path(keyword)` | 按 URL 路径关键词过滤 |
| `filter_by_method(method)` | 按 HTTP 方法过滤（GET / POST 等）|
| `filter_by_status(code)` | 按 HTTP 状态码过滤 |
| `check_keyword_exists(keyword)` | 探测关键词位置，返回轻量索引，并解锁 filter_by_keyword |
| `filter_by_keyword(keyword)` | 返回含关键词的条目预览，匹配数 > 30 时需先调 check |
| `filter_by_encryption(threshold=3.9)` | 按香农熵扫描，找出疑似加密/压缩/编码的 body，按熵值降序 |

### 详情

| 工具 | 说明 |
| --- | --- |
| `get_raw_data(entry_id)` | 获取完整原始数据。先在当前窗口找，找不到从 ARCHIVE 全局搜 |

### 环境控制

| 工具 | 说明 |
| --- | --- |
| `set_throttling(preset)` | 开启 / 关闭弱网模拟，MCP 退出时自动还原 |

---

## 典型工作流

```
harvest_data()               # 同步流量，创建 checkpoint
summarize_traffic()          # 看全局：哪个 host 流量多，有没有大量 403
filter_by_host("api.xxx")    # 缩小到目标域名
filter_by_encryption()       # 找加密字段
get_raw_data(entry_id)       # 取完整报文深度分析
```

切换分析目标：

```
harvest_data(fresh_start=True)   # 标记重置点，不加载旧流量
# 在 App 触发目标操作
harvest_data()                   # 只拿新流量
```

回溯历史：

```
list_checkpoints()               # 查看时间线
load_checkpoint(2)               # 切到第 2 次收割的数据
filter_by_keyword("sign")        # 在那个时间窗口里搜
```

---

## 前置条件

Charles Proxy 已启动，开启 Web Interface：`Proxy → Web Interface Settings`

- 勾选 **Enable web interface**
- 用户名：`Charles-mcp-server`（或通过环境变量自定义）
- 密码：`123456`（或通过环境变量自定义）

---

## 安装

### 方式一：PyPI（推荐）

```shell
pip install charles-mcp-server
```

### 方式二：从源码安装

```shell
git clone <repo-url>
cd Charles-mcp-server
pip install .
```

### 方式三：让 AI 自动安装

将下方提示词完整粘贴给任意 AI agent（Claude Code、Cursor Agent、Codex 等），agent 会自动完成安装和配置：

<details>
<summary><strong>点击展开自动安装提示词</strong></summary>

```
请帮我安装并配置 "charles-mcp-server" MCP server，按以下步骤执行：

Step 1 — 检测操作系统：
  判断当前系统是 Windows、macOS 还是 Linux。

Step 2 — 安装 charles-mcp-server：
  运行：pip install charles-mcp-server
  如果失败，尝试：pip3 install charles-mcp-server
  安装完成后验证：charles-mcp-server --help（有输出即成功）

Step 3 — 检测 MCP 客户端（按顺序匹配，用第一个命中的）：

  a) Claude Code — 运行：claude --version
     成功则执行以下命令，然后跳到 Step 5：
       claude mcp add-json charles '{"type":"stdio","command":"charles-mcp-server","args":[]}'

  b) Claude Desktop — 检查配置文件是否存在：
     - macOS：  ~/Library/Application Support/Claude/claude_desktop_config.json
     - Windows：%APPDATA%\Claude\claude_desktop_config.json
     - Linux：  ~/.config/Claude/claude_desktop_config.json

  c) Cursor — 检查以下路径是否存在：
     - ~/.cursor/mcp.json
     - .cursor/mcp.json（当前项目目录下）

  d) Windsurf — 检查：~/.codeium/windsurf/mcp_config.json

  e) Codex CLI — 检查：~/.codex/config.toml

  f) Gemini CLI — 检查：~/.gemini/settings.json

  如果以上均未找到，询问用户使用的是哪个客户端。

Step 4 — 写入配置（适用于 b/c/d/f 的 JSON 格式客户端）：
  要添加的配置项为：
    "charles": {
      "command": "charles-mcp-server",
      "args": []
    }

  重要：如果配置文件已存在，先读取再解析 JSON，
  在 "mcpServers" 内追加 "charles" 键（若无 "mcpServers" 则创建），
  不要覆盖已有的其他 MCP server 配置。
  如果文件不存在，创建并写入：
    { "mcpServers": { "charles": { "command": "charles-mcp-server", "args": [] } } }

  适用于 e（Codex CLI TOML 格式）：
  在 ~/.codex/config.toml 中追加：
    [mcp_servers.charles]
    command = "charles-mcp-server"
    args    = []

Step 5 — 验证：
  运行：charles-mcp-server
  等待 2 秒后终止进程。
  没有 import 报错则安装成功。

Step 6 — 报告结果：
  输出："charles-mcp-server 安装完成，请重启 MCP 客户端以加载新服务。"
  并提示：Charles Proxy 需处于运行状态，且已在 Proxy → Web Interface Settings 中
  启用 Web Interface（用户名：Charles-mcp-server，密码：123456）。
```

</details>

---

## 各客户端配置

> 所有客户端使用相同的 MCP server，只是配置文件路径和格式不同。

### Claude Code（CC）

```bash
claude mcp add-json charles '{
  "type": "stdio",
  "command": "charles-mcp-server",
  "args": []
}'
```

如需自定义 Charles 账号：

```bash
claude mcp add-json charles '{
  "type": "stdio",
  "command": "charles-mcp-server",
  "args": [],
  "env": {
    "CHARLES_USER": "your-username",
    "CHARLES_PASS": "your-password"
  }
}'
```

### Claude Desktop

配置文件路径：
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "charles": {
      "command": "charles-mcp-server",
      "args": []
    }
  }
}
```

### Cursor

配置文件路径（二选一）：
- 全局：`~/.cursor/mcp.json`
- 项目级：`.cursor/mcp.json`（只对当前项目生效）

```json
{
  "mcpServers": {
    "charles": {
      "command": "charles-mcp-server",
      "args": []
    }
  }
}
```

### Windsurf

配置文件路径：`~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "charles": {
      "command": "charles-mcp-server",
      "args": []
    }
  }
}
```

### Codex CLI

配置文件路径：`~/.codex/config.toml`

```toml
[mcp_servers.charles]
command = "charles-mcp-server"
args    = []
```

自定义账号：

```toml
[mcp_servers.charles]
command = "charles-mcp-server"
args    = []

[mcp_servers.charles.env]
CHARLES_USER = "your-username"
CHARLES_PASS = "your-password"
```

### Gemini CLI

配置文件路径：`~/.gemini/settings.json`

```json
{
  "mcpServers": {
    "charles": {
      "command": "charles-mcp-server",
      "args": []
    }
  }
}
```

### pyenv 环境（通用）

如果通过 pyenv 管理 Python 环境，将上述所有配置的 `"command"` 替换为对应 Python 的绝对路径，并改用 `-m` 方式启动：

```json
{
  "mcpServers": {
    "charles": {
      "command": "/path/to/pyenv/versions/3.11.x/bin/python",
      "args": ["-m", "charles_mcp_server.main"]
    }
  }
}
```

---

## 环境变量

所有客户端均支持通过环境变量覆盖默认配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CHARLES_USER` | `Charles-mcp-server` | Charles Web Interface 用户名 |
| `CHARLES_PASS` | `123456` | Charles Web Interface 密码 |
| `CHARLES_PROXY_HOST` | `127.0.0.1` | Charles 代理地址 |
| `CHARLES_PROXY_PORT` | `8888` | Charles 代理端口 |

---

## License

MIT License — Owner: **tianhetonghua**
