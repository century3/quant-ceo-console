#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 biz-skill-creator 技能的完整流程

模拟场景: 业务人员要沉淀"债券信用评估"经验
测试内容:
1. 验证 biz-skill-creator 自身的 SKILL.md 结构完整
2. 用模拟数据走完 Step 4 生成流程
3. 验证生成的技能文件结构和内容
"""
import os
import sys
import textwrap
from pathlib import Path

if sys.platform == "win32":
    os.environ.setdefault("PYTHONUTF8", "1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WORKSPACE = Path(__file__).resolve().parent
SKILL_DIR = WORKSPACE / "skills" / "biz-skill-creator"


def test_skill_structure():
    """验证 biz-skill-creator 自身的文件结构"""
    print("=" * 60)
    print("Step 0: 验证 biz-skill-creator 技能文件结构")
    print("=" * 60)

    required_files = [
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "references" / "skill-template.md",
        SKILL_DIR / "references" / "interview-guide.md",
    ]

    all_ok = True
    for f in required_files:
        exists = f.exists()
        status = "OK" if exists else "MISSING"
        print(f"  [{status}] {f.relative_to(WORKSPACE)}")
        if not exists:
            all_ok = False

    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    checks = [
        ("name: biz-skill-creator" in skill_md, "frontmatter name"),
        ("description:" in skill_md, "frontmatter description"),
        ("Step 1:" in skill_md or "Step 1: " in skill_md
         or "场景定位" in skill_md, "Step 1 存在"),
        ("Step 2:" in skill_md or "Step 2: " in skill_md
         or "示例采集" in skill_md, "Step 2 存在"),
        ("Step 3:" in skill_md or "Step 3: " in skill_md
         or "规则提炼" in skill_md, "Step 3 存在"),
        ("Step 4:" in skill_md or "Step 4: " in skill_md
         or "生成" in skill_md, "Step 4 存在"),
    ]
    for passed, label in checks:
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] SKILL.md -> {label}")
        if not passed:
            all_ok = False

    return all_ok


def simulate_step1():
    """模拟 Step 1: 场景定位"""
    print("\n" + "=" * 60)
    print("Step 1: 场景定位 (模拟业务人员回答)")
    print("=" * 60)

    scenario = {
        "what": "帮投资经理评估某只债券的信用风险，判断是否值得投资",
        "when": [
            "客户主动问某只债券能不能买",
            "新债发行需要快速出信用评估",
            "存量债券的发行人信用评级发生变动",
        ],
        "input": "公司名称/债券代码",
        "output": "信用评估结论（建议关注/建议规避 + 理由 + 风险点）",
    }

    print(f"  做什么: {scenario['what']}")
    print(f"  什么时候做: {', '.join(scenario['when'])}")
    print(f"  输入: {scenario['input']} -> 输出: {scenario['output']}")

    return scenario


def simulate_step2():
    """模拟 Step 2: 示例采集"""
    print("\n" + "=" * 60)
    print("Step 2: 示例采集 (模拟真实案例)")
    print("=" * 60)

    example = {
        "title": "XX 城投债信用评估",
        "input": "客户问：'XX 城投 2026 年到期的那只债能不能买？'",
        "steps": [
            "查询 XX 城投的最新信用评级（中诚信、联合资信），确认当前评级为 AA",
            "下载 XX 城投最近一期年报，提取关键指标：资产负债率(62%)、经营性现金流(+3.2亿)、有息负债规模(85亿)",
            "查询 XX 城投所在地区的财政数据：一般公共预算收入、政府性基金收入、地方债务率",
            "横向对比：与同评级(AA)、同地区的其他城投平台比较资产负债率和现金流指标",
            "综合判断：评级稳定、现金流为正、区域财政尚可，但有息负债规模偏高需关注",
        ],
        "output": textwrap.dedent("""\
            信用评估结论：建议关注（中性偏积极）

            主体概况：XX 城投为某市核心平台，当前评级 AA（稳定）。
            财务分析：资产负债率 62%，经营性现金流 +3.2 亿，有息负债 85 亿。
            区域分析：所在地区一般公共预算收入 XX 亿，债务率处于中等水平。
            横向对比：资产负债率低于同评级中位数(65%)，现金流优于中位数。
            主要风险：有息负债规模较大，需关注再融资压力和政策变化。
            建议：可适度配置，但需持续跟踪区域财政和再融资进展。"""),
    }

    print(f"  案例: {example['title']}")
    print(f"  输入: {example['input']}")
    for i, step in enumerate(example["steps"], 1):
        print(f"  步骤{i}: {step}")
    print(f"  输出: {example['output'][:80]}...")

    return example


def simulate_step3():
    """模拟 Step 3: 规则提炼"""
    print("\n" + "=" * 60)
    print("Step 3: 规则提炼 (模拟关键规则)")
    print("=" * 60)

    rules = {
        "must_do": [
            "必须使用最近一期的财务数据（不超过两个季度）",
            "必须交叉验证至少两个评级机构的评级结果",
            "信用评估结论必须包含：评级信息、关键财务指标、风险点、投资建议",
        ],
        "must_not": [
            "不能仅凭单一指标（如 ROE 或资产负债率）下结论",
            "不能使用非官方披露的财务数据",
            "不能忽略区域风险因素（地方财政、政策变化）",
        ],
        "references": [
            "中诚信/联合资信等评级机构的最新评级报告",
            "发行人最近一期年报或半年报",
            "地方财政数据（财政部/各省财政厅官网）",
        ],
    }

    print("  必须做:")
    for r in rules["must_do"]:
        print(f"    - {r}")
    print("  不能做:")
    for r in rules["must_not"]:
        print(f"    - {r}")
    print("  参考文档:")
    for r in rules["references"]:
        print(f"    - {r}")

    return rules


def generate_skill(scenario, example, rules):
    """模拟 Step 4: 生成技能文件"""
    print("\n" + "=" * 60)
    print("Step 4: 生成技能文件")
    print("=" * 60)

    skill_name = "bond-credit-review"
    output_dir = WORKSPACE / "skills" / skill_name
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "references").mkdir(parents=True, exist_ok=True)

    when_list = "、".join(scenario["when"])
    must_do_lines = "\n".join(f"- {r}" for r in rules["must_do"])
    must_not_lines = "\n".join(f"- {r}" for r in rules["must_not"])
    ref_lines = "\n".join(f"- {r}" for r in rules["references"])

    steps_lines = "\n".join(
        f"{i}. {s}" for i, s in enumerate(example["steps"], 1)
    )

    ref_bullet_lines = "\n".join(f"- {r}" for r in rules["references"])

    skill_md_content = (
        f"---\n"
        f"name: {skill_name}\n"
        f'description: "{scenario["what"]}。当{when_list}时使用。"\n'
        f"---\n\n"
        f"# 债券信用评估\n\n"
        f"## 操作流程\n\n"
        f"{steps_lines}\n"
        f"6. 综合以上信息，输出信用评估结论（参考 references/examples.md 中的输出格式）\n\n"
        f"## 关键规则\n\n"
        f"### 必须遵守\n{must_do_lines}\n\n"
        f"### 禁止事项\n{must_not_lines}\n\n"
        f"## 参考文档\n\n"
        f"- 示例案例与输出格式: [examples.md](references/examples.md)\n"
        f"{ref_bullet_lines}\n"
    )

    examples_md_content = (
        f"# 示例案例\n\n"
        f"## 案例 1: {example['title']}\n\n"
        f"### 输入\n{example['input']}\n\n"
        f"### 执行步骤\n{steps_lines}\n\n"
        f"### 输出\n{example['output']}\n"
    )

    skill_md_path = output_dir / "SKILL.md"
    examples_md_path = output_dir / "references" / "examples.md"

    skill_md_path.write_text(skill_md_content, encoding="utf-8")
    examples_md_path.write_text(examples_md_content, encoding="utf-8")

    print(f"  [OK] 已创建 skills/{skill_name}/SKILL.md")
    print(f"  [OK] 已创建 skills/{skill_name}/references/examples.md")

    return output_dir, skill_name


def validate_output(output_dir, skill_name):
    """验证生成的技能文件"""
    print("\n" + "=" * 60)
    print("验证生成结果")
    print("=" * 60)

    all_ok = True

    skill_md = output_dir / "SKILL.md"
    examples_md = output_dir / "references" / "examples.md"

    for f in [skill_md, examples_md]:
        exists = f.exists()
        status = "OK" if exists else "FAIL"
        print(f"  [{status}] {f.relative_to(WORKSPACE)} 存在")
        if not exists:
            all_ok = False

    content = skill_md.read_text(encoding="utf-8")
    checks = [
        (f"name: {skill_name}" in content, "name 字段正确"),
        ("description:" in content, "description 字段存在"),
        ("操作流程" in content, "操作流程章节存在"),
        ("必须遵守" in content, "必须遵守章节存在"),
        ("禁止事项" in content, "禁止事项章节存在"),
        ("参考文档" in content, "参考文档章节存在"),
        ("examples.md" in content, "引用了 examples.md"),
    ]

    for passed, label in checks:
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] SKILL.md -> {label}")
        if not passed:
            all_ok = False

    ex_content = examples_md.read_text(encoding="utf-8")
    ex_checks = [
        ("案例 1" in ex_content, "包含案例标题"),
        ("输入" in ex_content, "包含输入部分"),
        ("执行步骤" in ex_content or "步骤" in ex_content, "包含步骤部分"),
        ("输出" in ex_content, "包含输出部分"),
    ]

    for passed, label in ex_checks:
        status = "OK" if passed else "FAIL"
        print(f"  [{status}] examples.md -> {label}")
        if not passed:
            all_ok = False

    return all_ok


def show_generated_skill(output_dir):
    """展示生成的 SKILL.md 内容"""
    print("\n" + "=" * 60)
    print("生成的 SKILL.md 内容预览")
    print("=" * 60)

    content = (output_dir / "SKILL.md").read_text(encoding="utf-8")
    print(content)


def main():
    print("biz-skill-creator 完整流程测试")
    print("模拟场景: 业务人员沉淀'债券信用评估'经验\n")

    ok0 = test_skill_structure()
    if not ok0:
        print("\n[FAIL] biz-skill-creator 自身结构不完整，中止测试")
        return 1

    scenario = simulate_step1()
    example = simulate_step2()
    rules = simulate_step3()
    output_dir, skill_name = generate_skill(scenario, example, rules)

    show_generated_skill(output_dir)

    ok1 = validate_output(output_dir, skill_name)

    print("\n" + "=" * 60)
    if ok0 and ok1:
        print("测试结果: ALL PASSED")
        print(f"生成的技能目录: skills/{skill_name}/")
    else:
        print("测试结果: SOME CHECKS FAILED")
    print("=" * 60)

    return 0 if (ok0 and ok1) else 1


if __name__ == "__main__":
    sys.exit(main())
