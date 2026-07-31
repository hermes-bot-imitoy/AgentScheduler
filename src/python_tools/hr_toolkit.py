"""人力资源工具类 (HR ToolKit).

包含:
  - post_job_posting: 发布招聘启事. 后台将 HR 输入的需求交给"提示词魔术师"(RoleFactory)
    生成新角色的完整配置, 并注册到角色模板池中.
  - list_candidates: 列出已生成的候选人(新角色).

用法:
    from src.python_tools.hr_toolkit import create_hr_toolkit
    tk = create_hr_toolkit()
    hr_role.add_toolkit(tk)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.core.tools import ToolKit

logger = logging.getLogger(__name__)


def create_hr_toolkit(api_key: str | None = None) -> ToolKit:
    """创建人力资源工具类.

    参数:
        api_key: DeepSeek API 密钥 (可选, 默认读环境变量).

    返回:
        包含招聘相关工具的 ToolKit 实例.
    """
    tk = ToolKit(name="hr", description="人力资源工具类: 招聘, 面试, 入职")

    def _post_job_posting(args: dict[str, Any]) -> str:
        """发布招聘启事: 调用提示词魔术师生成新角色.

        参数:
            args: {"requirement": 用人需求描述, "source": 申请人来源(可选)}

        流程:
            1. HR 输入自然语言的用人需求
            2. 后台交给 RoleFactory (提示词魔术师) 生成完整角色配置
            3. 新角色自动注册到模板池
            4. 返回新角色信息给 HR 确认

        返回:
            新角色的 JSON 信息 (role_id, 姓名, 职位, 技能等).
        """
        requirement = args.get("requirement", "").strip()
        if not requirement:
            return "错误: 'requirement' (用人需求) 为必填参数."

        from src.core.role_factory import RoleFactory

        # 调用"提示词魔术师"生成新角色
        factory = RoleFactory(api_key=api_key)
        try:
            new_role = factory.create_role(requirement)
        except Exception as exc:
            logger.error("提示词魔术师生成角色失败: %s", exc)
            return f"错误: 招聘启事处理失败 - {exc}"

        # 返回新角色的完整信息
        info = {
            "role_id": new_role.role_id,
            "name": new_role.name,
            "title": new_role.title,
            "responsibilities": new_role.responsibilities,
            "personality": new_role.personality,
            "skills": new_role.skills,
            "interest_keywords": sorted(new_role.interest_keywords),
            "status": "已入职模板池",
        }
        return json.dumps(info, ensure_ascii=False, indent=2)

    def _list_candidates(args: dict[str, Any]) -> str:
        """列出模板池中的所有候选人(角色).

        参数:
            args: 无需参数.

        返回:
            所有已注册角色的列表.
        """
        from src.core.role_templates import TEMPLATES

        roles = []
        for tname, factory_fn in TEMPLATES.items():
            r = factory_fn()
            roles.append({
                "role_id": r.role_id,
                "name": r.name,
                "title": r.title,
                "skills_count": len(r.skills),
            })
        return json.dumps(roles, ensure_ascii=False, indent=2)

    tk.add_python_tool(
        name="post_job_posting",
        description=(
            "发布招聘启事. 输入用人需求, 后台会调用提示词魔术师自动生成新角色的完整配置 "
            "(包括 role_id, 姓名, 职位, 性格, 技能, 关键词), 并注册到角色模板池中. "
            "示例需求: '需要一位精通 Rust 的后端工程师, 熟悉 gRPC 和 PostgreSQL'"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "requirement": {
                    "type": "string",
                    "description": "用人需求描述 (自然语言, 尽量包含技能要求和性格偏好)",
                },
            },
            "required": ["requirement"],
        },
        handler=_post_job_posting,
    )

    tk.add_python_tool(
        name="list_candidates",
        description=(
            "列出当前角色模板池中的所有候选人 (已入职的角色), 包含 role_id, 姓名, 职位."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=_list_candidates,
    )

    return tk
