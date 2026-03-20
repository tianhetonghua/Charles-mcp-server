import sys
import time
import json
import os
from typing import List, Dict, Optional, Any
from contextlib import asynccontextmanager
from mcp.server.fastmcp import FastMCP, Context
import CMS_tools

mcp = FastMCP("CharlesMCP", json_response=True)
ACTIVE_CACHE = {"task_id": None, "data": [], "last_update": 0}
SEARCH_SESSIONS = {} # 互锁授权表

@asynccontextmanager
async def lifespan(server: FastMCP):
    CMS_tools.copy_config()
    try: yield
    finally: await CMS_tools.reset_config()
mcp.lifespan = lifespan

async def get_data_for_analysis(task_id: str) -> List[Dict]:
    if ACTIVE_CACHE["task_id"] == task_id and ACTIVE_CACHE["data"]:
        return ACTIVE_CACHE["data"]
    disk_data = await CMS_tools.load_task_data_from_disk(task_id)
    ACTIVE_CACHE["task_id"], ACTIVE_CACHE["data"] = task_id, disk_data
    ACTIVE_CACHE["last_update"] = time.time()
    return disk_data

#======    【数据过滤接口】   ======
@mcp.tool()
async def harvest_data(task_id: str, ctx: Context) -> Dict:
    """【同步】更新流量并强制同步内存"""
    CMS_tools.manage_task_storage(task_id)
    raw = await CMS_tools.get_proxy_data_instant(ctx)
    CMS_tools.smart_merge(task_id)
    new_data = await CMS_tools.load_task_data_from_disk(task_id)
    ACTIVE_CACHE["task_id"], ACTIVE_CACHE["data"] = task_id, new_data
    ACTIVE_CACHE["last_update"] = time.time()
    return CMS_tools.wrap_response({"status": "Success", "captured": len(raw)}, task_id)

@mcp.tool()
async def check_keyword_exists(keyword: str, task_id: str) -> Dict:
    """【互锁-1】探测关键词位置并授权 filter_by_keyword"""
    data = await get_data_for_analysis(task_id)
    matches = [{"id": e.get("id"), "url": e.get("path"), "hit_at": CMS_tools.get_hit_locations(e, keyword)} 
               for e in data if CMS_tools.get_hit_locations(e, keyword)]
    # 建立 5 分钟授权
    SEARCH_SESSIONS[f"{keyword}_{task_id}"] = {"expire": time.time() + 300}
    return CMS_tools.wrap_response({"total": len(matches), "matches": matches[:50]}, task_id)

@mcp.tool()
async def filter_by_keyword(keyword: str, task_id: str, limit: int = 10) -> Dict:
    """【互锁-2】获取精简结果。匹配数 > 30 时必须先执行 check。"""
    session_key = f"{keyword}_{task_id}"
    is_auth = session_key in SEARCH_SESSIONS and SEARCH_SESSIONS[session_key]["expire"] > time.time()
    
    data = await get_data_for_analysis(task_id)
    k_lower = keyword.lower()
    filtered = [CMS_tools.simplify_entry(e) for e in data if k_lower in json.dumps(e, ensure_ascii=False).lower()]
    
    if len(filtered) > 30 and not is_auth:
        return CMS_tools.wrap_response({
            "error": "PRE_CHECK_REQUIRED", 
            "message": f"匹配到 {len(filtered)} 条，请先通过 check_keyword_exists 授权。"
        }, task_id)
        
    return CMS_tools.wrap_response({"results": filtered[-limit:]}, task_id)

@mcp.tool()
async def filter_by_path(path_keyword: str, task_id: str) -> Dict:
    """【空间过滤】通过路径快速锁定业务接口"""
    data = await get_data_for_analysis(task_id)
    results = [CMS_tools.simplify_entry(e) for e in data if path_keyword.lower() in (e.get("path") or "").lower()]
    return CMS_tools.wrap_response({"results": results[-15:]}, task_id)

@mcp.tool()
async def filter_by_status(status_code: int, task_id: str) -> Dict:
    """【状态过滤】通过状态码排查异常报文"""
    data = await get_data_for_analysis(task_id)
    results = [CMS_tools.simplify_entry(e) for e in data if e.get("response", {}).get("status") == status_code]
    return CMS_tools.wrap_response({"results": results[-15:]}, task_id)

@mcp.tool()
async def detect_encryption(entry_id: str, task_id: str) -> Dict:
    """【分析】对特定报文 Body 进行熵值判定"""
    data = await get_data_for_analysis(task_id)
    entry = next((e for e in data if str(e.get("id")) == str(entry_id)), None)
    if not entry: return CMS_tools.wrap_response({"error": "NOT_FOUND"}, task_id)
    req_ent = CMS_tools.calc_entropy(entry.get("request", {}).get("body", {}).get("text", "") or "")
    res_ent = CMS_tools.calc_entropy(entry.get("response", {}).get("body", {}).get("text", "") or "")
    return CMS_tools.wrap_response({
        "id": entry_id, "req_ent": round(req_ent, 2), "res_ent": round(res_ent, 2), 
        "is_suspicious": req_ent > 3.9 or res_ent > 3.9
    }, task_id)

@mcp.tool()
async def get_raw_data(entry_id: str, task_id: str) -> Dict:
    """【详细】获取 100% 原始数据"""
    data = await get_data_for_analysis(task_id)
    entry = next((e for e in data if str(e.get("id")) == str(entry_id)), None)
    return CMS_tools.wrap_response(entry or {"error": "NOT_FOUND"}, task_id)

#======    【弱网模拟接口】   ======
@mcp.tool()
async def get_throttle_presets() -> Dict:
    """获取 Charles 支持的所有弱网预设名称列表。"""
    return {"presets": CMS_tools.THROTTLE_PRESETS}
@mcp.tool()
async def set_throttling(preset: Optional[str], task_id: str, ctx: Context) -> Dict:
    """
    【环境控制】切换网络带宽限制。
    - preset: 传入预设名称（如 '4G'）开启弱网；传入 null 或不传则彻底关闭弱网模拟。
    """
    success = await CMS_tools.set_charles_throttling(preset, ctx)
    return CMS_tools.wrap_response({
        "action": "Activate" if preset else "Deactivate",
        "preset": preset or "None",
        "success": success
    }, task_id)

#入口
if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    mcp.run(transport="stdio")