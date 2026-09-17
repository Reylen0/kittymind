"""配置值不得在 import 期被钉死（报告 §3.3 回归）。

`cfg` 是模块级单例，但 `cfg.X` 写错位置就会在 import 时**求值一次并固化**：
写成模块常量 / 类属性 / pydantic 字段默认值 / 函数默认参数都算。后果是设置面板
保存后调用的 `cfg.reload()` 形同虚设——必须重启进程才生效（`reload()` 无调用方，
所以现象更隐蔽：用户以为改了，其实没改）。

两条线钉住：
  1. **AST 静态扫描**——覆盖全部源文件，不依赖目标模块能否被实例化（比逐个 import 更可靠）；
  2. **行为验证**——reload 之后新值真的被读到，而不只是"没有静态违规"。
"""

import ast
import json
from pathlib import Path

import pytest

from kittymind import config as config_mod
from kittymind.config import _OVERRIDABLE, cfg

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("kittymind", "server")
SCAN_EXTRA_FILES = ("chat.py", "chat_async.py")


# ── ① AST 静态扫描 ───────────────────────────────────────────────

def _cfg_attr(node: ast.AST | None) -> str | None:
    """node 形如 `cfg.X` 时返回 'X'，否则 None。"""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "cfg"
    ):
        return node.attr
    return None


def _scan_body(body: list[ast.stmt], where: str, out: list[tuple]) -> None:
    """扫一层作用域（模块体 / 类体）里的 import 期赋值。"""
    for stmt in body:
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        value = stmt.value
        if value is None:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]

        direct = _cfg_attr(value)
        if direct:
            for t in targets:
                name = getattr(t, "id", None) or getattr(t, "attr", "?")
                out.append((where, name, direct, stmt.lineno))

        # 赋值里嵌套的 cfg，例如 pydantic 的 Field(default=cfg.X)
        for sub in ast.walk(value):
            if not isinstance(sub, ast.Call):
                continue
            for kw in sub.keywords:
                attr = _cfg_attr(kw.value)
                if attr:
                    func = getattr(sub.func, "id", None) or getattr(sub.func, "attr", "?")
                    out.append((where, f"{func}({kw.arg}=)", attr, stmt.lineno))


def find_import_time_captures() -> list[tuple[str, str, str, int]]:
    """返回全部 (位置, 名字, cfg 属性, 行号)。"""
    hits: list[tuple[str, str, str, int]] = []
    files: list[Path] = []
    for d in SCAN_DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    files += [ROOT / f for f in SCAN_EXTRA_FILES]

    for path in files:
        if not path.exists() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        _scan_body(tree.body, f"{rel}:<module>", hits)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                _scan_body(node.body, f"{rel}:class {node.name}", hits)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
                for default in defaults:
                    attr = _cfg_attr(default)
                    if attr:
                        hits.append((f"{rel}:{node.name}()", "参数默认值", attr, node.lineno))
    return hits


def test_no_reloadable_config_is_captured_at_import_time():
    """可覆盖配置项（在 _OVERRIDABLE 内）绝不能在 import 期求值。

    不可覆盖的常量（SKIP_DIRS / BINARY_EXTS / WORKSPACES_FILE 等）是常量而非配置，
    模块级捕获无害，故不在检查范围——检查范围精确等于"reload 改得动的那些 key"。
    """
    bad = [h for h in find_import_time_captures() if h[2] in _OVERRIDABLE]
    detail = "\n".join(
        f"  {where}:{line}  {name}  <- cfg.{attr}"
        for where, name, attr, line in sorted(bad, key=lambda h: (h[0], h[3]))
    )
    assert not bad, (
        "以下位置在 import 期把可覆盖配置钉死了，cfg.reload() 之后不会生效：\n"
        f"{detail}\n\n"
        "修法：\n"
        "  模块常量      → 用到处直接读 cfg.X\n"
        "  pydantic 字段 → Field(default_factory=lambda: cfg.X)\n"
        "  函数默认参数  → 传 None 哨兵，函数体内 `cfg.X if arg is None else arg`\n"
        "  类属性        → 改成 @property"
    )


def test_scanner_is_actually_working():
    """防止扫描范围被改坏：扫到空目录时上一条测试会"虚假通过"。"""
    scanned = list((ROOT / "kittymind").rglob("*.py"))
    assert len(scanned) > 40, f"只扫到 {len(scanned)} 个文件，扫描范围可能被改坏"

    hits = find_import_time_captures()
    # 不可覆盖的常量应仍被扫到 —— 证明扫描器确实在读源码，而不是什么都没找到
    assert hits, "扫描结果为空，扫描器可能已失效"
    assert any(h[2] in {"SKIP_DIRS", "BINARY_EXTS", "WORKSPACES_FILE"} for h in hits)


# ── ② 行为验证：reload 之后新值真的被读到 ───────────────────────

@pytest.fixture
def reload_cfg(tmp_path, monkeypatch):
    """把 settings.json 指向临时文件，返回 apply(values) → 写文件并 cfg.reload()。"""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config_mod, "_SETTINGS_FILE", path)
    saved = {k: getattr(cfg, k) for k in _OVERRIDABLE if hasattr(cfg, k)}

    def _apply(values: dict) -> None:
        path.write_text(json.dumps(values), encoding="utf-8")
        cfg.reload()

    yield _apply

    for key, value in saved.items():
        setattr(cfg, key, value)


def test_tool_param_defaults_follow_reload(reload_cfg):
    """pydantic 字段默认值必须跟着 reload 走（原先在类体里被钉死）。"""
    from kittymind.tools.builtin.bash_tool import BashToolParam
    from kittymind.tools.builtin.git_tool import GitToolParam
    from kittymind.tools.builtin.grep_tool import GrepToolParam
    from kittymind.tools.builtin.ls_tool import LsToolParam
    from kittymind.tools.builtin.screenshot_tool import ScreenshotToolParam
    from kittymind.tools.builtin.verify_tool import VerifyToolParam

    reload_cfg({
        "BASH_TIMEOUT": 7,
        "GIT_LOG_COUNT": 3,
        "GREP_CONTEXT_LINES": 9,
        "LS_DEPTH": 4,
        "SCREENSHOT_MONITOR": 2,
        "VERIFY_TIMEOUT": 11,
        "VERIFY_PROBE_RETRIES": 6,
        "VERIFY_PROBE_INTERVAL": 2.5,
    })

    assert BashToolParam(command="ls").timeout == 7
    assert GitToolParam(action="log").count == 3
    assert GrepToolParam(pattern="x").context_lines == 9
    assert LsToolParam().depth == 4
    assert ScreenshotToolParam().monitor == 2
    verify = VerifyToolParam()
    assert verify.timeout == 11
    assert verify.retries == 6
    assert verify.interval == pytest.approx(2.5)


def test_explicit_argument_still_wins_over_reload(reload_cfg):
    """显式传参必须优先于配置默认值。"""
    from kittymind.tools.builtin.bash_tool import BashToolParam

    reload_cfg({"BASH_TIMEOUT": 7})
    assert BashToolParam(command="ls", timeout=99).timeout == 99


def test_tool_run_limits_follow_reload(reload_cfg, tmp_path):
    """运行期上限（不只是参数默认值）也要跟着 reload 走。"""
    from kittymind.tools.builtin.bash_tool import bash_cwd
    from kittymind.tools.builtin.file_read_tool import FileReadTool
    from kittymind.tools.builtin.file_write_tool import FileWriteTool

    token = bash_cwd.set(str(tmp_path))
    try:
        (tmp_path / "big.txt").write_text(
            "\n".join(f"line{i}" for i in range(50)), encoding="utf-8"
        )

        reload_cfg({"FILE_READ_MAX_LINES": 5})
        out = FileReadTool().run({"path": "big.txt"})
        assert "已截断至 5 行" in out.content

        reload_cfg({"FILE_READ_MAX_LINES": 20})
        out = FileReadTool().run({"path": "big.txt"})
        assert "已截断至 20 行" in out.content

        reload_cfg({"FILE_WRITE_MAX_BYTES": 20})
        out = FileWriteTool().run({"path": "x.txt", "content": "y" * 500})
        assert not out.ok and "超过" in out.content
    finally:
        bash_cwd.reset(token)


def test_error_message_quotes_actual_limit(reload_cfg, tmp_path):
    """错误文案必须引用真实上限，不能硬编码 '1MB' / '800KB'（改配置后会说谎）。"""
    from kittymind.tools.builtin.bash_tool import bash_cwd
    from kittymind.tools.builtin.file_write_tool import FileWriteTool

    token = bash_cwd.set(str(tmp_path))
    try:
        reload_cfg({"FILE_WRITE_MAX_BYTES": 2000})
        out = FileWriteTool().run({"path": "x.txt", "content": "y" * 5000})
        assert "2KB" in out.content, out.content
        assert "1MB" not in out.content
    finally:
        bash_cwd.reset(token)


def test_memory_recall_limits_follow_reload(reload_cfg):
    """类属性改 property 之后，召回上限跟着 reload 走。"""
    from kittymind.memory.recall import MemoryRecall

    recall = MemoryRecall(memory_store=None, llm=None)
    reload_cfg({"MEMORY_RECALL_MAX_RELEVANT": 2, "MEMORY_RECALL_MAX_BODY_CHARS": 30})
    assert recall.MAX_RELEVANT == 2
    assert recall.MAX_BODY_CHARS == 30

    reload_cfg({"MEMORY_RECALL_MAX_RELEVANT": 7, "MEMORY_RECALL_MAX_BODY_CHARS": 90})
    assert recall.MAX_RELEVANT == 7
    assert recall.MAX_BODY_CHARS == 90


def test_kitty_agent_max_iterations_follows_reload(reload_cfg):
    """原先写成函数默认参数，import 期就固化了。"""
    from unittest.mock import MagicMock

    from kittymind.agent.kitty_agent import KittyAgent

    reload_cfg({"AGENT_MAX_ITERATIONS": 3})
    agent = KittyAgent(name="t", llm=MagicMock())
    assert agent.max_iterations == 3

    reload_cfg({"AGENT_MAX_ITERATIONS": 12})
    assert KittyAgent(name="t", llm=MagicMock()).max_iterations == 12
    # 显式传参仍然优先
    assert KittyAgent(name="t", llm=MagicMock(), max_iterations=5).max_iterations == 5


def test_llm_temperature_follows_reload(reload_cfg, monkeypatch):
    """原先写成函数默认参数；显式传 0.0 必须被保留（不能用 `or` 兜底）。"""
    from kittymind.core import llm as llm_mod

    monkeypatch.setenv("LLM_MODEL_ID", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(llm_mod, "create_adapter", lambda *a, **k: object())

    reload_cfg({"LLM_TEMPERATURE": 0.15})
    assert llm_mod.BaseAgentLLM().temperature == pytest.approx(0.15)

    reload_cfg({"LLM_TEMPERATURE": 0.9})
    assert llm_mod.BaseAgentLLM().temperature == pytest.approx(0.9)

    assert llm_mod.BaseAgentLLM(temperature=0.0).temperature == 0.0
    assert llm_mod.BaseAgentLLM(temperature=0.42).temperature == pytest.approx(0.42)
