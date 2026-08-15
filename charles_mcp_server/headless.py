"""Managed Charles headless lifecycle support.

Charles exposes live-session controls through its Web Interface.  This module owns
at most one local Charles process and protects it with an OS file lock, so two
MCP processes cannot clear or read each other's sessions accidentally.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests
from requests.auth import HTTPBasicAuth


class HeadlessError(RuntimeError):
    """A managed Charles instance could not be started or controlled."""


class _InstanceLock:
    """Non-blocking, cross-platform advisory lock held for manager lifetime."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: Optional[Any] = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                if not self._file.read(1):
                    self._file.write("0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._file.close()
            self._file = None
            raise HeadlessError(
                f"另一个 charles-mcp-server 已托管 Charles（锁文件：{self._path}）。"
            ) from exc

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class CharlesHeadlessManager:
    """Start exactly one Charles --headless process for the current MCP process."""

    def __init__(self) -> None:
        self.proxy_host = os.getenv("CHARLES_PROXY_HOST", "127.0.0.1")
        self.proxy_port = int(os.getenv("CHARLES_PROXY_PORT", "8888"))
        root = Path(os.getenv("CHARLES_MCP_RUNTIME_DIR", Path.home() / ".charles-mcp-server"))
        self.runtime_dir = root.expanduser().resolve()
        self.config_path = self.runtime_dir / "charles.config"
        self.data_dir = self.runtime_dir / "data"
        self.log_path = self.runtime_dir / "charles.log"
        self.state_path = self.runtime_dir / "state.json"
        self._lock = _InstanceLock(self.runtime_dir / "charles.lock")
        self._process: Optional[subprocess.Popen[str]] = None
        self._credentials: Optional[tuple[str, str]] = None
        self._started_at: Optional[float] = None

    @staticmethod
    def enabled() -> bool:
        return os.getenv("CHARLES_MCP_MODE", "external").lower() == "managed-headless"

    def _find_executable(self) -> str:
        configured = os.getenv("CHARLES_EXECUTABLE")
        candidates = [configured] if configured else []
        if sys.platform == "darwin":
            candidates.append("/Applications/Charles.app/Contents/MacOS/Charles")
        candidates.extend([shutil.which("charles"), shutil.which("Charles")])
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        raise HeadlessError(
            "未找到 Charles 可执行文件。请安装 Charles，或设置 CHARLES_EXECUTABLE 指向其可执行文件。"
        )

    def _write_config(self, username: str, password: str) -> None:
        # This is the minimal Charles 5.x serialisation format.  The fixed proxy
        # port is intentionally part of the MVP: Charles multi-instance port
        # isolation is not reliable, and the OS lock owns this port/session.
        xml = f"""<?xml version='1.0' encoding='UTF-8' ?>
<?charles serialisation-version='2.0' ?>
<configuration>
  <remoteControlConfiguration>
    <enabled>true</enabled>
    <allowAnonymous>false</allowAnonymous>
    <users>
      <remoteControlUser>
        <username>{username}</username>
        <password>{password}</password>
      </remoteControlUser>
    </users>
  </remoteControlConfiguration>
  <startupConfiguration>
    <acceptedEulaVersion>20240608</acceptedEulaVersion>
  </startupConfiguration>
</configuration>
"""
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.mkdir(mode=0o700, exist_ok=True)
        self.config_path.write_text(xml, encoding="utf-8")
        try:
            self.config_path.chmod(0o600)
        except OSError:
            pass

    def _request(self, path: str, timeout: float = 3) -> requests.Response:
        if self._credentials is None:
            raise HeadlessError("Charles Headless 尚未初始化。")
        username, password = self._credentials
        return requests.get(
            f"http://control.charles{path}",
            auth=HTTPBasicAuth(username, password),
            proxies={"http": f"http://{self.proxy_host}:{self.proxy_port}"},
            timeout=timeout,
        )

    def _write_state(self) -> None:
        self.state_path.write_text(json.dumps({
            "pid": self._process.pid if self._process else None,
            "proxy": f"{self.proxy_host}:{self.proxy_port}",
            "started_at": self._started_at,
            "managed": True,
        }), encoding="utf-8")
        try:
            self.state_path.chmod(0o600)
        except OSError:
            pass

    def start(self, timeout: float = 25) -> dict[str, Any]:
        if self._process and self._process.poll() is None:
            return self.status()
        self._lock.acquire()
        try:
            executable = self._find_executable()
            username = "mcp_" + secrets.token_hex(8)
            password = secrets.token_urlsafe(24)
            self._credentials = (username, password)
            self._write_config(username, password)
            log_file = self.log_path.open("a", encoding="utf-8")
            kwargs: dict[str, Any] = {"stdout": log_file, "stderr": subprocess.STDOUT, "text": True}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                self._process = subprocess.Popen(
                    [executable, "--headless", "--config", str(self.config_path), "--data", str(self.data_dir)],
                    **kwargs,
                )
            finally:
                log_file.close()
            self._started_at = time.time()
            self._write_state()
            deadline = time.monotonic() + timeout
            last_error = "Charles 正在启动"
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise HeadlessError(f"Charles 启动后退出（exit={self._process.returncode}）：{self._log_tail()}")
                try:
                    # The documented Web Interface is the stable readiness
                    # contract. Export formats vary between Charles versions,
                    # so export_session() remains the authoritative data check.
                    response = self._request("/")
                    body = response.text.lower()
                    if response.status_code == 200 and "web interface is disabled" not in body:
                        return self.status()
                    if response.status_code in (401, 403):
                        last_error = "Web Interface 认证失败，配置可能未生效"
                    elif "web interface is disabled" in body:
                        last_error = "Web Interface 未启用，配置可能不兼容当前 Charles 版本"
                    else:
                        last_error = f"Web Interface 返回 HTTP {response.status_code}"
                except requests.RequestException as exc:
                    last_error = str(exc)
                time.sleep(0.5)
            raise HeadlessError(f"Charles 在 {timeout:.0f} 秒内未就绪：{last_error}。日志：{self._log_tail()}")
        except Exception:
            self.stop(force=True)
            raise

    def _log_tail(self, max_chars: int = 1000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-max_chars:].strip()
        except OSError:
            return "（无日志）"

    def stop(self, force: bool = False) -> None:
        process = self._process
        try:
            if process and process.poll() is None and not force:
                try:
                    self._request("/quit", timeout=2)
                except (HeadlessError, requests.RequestException):
                    pass
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    force = True
            if process and process.poll() is None and force:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
        finally:
            self._process = None
            self._credentials = None
            self._started_at = None
            try:
                self.state_path.unlink(missing_ok=True)
            finally:
                self._lock.release()

    def status(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        return {
            "mode": "managed_headless",
            "managed": True,
            "running": running,
            "pid": self._process.pid if running else None,
            "proxy": f"{self.proxy_host}:{self.proxy_port}",
            "web_interface": "ready" if running else "not_running",
            "runtime_dir": str(self.runtime_dir),
            "https_note": "HTTPS 解密要求目标客户端已信任 Charles Root Certificate，且不存在证书锁定。",
        }


manager = CharlesHeadlessManager()
atexit.register(manager.stop)
