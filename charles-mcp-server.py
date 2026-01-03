import os
import re
import sys
import time
import json
import shutil
import asyncio
import requests
from typing import Optional, List, Dict, Any
from datetime import datetime
from requests.auth import HTTPBasicAuth
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP, Context

# --- 环境自适配 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

# --- 配置常量 ---
AUTH = HTTPBasicAuth('tower', '123456')
PROXIES = {"http": "http://127.0.0.1:8888"}
CHARLES_BASE_URL = "http://control.charles"
PACKAGE_DIR = os.path.join(BASE_DIR, "package")
BACKUP_DIR = os.path.join(BASE_DIR, "back")

def get_appdata_path():
    """获取 AppData 路径并输出调试信息到 stderr"""
    path = os.getenv('AppData')
    if not path:
        user_profile = os.getenv('USERPROFILE')
        if user_profile:
            path = os.path.join(user_profile, 'AppData', 'Roaming')
    
    if path:
        sys.stderr.write(f">>> [DEBUG] 成功定位 AppData: {path}\n")
    return path

APPDATA = get_appdata_path()
if not APPDATA:
    sys.stderr.write(">>> [CRITICAL] 无法获取 AppData 路径，备份功能将受限!\n")

CONFIG_PATH = os.path.join(APPDATA, "Charles/charles.config") if APPDATA else None
PROFILES_DIR = os.path.join(APPDATA, "Charles/data/profiles") if APPDATA else None

mcp = FastMCP("CharlesMCP", json_response=True)

# --- 备份与复位逻辑 ---

def copy_config():
    """备份 Charles 配置文件到项目目录下的 back 文件夹"""
    if not CONFIG_PATH or not os.path.exists(CONFIG_PATH):
        sys.stderr.write(f">>> [WARN] 找不到源配置: {CONFIG_PATH}\n")
        return

    try:
        cfg_back = os.path.join(BACKUP_DIR, "config")
        prf_back = os.path.join(BACKUP_DIR, "profiles")
        os.makedirs(cfg_back, exist_ok=True)
        os.makedirs(prf_back, exist_ok=True)
        
        shutil.copy2(CONFIG_PATH, os.path.join(cfg_back, "charles.config"))
        
        if PROFILES_DIR and os.path.exists(PROFILES_DIR):
            if os.path.exists(prf_back):
                shutil.rmtree(prf_back)
            shutil.copytree(PROFILES_DIR, prf_back)
        
        sys.stderr.write(f">>> [SUCCESS] 配置文件已成功备份至: {BACKUP_DIR}\n")
    except Exception as e:
        sys.stderr.write(f">>> [ERROR] 备份执行失败: {str(e)}\n")

async def reset_config():
    """退出时的核心清理流程"""
    try:
        sys.stderr.write(">>> [DEBUG] 准备退出并销毁临时数据...\n")
        try:
            requests.get(f"{CHARLES_BASE_URL}/quit", auth=AUTH, proxies=PROXIES, timeout=3)
            await asyncio.sleep(2) # 等待进程完全释放文件
        except:
            pass
        
        cfg_source = os.path.join(BACKUP_DIR, "config/charles.config")
        if os.path.exists(cfg_source) and CONFIG_PATH:
            shutil.copy2(cfg_source, CONFIG_PATH)
            
        prf_source = os.path.join(BACKUP_DIR, "profiles")
        if os.path.exists(prf_source) and PROFILES_DIR:
            if os.path.exists(PROFILES_DIR):
                shutil.rmtree(PROFILES_DIR)
            shutil.copytree(prf_source, PROFILES_DIR)
        sys.stderr.write(">>> [SUCCESS] 配置文件已回滚至原始状态。\n")
        
        if os.path.exists(PACKAGE_DIR):
            shutil.rmtree(PACKAGE_DIR)
        os.makedirs(PACKAGE_DIR, exist_ok=True)
        sys.stderr.write(">>> [SUCCESS] 退出销毁：package 目录已物理清空。\n")
        
    except Exception as e:
        sys.stderr.write(f">>> [ERROR] 退出销毁流程失败: {e}\n")

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    copy_config() # 启动备份
    try:
        yield
    finally:
        await reset_config()

mcp.lifespan = lifespan

# --- 工具辅助函数 ---

async def _get_proxy_data(stoptime: int, ctx: Context) -> List[Dict]:
    os.makedirs(PACKAGE_DIR, exist_ok=True)
    req_params = {"auth": AUTH, "proxies": PROXIES, "timeout": 5}
    
    if stoptime > 0:
        try:
            await ctx.info("正在清理旧会话并开启新录制...")
            requests.get(f"{CHARLES_BASE_URL}/session/clear", **req_params)
            
            for i in range(0, stoptime, 10):
                remaining = stoptime - i
                await ctx.info(f"⏳ 录制持续中... 已完成 {i}s，剩余 {remaining}s")
                
                sleep_time = min(10, remaining)
                await asyncio.sleep(sleep_time)
            
            await ctx.info("✅ 录制结束，正在导出数据...")
            requests.get(f"{CHARLES_BASE_URL}/recording/stop", **req_params)
            
            filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}.chlsj"
            filepath = os.path.join(PACKAGE_DIR, filename)
            resp = requests.get(f"{CHARLES_BASE_URL}/session/export-json", **req_params)
            resp.raise_for_status()
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(resp.text)
            return json.loads(resp.text)
        except Exception as e:
            await ctx.error(f"抓包失败: {str(e)}")
            return [{"error": str(e)}]
    else:
        files = [f for f in os.listdir(PACKAGE_DIR) if f.endswith('.chlsj')]
        if not files: return [{"error": "未找到历史包"}]
        filepath = os.path.join(PACKAGE_DIR, sorted(files)[-1])
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
# --- MCP Tools ---

@mcp.tool()
async def proxy_by_time(stoptime: int, ctx: Context) -> List[Dict]:
    """抓取或读取 Charles 流量包。stoptime=0 为读取最新。"""
    return await _get_proxy_data(stoptime, ctx)

@mcp.tool()
async def filter_func(
    time_sec: int,
    ctx: Context,
    host: Optional[str] = None,
    method: Optional[str] = None,
    keyword_regex: Optional[str] = None,
    keep_request: bool = True,
    keep_response: bool = True
) -> List[Dict]:
    """高级过滤与搜索工具。支持正则定位关键字行号。"""
    raw_data = await _get_proxy_data(time_sec, ctx)
    if not isinstance(raw_data, list): return raw_data

    filtered_results = []
    for entry in raw_data:
        if host and host not in entry.get('host', ''): continue
        if method and method.upper() != entry.get('method', '').upper(): continue

        match_info = None
        if keyword_regex:
            entry_str = json.dumps(entry, indent=2, ensure_ascii=False)
            lines = entry_str.splitlines()
            try:
                regex = re.compile(keyword_regex, re.IGNORECASE)
                for i, line in enumerate(lines):
                    if regex.search(line):
                        match_info = {"line": i + 1, "content": line.strip()}
                        break
                if not match_info: continue
            except: continue

        res = entry.copy()
        if not keep_request: res.pop('request', None)
        if not keep_response: res.pop('response', None)
        if match_info: res["_match_location"] = match_info
        filtered_results.append(res)
    return filtered_results

@mcp.tool()
async def throttling(status: str) -> str:
    """
    设置弱网预设。
    常用: 3G, 4G, 56+kbps+Modem, 100+Mbps+Fibre, deactivate
    """
    valid_presets = ["3G", "4G", "100+Mbps+Fibre", "32+Mbps+Fibre", "16+Mbps+ADSL2%2B","8+Mbps+ADSL2","2+Mbps+ADSL","32+Mbps+VDSL","256+kbps+ISDN%2FDSL","512+kbps+ISDN%2FDSL","56+kbps+Modem","deactivate"]
    
    if status.lower() in ["start", "on"]: status = "3G"
    
    if status not in valid_presets:
        return f"无效预设。可选值: {', '.join(valid_presets)}"
        
    url = f"{CHARLES_BASE_URL}/throttling/deactivate" if status.lower() == "deactivate" else f"{CHARLES_BASE_URL}/throttling/activate?preset={status}"
    try:
        requests.get(url, auth=AUTH, proxies=PROXIES, timeout=5)
        return f"Success: {status}"
    except Exception as e:
        return f"Error: {str(e)}"


# --- 复位逻辑函数 ---

def _perform_cleanup():
    """同步执行的物理清理逻辑"""
    sys.stderr.write(">>> [ACTION] 正在执行手动重置清理...\n")
    try:
        try:
            requests.get(f"{CHARLES_BASE_URL}/quit", auth=AUTH, proxies=PROXIES, timeout=2)
            time.sleep(1.5) # 给进程一点响应时间
        except:
            pass

        if CONFIG_PATH and os.path.exists(os.path.join(BACKUP_DIR, "config/charles.config")):
            shutil.copy2(os.path.join(BACKUP_DIR, "config/charles.config"), CONFIG_PATH)
            
        if PROFILES_DIR and os.path.exists(os.path.join(BACKUP_DIR, "profiles")):
            if os.path.exists(PROFILES_DIR):
                shutil.rmtree(PROFILES_DIR)
            shutil.copytree(os.path.join(BACKUP_DIR, "profiles"), PROFILES_DIR)

        if os.path.exists(PACKAGE_DIR):
            shutil.rmtree(PACKAGE_DIR)
        os.makedirs(PACKAGE_DIR, exist_ok=True)
        
        return True
    except Exception as e:
        sys.stderr.write(f">>> [ERROR] 清理失败: {e}\n")
        return False

# --- 新增 MCP Tool ---

@mcp.tool()
async def reset_environment(ctx: Context) -> str:
    """
    【强制重置工具】
    作用：立即销毁所有抓包数据、关闭 Charles 并恢复系统原始配置文件。
    使用场景：
    1. 当用户想要“清理现场”或“结束测试”时。
    2. 当抓包记录过多需要清空时。
    3. 想要确保 Charles 配置回到初始状态时。
    """
    await ctx.info("正在销毁数据包并回滚配置文件...")
    success = _perform_cleanup()
    
    if success:
        return "环境重置成功：Charles 已尝试关闭，配置已还原，流量包目录已清空。"
    else:
        return "重置过程中出现部分错误，请检查后台日志。"
@mcp.prompt()
def throttling_helper() -> str:
    """弱网预设参考手册"""
    return "可选状态: 3G, 4G, 56+kbps+Modem, 100+Mbps+Fibre, deactivate。"

if __name__ == "__main__":
    copy_config()
    
    if sys.platform == "win32":
        import io
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    mcp.run(transport="stdio")