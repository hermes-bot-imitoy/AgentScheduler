#!/usr/bin/env python3
"""示例: 如何添加一个 Python 工具.

本示例演示完整流程:
  1. 创建一个 ToolKit (工具类)
  2. 用 add_python_tool 添加 Python 工具
  3. 将工具类注册到角色上
  4. 让 LLM 通过 tool_call 调用该工具

运行:
    cd maf_scheduler && source .venv/bin/activate && python -m src.python_tools.examples.add_python_tool
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

os.environ.setdefault("DEEPSEEK_API_KEY", "")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-v4-flash")
os.environ.setdefault("DEEPSEEK_THINKING", "true")

# ── 第 0 步: 导入基础设施 ──────────────────────────────────
from src.core.tools import ToolKit                        # 工具类
from src.core.roles import AgentRole, RolePool, Task, Urgency
from src.core.role_templates import get_template


# ── 第 1 步: 写一个工具处理函数 (接收 args 字典, 返回字符串) ──

def _today_weather(args: dict) -> str:
    """查询今日天气. args 包含 city 参数."""
    city = args.get("city", "上海")
    # 这里可以换成真实天气 API
    return f"{city} 今日: 晴, 28~34℃, 东南风3级, 适合户外活动."


def _add_numbers(args: dict) -> str:
    """两个数字相加."""
    a = args.get("a", 0)
    b = args.get("b", 0)
    return f"{a} + {b} = {a + b}"


# ── 第 2 步: 创建一个 ToolKit 并添加工具 ────────────────────

def create_weather_toolkit() -> ToolKit:
    """创建天气工具类."""
    tk = ToolKit(name="weather", description="天气查询与计算工具类")

    # add_python_tool 参数说明:
    #   name        工具名 (LLM 调用时使用)
    #   description 工具描述 (LLM 决定何时调用)
    #   input_schema JSON Schema 定义参数 (LLM 按此生成 arguments)
    #   handler     处理函数 (dict[str, Any] -> str)
    tk.add_python_tool(
        name="get_weather",
        description="查询指定城市的今日天气. 需要用户提到天气时调用.",
        input_schema={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称, 如 北京/上海/深圳"},
            },
        },
        handler=_today_weather,
    )

    tk.add_python_tool(
        name="add_numbers",
        description="两个数字相加. 需要用户要求计算时调用.",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个数字"},
                "b": {"type": "number", "description": "第二个数字"},
            },
            "required": ["a", "b"],
        },
        handler=_add_numbers,
    )

    return tk


# ── 第 3 步: 注册到角色并运行 ──────────────────────────────

def main():
    print("═" * 60)
    print("  示例: 如何添加一个 Python 工具")
    print("═" * 60)

    # 创建工具类
    weather_tk = create_weather_toolkit()
    print(f"\n工具类 'weather' 创建完成, 包含工具: {weather_tk.tool_names}")

    # 创建角色并导入工具类
    assistant = get_template("support_agent")
    added = assistant.add_toolkit(weather_tk)
    print(f"角色 {assistant.name}({assistant.role_id}) 导入工具类, 新增 {added} 个工具")

    # 启动角色池
    pool = RolePool()
    pool.add_role(assistant)
    pool.start()

    print(f"\n角色当前工具: {assistant.mcp_tool_names}\n")

    # 给 LLM 一个需要调用工具的任务
    pool.assign_task("support_agent", Task(
        urgency=Urgency.NORMAL,
        description=(
            "用户问: '明天上海天气怎么样?' 请用工具查询天气, 然后回复用户. "
            "另外用户还想知道 12 和 34 相加等于多少, 也用工具计算."
        ),
    ))

    # 等待工具调用完成 (LLM -> tool_call -> 执行 -> 反馈 -> 最终回复)
    time.sleep(15)

    # 查看结果
    status = pool.get_status()
    print(f"\n{'-' * 60}")
    print(f"角色状态: busy={status['support_agent']['busy']}, queue={status['support_agent']['queue_depth']}")

    pool.shutdown(wait=False)
    print("\n✅ 示例运行完成. 观察上面的日志可以看到 LLM 调用 get_weather 和 add_numbers 工具的过程.")


if __name__ == "__main__":
    main()
