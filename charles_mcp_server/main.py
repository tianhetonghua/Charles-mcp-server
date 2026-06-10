import os
import sys
import time
import json
from typing import Dict, List, Optional
from contextlib import asynccontextmanager
from datetime import datetime
from mcp.server.fastmcp import FastMCP, Context

try:
    from . import CMS_tools
except ImportError:
    import CMS_tools

mcp = FastMCP("CharlesMCP", json_response=True)

# ---------- 运行时状态 ----------

# ARCHIVE          live 条目，int(id) → entry
# ARCHIVE_REC      录包条目，"rec:filename:id" → entry（避免与 live id 冲突）
# CHECKPOINTS      时间线表，harvest_data / load_recording 均追加一条
# MAX_SEEN_MILLIS  已处理过的最大 times.start 毫秒时间戳，用于 live 增量判断
# CACHE            当前 agent 的可见窗口，指向某个 checkpoint 的数据切片
ARCHIVE:         Dict[int, Dict] = {}
ARCHIVE_REC:     Dict[str, Dict] = {}
CHECKPOINTS:     List[Dict]      = []
MAX_SEEN_MILLIS: int              = 0
CACHE: Dict = {
    "data":          [],    # 当前可见条目
    "checkpoint_id": None,  # 正在查看的 checkpoint（None = 未收割）
    "harvested_at":  0.0,   # 最近一次 harvest_data 的时间戳
    "total_in_session": 0,
}
KEYWORD_AUTH: Dict[str, float] = {}

# ---------- Lifespan ----------

@asynccontextmanager
async def lifespan(server: FastMCP):
    try:
        yield
    finally:
        CMS_tools.deactivate_throttling_silent()

mcp.lifespan = lifespan

# ---------- 内部工具 ----------

def _parse_millis(iso_str: str) -> int:
    """将 Charles times.start（ISO 8601）转为毫秒时间戳。"""
    if not iso_str:
        return 0
    try:
        return int(datetime.fromisoformat(iso_str).timestamp() * 1000)
    except (ValueError, OSError):
        return 0

def _entry_start_time(entry: Dict) -> str:
    """取 entry 的请求开始时间字符串，用于 checkpoint 记录。"""
    return (entry.get("times") or {}).get("start") or ""

def _get_archived(key) -> Optional[Dict]:
    """统一查 ARCHIVE（live）和 ARCHIVE_REC（录包），找不到返回 None。"""
    if isinstance(key, int):
        return ARCHIVE.get(key)
    if isinstance(key, str):
        if key.startswith("rec:"):
            return ARCHIVE_REC.get(key)
        try:
            return ARCHIVE.get(int(key))
        except (ValueError, TypeError):
            return ARCHIVE_REC.get(key)
    return None

def _data_hint() -> Optional[str]:
    """无数据或最新 checkpoint 明显陈旧时返回提示，回溯历史时不打扰。"""
    if not CHECKPOINTS:
        return "还没有数据，请先调用 harvest_data()。"
    # 只在查看最新 checkpoint 时检查陈旧
    if CACHE["checkpoint_id"] == len(CHECKPOINTS):
        age = time.time() - CACHE["harvested_at"]
        if age > 180:
            return f"数据已 {int(age / 60)} 分钟未更新，如需最新流量可调用 harvest_data()。"
    return None

# ====================================================================
#  数据收割
# ====================================================================

@mcp.tool()
async def harvest_data(fresh_start: bool = False) -> Dict:
    """从 Charles 同步增量流量，并在时间线上创建一个新 checkpoint。

    【fresh_start=False】（默认）
      取上次 harvest 之后产生的新条目，写入 ARCHIVE，创建 checkpoint。
      首次调用时加载 session 全部流量。

    【fresh_start=True】
      在时间线上插一个"重置点"，不加载任何条目。
      之后的 harvest_data() 只返回此刻之后的新流量。
      典型用法：切换分析目标前调用一次。

    每次调用都会：
      - 将新条目存入 ARCHIVE（可通过 load_checkpoint 随时回溯）
      - 将 CACHE 切换到本次新增的条目
      - 清空 Charles session 并重启录制（导出始终只含最新增量）
      - 使所有关键词授权失效
    """
    global MAX_SEEN_MILLIS

    all_entries = CMS_tools.export_session()
    now_str     = datetime.now().strftime("%H:%M:%S")

    # 取当前 session 中最大的请求时间戳，用于推进水位线
    cur_max_millis = max((_entry_millis(e) for e in all_entries), default=MAX_SEEN_MILLIS)

    if fresh_start:
        # 推进水位线但不加载任何条目
        MAX_SEEN_MILLIS = cur_max_millis
        new_entries: list = []
    else:
        # 时间戳 > 水位线的都是新条目；millis=0 表示时间缺失，只要 id 没见过就收
        # ARCHIVE 去重兜底同一毫秒内的多条请求
        new_entries = [
            e for e in all_entries
            if (_entry_millis(e) > MAX_SEEN_MILLIS or _entry_millis(e) == 0)
            and e.get("id") not in ARCHIVE
        ]
        MAX_SEEN_MILLIS = cur_max_millis
        for e in new_entries:
            ARCHIVE[e["id"]] = e

    # 数据已安全写入 ARCHIVE，现在清空 Charles session
    # Charles 导出始终只包含本次之后的增量，保持小而快
    cleared = CMS_tools.clear_and_restart()

    # 创建 checkpoint 记录
    cp: Dict = {
        "id":         len(CHECKPOINTS) + 1,
        "source":     "live",
        "read_at":    now_str,
        "count":      len(new_entries),
        "entry_ids":  [e["id"] for e in new_entries],
        "is_reset":   fresh_start,
        "start_time": _entry_start_time(new_entries[0])  if new_entries else None,
        "end_time":   _entry_start_time(new_entries[-1]) if new_entries else None,
    }
    CHECKPOINTS.append(cp)

    KEYWORD_AUTH.clear()
    CACHE["data"]             = new_entries
    CACHE["checkpoint_id"]    = cp["id"]
    CACHE["harvested_at"]     = time.time()
    CACHE["total_in_session"] = len(all_entries)

    result: Dict = {
        "checkpoint_id":    cp["id"],
        "fresh_start":      fresh_start,
        "new_entries":      len(new_entries),
        "total_archived":   len(ARCHIVE),
        "total_in_session": len(all_entries),
        "charles_cleared":  cleared, # session 已清空，下次导出从零开始
    }
    if fresh_start:
        result["hint"] = "重置点已记录。在 App 触发目标操作后，再次调用 harvest_data() 即可看到新流量。"
    return result

# ====================================================================
#  Checkpoint 时间线
# ====================================================================

@mcp.tool()
async def list_checkpoints() -> Dict:
    """列出所有 checkpoint，展示完整的收割时间线。

    每条记录包含：
      id          checkpoint 序号
      read_at     收割时间（HH:MM:SS）
      count       本次新增条目数
      start_time  本批流量的最早请求时间
      end_time    本批流量的最晚请求时间
      is_reset    是否为 fresh_start 重置点

    当前正在查看的 checkpoint 由 current_checkpoint_id 标注。
    """
    summary = [
        {k: v for k, v in cp.items() if k != "entry_ids"}
        for cp in CHECKPOINTS
    ]
    return {
        "total":                len(CHECKPOINTS),
        "current_checkpoint_id": CACHE["checkpoint_id"],
        "total_archived":       len(ARCHIVE),
        "checkpoints":          summary,
    }

@mcp.tool()
async def load_checkpoint(checkpoint_id: int) -> Dict:
    """将 CACHE 切换到指定 checkpoint 的数据，所有过滤工具随即在该时间窗口内工作。

    这是只读回溯——ARCHIVE 和 CHECKPOINTS 不会被修改。
    调用 harvest_data() 可随时切回最新增量。

    checkpoint_id：来自 list_checkpoints 返回的 id 字段。
    """
    if not CHECKPOINTS:
        return {"error": "NO_CHECKPOINTS", "message": "还没有任何 checkpoint，请先调用 harvest_data()。"}

    if checkpoint_id < 1 or checkpoint_id > len(CHECKPOINTS):
        return {
            "error":   "INVALID_ID",
            "message": f"checkpoint_id 须在 1~{len(CHECKPOINTS)} 之间。",
        }

    cp = CHECKPOINTS[checkpoint_id - 1]

    if cp.get("is_reset"):
        entries = []
    else:
        entries = [e for eid in cp["entry_ids"] if (e := _get_archived(eid)) is not None]

    KEYWORD_AUTH.clear()
    CACHE["data"]          = entries
    CACHE["checkpoint_id"] = checkpoint_id

    return {
        "checkpoint_id": checkpoint_id,
        "read_at":       cp["read_at"],
        "loaded":        len(entries),
        "start_time":    cp.get("start_time"),
        "end_time":      cp.get("end_time"),
        "is_reset":      cp.get("is_reset", False),
        "hint":          "已切换到该时间窗口，所有过滤工具在此范围内生效。调用 harvest_data() 可切回最新数据。",
    }

@mcp.tool()
async def load_recording(file_path: str) -> Dict:
    """加载本地 .chlsj 历史录包到 ARCHIVE，创建 checkpoint 并切换到该窗口。

    录包与 live 流量共用所有过滤工具（filter_by_*、filter_by_encryption 等）。
    录包条目用 "rec:文件名:id" 命名空间存储，不会与 live 条目的 id 冲突。
    加载后调用 list_checkpoints 可在时间线上看到录包的真实抓包时间范围。

    file_path：.chlsj 文件的绝对路径或相对路径。
    """
    if not os.path.exists(file_path):
        return {"error": "FILE_NOT_FOUND", "file_path": file_path}

    fname = os.path.basename(file_path)
    try:
        entries = CMS_tools.read_chlsj(file_path)
    except Exception as exc:
        return {"error": "READ_FAILED", "message": str(exc)}

    # 注入 _mcp_id：让 simplify_entry / get_raw_data 能用命名空间 key 定位条目
    # chlsj 文件的 entry 没有 id 字段，_mcp_id 是唯一可用的标识符
    rec_ids: List = []
    for i, e in enumerate(entries):
        raw_id = e.get("id") if e.get("id") is not None else i
        key = f"rec:{fname}:{raw_id}"
        e["_mcp_id"] = key          # 注入到 entry 本身，simplify_entry 会读取它
        ARCHIVE_REC[key] = e
        rec_ids.append(key)

    cp: Dict = {
        "id":         len(CHECKPOINTS) + 1,
        "source":     f"recording:{fname}",
        "read_at":    datetime.now().strftime("%H:%M:%S"),
        "count":      len(entries),
        "entry_ids":  rec_ids,
        "is_reset":   False,
        "start_time": _entry_start_time(entries[0])  if entries else None,
        "end_time":   _entry_start_time(entries[-1]) if entries else None,
    }
    CHECKPOINTS.append(cp)

    KEYWORD_AUTH.clear()
    CACHE["data"]          = entries
    CACHE["checkpoint_id"] = cp["id"]

    return {
        "checkpoint_id":  cp["id"],
        "file":           fname,
        "loaded":         len(entries),
        "start_time":     cp["start_time"],
        "end_time":       cp["end_time"],
        "total_archived": len(ARCHIVE) + len(ARCHIVE_REC),
    }

# ====================================================================
#  概览
# ====================================================================

@mcp.tool()
async def summarize_traffic() -> Dict:
    """对当前 checkpoint 的流量做全局统计，帮助 agent 快速定向。

    不拉取任何 body，只统计路由维度：
      top_hosts    流量最多的域名
      top_paths    最活跃的路径前缀（取前两段）
      status_dist  HTTP 状态码分布
      method_dist  HTTP 方法分布
    """
    if hint := _data_hint():
        return {"warn": hint, "total": 0}

    data = CACHE["data"]
    host_count:   Dict[str, int] = {}
    path_count:   Dict[str, int] = {}
    status_count: Dict[str, int] = {}
    method_count: Dict[str, int] = {}

    for e in data:
        host   = e.get("host")   or "unknown"
        path   = e.get("path")   or "/"
        method = e.get("method") or "unknown"
        status = str((e.get("response") or {}).get("status") or "unknown")

        parts    = [p for p in path.split("/") if p]
        path_key = "/" + "/".join(parts[:2]) if parts else "/"

        host_count[host]     = host_count.get(host, 0)     + 1
        path_count[path_key] = path_count.get(path_key, 0) + 1
        status_count[status] = status_count.get(status, 0) + 1
        method_count[method] = method_count.get(method, 0) + 1

    def top(d: Dict[str, int], n: int = 10) -> list:
        return [{"key": k, "count": v}
                for k, v in sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]]

    return {
        "checkpoint_id": CACHE["checkpoint_id"],
        "total":         len(data),
        "top_hosts":     top(host_count),
        "top_paths":     top(path_count),
        "status_dist":   top(status_count),
        "method_dist":   top(method_count),
    }

# ====================================================================
#  探测与过滤
# ====================================================================

@mcp.tool()
async def check_keyword_exists(keyword: str) -> Dict:
    """【互锁-1】探测关键词在当前 checkpoint 的哪些条目中出现。

    只返回 id + 命中位置的轻量索引，不返回 body 内容。
    调用后 filter_by_keyword 对该关键词的批量限制解除（5 分钟内有效）。
    """
    if hint := _data_hint():
        return {"warn": hint, "total": 0, "matches": []}

    matches = []
    for e in CACHE["data"]:
        hits = CMS_tools.get_hit_locations(e, keyword)
        if hits:
            matches.append({"id": e.get("id"), "path": e.get("path"), "hit_at": hits})

    KEYWORD_AUTH[keyword.lower()] = time.time() + 300

    return {"total": len(matches), "matches": matches[:50]}

@mcp.tool()
async def filter_by_keyword(keyword: str, limit: int = 10) -> Dict:
    """【互锁-2】返回含关键词的条目精简视图（含 body 预览）。

    匹配数 > 30 时须先调用 check_keyword_exists 解锁，防止上下文溢出。
    limit 默认 10，上限 50，返回最近 N 条。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    k         = keyword.lower()
    is_authed = k in KEYWORD_AUTH and KEYWORD_AUTH[k] > time.time()
    filtered  = [
        CMS_tools.simplify_entry(e)
        for e in CACHE["data"]
        if k in json.dumps(e, ensure_ascii=False).lower()
    ]

    if len(filtered) > 30 and not is_authed:
        return {
            "error":   "PRE_CHECK_REQUIRED",
            "message": f"匹配 {len(filtered)} 条，请先调用 check_keyword_exists('{keyword}') 后再试。",
        }

    limit = min(limit, 50)
    return {
        "results":       filtered[-limit:],
        "total_matched": len(filtered),
        "returned":      min(len(filtered), limit),
    }

@mcp.tool()
async def filter_by_path(path_keyword: str, limit: int = 15) -> Dict:
    """按 URL 路径关键词过滤，返回精简视图。

    适合快速定位某个业务接口，例如 '/api/sign'、'/login'。
    limit 默认 15，上限 50，返回最近 N 条。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    k       = path_keyword.lower()
    results = [
        CMS_tools.simplify_entry(e)
        for e in CACHE["data"]
        if k in (e.get("path") or "").lower()
    ]
    limit = min(limit, 50)
    return {
        "results":       results[-limit:],
        "total_matched": len(results),
        "returned":      min(len(results), limit),
    }

@mcp.tool()
async def filter_by_host(host_keyword: str, limit: int = 15) -> Dict:
    """按 host 字段过滤，返回精简视图。

    只匹配 entry 顶层的 host 字段，不会误命中 body 或 headers 中出现的域名。
    支持部分匹配，例如 'example.com' 可匹配所有子域名。
    limit 默认 15，上限 50，返回最近 N 条。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    k       = host_keyword.lower()
    results = [
        CMS_tools.simplify_entry(e)
        for e in CACHE["data"]
        if k in (e.get("host") or "").lower()
    ]
    limit = min(limit, 50)
    return {
        "results":       results[-limit:],
        "total_matched": len(results),
        "returned":      min(len(results), limit),
    }

@mcp.tool()
async def filter_by_status(status_code: int, limit: int = 15) -> Dict:
    """按 HTTP 状态码过滤，返回精简视图。

    适合排查异常请求，例如 403（鉴权失败）、500（服务错误）。
    limit 默认 15，上限 50，返回最近 N 条。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    results = [
        CMS_tools.simplify_entry(e)
        for e in CACHE["data"]
        if (e.get("response") or {}).get("status") == status_code
    ]
    limit = min(limit, 50)
    return {
        "results":       results[-limit:],
        "total_matched": len(results),
        "returned":      min(len(results), limit),
    }

@mcp.tool()
async def filter_by_method(method: str, limit: int = 15) -> Dict:
    """按 HTTP 方法过滤（GET / POST / PUT / DELETE 等），返回精简视图。

    大小写不敏感。limit 默认 15，上限 50，返回最近 N 条。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    m       = method.upper()
    results = [
        CMS_tools.simplify_entry(e)
        for e in CACHE["data"]
        if (e.get("method") or "").upper() == m
    ]
    limit = min(limit, 50)
    return {
        "results":       results[-limit:],
        "total_matched": len(results),
        "returned":      min(len(results), limit),
    }

# ====================================================================
#  深度分析
# ====================================================================

@mcp.tool()
async def filter_by_encryption(threshold: float = 3.9, limit: int = 20) -> Dict:
    """扫描当前 checkpoint 的所有条目，找出 body 疑似加密/压缩/编码的请求。

    按 max(req_entropy, res_entropy) 降序排列。
    threshold 默认 3.9：纯文本 JSON 通常 2.5~3.5，Base64 约 4.0~5.0，加密 > 5.0。
    limit 默认 20，上限 100。
    """
    if hint := _data_hint():
        return {"warn": hint, "results": [], "total_matched": 0}

    hits = []
    for e in CACHE["data"]:
        req     = e.get("request") or {}
        res     = e.get("response") or {}
        req_txt = (req.get("body") or {}).get("text") or ""
        res_txt = (res.get("body") or {}).get("text") or ""
        req_ent = CMS_tools.calc_entropy(req_txt)
        res_ent = CMS_tools.calc_entropy(res_txt)
        max_ent = max(req_ent, res_ent)
        if max_ent > threshold:
            row = CMS_tools.simplify_entry(e)
            row["req_entropy"]  = round(req_ent, 2)
            row["res_entropy"]  = round(res_ent, 2)
            row["req_body_len"] = len(req_txt)
            row["res_body_len"] = len(res_txt)
            hits.append((max_ent, row))

    hits.sort(key=lambda x: x[0], reverse=True)
    results = [row for _, row in hits]
    limit   = min(limit, 100)
    return {
        "results":       results[:limit],
        "total_matched": len(results),
        "returned":      min(len(results), limit),
        "threshold":     threshold,
    }

@mcp.tool()
async def get_raw_data(entry_id: str) -> Dict:
    """获取指定条目的完整原始数据（headers、body、timing 等全部字段）。

    优先在当前 checkpoint 查找，找不到时从 ARCHIVE 全局搜索。
    这样即使切换了 checkpoint，仍可用 entry_id 直接取历史数据。
    """
    # 先在当前窗口找（认 _mcp_id 或 id，兼容 live 和录包条目）
    entry = next(
        (e for e in CACHE["data"]
         if str(e.get("_mcp_id") or e.get("id")) == str(entry_id)),
        None,
    )
    # 再从全局 ARCHIVE / ARCHIVE_REC 找
    if entry is None:
        entry = _get_archived(entry_id)

    if entry is None:
        return {"error": "NOT_FOUND", "entry_id": entry_id}
    return {"entry": entry}

# ====================================================================
#  弱网控制
# ====================================================================

@mcp.tool()
async def set_throttling(preset: Optional[str], ctx: Context) -> Dict:
    """切换网络带宽限制。

    - preset 为预设名称 → 开启弱网；preset 为 null → 关闭弱网模拟。
    - MCP 进程退出时自动关闭弱网，无需手动清理。

    可用预设（直接填名称字符串）：
      56 kbps Modem | 256 kbps ISDN/DSL | 512 kbps ISDN/DSL
      2 Mbps ADSL   | 8 Mbps ADSL2      | 16 Mbps ADSL2+
      32 Mbps VDSL  | 32 Mbps Fibre     | 100 Mbps Fibre
      3G | 4G
    """
    success = await CMS_tools.set_charles_throttling(preset, ctx)
    return {"action": "activate" if preset else "deactivate", "preset": preset, "success": success}

# ====================================================================
#  入口
# ====================================================================

def main():
    if sys.platform == "win32":
        import io
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
