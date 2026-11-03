#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mathkit 自检：逐条跑命令，校验返回码与关键片段。"""
import os
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.expanduser("~/.workbuddy/skills/sympy-math/scripts/math_tool.py")
HAS_CORE = os.path.isfile(CORE) and not os.environ.get("MATHKIT_NO_CORE")
PY = sys.executable
TOOL = os.path.join(HERE, "mathkit.py")

CASES = [
    # (args, 期望片段)
    (["expand", "(x+1)^3"], "x**3"),
    (["factor", "x^2-1"], "(x - 1)*(x + 1)"),
    (["simplify", "sin(x)^2+cos(x)^2"], "1"),
    (["apart", "1/(x^2-1)", "x"], "x - 1"),
    (["together", "1/x+1/(x+1)"], "2*x + 1"),
    (["cancel", "(x^2-1)/(x-1)"], "x + 1"),
    (["trigsimp", "sin(x)^2+cos(x)^2"], "1"),
    (["logcombine", "log(x)+log(x+1)"], "log(x*(x + 1))"),
    (["powsimp", "x^a*x^b"], "x**(a + b)"),
    (["radsimp", "1/(sqrt(2)+1)"], "sqrt{2}"),
    (["collect", "x*y+x*z", "x"], "x*(y + z)"),
    (["rewrite", "sin(x)", "exp"], "exp"),
    (["gcd", "x^2-1", "x-1"], "x - 1"),
    (["lcm", "x^2-1", "x-1"], "x**2 - 1"),
    (["resultant", "x^2-a", "x-a", "x"], "a**2 - a"),
    (["discriminant", "x^2+1", "x"], "-4"),
    (["groebner", "x^2+y^2-1,x-y", "x,y"], "GroebnerBasis"),
    (["roots", "x^3-1", "x"], "1"),
    (["nroots", "x^5-x+1", "--n", "6"], "-1.16730"),
    (["minpoly", "sqrt(2)+sqrt(3)", "x"], "x**4 - 10*x**2 + 1"),
    (["degree", "x^3-2*x+1"], "degree=3"),
    # 委托 math_tool
    (["diff", "sin(x)*exp(x)", "x", "2"], "2 e^{x}"),
    (["integrate", "exp(-x^2)", "x", "-oo", "oo"], "\\sqrt{\\pi}"),
    (["limit", "sin(x)/x", "x", "0"], "1"),
    (["series", "exp(x)", "x", "0", "4"], "\\frac{x^{3}}{6}"),
    (["summation", "n", "1", "10", "n^2"], "385"),
    (["product", "n", "1", "5", "n"], "120"),
    (["residue", "exp(z)/(z-1)^2", "z", "1"], "结果"),
    (["solve", "x^2-1", "x"], "x = -1"),
    (["dsolve", "Derivative(y(x),x)==y(x)", "y(x)"], "C_{1} e^{x}"),
    (["matrix", "det", "[[1,2],[3,4]]"], "-2"),
    # 新增
    (["laplace", "exp(-2*t)", "t"], "1/(s + 2)"),
    (["ilaplace", "1/(s+2)", "s"], "exp(-2*t)"),
    (["ftransform", "exp(-x^2)", "x"], "exp(-pi**2*k**2)"),
    (["contour", "1/(1+x^2)", "x"], "\\pi"),
    (["contour", "1/(2+cos(x))", "x", "--trig"], "2*sqrt(3)*pi/3"),
    (["dirac", "gamma^mu gamma^nu gamma_mu"], "-2*gamma^nu"),
    (["dirac", "pslash pslash"], "p^2"),
    (["dirac", "gamma^mu gamma^nu gamma^rho gamma^sigma", "--trace"], "g(mu,sigma)*g(nu,rho)"),
    (["dirac", "gamma5 gamma^mu gamma^nu gamma^rho gamma^sigma", "--trace"], "eps"),
    (["solveset", "sin(x)-1/2", "x", "--domain", "reals"], "ImageSet"),
    (["linsolve", "x+y-1,x-y-1", "x,y"], "(1, 0)"),
    (["nonlinsolve", "x^2+y^2-5,x-y-1", "x,y"], "(2, 1)"),
    (["rsolve", "a(n+1)-2*a(n)", "a"], "2**n"),
    (["diophantine", "2*x+3*y-5"], "t_0"),
    (["ineq", "x^2-3*x+2<0", "x"], "1 < x"),
    (["nsolve", "cos(x)-x", "x", "1"], "0.739"),
    (["linalg", "eigen", "[[1,2],[2,1]]"], "3"),
    (["linalg", "jordan", "[[1,1],[0,1]]"], "J="),
    (["linalg", "qr", "[[1,2],[3,4]]"], "Q="),
    (["linalg", "svd", "[[4,2],[2,3]]"], "sqrt"),
    (["linalg", "expm", "[[0,1],[0,0]]"], "1"),
    (["nt", "factorint", "2^32-1"], "65537"),
    (["nt", "totient", "100"], "40"),
    (["nt", "crt", "2,3,2", "3,5,7"], "(23, 105)"),
    (["nt", "cf", "sqrt(2)"], "[1, [2]]"),
    (["nt", "partitions", "10"], "42"),
    (["stats", "pdf", "normal", "0", "1", "x"], "exp(-x**2/2)"),
    (["stats", "cdf", "normal", "0", "1", "0"], "1/2"),
    (["convert", "3*kilometer/hour", "meter/second"], "0.833333"),
    (["wigner", "1", "1", "0", "0", "0", "0"], "sqrt(3)"),
    (["cg", "1/2", "1/2", "1", "1/2", "-1/2", "0"], "sqrt(2)/2"),
    (["hydrogen", "1", "0"], "2*exp(-r)"),
    (["logic", "sat", "p & ~p"], "False"),
    (["logic", "simplify", "(p|q)&(p|~q)"], "p"),
    (["latex", "x^2/2"], "\\frac{x^{2}}{2}"),
    (["codegen", "sin(x)^2", "--lang", "c"], "pow(sin(x), 2)"),
    (["expand_trig", "sin(x+y)"], "cos(y)"),
    (["sqrtdenest", "sqrt(5+2*sqrt(6))"], "sqrt(3)"),
    (["ratsimp", "(x^2-1)/(x-1)"], "x + 1"),
    (["separatevars", "x*y+x*z"], "x*(y + z)"),
    # 靶向化简：simplify 够不着的那几块
    (["refine", "sqrt(x^2)", "x:real"], "Abs(x)"),
    (["refine", "Abs(x)", "x:negative"], "-x"),
    (["fu", "sin(x)^2/(1-cos(x))"], "cos(x) + 1"),
    (["fu", "sin(x)*cos(y)", "--rule", "TR8"], "sin(x + y)"),
    (["factor_terms", "x*y+x*z"], "x*(y + z)"),
    (["trigsimp", "sin(x)^4-2*sin(x)^2*cos(x)^2+cos(x)^4", "--method", "matching"], "cos(4*x)/2"),
    # combined/groebner 依赖进程哈希种子，结果在 cos 基与 sin 基之间摆动 —— 断言只锁「告警存在」，
    # 不锁具体形状，否则这条用例本身就是 flaky 的（见过 3/5 次给 cos(4x)/2+1/2）。
    (["trigsimp", "sin(x)^4-2*sin(x)^2*cos(x)^2+cos(x)^4", "--method", "combined"], "哈希种子"),
    (["simp", "sin(x)^2/(1-cos(x))"], "cos(x) + 1"),
    (["simp", "(x^2-1)/(x-1)"], "x + 1"),
    (["simp", "(x^2-1)/(x-1)"], "可去奇点"),
    (["simp", "sin(x)*cos(x)"], "sin(2*x)/2"),
    (["perm", "order", "[2,0,1]"], "3"),
    (["perm", "cycles", "[2,0,1]"], "[0, 2, 1]"),
    (["geo", "distance", "Point(0,1)", "Line(Point(0,0),Point(1,1))"], "sqrt(2)/2"),
    (["geo", "area", "Circle(Point(0,0),1)"], "pi"),
    (["geo", "intersection", "Circle(Point(0,0),1)", "Line(Point(-2,0),Point(2,0))"], "Point2D"),
    (["neval", "sin(x)*exp(-x)", "x=1.0"], "0.30955987"),
    (["linalg", "diagonalize", "[[1,2],[2,1]]"], "D=Matrix"),
    (["linalg", "lstsq", "[[1,1],[1,2],[1,3]]", "[[1],[2],[2]]"], "2/3"),
    (["linalg", "nullity", "[[1,2,3],[2,4,6]]"], "2"),
    (["linalg", "gramschmidt", "[[1,1,0],[1,0,1]]"], "Matrix"),
    (["linalg", "adjugate", "[[1,2],[3,4]]"], "4"),
    (["pretty", "x^2+1"], "x"),
    (["se", "expand", "(x+1)^20"], "x**20"),
    (["se", "diff", "sin(x)*exp(x)", "x", "2"], "2*exp(x)*cos(x)"),
    (["to-numpy", "x^2+1", "x"], "lambdify"),
    (["evalf", "pi", "30"], "3.14159265358979323846264338328"),
    (["check", "sin(x)^2+cos(x)^2==1"], "PASS"),
    # ---- GR 张量：用有解析答案的度规锁死，数值见 SKILL.md「GR 张量」节
    (["tensor", "christoffel", "--diag", "1,sin(theta)^2", "theta,phi"],
     "-sin(theta)*cos(theta)"),          # Γ^θ_φφ
    (["tensor", "christoffel", "--first", "--diag", "1,sin(theta)^2", "theta,phi"],
     "Christoffel 第一类"),
    (["tensor", "riemann", "--diag", "1,sin(theta)^2", "theta,phi"],
     "sin(theta)**2"),                   # R^θ_φθφ = sin²θ
    (["tensor", "ricci", "--diag", "1,sin(theta)^2", "theta,phi"],
     "对账 R_μν = R_νμ → PASS"),
    (["tensor", "scalar", "--diag", "1,sin(theta)^2", "theta,phi"],
     "结果: 2\n"),                        # 单位球面 R=2
    (["tensor", "kretschmann", "--diag", "1,sin(theta)^2", "theta,phi"],
     "结果: 4\n"),                        # S²: R_abcdR^abcd = R² = 4
    (["tensor", "scalar", "--diag", "1,sin(chi)^2,sin(chi)^2*sin(theta)^2",
      "chi,theta,phi"], "结果: 6\n"),    # 单位 S³: R=6
    (["tensor", "weyl", "--diag", "1,sin(chi)^2,sin(chi)^2*sin(theta)^2",
      "chi,theta,phi"], "全部分量为零"),  # 3 维 Weyl 恒为零
    (["tensor", "scalar", "--diag", "1,r^2,r^2*sin(theta)^2", "r,theta,phi"],
     "结果: 0\n"),                        # R³ 球坐标是平坦的（易误判为球面）
    (["tensor", "geodesic", "--diag", "1,r^2", "r,phi"], "r*v_phi**2"),
    (["tensor", "metric", "--diag", "1,sin(theta)^2", "theta,phi"],
     "det(g) = sin(theta)**2"),
    # Schwarzschild：真空但曲率非零，一条同时验证 Riemann/指标升降/真空性
    (["tensor", "ricci", "--diag", "-(1-2*M/r),1/(1-2*M/r),r^2,r^2*sin(theta)^2",
      "t,r,theta,phi"], "真空解"),
    (["tensor", "kretschmann", "--diag", "-(1-2*M/r),1/(1-2*M/r),r^2,r^2*sin(theta)^2",
      "t,r,theta,phi"], "48*M**2/r**6"),
    (["--json", "expand", "x^2"], '"ok": true'),
]

DELEGATED = {"diff", "integrate", "limit", "series", "summation", "product", "residue",
             "dsolve", "solve", "matrix", "evalf", "check", "latex"}

fails = 0
skipped = 0
for args, expect in CASES:
    cmd = next((a for a in args if not a.startswith("--")), "")
    if not HAS_CORE and cmd in DELEGATED:
        skipped += 1
        continue
    cmd = [PY, TOOL] + args
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    ok = (p.returncode == 0) and (expect in out)
    if not ok:
        fails += 1
        print(f"FAIL  {' '.join(args[:4])!r}  rc={p.returncode}  期望含 {expect!r}")
        print("      got:", out.strip().replace("\n", " | ")[:220])
total = len(CASES) - skipped
print(f"\n{total - fails}/{total} passed" + (f"  ({skipped} skipped: math_tool 不在 PATH 上)" if skipped else ""))
sys.exit(1 if fails else 0)
