#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档数字审计：校验文档里声明的命令数 / 用例数是否与代码实测一致。

背景：本项目的手写数字已经错过两次 ——
  - v0.1.0 的 release notes 写 "46 built-in + 35 delegated"，实测当时即为 56 / 34；
  - v0.1.2 之前 documentation 仍写 85 条用例，实测已是 89。
所以把「数字」这个最容易腐化的部分做成可执行断言，纳入 CI。

审计对象只限「描述当前状态」的文档。CHANGELOG 是历史记录，里面出现旧数字
（89 之前的 85、46/35）是正确行为，必须排除，否则会误报。

用法:
    python scripts/audit_docs.py        # 全过退出码 0，任一处不一致退出码 1
"""
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 实测基准
def measure():
    """基准值一律从代码取权威值，不用正则数 —— 我第一版用正则数 TABLE，
    因为字符类漏了 `_` 把 `expand_trig` 漏掉，算出 55（真值 56）。
    正则口径本身就会错，所以这里直接 import 拿 len()。
    """
    import importlib.util

    path = os.path.join(HERE, "mathkit.py")
    spec = importlib.util.spec_from_file_location("_audit_mathkit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    own, deleg = len(mod.TABLE), len(mod.DELEGATE)

    # selfcheck.py 在模块级就会跑测试，不能 import，只能数 CASES 列表
    sc = _read("scripts", "selfcheck.py")
    body = sc.split("CASES = [")[1].split("\n]\n")[0]
    cases = len(re.findall(r'^\s*\(\["', body, re.M))
    return own, deleg, cases


OWN, DELEG, CASES = measure()
TRUTH = {"builtin": OWN, "delegated": DELEG, "cases": CASES}

# 模式 -> 基准键。只匹配「数字 + 明确名词」，避免误伤 3000 字符、2.4 ms 之类。
PATTERNS = [
    (re.compile(r"(\d+)\s+built-in\s+commands?"), "builtin"),
    (re.compile(r"(\d+)\s+own\s+handlers?\s*\((\d+)\s+commands?\)"), "builtin"),
    (re.compile(r"(\d+)\s+commands?\s*$"), "builtin"),
    (re.compile(r"(\d+)\s+delegated"), "delegated"),
    (re.compile(r"delegate to math_tool\.py\s*\((\d+)\s+commands?"), "delegated"),
    (re.compile(r"(\d+)\s+self-check\s+cases?"), "cases"),
    (re.compile(r"(\d+)\s+cases?;"), "cases"),
    (re.compile(r"(\d+)\s+条用例"), "cases"),
]

# CHANGELOG 是历史，故意排除
TARGETS = ["README.md", "SKILL.md"] + [
    os.path.join("references", f)
    for f in sorted(os.listdir(os.path.join(ROOT, "references")))
    if f.endswith(".md")
]


def main():
    print("实测基准: builtin=%d  delegated=%d  cases=%d" % (OWN, DELEG, CASES))
    fails = 0
    checked = 0

    for rel in TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        for ln, line in enumerate(_read(rel).splitlines(), 1):
            for pat, key in PATTERNS:
                for m in pat.finditer(line):
                    for g in m.groups():
                        if g is None or not g.isdigit():
                            continue
                        val = int(g)
                        # 只校验「像命令数/用例数」的值，跳过明显无关的小数字
                        if val < 20:
                            continue
                        checked += 1
                        if val != TRUTH[key]:
                            fails += 1
                            print("FAIL  %s:%d  %s 声明 %d，实测 %d"
                                  % (rel, ln, key, val, TRUTH[key]))
                            print("      %s" % line.strip()[:120])

    # 交叉校验：总命令数声明（若有）
    if checked == 0:
        print("FAIL  一条数字声明都没扫到 —— 正则可能已与文档脱节")
        return 1

    print("\n%d 处声明已核对，%d 处不一致" % (checked, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
