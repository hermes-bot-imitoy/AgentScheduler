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

# 默认 podman 镜像 (node:22-alpine: 轻量且带 node/npx, MCP 服务器在容器内跑)
DEFAULT_IMAGE = "node:22-alpine"

# MCP filesystem 服务器包 (容器内全局安装, 避免每次 npx 拉包)
MCP_FILESYSTEM_PACKAGE = "@modelcontextprotocol/server-filesystem"


class Computer(ABC):
    """电脑标准接口 (抽象基类).

    参数:
        role_id: 所属角色标识 (用于命名容器/目录隔离).

    子类需实现: power_on / power_off / run_command / run_mcp_tool /
    read_file / write_file / list_dir.
    """

    def __init__(self, role_id: str, auto_mcp: bool = False):
        self.role_id = role_id
        self._on = False
        self._auto_mcp = auto_mcp          # 自动创建的电脑: 创建时自动安装 MCP 服务器
        self._mcp_tools: dict[str, Any] = {}  # 已安装到本电脑的 MCP 工具 (name → ToolDef)
        self._mcp_server: Any = None       # 本电脑独立的 MCP 服务器连接 (懒创建)

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

    @property
    def host_dir(self) -> str:
        """宿主机上该电脑工作目录的映射路径 (MCP 服务器授权目录).

        - LocalComputer: 就是 workdir (data/computers/<role>)
        - PodmanComputer: 容器挂载的宿主机目录 (容器内 /home/agent ↔ 宿主机 data/computers/<role>)
        - SSHComputer: 远程电脑无宿主机映射 → None (不自动装 MCP 服务器)

        MCP filesystem 服务器跑在宿主机, 授权这个目录 = 操作该角色电脑上的文件.
        """
        return str(getattr(self, "_host_dir", None) or "")

    def install_mcp_server(self) -> list[str]:
        """在本电脑上安装独立的 MCP 服务器 (filesystem, 授权本电脑目录).

        每个电脑一个独立服务器进程 (npx 启动), 授权目录 = 本电脑 host_dir,
        工具注册进 self._mcp_tools, handler 绑定本电脑自己的服务器连接 —
        执行即发生在该角色电脑的目录上. 幂等: 已安装则直接返回.

        返回:
            已安装的工具名列表.
        """
        if self._mcp_server is not None:
            return self.list_installed_mcp_tools()
        if not self._auto_mcp:
            logger.info("电脑[%s] 非自动创建, 不自动安装 MCP 服务器", self.role_id)
            return []
        if not self.host_dir:
            logger.warning("电脑[%s] 无宿主机目录映射, 跳过 MCP 服务器安装 (SSH 远程电脑)",
                           self.role_id)
            return []

        try:
            from src.python_tools.mcp_toolkit import MCPServer
            self._mcp_server = MCPServer(
                package="@modelcontextprotocol/server-filesystem",
                args=[self.host_dir],
            )
            self._mcp_server.connect()
            tools = self._mcp_server.list_tools()
            from src.core.tools import ToolDef
            for tool in tools:
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                server = self._mcp_server

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                self._mcp_tools[tname] = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{server.package} (本电脑)",
                )
            logger.info("电脑[%s] 独立 MCP 服务器已安装, %d 个工具: %s",
                        self.role_id, len(self._mcp_tools),
                        self.list_installed_mcp_tools())
        except Exception as exc:
            logger.exception("电脑[%s] MCP 服务器安装失败", self.role_id)
            return []
        return self.list_installed_mcp_tools()

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

    def reboot(self) -> str:
        """重启电脑 (关机后再开机). 所有实现通用."""
        off = self.power_off()
        on = self.power_on()
        return f"电脑[{self.role_id}] 已重启.\n- {off}\n- {on}"

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

    def __init__(self, role_id: str, base_dir: str = "./data/computers",
                 auto_mcp: bool = False):
        super().__init__(role_id, auto_mcp=auto_mcp)
        self._dir = Path(base_dir).resolve() / (role_id or "shared")
        self._dir.mkdir(parents=True, exist_ok=True)
        self._on = True  # 本地模拟默认开机

    @property
    def host_dir(self) -> str:
        # 本地电脑: 工作目录即宿主机目录 (MCP 服务器直接授权它)
        return str(self._dir)

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

    def __init__(self, role_id: str, image: str = DEFAULT_IMAGE,
                 auto_mcp: bool = False):
        super().__init__(role_id, auto_mcp=auto_mcp)
        self.image = image
        self.container_name = f"maf-{role_id or 'shared'}"
        if shutil.which("podman") is None:
            logger.warning(
                "Podman 未安装, 角色 %s 的电脑降级为本地目录模拟 (LocalComputer)",
                role_id,
            )
            self._fallback = LocalComputer(role_id, auto_mcp=auto_mcp)
        else:
            self._fallback = None

    @property
    def host_dir(self) -> str:
        # 降级: 透传本地电脑的宿主机目录
        if self._fallback is not None:
            return self._fallback.host_dir
        # 容器挂载的宿主机目录: data/computers/<role> ↔ 容器内 /home/agent
        return str((Path("./data/computers").resolve() / (self.role_id or "shared")))

    @property
    def workdir(self) -> str:
        if self._fallback is not None:
            return self._fallback.workdir
        return "/home/agent"

    def get_lan_ip(self) -> str:
        """获取本电脑在自定义桥接网络 (maf-net) 中的 IP 地址.

        返回:
            IP 字符串; 降级本地模拟返回 localhost; 查不到返回空串.
        """
        if self._fallback is not None:
            return "127.0.0.1 (本地模拟)"
        try:
            # 网络名含连字符, Go template 必须用 index 取 (直接 .maf-net 会被当减号)
            fmt = '{{(index .NetworkSettings.Networks "%s").IPAddress}}' % DEFAULT_NETWORK_NAME
            r = self._pod("inspect", self.container_name, "-f", fmt)
            ip = (r.stdout or "").strip()
            return ip or ""
        except Exception:
            logger.warning("电脑[%s] 获取内网 IP 失败", self.role_id, exc_info=True)
            return ""

    def install_mcp_server(self) -> list[str]:
        """在本电脑 (容器) 内安装独立的 MCP 服务器.

        C 方案: MCP 服务器跑在容器内 (podman exec -i 保持 stdio 管道),
        授权目录 = 容器内 workdir (/home/agent) — 与 LLM 看到的路径字面一致,
        不再有宿主机/容器路径空间不一致的问题.

        降级 (无 podman) 时走 LocalComputer (宿主机 npx, 授权 data/computers/<role>).
        """
        if self._fallback is not None:
            return self._fallback.install_mcp_server()
        if self._mcp_server is not None:
            return self.list_installed_mcp_tools()
        if not self._auto_mcp:
            logger.info("电脑[%s] 非自动创建, 不自动安装 MCP 服务器", self.role_id)
            return []

        try:
            from src.python_tools.mcp_toolkit import MCPServer
            # 容器内启动 filesystem 服务器: podman exec -i <容器> npx -y <包> /home/agent
            # -i 保持 stdin/stdout 管道, MCP stdio 协议走容器内进程
            self._ensure_container()  # 确保容器运行 + 包已预装
            self._mcp_server = MCPServer(
                package=MCP_FILESYSTEM_PACKAGE,
                args=[self.workdir],  # 授权容器内工作目录
                command="podman",
                command_args=["exec", "-i", self.container_name, "npx", "-y",
                              MCP_FILESYSTEM_PACKAGE, self.workdir],
            )
            self._mcp_server.connect()
            tools = self._mcp_server.list_tools()
            from src.core.tools import ToolDef
            for tool in tools:
                tname = getattr(tool, "name", "")
                if not tname:
                    continue
                server = self._mcp_server

                def _make_handler(srv=server, tn=tname):
                    def handler(args: dict[str, Any]) -> str:
                        return srv.call_tool(tn, args)
                    return handler

                self._mcp_tools[tname] = ToolDef(
                    name=tname,
                    description=getattr(tool, "description", "") or "",
                    input_schema=getattr(tool, "input_schema", {}) or {},
                    handler=_make_handler(),
                    source=f"mcp:{MCP_FILESYSTEM_PACKAGE} (容器内 {self.container_name})",
                )
            logger.info("电脑[%s] 容器内 MCP 服务器已安装, %d 个工具: %s",
                        self.role_id, len(self._mcp_tools),
                        self.list_installed_mcp_tools())
        except Exception as exc:
            logger.exception("电脑[%s] 容器内 MCP 服务器安装失败", self.role_id)
            return []
        return self.list_installed_mcp_tools()

    def _pod(self, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        """执行 podman 命令."""
        return subprocess.run(
            ["podman", *args], capture_output=True, text=True, timeout=timeout,
        )

    def _ensure_container(self) -> None:
        """确保容器存在并运行 (不存在则创建), 并创建工作目录."""
        # 容器挂载宿主机目录: data/computers/<role> ↔ 容器内 /home/agent
        host_dir = self.host_dir
        Path(host_dir).mkdir(parents=True, exist_ok=True)
        r = self._pod("ps", "-a", "--filter", f"name={self.container_name}", "--format", "{{.Names}}")
        if self.container_name not in (r.stdout or ""):
            # 加入自定义桥接网络 (电脑间互通), 网络不存在则先创建
            from src.core.computer import _COMPUTER_MANAGER
            network = _COMPUTER_MANAGER.ensure_network()
            self._pod("run", "-d", "--name", self.container_name,
                      "--network", network,
                      "-v", f"{host_dir}:{self.workdir}",
                      self.image, "sleep", "infinity")
        r = self._pod("ps", "--filter", f"name={self.container_name}", "--format", "{{.Names}}")
        if self.container_name not in (r.stdout or ""):
            self._pod("start", self.container_name)
        # 确保工作目录存在 (alpine 默认无 /home/agent, 挂载后即存在)
        self._pod("exec", self.container_name, "sh", "-c",
                  f"mkdir -p '{self.workdir}'")
        # 预装 MCP filesystem 服务器包 (容器内全局安装, 之后启动即用, 免 npx 拉包)
        if not self._mcp_pkg_installed:
            r = self._pod("exec", self.container_name, "sh", "-c",
                          "npm ls -g --depth=0 2>/dev/null | grep -q 'server-filesystem' "
                          "|| npm install -g --no-fund --no-audit "
                          f"'{MCP_FILESYSTEM_PACKAGE}'", timeout=300)
            self._mcp_pkg_installed = True
            logger.info("电脑[%s] 容器内已预装 MCP filesystem 服务器 (npm -g)",
                        self.role_id)

    @property
    def _mcp_pkg_installed(self) -> bool:
        return getattr(self, "_mcp_pkg_flag", False)

    @_mcp_pkg_installed.setter
    def _mcp_pkg_installed(self, value: bool) -> None:
        self._mcp_pkg_flag = value

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
        # 用 heredoc 写入容器内文件: $(dirname) 必须可展开 → 用双引号包 $(),
        # 内部路径用单引号保护空格
        escaped = content.replace("'", "'\\''")
        return self.run_command(
            f"mkdir -p \"$(dirname '{path}')\" && cat > '{path}' <<'EOF'\n{escaped}\nEOF"
        )

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
        auto_mcp: bool = False,
    ):
        super().__init__(role_id, auto_mcp=auto_mcp)
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

def create_computer(kind: str = "podman", role_id: str = "", *,
                    auto_mcp: bool = False, **kwargs: Any) -> Computer:
    """按类型创建电脑实例.

    参数:
        kind:     "podman" (默认) | "ssh" | "local".
        role_id:  角色标识.
        auto_mcp: 是否自动创建的电脑实例. True = 创建实例时自动安装独立的
                  MCP 服务器 (AgentRole.computer 自动创建时传 True);
                  False = 不自动安装 (手动 create_computer 调用).
        kwargs:   透传给具体实现 (ssh 需 host/user 等).

    返回:
        Computer 实例.
    """
    kind = (kind or "podman").lower()
    if kind == "local":
        return LocalComputer(role_id=role_id, auto_mcp=auto_mcp)
    if kind == "ssh":
        if not kwargs.get("host"):
            raise ValueError("SSHComputer 需要 host 参数 (远程主机地址)")
        return SSHComputer(role_id=role_id, auto_mcp=auto_mcp, **kwargs)
    return PodmanComputer(role_id=role_id, auto_mcp=auto_mcp, **kwargs)


# ── ComputerManager (电脑管理类) ──────────────────────────

DEFAULT_NETWORK_NAME = "maf-net"  # podman 自定义桥接网络 (电脑间互通)


class ComputerManager:
    """电脑管理类: 分配 / 注册 / 查询 / 销毁 各角色电脑.

    职责:
      - 维护角色 → 电脑的注册表 (含人名, 供内网设备列表展示)
      - 确保 podman 自定义桥接网络存在 (电脑间可互相通信)
      - 统一销毁入口 (关机 + 删除容器 + 注销)
      - 查询内网电脑设备 (人名 / 电脑名 / IP)

    全局单例 _COMPUTER_MANAGER (computer.py 末尾), AgentRole.computer
    自动创建的电脑都会注册进来; 手动 create_computer() 创建的不会.
    """

    def __init__(self, network_name: str = DEFAULT_NETWORK_NAME):
        self.network_name = network_name
        self._computers: dict[str, Any] = {}   # role_id → Computer
        self._names: dict[str, str] = {}       # role_id → 人名

    # ── 网络 ──────────────────────────────────────────────

    def ensure_network(self) -> str:
        """确保 podman 自定义桥接网络存在 (幂等). 返回网络名.

        网络用于让各角色电脑 (容器) 之间可以互相通信.
        本机无 podman 时直接返回网络名 (不实际创建, 降级环境无网络).
        """
        if shutil.which("podman") is None:
            return self.network_name
        r = subprocess.run(["podman", "network", "exists", self.network_name],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            subprocess.run(["podman", "network", "create", self.network_name],
                           capture_output=True, text=True, timeout=60)
            logger.info("podman 自定义桥接网络已创建: %s", self.network_name)
        return self.network_name

    # ── 分配 / 注册 ───────────────────────────────────────

    def create(self, kind: str = "podman", role_id: str = "", name: str = "",
               auto_mcp: bool = False, **kwargs: Any) -> Any:
        """创建并注册一台角色电脑 (分配).

        参数:
            kind:     电脑类型 ("podman" 默认 | "ssh" | "local").
            role_id:  角色 ID (注册表键).
            name:     人名 (供内网设备列表展示, 可空).
            auto_mcp: 是否自动安装独立 MCP 服务器.
            kwargs:   透传给 create_computer.

        返回:
            Computer 实例.
        """
        self.ensure_network()
        comp = create_computer(kind=kind, role_id=role_id,
                               auto_mcp=auto_mcp, **kwargs)
        self.register(comp, name=name)
        return comp

    def register(self, computer: Any, name: str = "") -> None:
        """注册一台已创建的电脑到管理器."""
        self._computers[computer.role_id] = computer
        if name:
            self._names[computer.role_id] = name

    # ── 查询 ──────────────────────────────────────────────

    def get(self, role_id: str) -> Any:
        """按角色 ID 获取电脑 (不存在抛 KeyError)."""
        return self._computers[role_id]

    def list_all(self) -> list[Any]:
        """返回全部已注册电脑列表 (按注册顺序)."""
        return list(self._computers.values())

    # ── 销毁 ──────────────────────────────────────────────

    def destroy(self, role_id: str) -> bool:
        """销毁角色电脑: 关机 + 删除容器 + 注销. 返回是否销毁成功.

        参数:
            role_id: 角色 ID.
        """
        comp = self._computers.pop(role_id, None)
        self._names.pop(role_id, None)
        if comp is None:
            return False
        # 关机
        try:
            if comp.is_on:
                comp.power_off()
        except Exception:
            logger.warning("电脑[%s] 关机失败 (销毁继续)", role_id, exc_info=True)
        # 删除 podman 容器 (仅真实容器, 降级 LocalComputer 无容器)
        if isinstance(comp, PodmanComputer) and comp._fallback is None:
            try:
                comp._pod("rm", "-f", comp.container_name)
                logger.info("电脑[%s] 容器已删除: %s", role_id, comp.container_name)
            except Exception:
                logger.warning("电脑[%s] 容器删除失败", role_id, exc_info=True)
        logger.info("电脑[%s] 已销毁 (注销)", role_id)
        return True

    # ── 内网设备 ──────────────────────────────────────────

    def list_lan_devices(self) -> list[dict[str, str]]:
        """列出内网电脑设备: 人名 / 电脑名 / IP.

        返回:
            [{"person", "role_id", "computer", "ip"}, ...] 按角色排序.
        """
        devices = []
        for role_id, comp in sorted(self._computers.items()):
            ip = ""
            if hasattr(comp, "container_name"):
                # podman 容器: 查网络内 IP
                ip = comp.get_lan_ip() if hasattr(comp, "get_lan_ip") else ""
            elif hasattr(comp, "host"):
                ip = comp.host  # ssh 电脑: 远程主机地址
            devices.append({
                "person": self._names.get(role_id, role_id),
                "role_id": role_id,
                "computer": getattr(comp, "container_name",
                                    f"local-{role_id.lower()}"),
                "ip": ip or "(无内网IP)",
            })
        return devices


# 全局单例: 角色自动创建的电脑统一注册到这里
_COMPUTER_MANAGER = ComputerManager()
