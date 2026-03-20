import os
import sys
import time
import json
import shutil
import requests
import math
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from requests.auth import HTTPBasicAuth
from mcp.server.fastmcp import Context

# --- 环境初始化 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

AUTH = HTTPBasicAuth('tower', '123456')
PROXIES = {"http": "http://127.0.0.1:8888"}
CHARLES_BASE_URL = "http://control.charles"
PACKAGE_DIR = os.path.join(BASE_DIR, "package")
BACKUP_DIR = os.path.join(BASE_DIR, "back")
STATE_FILE = os.path.join(BASE_DIR, "task_state.json")

#======    【弱网模拟工具】   ======
# --- 4. 弱网模拟预设 (对接 Charles 物理接口) ---
THROTTLE_PRESETS = [
    "56 kbps Modem", "256 kbps ISDN/DSL", "512 kbps ISDN/DSL",
    "2 Mbps ADSL", "8 Mbps ADSL2", "16 Mbps ADSL2+",
    "32 Mbps VDSL", "32 Mbps Fibre", "100 Mbps Fibre",
    "3G", "4G"
]
async def set_charles_throttling(preset_name: Optional[str], ctx: Context) -> bool:
    """
    调用 Charles 物理接口切换弱网环境。
    - 若 preset_name 为 None，则执行全局关闭 (deactivate)。
    - 若指定 preset_name，则执行激活 (activate)。
    """
    params = {"auth": AUTH, "proxies": PROXIES, "timeout": 5}
    try:
        if not preset_name:
            # 执行全局关闭
            requests.get(f"{CHARLES_BASE_URL}/throttling/deactivate", **params)
            await ctx.info("🌐 弱网模拟已全局关闭")
            return True
        
        if preset_name not in THROTTLE_PRESETS:
            await ctx.error(f"无效的预设名: {preset_name}")
            return False
        
        # 执行激活特定预设
        # 格式：http://control.charles/throttling/activate?preset=4G
        url = f"{CHARLES_BASE_URL}/throttling/activate"
        requests.get(url, params={"preset": preset_name}, **params)
        await ctx.info(f"🌐 弱网模拟已激活: {preset_name}")
        return True
    except Exception as e:
        await ctx.error(f"Throttling 操作失败: {e}")
        return False


#======    【数据过滤工具】   ======

def wrap_response(data: Any, task_id: str) -> Dict[str, Any]:
    """包装器：返回数据并附带任务状态"""
    last_h = "N/A"
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                last_h = datetime.fromtimestamp(json.load(f).get("time", 0)).strftime('%H:%M:%S')
        except: pass
    return {
        "context": {"current_task": task_id, "last_harvest": last_h, "cache": "Memory_Hit"},
        "payload": data
    }

def manage_task_storage(task_id: str):
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    last_task = ""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                last_task = json.load(f).get("current_task", "")
        except: pass
    if last_task and last_task != task_id:
        for f in os.listdir(PACKAGE_DIR):
            f_path = os.path.join(PACKAGE_DIR, f)
            try:
                if os.path.isfile(f_path): os.remove(f_path)
                elif os.path.isdir(f_path): shutil.rmtree(f_path)
            except: pass
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({"current_task": task_id, "time": time.time()}, f)

async def load_task_data_from_disk(task_id: str) -> List[Dict]:
    all_data = []
    if not os.path.exists(PACKAGE_DIR): return []
    blocks = sorted([f for f in os.listdir(PACKAGE_DIR) if f.startswith(f"block_{task_id}_")])
    for b in blocks:
        try:
            with open(os.path.join(PACKAGE_DIR, b), 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, list): all_data.extend(d)
        except: continue
    return all_data

def smart_merge(task_id: str):
    fragments = sorted([f for f in os.listdir(PACKAGE_DIR) if f.startswith("flow_")])
    if not fragments: return
    block_path = os.path.join(PACKAGE_DIR, f"block_{task_id}_{datetime.now().strftime('%Y%m%d%H')}.json")
    combined = []
    if os.path.exists(block_path):
        try:
            with open(block_path, 'r', encoding='utf-8') as f: combined = json.load(f)
        except: pass
    for f_name in fragments:
        try:
            with open(os.path.join(PACKAGE_DIR, f_name), 'r', encoding='utf-8') as f:
                d = json.load(f)
                if isinstance(d, list): combined.extend(d)
            os.remove(os.path.join(PACKAGE_DIR, f_name))
        except: continue
    with open(block_path, 'w', encoding='utf-8') as f:
        json.dump(combined[-2500:], f, ensure_ascii=False)

def calc_entropy(s: str) -> float:
    if not s or len(s) < 5: return 0
    prob = [float(s.count(c)) / len(s) for c in dict.fromkeys(list(s))]
    return -sum([p * math.log(p, 2) for p in prob])

def get_hit_locations(entry: Dict, keyword: str) -> List[str]:
    hits = []
    k = keyword.lower()
    if k in (entry.get("path") or "").lower(): hits.append("url_path")
    req, res = entry.get("request", {}), entry.get("response", {})
    if k in json.dumps(req.get("headers", [])).lower(): hits.append("req_header")
    if k in (req.get("body", {}).get("text", "") or "").lower(): hits.append("req_body")
    if k in json.dumps(res.get("headers", [])).lower(): hits.append("res_header")
    if k in (res.get("body", {}).get("text", "") or "").lower(): hits.append("res_body")
    return hits

def simplify_entry(entry: Dict) -> Dict:
    req_txt = entry.get("request", {}).get("body", {}).get("text", "") or ""
    res_txt = entry.get("response", {}).get("body", {}).get("text", "") or ""
    return {
        "id": entry.get("id"), "method": entry.get("method"), "host": entry.get("host"),
        "path": entry.get("path"), "status": entry.get("response", {}).get("status"),
        "req_preview": req_txt[:500], "res_preview": res_txt[:500]
    }

async def get_proxy_data_instant(ctx: Context) -> List[Dict]:
    params = {"auth": AUTH, "proxies": PROXIES, "timeout": 15}
    try:
        resp = requests.get(f"{CHARLES_BASE_URL}/session/export-json", **params)
        resp.raise_for_status()
        requests.get(f"{CHARLES_BASE_URL}/session/clear", **params)
        requests.get(f"{CHARLES_BASE_URL}/recording/start", **params)
        raw = resp.json()
        if raw:
            fname = f"flow_{datetime.now().strftime('%M%S_%f')}.json"
            with open(os.path.join(PACKAGE_DIR, fname), 'w', encoding='utf-8') as f:
                json.dump(raw, f, ensure_ascii=False)
        return raw
    except: return []


#====  【备份设置函数】  ====

def copy_config():
    path = os.getenv('AppData') or os.path.join(os.getenv('USERPROFILE', ''), 'AppData', 'Roaming')
    cfg = os.path.join(path, "Charles/charles.config")
    if os.path.exists(cfg):
        os.makedirs(os.path.join(BACKUP_DIR, "config"), exist_ok=True)
        shutil.copy2(cfg, os.path.join(BACKUP_DIR, "config/charles.config"))

async def reset_config():
    try:
        requests.get(f"{CHARLES_BASE_URL}/quit", auth=AUTH, proxies=PROXIES, timeout=2)
        src = os.path.join(BACKUP_DIR, "config/charles.config")
        dst = os.path.join(os.getenv('AppData', ''), "Charles/charles.config")
        if os.path.exists(src): shutil.copy2(src, dst)
    except: pass