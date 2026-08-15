import os
import json
import math
import requests
from collections import Counter
from typing import Optional, List, Dict, Any
from requests.auth import HTTPBasicAuth
from mcp.server.fastmcp import Context

# --- 配置（支持环境变量覆盖）---
CHARLES_USER       = os.getenv("CHARLES_USER",       "Charles-mcp-server")
CHARLES_PASS       = os.getenv("CHARLES_PASS",       "123456")
CHARLES_PROXY_HOST = os.getenv("CHARLES_PROXY_HOST", "127.0.0.1")
CHARLES_PROXY_PORT = os.getenv("CHARLES_PROXY_PORT", "8888")

AUTH    = HTTPBasicAuth(CHARLES_USER, CHARLES_PASS)
PROXIES = {"http": f"http://{CHARLES_PROXY_HOST}:{CHARLES_PROXY_PORT}"}
CHARLES_BASE_URL = "http://control.charles"

THROTTLE_PRESETS: List[str] = [
    "56 kbps Modem", "256 kbps ISDN/DSL", "512 kbps ISDN/DSL",
    "2 Mbps ADSL",   "8 Mbps ADSL2",      "16 Mbps ADSL2+",
    "32 Mbps VDSL",  "32 Mbps Fibre",     "100 Mbps Fibre",
    "3G", "4G",
]

# ======  Charles 控制接口  ======

def _get(path: str, *, timeout: int = 10, **kwargs) -> requests.Response:
    """向 Charles 控制接口发 GET 请求。"""
    return requests.get(
        f"{CHARLES_BASE_URL}{path}",
        auth=AUTH, proxies=PROXIES, timeout=timeout,
        **kwargs,
    )

def read_chlsj(file_path: str) -> List[Dict]:
    """读取本地 .chlsj 历史录包，返回 entry 列表。

    .chlsj 与 /session/export-json 格式相同，所有过滤/分析工具可直接使用。
    文件不存在或格式错误时抛出异常（由调用方处理并返回错误信息）。
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"不是有效的 .chlsj 文件（期望 list，实际 {type(data).__name__}）")
    return data

def export_session() -> Dict[str, Any]:
    """只读导出当前 Charles session，并返回可供 MCP 呈现的结构化结果。"""
    try:
        resp = _get("/session/export-json", timeout=15)
        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "error": "AUTH_FAILED",
                "message": "Charles Web Interface 拒绝认证，请检查 CHARLES_USER 与 CHARLES_PASS。",
            }
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return {
                "ok": False,
                "error": "INVALID_EXPORT_FORMAT",
                "message": "Charles 导出结果不是预期的 JSON 数组。",
            }
        return {"ok": True, "entries": data}
    except requests.Timeout:
        return {
            "ok": False,
            "error": "EXPORT_TIMEOUT",
            "message": "等待 Charles 导出超时，请确认 Charles 正在运行且 Web Interface 已启用。",
        }
    except requests.ConnectionError:
        return {
            "ok": False,
            "error": "CHARLES_UNREACHABLE",
            "message": "无法连接 Charles。请确认代理地址/端口及 Web Interface 设置。",
        }
    except requests.RequestException as exc:
        return {"ok": False, "error": "EXPORT_FAILED", "message": str(exc)}
    except ValueError:
        return {
            "ok": False,
            "error": "INVALID_EXPORT_FORMAT",
            "message": "Charles 导出结果无法解析为 JSON。",
        }

def clear_and_restart() -> Dict[str, Any]:
    """显式清空 Charles session 并恢复录制，分别报告两个操作的结果。"""
    result: Dict[str, Any] = {"cleared": False, "recording_started": False}
    try:
        clear_response = _get("/session/clear", timeout=5)
        clear_response.raise_for_status()
        result["cleared"] = True
    except requests.RequestException as exc:
        result["clear_error"] = str(exc)
    try:
        recording_response = _get("/recording/start", timeout=5)
        recording_response.raise_for_status()
        result["recording_started"] = True
    except requests.RequestException as exc:
        result["recording_error"] = str(exc)
    result["ok"] = result["cleared"] and result["recording_started"]
    return result

def deactivate_throttling_silent() -> None:
    """静默关闭弱网，进程退出时调用，不抛异常。"""
    try:
        _get("/throttling/deactivate", timeout=3)
    except Exception:
        pass

async def set_charles_throttling(preset_name: Optional[str], ctx: Context) -> bool:
    """激活或关闭 Charles 弱网模拟。"""
    try:
        if not preset_name:
            _get("/throttling/deactivate", timeout=5)
            await ctx.info("🌐 弱网模拟已关闭")
            return True
        if preset_name not in THROTTLE_PRESETS:
            await ctx.error(
                f"无效预设: '{preset_name}'，可用: {', '.join(THROTTLE_PRESETS)}"
            )
            return False
        _get("/throttling/activate", timeout=5, params={"preset": preset_name})
        await ctx.info(f"🌐 弱网已激活: {preset_name}")
        return True
    except Exception as e:
        await ctx.error(f"Throttling 操作失败: {e}")
        return False

# ======  数据处理工具  ======

def calc_entropy(s: str) -> float:
    """计算字符串的香农信息熵（比特/字符）。

    用 Counter 一次遍历统计所有字符频率，O(n)，
    比逐字符 str.count 的 O(n×k) 快一个量级。
    """
    if not s or len(s) < 5:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())

def get_hit_locations(entry: Dict, keyword: str) -> List[str]:
    """返回关键词在 entry 中命中的位置标签列表。

    Charles JSON 格式：headers 嵌套在 request.header.headers，
    而非 request.headers。
    """
    k   = keyword.lower()
    req = entry.get("request") or {}
    res = entry.get("response") or {}
    hits: List[str] = []

    if k in (entry.get("path") or "").lower():
        hits.append("url_path")
    # headers 实际路径：request.header.headers（二层嵌套）
    req_headers = (req.get("header") or {}).get("headers") or []
    if k in json.dumps(req_headers).lower():
        hits.append("req_header")
    if k in ((req.get("body") or {}).get("text") or "").lower():
        hits.append("req_body")
    res_headers = (res.get("header") or {}).get("headers") or []
    if k in json.dumps(res_headers).lower():
        hits.append("res_header")
    if k in ((res.get("body") or {}).get("text") or "").lower():
        hits.append("res_body")

    return hits

# 这些 mimeType 前缀的 body 对 agent 没有可读价值，用占位符替代
_RESOURCE_MIME_PREFIXES = (
    "image/", "video/", "audio/", "font/",
    "application/javascript", "text/javascript",
    "text/css", "application/octet-stream", "application/wasm",
)

def _fmt_size(n: int) -> str:
    """将字节数格式化为可读字符串。"""
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n}B"

def _body_preview(body: Dict, mime_type: str, body_size: int, preview_chars: int) -> Optional[str]:
    """返回 body 预览字符串，或对资源文件返回占位符。

    二进制/资源内容（图片、JS、CSS 等）返回 "[image/png 45.2KB]" 形式的占位符，
    agent 由此得知类型和大小，不消耗 token 在无法阅读的 base64 上。
    只有 agent 明确需要时再调 get_raw_data 获取实际内容。
    """
    if not body:
        return None

    encoded  = body.get("encoded", False)
    text     = body.get("text") or ""
    mime     = (mime_type or "").lower().split(";")[0].strip()

    is_resource = encoded or any(mime.startswith(p) for p in _RESOURCE_MIME_PREFIXES)

    if is_resource:
        # body_size 来自 Charles 记录的实际字节数；fallback 用 text 长度
        size  = body_size or len(text)
        label = mime or "binary"
        return f"[{label} {_fmt_size(size)}]"

    return text[:preview_chars] or None

def simplify_entry(entry: Dict, preview_chars: int = 300) -> Dict:
    """将完整 entry 压缩为供 agent 决策用的精简视图。

    - 普通 API 响应：返回 body 前 N 字符预览，agent 据此决定是否调 get_raw_data。
    - 资源文件（图片/JS/CSS/二进制）：返回 "[image/png 45.2KB]" 占位符，
      agent 知道类型和大小即可，不浪费 token 在不可读内容上。
    """
    req      = entry.get("request") or {}
    res      = entry.get("response") or {}
    req_body = req.get("body") or {}
    res_body = res.get("body") or {}

    return {
        "id":          entry.get("_mcp_id") or entry.get("id"),
        "method":      entry.get("method"),
        "host":        entry.get("host"),
        "path":        entry.get("path"),
        "status":      res.get("status"),
        "req_preview": _body_preview(
            req_body,
            mime_type  = req.get("mimeType") or "",
            body_size  = (req.get("sizes") or {}).get("body") or 0,
            preview_chars = preview_chars,
        ),
        "res_preview": _body_preview(
            res_body,
            mime_type  = res.get("mimeType") or "",
            body_size  = (res.get("sizes") or {}).get("body") or 0,
            preview_chars = preview_chars,
        ),
    }
