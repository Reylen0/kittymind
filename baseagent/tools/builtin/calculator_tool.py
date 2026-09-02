import ast
import operator

from pydantic import BaseModel, Field

from ..base import BaseTool

_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量类型: {type(node.value)}")
    if isinstance(node, ast.BinOp):
        op = _OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的一元运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式节点: {type(node).__name__}")


class CalculatorParam(BaseModel):
    expression: str = Field(description="数学表达式，例如 2 + 3 * 4")


class CalculatorTool(BaseTool):
    """数学表达式计算工具"""

    name: str = "calculator"
    description: str = "计算数学表达式，支持 +、-、*、/、//、%、** 运算"
    param_class = CalculatorParam

    def execute(self, parameters: CalculatorParam) -> str:
        try:
            tree = ast.parse(parameters.expression.strip(), mode="eval")
            result = _safe_eval(tree.body)
            # 整数结果去掉小数点
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            return str(result)
        except ZeroDivisionError:
            return "错误: 除数不能为零"
        except Exception as e:
            return f"错误: {e}"
