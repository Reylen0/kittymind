"""execute_batch 分区并行测试。

钉住三件事：
  1. **分区规则**：连续的 is_concurrency_safe=True 调用并行、非安全调用是串行屏障；
     未声明安全的工具一律串行（fail-closed）。
  2. **声明序不变式**：结果按 tool_calls 声明顺序返回，与模型预期的回灌顺序一致。
  3. **守护栏记账顺序**：跨屏障的前序结果对后续决策可见（同批连续失败逐个升级）。

另附一条分类一致性测试：内置工具的 is_concurrency_safe 必须与 guardrails 的
IDEMPOTENT/MUTATING 分类对齐，且 14 个工具全部被分类覆盖。
"""

import json
import time


from kittymind.tools.base import BaseTool, ToolResult
from kittymind.tools.executor import ToolExecutor, TurnContext


# ── 测试基建 ─────────────────────────────────────────────────────────

class _ProbeTool(BaseTool):
    """可配置的假工具：记录执行起止时间线、可注入结果与并发安全标记。

    run 被覆写以绕过 param_class 解析；time.sleep 在 to_thread 的 worker 里执行，
    两个并行的 probe 必然发生线程级重叠。
    """

    name: str = "probe"
    description: str = "test"

    def __init__(self, name="probe", result="ok", ok=True, safe=False,
                 dur=0.06, timeline=None):
        super().__init__()
        self.name = name
        self._result, self._ok, self._safe = result, ok, safe
        self._dur, self._timeline = dur, timeline

    @property
    def is_concurrency_safe(self) -> bool:
        return self._safe

    def execute(self, parameters):
        raise AssertionError("测试不应走到抽象 execute")

    def run(self, args: dict) -> ToolResult:
        if self._timeline is not None:
            self._timeline.append(("start", time.monotonic()))
        time.sleep(self._dur)
        if self._timeline is not None:
            self._timeline.append(("end", time.monotonic()))
        return ToolResult(self._ok, self._result)


class _MapRegistry:
    def __init__(self, tools: dict):
        self._tools = tools

    def get(self, name):
        if name not in self._tools:
            raise KeyError(name)
        return self._tools[name]


def _call(name: str, args: dict | None = None, call_id: str | None = None) -> dict:
    return {
        "id": call_id or f"call_{name}",
        "function": {"name": name, "arguments": json.dumps(args or {})},
    }


def _max_concurrency(timeline: list[tuple[str, float]]) -> int:
    cur = peak = 0
    for kind, _ts in sorted(timeline, key=lambda e: e[1]):
        cur += 1 if kind == "start" else -1
        peak = max(peak, cur)
    return peak


# ── 分区规则 ─────────────────────────────────────────────────────────

async def test_consecutive_safe_tools_run_in_parallel():
    """连续三个安全工具 → 同一并行区，执行线程重叠。"""
    timeline: list[tuple[str, float]] = []
    reg = _MapRegistry({
        f"read{i}": _ProbeTool(name=f"read{i}", safe=True, timeline=timeline)
        for i in range(3)
    })
    executor = ToolExecutor(reg)
    results = await executor.execute_batch(
        [_call(f"read{i}") for i in range(3)], ctx=TurnContext()
    )
    assert [r["tool_call_id"] for r in results] == [f"call_read{i}" for i in range(3)]
    assert _max_concurrency(timeline) == 3, "安全工具应当并行执行"


async def test_unsafe_tool_is_serial_barrier():
    """[safe, unsafe, safe]：两个 safe 被 unsafe 隔开，不得并行。"""
    timeline: list[tuple[str, float]] = []
    reg = _MapRegistry({
        "read1": _ProbeTool(name="read1", safe=True, timeline=timeline),
        "bash": _ProbeTool(name="bash", safe=False, timeline=timeline),
        "read2": _ProbeTool(name="read2", safe=True, timeline=timeline),
    })
    executor = ToolExecutor(reg)
    results = await executor.execute_batch(
        [_call("read1"), _call("bash"), _call("read2")], ctx=TurnContext()
    )
    assert [r["tool_call_id"] for r in results] == ["call_read1", "call_bash", "call_read2"]
    assert _max_concurrency(timeline) == 1, "变更工具必须成为并行屏障"


async def test_undeclared_tool_defaults_to_serial():
    """未声明 is_concurrency_safe 的工具（fail-closed 默认 False）一律串行。"""
    timeline: list[tuple[str, float]] = []
    # _ProbeTool 显式传 safe=False；这里用第三个「既不 True 也不 False 声明」的裸子类
    class _Undeclared(_ProbeTool):
        pass

    reg = _MapRegistry({
        "u1": _Undeclared(name="u1", timeline=timeline),
        "u2": _Undeclared(name="u2", timeline=timeline),
    })
    executor = ToolExecutor(reg)
    await executor.execute_batch([_call("u1"), _call("u2")], ctx=TurnContext())
    assert _max_concurrency(timeline) == 1, "未声明安全的工具必须串行"


async def test_unknown_tool_errors_without_killing_batch():
    """registry 未命中的调用报错为该调用结果，同批其它调用不受影响。"""
    reg = _MapRegistry({
        "read": _ProbeTool(name="read", safe=True, result="fine"),
    })
    executor = ToolExecutor(reg)
    results = await executor.execute_batch(
        [_call("no_such_tool"), _call("read")], ctx=TurnContext()
    )
    assert "no_such_tool" in results[0]["content"] or "KeyError" in results[0]["content"]
    assert results[1]["content"] == "fine"


# ── 守护栏记账顺序 ──────────────────────────────────────────────────

async def test_sequential_barrier_preserves_guardrail_escalation():
    """同批两个相同失败调用（串行语义）→ 第二个触发守护栏 warn。

    这条钉住「乱序执行、顺序记账」的下界：屏障内的串行路径必须与旧的逐个
    execute 行为一致（exact_failure_warn 阈值 = 2）。
    """
    from kittymind.tools.guardrails import GuardrailController

    reg = _MapRegistry({
        "bash": _ProbeTool(name="bash", result="Error: fail", ok=False, safe=False),
    })
    executor = ToolExecutor(reg, guardrail=GuardrailController(interactive=True))
    call = _call("bash", args={"cmd": "x"})
    results = await executor.execute_batch([call, dict(call, id="call_b")], ctx=TurnContext())
    assert "守护栏警告" not in results[0]["content"]
    assert "守护栏警告" in results[1]["content"], "第二次相同失败必须升级为 warn"


# ── 决策段拦截 ───────────────────────────────────────────────────────

async def test_permission_denied_short_circuits_execution():
    """权限拒绝 → 该调用返回合成结果且工具体不执行，同批其它调用照常。"""
    executed: list[str] = []

    class _Spy(_ProbeTool):
        def run(self, args: dict) -> ToolResult:
            executed.append(self.name)
            return super().run(args)

    from kittymind.config import cfg
    from kittymind.tools import permission as perm_mod

    async def _deny(tool_name, args, reason):
        return False

    reg = _MapRegistry({
        "bash": _Spy(name="bash", safe=False),
        "read": _Spy(name="read", safe=True, result="fine"),
    })
    executor = ToolExecutor(reg, ask_fn=_deny)

    # 让 bash 命中审批规则：直接替换 _RULES 代价太大，改用 monkeypatch 式的
    # 局部规则注入——这里走 check_permission 的真实路径，用 rm 命令触发。
    original_rules = perm_mod._RULES
    perm_mod._RULES = [
        ({"bash"}, lambda args: True, "测试规则：全部审批"),
        *original_rules,
    ]
    try:
        results = await executor.execute_batch(
            [_call("bash", {"command": "ls"}), _call("read")], ctx=TurnContext()
        )
    finally:
        perm_mod._RULES = original_rules

    assert "bash" not in executed, "被拒绝的调用不得执行工具体"
    assert "Permission denied" in results[0]["content"]
    assert results[1]["content"] == "fine"
    assert cfg is not None  # 保持导入不失效


# ── 非法参数：fail-closed ────────────────────────────────────────────

class _ArgRecorder(_ProbeTool):
    """记录真实收到的 args，用于断言"工具是否执行、用什么参数执行"。"""

    def __init__(self, sink: list[dict], **kwargs):
        super().__init__(**kwargs)
        self._sink = sink

    def run(self, args: dict) -> ToolResult:
        self._sink.append(args)
        return ToolResult(True, "ok")


def _raw_call(name: str, arguments, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


async def test_invalid_arguments_are_rejected_without_running_tool():
    """参数不是合法 JSON → 拒绝执行，绝不带着兜底的 {} 跑。

    旧实现 `except: args = {}`：工具照常执行，而守护栏与权限判定同样按 args
    做判断——静默失败等于既绕过参数检查、又真的执行了一次错误调用。
    """
    seen: list[dict] = []
    executor = ToolExecutor(_MapRegistry({"probe": _ArgRecorder(seen, name="probe")}))

    results = await executor.execute_batch(
        [_raw_call("probe", '{"a": 1,')], ctx=TurnContext()
    )

    assert seen == [], "参数解析失败时工具不得执行"
    assert len(results) == 1 and results[0]["tool_call_id"] == "c1"
    assert results[0]["role"] == "tool" and "JSON" in results[0]["content"]


async def test_empty_and_missing_arguments_are_legitimate():
    """无参工具（arguments 缺省 / 空串）是合法调用，不能被误伤。"""
    seen: list[dict] = []
    executor = ToolExecutor(_MapRegistry({"probe": _ArgRecorder(seen, name="probe")}))

    await executor.execute_batch([
        _raw_call("probe", None, "c1"),
        _raw_call("probe", "", "c2"),
    ], ctx=TurnContext())

    assert seen == [{}, {}]


async def test_non_object_arguments_are_rejected():
    """arguments 是合法 JSON 但不是对象（如数组）同样拒绝：工具体只吃对象。"""
    seen: list[dict] = []
    executor = ToolExecutor(_MapRegistry({"probe": _ArgRecorder(seen, name="probe")}))

    results = await executor.execute_batch(
        [_raw_call("probe", "[1, 2]")], ctx=TurnContext()
    )

    assert seen == [] and "JSON" in results[0]["content"]


async def test_valid_arguments_reach_tool_unchanged():
    seen: list[dict] = []
    executor = ToolExecutor(_MapRegistry({"probe": _ArgRecorder(seen, name="probe")}))

    await executor.execute_batch([_call("probe", {"x": [1, 2]})], ctx=TurnContext())

    assert seen == [{"x": [1, 2]}]


# ── 分类一致性（真实内置工具）───────────────────────────────────────

def test_builtin_concurrency_flags_match_guardrail_classification():
    """内置工具的 is_concurrency_safe 必须与 guardrails 分类对齐且全覆盖。"""
    from kittymind.tools import builtin as B
    from kittymind.tools.guardrails import IDEMPOTENT_TOOLS, MUTATING_TOOLS

    classes = {
        cls.name: cls for cls in (
            B.BashTool, B.ClipboardTool, B.FileEditTool, B.FileReadTool,
            B.FileWriteTool, B.GetCurrentTimeTool, B.GitTool, B.GlobTool,
            B.GrepTool, B.LsTool, B.ScreenshotTool, B.TaskTool,
            B.VerifyTool, B.WriteMemoryTool,
        )
    }
    # 覆盖：每个注册工具必须被分类（漏一个 = 并发调度或守护栏统计的盲区）
    assert set(classes) == IDEMPOTENT_TOOLS | MUTATING_TOOLS, \
        "存在未被分类的内置工具"
    assert set() == IDEMPOTENT_TOOLS & MUTATING_TOOLS, "两个分类必须互斥"
    # 对齐：只读（幂等）工具才允许并行
    for name, cls in classes.items():
        expected = name in IDEMPOTENT_TOOLS
        assert cls.is_concurrency_safe is expected, \
            f"{name}: is_concurrency_safe={cls.is_concurrency_safe}，与分类不符"
