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
| CFO | 钱财 | 预算批复→Token 限额→高风险审批 |

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

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DEEPSEEK_API_KEY | (必填) | DeepSeek API 密钥 |
| DEEPSEEK_MODEL | deepseek-v4-flash | 模型名称 |
| DEEPSEEK_THINKING | true | 是否启用思考模式 |
| DEEPSEEK_BASE_URL | https://api.deepseek.com | API 地址 |
