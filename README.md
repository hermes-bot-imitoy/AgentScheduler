# MAF Shift & Event-Driven Agent Scheduler

基于 Microsoft Agent Framework 理念的**企业作息与事件驱动 Agent 调度框架**。

打破传统 Agent `while(true)` 循环，解决"长任务 Context 爆炸、状态不可恢复、Token 成本失控、权限无隔离"问题。

---

## 架构概览

```
┌──────────────────────────────────────────────────────┐
│              Event Dispatcher (事件广播)               │
│   trigger(event) → fan-out to ALL roles              │
│   Each role runs Layer 1-2-3 filter independently    │
└──────────────┬───────────────────────────────────────┘
               │  PASS events become Tasks
               ▼
┌──────────────────────────────────────────────────────┐
│              RolePool (角色线程池)                     │
│   ThreadPoolExecutor — 每个角色独立线程               │
│   Priority Queue (heapq) — CRITICAL > HIGH > NORMAL   │
└──────────────┬───────────────────────────────────────┘
               │  Task execution
               ▼
┌──────────────────────────────────────────────────────┐
│         AgentRole + MCP Tools + talk                  │
│   LLM(Task) → tool_call → execute → feedback → loop  │
│   talk: inter-role communication                     │
└──────────────────────────────────────────────────────┘
```

---

## 核心功能

### 1. 事件总线与多层过滤器 (`src/core/event_bus.py`)

3 层过滤，0 Token 消耗拦截低价值事件：

| 层 | 名称 | 机制 | Token |
|----|------|------|-------|
| Layer 1 | State Mask | OFF_DUTY 状态拦截非 EMERGENCY 事件 | 0 |
| Layer 2 | Salience Evaluator | 关键词匹配 + 优先级加权计算显著性 | 0 |
| Layer 3 | Wake | 通过前两层的事件唤醒 Agent 工作流 | 按需 |

### 2. 作息调度器 (`src/core/scheduler.py`)

- **上班流程**：加载昨日日记 → 冷启动 System Prompt → ON_DUTY_IDLE
- **下班流程**：读取 AmbientBuffer + Session Trace → LLM 写日记 → Context Flush → OFF_DUTY
- **Context Flush**：彻底清空对话历史，防止跨天上下文泄露和 Token 膨胀

### 3. 多角色并发任务调度 (`src/core/roles.py`)

- 每个角色独立线程 + 独立锁 + 独立 LLM 实例
- 优先级任务队列（heapq max-heap）：CRITICAL(10) > HIGH(6) > NORMAL(3) > LOW(1)
- 添加任务时自动按紧急度排序，完成当前任务后再取下一个最紧急的
- RolePool 统一管理所有角色生命周期

### 4. 12 个预定义角色模板 (`src/core/role_templates.py`)

**管理层（默认角色）**：

| 角色 | 姓名 | 职责 |
|------|------|------|
| CEO | 林总 | 接收用户需求→战略目标→汇总报告 |
| COO | 陈总 | 拆解目标→盘点员工→发起招聘 |
| HR | 王人事 | 招聘申请→生成 Agent→面试→入职 |
| CFO | 钱财 | 预算批复→Token 限额→高风险审批（模板保留，初级阶段暂不启用） |

**技术团队**：

| 角色 | 姓名 | 职责 |
|------|------|------|
| architect | 王建国 | 系统架构设计、技术选型、架构评审 |
| fullstack_dev | 李明 | 前后端开发、Bug 修复、Code Review |
| reviewer | 张伟 | 代码审查、安全审计、漏洞扫描 |
| qa_engineer | 刘洋 | 测试设计、自动化测试、回归测试 |
| ops_engineer | 赵强 | 服务器运维、监控告警、CI/CD |

**业务团队**：

| 角色 | 姓名 | 职责 |
|------|------|------|
| content_marketer | 陈静 | 技术博客、产品文案、SEO |
| data_analyst | 孙晓 | 数据分析、报表开发、A/B 测试 |
| support_agent | 周梅 | 用户支持、工单处理、故障升级 |

### 5. MCP 工具系统 (`src/core/tools.py`)

- MCP Python SDK 兼容的工具注册与执行
- 每个角色独立的 ToolRegistry
- 工具调用循环：LLM 决定调用 → 执行 → 结果反馈 → 最多 5 轮
- `AgentRole.add_mcp_tool(name, description, schema, handler)` 注册工具

### 6. 角色间通信 (`talk` 工具)

- pool.start() 时自动注册 talk 工具
- LLM 可通过 talk 向其他角色发送消息/委托任务
- 消息格式：`[FROM architect(王建国)] Please review PR #188`
- 目标角色按优先级插入队列
- talk 工具描述包含完整团队花名册（姓名、角色、职责、技能）

### 7. 动态角色工厂 (`src/core/role_factory.py`)

- 用人需求 → LLM 生成角色配置 → AgentRole 实例 → 注册到模板池
- 自动分配不重名的人名（24 人名字库）
- 验证必填字段：role_id、title、responsibilities、personality、skills、keywords

### 8. 事件分发器 (`src/core/dispatcher.py`)

- `trigger(event)` 将事件广播给所有角色
- 每个角色独立运行 Layer 1-3 过滤
- PASS 的事件自动转为 Task 插入对应角色队列
- 角色级关键字过滤：ceo 关注"需求/战略"，ops 关注"宕机/告警"等

### 9. 潜意识暂存区 (`src/storage/ambient_buffer.py`)

- SQLite 持久化的低显著度事件暂存
- 被 Layer 1/2 拦截的事件自动存入
- 下班时一次性取出供 LLM 总结，不浪费上班时的 Token

### 10. AI 后端 (`src/core/llm.py`)

- DeepSeek V4 Flash 模型（OpenAI 兼容 API）
- Thinking 模式（链式推理）默认开启
- 支持 DEEPSEEK_API_KEY / DEEPSEEK_MODEL / DEEPSEEK_THINKING 环境变量配置

---

## 项目结构

```
maf_scheduler/
├── src/
│   ├── core/
│   │   ├── types.py           # Event, AgentState, Priority, Journal 等数据类型
│   │   ├── event_bus.py       # 3 层事件过滤器
│   │   ├── scheduler.py       # 作息调度器（上下班流程）
│   │   ├── roles.py           # AgentRole + RolePool（多角色线程池）
│   │   ├── role_templates.py  # 12 个预定义角色模板
│   │   ├── role_factory.py    # LLM 驱动动态创建角色
│   │   ├── dispatcher.py      # 事件广播到所有角色
│   │   ├── tools.py           # MCP 工具注册与执行引擎
│   │   └── llm.py             # DeepSeek API 客户端
│   ├── storage/
│   │   ├── ambient_buffer.py  # 潜意识事件暂存（SQLite）
│   │   └── journal_store.py   # 日记持久化（JSON 文件）
│   ├── workflow/
│   │   ├── engine.py          # MAF 风格工作流图执行器
│   │   └── agent_workflow.py  # 业务工作流模板
│   ├── main.py                # 完整一天模拟（09:00→11:00→14:00→18:00）
│   ├── role_demo.py           # 多角色并发调度演示
│   ├── mcp_demo.py            # MCP 工具调用演示
│   └── talk_demo.py           # 角色间通信演示
├── .venv/                     # Python 虚拟环境
└── .gitignore
```

共 21 个 .py 文件，~3600 行代码。

---

## 快速开始

### 方式一: AgentSystem 一键启动 (推荐)

```python
from src.core.agent_system import AgentSystem
from src.core.types import Event, Priority

# 统一管理 TimeManager + RolePool + 事件总线
system = AgentSystem(role_ids=["ceo", "coo", "hr", "cfo"])
system.start()                       # 启动角色线程 + 时间线程 (Tick 0 / 第 1 天)

# 投递事件 (SHIFT_START/SHIFT_END 由时间线程自动触发)
system.trigger(Event(source="github", event_type="new_pr",
                     priority=Priority.HIGH, payload={"pr_number": 188}))

print(system.describe())             # 第 X 天, Tick Y (上班中/已下班)
system.stop()
```

### 方式二: 手动组合

```python
from src.core.dispatcher import EventDispatcher
from src.core.roles import RolePool
from src.core.role_templates import get_template
from src.core.time_manager import TimeManager

pool = RolePool()
pool.add_role(get_template("ceo"))

tm = TimeManager()
tm.set_event_sender(lambda ev: EventDispatcher(pool).trigger(ev))
tm.start()
pool.start()
```

```bash
cd maf_scheduler
source .venv/bin/activate

# 设置 API Key
export DEEPSEEK_API_KEY="sk-your-key-here"

# 运行完整一天模拟
python -m src.main

# 多角色并发调度演示
python -m src.role_demo

# MCP 工具调用演示
python -m src.mcp_demo

# 角色间通信演示
python -m src.talk_demo
```

---

## 使用示例

### 创建角色池并启动

```python
from src.core.role_templates import create_all_roles
from src.core.roles import RolePool

pool = RolePool()
for role in create_all_roles():
    pool.add_role(role)
pool.start()
```

### 为角色注册 MCP 工具

```python
architect = pool.get_role("architect")
architect.add_mcp_tool(
    name="query_db",
    description="查询数据库架构信息",
    input_schema={"type": "object", "properties": {"table": {"type": "string"}}},
    handler=lambda args: f"Table {args['table']}: 3 columns, 12000 rows",
)
```

### 广播事件

```python
from src.core.dispatcher import EventDispatcher
from src.core.types import Event, Priority

dispatcher = EventDispatcher(pool)
event = Event(source="monitoring", event_type="crash_alert",
              priority=Priority.EMERGENCY,
              payload={"title": "Production down!"})
results = dispatcher.trigger(event)
# → coder: ACCEPTED, reviewer: ACCEPTED, architect: ACCEPTED
```

### 动态创建角色

```python
from src.core.role_factory import RoleFactory

factory = RoleFactory()
new_role = factory.create_role("需要一位精通 Rust 的后端工程师，熟悉 gRPC 和 PostgreSQL")
pool.add_role(new_role)
```

### 角色间通信

```python
coder = pool.get_role("fullstack_dev")
coder.talk_to("reviewer", "请审查 PR #188", urgency="HIGH")
# → reviewer 队列收到: [FROM fullstack_dev(李明)] 请审查 PR #188
```

---

## 工具系统：Python 工具 与 MCP 工具

### Python 工具类（ToolKit）

项目内置 `src/python_tools/` 文件夹，存放 Python 实现的工具类：

```
src/python_tools/
├── __init__.py          # DEFAULT_TOOLKITS 默认工具注册表
├── talk_toolkit.py      # 通信工具类: talk, list_roles (角色间消息/名单)
├── memory_toolkit.py    # 记忆工具类: summary (总结+下班), write/edit/list/read_note
├── time_toolkit.py      # 时间工具类: get_time, take_rest (作息)
├── task_toolkit.py      # 定时任务工具类: create/list/edit/delete_task
├── hr_toolkit.py        # 人力资源工具类: post_job_posting, list_candidates (非默认)
├── client_toolkit.py    # 甲方交流工具类: talk_to_client (非默认, 通常只给 CEO)
└── examples/
    └── add_python_tool.py # 示例: 如何添加一个 Python 工具
```

**默认工具自动加载**：除 `hr`、`client` 外的所有工具类（memory/time/task）都
登记在 `src/python_tools/__init__.py` 的 `DEFAULT_TOOLKITS` 中，角色被添加进
`AgentSystem`（`auto_toolkits=True`）时自动逐个加载；`talk`/`list_roles` 由
`pool.start()` 自动注入。`hr`/`client` 需手动添加：

```python
from src.python_tools.hr_toolkit import create_hr_toolkit

hr = pool.get_role("hr")
hr.add_toolkit(create_hr_toolkit())   # 一次导入所有 HR 工具
```

### 安装 MCP 工具

MCP (Model Context Protocol) 工具来自外部服务器。本项目**不负责安装**任何服务器，用户通过 npx 自行准备。加载器使用 MCP Python SDK 连接服务器并获取工具列表，按分组规则自动分组。

**1. 用户用 npx 准备 MCP 服务器**（首次运行会自动下载包）：

```bash
# 官方常用服务器
npx -y @modelcontextprotocol/server-memory
npx -y @modelcontextprotocol/server-filesystem /tmp
npx -y @modelcontextprotocol/server-github
npx -y @modelcontextprotocol/server-git --repo /path/to/repo
```

**2. 安装 MCP Python SDK**：

```bash
cd maf_scheduler
source .venv/bin/activate
pip install mcp
```

**3. 配置要加载的服务器（只写 npx 包名）**：

编辑 `src/config/mcp_group_rules.json`：

```json
{
  "servers": [
    "@modelcontextprotocol/server-memory",
    "@modelcontextprotocol/server-filesystem",
    "@modelcontextprotocol/server-github"
  ],
  "groups": [
    {"name": "file_ops", "description": "文件操作工具组",
     "match": ["read_file", "write_file", "edit_file", "list_directory"]},
    {"name": "git_ops", "description": "Git 与代码仓库工具组",
     "match": ["git_*", "search_repos", "create_issue"]},
    {"name": "default", "description": "未分组工具默认归属", "match": []}
  ],
  "default_group": "default"
}
```

- `servers` 只列 npx 包名，加载器自动构造 `npx -y <包名>` 启动命令
- `match` 支持通配符（`git_*` 匹配所有 git_ 开头的工具）
- 未匹配的工具进入 `default_group`

**4. 加载 MCP 工具**：

```python
from src.python_tools.mcp_toolkit import MCPToolLoader, load_mcp_toolkits

# 方式一: 一次性加载 (默认读取 src/config/mcp_group_rules.json)
toolkits = load_mcp_toolkits()
# → {"file_ops": ToolKit, "git_ops": ToolKit, "default": ToolKit}

# 方式二: 管理连接生命周期 + 传递服务器附加参数
loader = MCPToolLoader(
    rules_file=None,  # 可指定自定义规则文件
    server_args={"@modelcontextprotocol/server-filesystem": ["/tmp", "/home/openclaw"]},
)
toolkits = loader.load()          # 连接服务器 + 加载工具 + 分组
loader.close()                    # 关闭所有服务器连接

# 注册到角色
dev = pool.get_role("fullstack_dev")
dev.add_toolkit(toolkits["file_ops"])   # 只导入文件操作工具组
dev.add_toolkit(toolkits["git_ops"])    # 再导入 git 工具组
```

**5. 常用 MCP 服务器**：

| 服务器 (npx 包名) | 工具示例 |
|--------|---------|
| `@modelcontextprotocol/server-github` | create_issue, search_repos |
| `@modelcontextprotocol/server-filesystem` | read_file, write_file |
| `@modelcontextprotocol/server-git` | git_status, git_commit |
| `@modelcontextprotocol/server-memory` | create_entities, create_relations |

> ⚠️ 注意: filesystem 服务器的 `search_files` 的 pattern 是**文件名 glob**
> （如 `*.py`、`**/*.md`），不是内容搜索。服务器需传授权目录参数:
> `MCPToolLoader(server_args={"@modelcontextprotocol/server-filesystem": ["/path/to/dir"]})`

**6. 工具冲突处理**：

当两个工具类（含 MCP 分组）注册了同名工具时，`ToolRegistry` 会跳过新工具并打印警告，保留先注册的版本：

```python
coding = create_coding_toolkit()      # 包含 read_file
file_tk = toolkits["file_ops"]        # MCP 也有 read_file
reg.add_toolkit(coding)               # 先注册, read_file 保留 coding 版本
reg.add_toolkit(file_tk)              # read_file 冲突 → 跳过, 保留 coding 版本
```

### HR 招聘工具示例

```python
from src.python_tools.hr_toolkit import create_hr_toolkit

hr = pool.get_role("hr")
hr.add_toolkit(create_hr_toolkit())

# 方式一: 编程式直接调用 (HR 处理 COO 的招聘申请时由 LLM 触发)
result = hr._tools.call_tool("post_job_posting", {
    "requirement": "需要一位精通 Rust 的后端工程师, 熟悉 gRPC 和 PostgreSQL",
})
# → 后台调用提示词魔术师(RoleFactory)生成新角色, 自动注册到模板池
# → 返回新角色的 role_id/姓名/职位/技能等配置

# 方式二: 通过 LLM 触发 (COO 发来招聘需求, HR 的 LLM 决定调用工具)
coo.talk_to("hr", "请发布招聘: 需要一位精通 Rust 的后端工程师", "HIGH")
```

---

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DEEPSEEK_API_KEY | (必填) | DeepSeek API 密钥 |
| DEEPSEEK_MODEL | deepseek-v4-flash | 模型名称 |
| DEEPSEEK_THINKING | true | 是否启用思考模式 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | API 地址 |
