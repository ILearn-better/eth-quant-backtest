"""Alpha 因子表达式解析引擎

世坤(WorldQuant)风格表达式 → 安全求值.
基于 Python ast 解析 (只读语法树, 不执行任意代码), 白名单校验:
    - 字段: open/high/low/close/volume/returns
    - 算子: alpha_lab.operators.OPERATORS 注册表
    - 支持: 数字 / 四则运算 / 幂 / 取负 / 括号 / 函数调用

示例:
    from alpha_lab.parser import evaluate_expression
    out = evaluate_expression("rank(ts_delta(close, 5)) - rank(ts_delta(close, 10))", data)
"""
import ast

from alpha_lab.operators import get_operator, validate_field

# 安全限制
MAX_NODES = 200         # 表达式节点上限 (防 DoS)
MAX_OPS = 30            # 算子调用次数上限


def evaluate_expression(expr, data):
    """解析并求值表达式.

    Args:
        expr: 表达式字符串, 如 "rank(ts_delta(close,5))-rank(ts_delta(close,10))"
        data: dict, 字段名 -> 数值数组 (含 open/high/low/close/volume/returns)

    Returns:
        numpy/pandas 数组 (等长)
    Raises:
        ValueError: 语法/字段/算子错误
    """
    expr = expr.strip()
    if not expr:
        raise ValueError("表达式为空")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e.msg} (位置 {e.offset})")

    # 节点安全检查
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError(f"表达式过于复杂 (节点数 > {MAX_NODES})")

    ctx = {"_op_count": 0, "fields": data}
    result = _eval_node(tree.body, ctx)
    return result


def _eval_node(node, ctx):
    """递归求值 AST 节点"""
    # 数字
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)

    # 字段引用 (Name)
    if isinstance(node, ast.Name):
        if node.id in ("True", "False", "None"):
            raise ValueError(f"不支持的常量: {node.id}")
        validate_field(node.id)
        if node.id not in ctx["fields"]:
            raise ValueError(f"字段 '{node.id}' 无数据")
        return ctx["fields"][node.id]

    # 一元运算: -x, +x
    if isinstance(node, ast.UnaryOp):
        v = _eval_node(node.operand, ctx)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return v
        raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")

    # 二元运算: + - * / **
    if isinstance(node, ast.BinOp):
        a = _eval_node(node.left, ctx)
        b = _eval_node(node.right, ctx)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            with np_err_ignore():
                return a / b
        if isinstance(node.op, ast.Pow):
            return _pow(a, b)
        if isinstance(node.op, ast.BitAnd):
            # & : 逻辑与 (0/1)
            return np.where(np.logical_and(a, b), 1.0, 0.0)
        if isinstance(node.op, ast.BitOr):
            # | : 逻辑或 (0/1)
            return np.where(np.logical_or(a, b), 1.0, 0.0)
        raise ValueError(f"不支持的运算符: {type(node.op).__name__}")

    # 比较运算: > < >= <= == != → 返回 0/1 数值
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            raise ValueError("不支持链式比较, 请用 & 连接多个条件")
        a = _eval_node(node.left, ctx)
        b = _eval_node(node.comparators[0], ctx)
        op = node.ops[0]
        with np_err_ignore():
            if isinstance(op, ast.Gt):
                cond = a > b
            elif isinstance(op, ast.Lt):
                cond = a < b
            elif isinstance(op, ast.GtE):
                cond = a >= b
            elif isinstance(op, ast.LtE):
                cond = a <= b
            elif isinstance(op, ast.Eq):
                cond = a == b
            elif isinstance(op, ast.NotEq):
                cond = a != b
            else:
                raise ValueError(f"不支持的比较运算符: {type(op).__name__}")
        return np.where(cond, 1.0, 0.0)

    # 逻辑运算: & | → 返回 0/1 数值
    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("只支持 & 和 | 逻辑运算")
        vals = [_eval_node(v, ctx) for v in node.values]
        acc = vals[0]
        for v in vals[1:]:
            with np_err_ignore():
                if isinstance(node.op, ast.And):
                    acc = np.logical_and(acc, v)
                else:
                    acc = np.logical_or(acc, v)
        return np.where(acc, 1.0, 0.0)

    # 函数调用 (算子)
    if isinstance(node, ast.Call):
        ctx["_op_count"] += 1
        if ctx["_op_count"] > MAX_OPS:
            raise ValueError(f"算子调用次数超限 (> {MAX_OPS})")
        if not isinstance(node.func, ast.Name):
            raise ValueError("只支持直接算子调用")
        op_name = node.func.id
        op_info = get_operator(op_name)
        # 参数求值: 位置参数, 可选参数留空(None)
        args = []
        for i, a in enumerate(node.args):
            if i == 0 and isinstance(a, ast.Name):
                validate_field(a.id)
                if a.id not in ctx["fields"]:
                    raise ValueError(f"字段 '{a.id}' 无数据")
                args.append(ctx["fields"][a.id])
            else:
                args.append(_eval_node(a, ctx))
        kwargs = {}
        for kw in node.keywords:
            kwargs[kw.arg] = _eval_node(kw.value, ctx)
        spec = op_info["args"]
        for i, s in enumerate(spec):
            if i >= len(args) and "=" in s:
                # 默认参数: n=5 → (n, 5)
                name, _, default = s.partition("=")
                if name not in kwargs:
                    kwargs[name] = float(default)
        return op_info["fn"](*args, **kwargs)

    raise ValueError(f"不支持的表达式元素: {type(node).__name__}")


import numpy as np
from contextlib import contextmanager


@contextmanager
def np_err_ignore():
    with np.errstate(divide="ignore", invalid="ignore"):
        yield


def _pow(a, b):
    """幂运算 (负底数+分数指数 → NaN)"""
    a = np.asarray(a, dtype=float)
    b = float(b)
    with np.errstate(invalid="ignore"):
        return np.power(a, b)
