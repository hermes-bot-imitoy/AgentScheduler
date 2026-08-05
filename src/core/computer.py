"""电脑基类与实现 (Computer) — 每个角色一台个人电脑.

标准接口 (Computer 基类), 供 LLM 工具调用:
  - power_on():  开机
  - power_off(): 关机
  - run_command(cmd): 运行命令
  - run_mcp_tool(tool_name, args): 运行 MCP 工具
  - read_file / write_file / list_dir: 个人目录文件操作

三种实现:
  - PodmanComputer: 用 podman 容器模拟虚拟电脑 (默认).
    容器名 maf-<role_id>, 工作目录 /home/agent. 本机无 podman 命令时
    自动降级为 LocalComputer (本地目录模拟, 语义一致, 便于无 podman 环境).
  - SSHComputer:   通过 ssh 连接远程主机执行命令 (需 host/user 配置).
  - LocalComputer: 本地目录模拟 (开发/降级用), 目录 data/computers/<role_id>/.

角色添加时自动创建电脑 (默认 podman): AgentRole.computer 惰性创建,
create_computer() 工厂按角色 computer_kind 选择实现.

用法:
    from src.core.computer import create_computer
    comp = create_computer("podman", role_id="CEO")
    comp.power_on()
    comp.run_command("ls -la")
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 默认 podman 镜像 (alpine 轻量, 带 sh/busybox)
DEFAULT_IMAGE = "alpine:latest"


class Computer(ABC):
    """电脑标准接口 (抽象基类).

    参数:
        role_id: 所属角色标识 (用于命名容器/目录隔离).

    子类需实现: power_on / power_off / run_command / run_mcp_tool /
    read_file / write_file / list_dir.
    """

    def __init__(self, role_id: str):
        self.role_id = role_id
        self._on = False
        self._mcp_tools: dict[str, Any] = {}  # 已安装到本电脑的 MCP 工具 (name → ToolDef)

    # ── 抽象接口 (子类实现) ──────────────────────────────

    @abstractmethod
    def power_on(self) -> str:
        """开机. 返回状态说明."""

    @abstractmethod
    def power_off(self) -> str:
        """关机. 返回状态说明."""

    @abstractmethod
    def run_command(self, command: str) -> str:
        """运行命令 (在个人电脑上执行). 返回命令输出."""

    @abstractmethod
    def read_file(self, path: str) -> str:
        """读取个人电脑上的文件内容."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> str:
        """写入个人电脑上的文件 (自动创建父目录). 返回路径."""

    @abstractmethod
    def list_dir(self, path: str = "") -> str:
        """列出个人电脑指定目录内容 (默认工作目录)."""

    # ── MCP 工具安装与执行 (所有实现共用) ────────────────

    def install_mcp_tool(self, tool_def: Any) -> None:
        """将 MCP 工具安装到本电脑 (按工具名记录).

        参数:
            tool_def: ToolDef 实例 (来自 MCPManager 工具池).
        """
        self._mcp_tools[tool_def.name] = tool_def

    def uninstall_mcp_tool(self, tool_name: str) -> bool:
        """从本电脑卸载一个 MCP 工具. 返回是否卸载成功."""
        return self._mcp_tools.pop(tool_name, None) is not None

    def has_mcp_tool(self, tool_name: str) -> bool:
        """本电脑是否已安装指定 MCP 工具."""
        return tool_name in self._mcp_tools

    def list_installed_mcp_tools(self) -> list[str]:
        """列出本电脑已安装的 MCP 工具名 (排序)."""
        return sorted(self._mcp_tools)

    def run_mcp_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """运行 MCP 工具 (在本电脑上执行).

        只执行已安装到本电脑的工具; 未安装则报错并提示可安装的工具来源.

        参数:
            tool_name: MCP 工具名.
            args:      工具参数.

        返回:
            工具执行结果文本.
        """
        td = self._mcp_tools.get(tool_name)
        if td is None:
            return (f"错误: MCP 工具 '{tool_name}' 未安装到本电脑. "
                    f"已安装: {self.list_installed_mcp_tools() or '(无)'}. "
                    f"可用 mcp_search / mcp_list 查看可用工具, 用 mcp_add 安装.")
        if td.handler is None:
            return f"错误: 工具 '{tool_name}' 缺少可执行 handler."
        try:
            return str(td.handler(args))
        except Exception as exc:
            logger.exception("MCP 工具 %s 执行失败", tool_name)
            return f"错误: 工具 '{tool_name}' 执行失败 - {exc}"

    # ── 通用 ──────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        """电脑是否开机."""
        return self._on

    @property
    def workdir(self) -> str:
        """个人工作目录 (电脑上的路径). 子类可覆盖."""
        return "/home/agent"

    def describe(self) -> str:
        """电脑状态描述 (供 LLM 查看)."""
        return (f"电脑[{self.role_id}] ({self.__class__.__name__}): "
                f"状态={'开机' if self._on else '关机'}, 工作目录={self.workdir}")


# ── LocalComputer (本地目录模拟) ──────────────────────────

class LocalComputer(Computer):
    """本地目录模拟电脑 (开发/降级用).

    工作目录: data/computers/<role_id>/, 命令用 subprocess 在本地执行.
    """

    def __init__(self, role_id: str, base_dir: str = "./data/computers"):
        super().__init__(role_id)
        self._dir = Path(base_dir).resolve() / (role_id or "shared")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._on = True  # 本地模拟默认开机

    @property
    def workdir(self) -> str:
        return str(self._dir)

    def power_on(self) -> str:
        self._on = True
        self._dir.mkdir(parents=True, exist_ok=True)
        return f"电脑[{self.role_id}] (本地模拟) 已开机. 工作目录: {self._dir}"

    def power_off(self) -> str:
        self._on = False
        return f"电脑[{self.role_id}] (本地模拟) 已关机."

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        # workdir 本身是绝对路径时, 子路径直接拼接; 相对路径则落到工作目录
        work = str(self._dir)
        if str(p).startswith(work):
            return p
        if not p.is_absolute():
            p = self._dir / p
        return p

    def run_command(self, command: str) -> str:
        if not self._on:
            return "错误: 电脑未开机."
        try:
            result = subprocess.run(
                command, shell=True, cwd=self._dir, capture_output=True,
                text=True, timeout=30,
            )
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0:
                return f"[exit {result.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: 命令超时 (30s)."
        except Exception as exc:
            return f"错误: {exc}"

    def read_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            return f"文件不存在: {p}"
        return p.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return str(p)

    def list_dir(self, path: str = "") -> str:
        p = self._resolve(path)
        if not p.exists() or not p.is_dir():
            return f"目录不存在: {p}"
        entries = sorted(x.name for x in p.iterdir())
        return "\n".join(entries) if entries else "(空目录)"


# ── PodmanComputer (podman 容器虚拟电脑, 默认) ────────────

class PodmanComputer(Computer):
    """Podman 容器虚拟电脑.

    每个角色一个容器 (名 maf-<role_id>), 命令经 podman exec 执行.
    本机无 podman 命令时自动降级为 LocalComputer (见 __init__).

    参数:
        role_id: 角色标识.
        image:   容器镜像 (默认 alpine:latest).
    """

    def __init__(self, role_id: str, image: str = DEFAULT_IMAGE):
        super().__init__(role_id)
        self.image = image
        self.container_name = f"maf-{role_id or 'shared'}"
        if shutil.which("podman") is None:
            logger.warning(
                "Podman 未安装, 角色 %s 的电脑降级为本地目录模拟 (LocalComputer)",
                role_id,
            )
            self._fallback = LocalComputer(role_id)
        else:
            self._fallback = None

    @property
    def workdir(self) -> str:
        if self._fallback is not None:
            return self._fallback.workdir
        return "/home/agent"

    def _pod(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """执行 podman 命令."""
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout,
        )

    def _ensure_container(self) -> None:
        """确保容器存在并运行 (不存在则创建)."""
        r = self._pod("ps", "-a", "--filter", f"name={self.container_name}", "--format", "{{.Names}}")
        if self.container_name not in (r.stdout or ""):
            self._pod("run", "-d", "--name", self.container_name, self.image,
                      "sleep", "infinity")
        r = self._pod("ps", "--filter", f"name={self.container_name}", "--format", "{{.Names}}")
        if self.container_name not in (r.stdout or ""):
            self._pod("start", self.container_name)

    def power_on(self) -> str:
        if self._fallback is not None:
            r = self._fallback.power_on()
            self._on = self._fallback.is_on  # 同步状态
            return r
        try:
            self._ensure_container()
            self._on = True
            return (f"电脑[{self.role_id}] (podman 容器 {self.container_name}) 已开机. "
                    f"工作目录: {self.workdir}")
        except Exception as exc:
            return f"错误: 开机失败 - {exc}"

    def power_off(self) -> str:
        if self._fallback is not None:
            r = self._fallback.power_off()
            self._on = self._fallback.is_on  # 同步状态
            return r
        try:
            self._pod("stop", self.container_name)
            self._on = False
            return f"电脑[{self.role_id}] (podman) 已关机."
        except Exception as exc:
            return f"错误: 关机失败 - {exc}"

    def run_command(self, command: str) -> str:
        if self._fallback is not None:
            return self._fallback.run_command(command)
        if not self._on:
            return "错误: 电脑未开机."
        try:
            r = self._pod("exec", self.container_name, "sh", "-c", command)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except Exception as exc:
            return f"错误: 命令执行失败 - {exc}"

    def read_file(self, path: str) -> str:
        if self._fallback is not None:
            return self._fallback.read_file(path)
        return self.run_command(f"cat '{path}'")

    def write_file(self, path: str, content: str) -> str:
        if self._fallback is not None:
            return self._fallback.write_file(path, content)
        # 用 heredoc 写入容器内文件
        escaped = content.replace("'", "'\\''")
        return self.run_command(f"mkdir -p '$(dirname '{path}')' && cat > '{path}' <<'EOF'\n{escaped}\nEOF")

    def list_dir(self, path: str = "") -> str:
        if self._fallback is not None:
            return self._fallback.list_dir(path)
        target = path or self.workdir
        return self.run_command(f"ls -la '{target}'")

    def describe(self) -> str:
        if self._fallback is not None:
            return self._fallback.describe()
        return (f"电脑[{self.role_id}] (podman 容器 {self.container_name}): "
                f"状态={'开机' if self._on else '关机'}, 工作目录={self.workdir}")


# ── SSHComputer (远程主机) ────────────────────────────────

class SSHComputer(Computer):
    """SSH 远程电脑.

    通过 ssh 在远程主机上执行命令. 需要 host/user 配置.
    工作目录: ~/maf-<role_id>/ (自动创建).

    参数:
        role_id: 角色标识.
        host:    远程主机 (必填).
        user:    登录用户 (默认当前用户).
        key_path: 私钥路径 (可选, 默认用 ssh-agent/默认密钥).
        port:    ssh 端口 (默认 22).
    """

    def __init__(
        self,
        role_id: str,
        host: str,
        user: Optional[str] = None,
        key_path: Optional[str] = None,
        port: int = 22,
    ):
        super().__init__(role_id)
        if not host:
            raise ValueError("SSHComputer 需要 host 参数 (远程主机地址)")
        self.host = host
        self.user = user
        self.key_path = key_path
        self.port = port

    @property
    def workdir(self) -> str:
        return f"~/maf-{self.role_id or 'shared'}"

    def _ssh(self, remote_cmd: str, timeout: int = 60) -> str:
        """执行远程命令, 返回输出文本."""
        target = self.host
        if self.user:
            target = f"{self.user}@{target}"
        cmd = ["ssh", "-p", str(self.port), "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=10"]
        if self.key_path:
            cmd += ["-i", self.key_path]
        cmd += [target, f"mkdir -p {self.workdir} && cd {self.workdir} && {remote_cmd}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            output = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return f"[exit {r.returncode}] {output.strip()[:2000]}"
            return output.strip()[:2000] or "(无输出)"
        except subprocess.TimeoutExpired:
            return "错误: ssh 命令超时 (60s)."
        except Exception as exc:
            return f"错误: ssh 执行失败 - {exc}"

    def power_on(self) -> str:
        # ssh 无"开机"概念, 建立会话即视为开机
        r = self._ssh("echo ok")
        if "ok" in r:
            self._on = True
            return f"电脑[{self.role_id}] (ssh {self.host}) 已连接. 工作目录: {self.workdir}"
        return f"错误: 无法连接 {self.host}: {r}"

    def power_off(self) -> str:
        self._on = False
        return f"电脑[{self.role_id}] (ssh) 已断开."

    def run_command(self, command: str) -> str:
        if not self._on:
            return "错误: 电脑未开机."
        return self._ssh(command)

    def read_file(self, path: str) -> str:
        return self._ssh(f"cat '{path}'")

    def write_file(self, path: str, content: str) -> str:
        escaped = content.replace("'", "'\\''")
        return self._ssh(f"mkdir -p '$(dirname '{path}')' && cat > '{path}' <<'EOF'\n{escaped}\nEOF")

    def list_dir(self, path: str = "") -> str:
        target = path or self.workdir
        return self._ssh(f"ls -la '{target}'")


# ── 工厂 ──────────────────────────────────────────────────

def create_computer(kind: str = "podman", role_id: str = "", **kwargs: Any) -> Computer:
    """按类型创建电脑实例.

    参数:
        kind:   "podman" (默认) | "ssh" | "local".
        role_id: 角色标识.
        kwargs:  透传给具体实现 (ssh 需 host/user 等).

    返回:
        Computer 实例.
    """
    kind = (kind or "podman").lower()
    if kind == "local":
        return LocalComputer(role_id=role_id)
    if kind == "ssh":
        if not kwargs.get("host"):
            raise ValueError("SSHComputer 需要 host 参数 (远程主机地址)")
        return SSHComputer(role_id=role_id, **kwargs)
    return PodmanComputer(role_id=role_id, **kwargs)
