"""_Config 配置解析测试（P1-5 回归）。

旧实现是 `setattr(self, key, type(default)(value))`，两个静默失败：

  1. 默认值为 True 时 `bool("false")` → True（非空字符串恒为真）——
     settings.json 里写 "TOOL_GUARDRAIL_ENABLED": "false" 反而把守护栏**打开**；
  2. JSON 语法错误、顶层不是对象、键名写错、类型转换失败，全部无声——
     「明明配了却不生效」无从排查。

本文件把「按字面量正确解析」和「解析不了必须告警」两条都钉住。
"""

import json

import pytest

from kittymind import config as config_mod


@pytest.fixture
def make_cfg(tmp_path, monkeypatch, capsys):
    """用临时 settings.json 构造一个独立的 _Config 实例。

    capsys 由 fixture 一并申请，保证测试内 readouterr() 能拿到输出。
    """
    def _make(settings: dict | str) -> config_mod._Config:
        path = tmp_path / "settings.json"
        if isinstance(settings, str):
            path.write_text(settings, encoding="utf-8")
        else:
            path.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(config_mod, "_SETTINGS_FILE", path)
        return config_mod._Config()

    return _make


# ── 布尔：必须按字面量解析，不能靠 bool(value) ───────────────────

def test_string_false_actually_disables_flag(make_cfg):
    """核心回归：旧代码 bool('false') → True，开关被反向打开。"""
    cfg = make_cfg({"TOOL_GUARDRAIL_ENABLED": "false"})
    assert cfg.TOOL_GUARDRAIL_ENABLED is False


@pytest.mark.parametrize("literal", [False, "false", "FALSE", " false ", "0", "no", "off", 0])
def test_falsy_literals(make_cfg, literal):
    cfg = make_cfg({"TOOL_AUDIT_ENABLED": literal})
    assert cfg.TOOL_AUDIT_ENABLED is False, f"{literal!r} 应解析为 False"


@pytest.mark.parametrize("literal", [True, "true", "TRUE", "1", "yes", "on", 1])
def test_truthy_literals(make_cfg, literal):
    cfg = make_cfg({"TOOL_AUDIT_ENABLED": literal})
    assert cfg.TOOL_AUDIT_ENABLED is True, f"{literal!r} 应解析为 True"


def test_unparsable_bool_keeps_default_and_warns(make_cfg, capsys):
    cfg = make_cfg({"TOOL_GUARDRAIL_ENABLED": "maybe"})
    assert cfg.TOOL_GUARDRAIL_ENABLED is True
    err = capsys.readouterr().err
    assert "TOOL_GUARDRAIL_ENABLED" in err and "maybe" in err


# ── 数字 / 字符串 ────────────────────────────────────────────────

def test_int_from_string(make_cfg):
    cfg = make_cfg({"BASH_TIMEOUT": "60"})
    assert cfg.BASH_TIMEOUT == 60


def test_float_from_string(make_cfg):
    cfg = make_cfg({"COMPRESS_THRESHOLD_RATIO": "0.5"})
    assert pytest.approx(0.5) == cfg.COMPRESS_THRESHOLD_RATIO


def test_bad_int_keeps_default_and_warns(make_cfg, capsys):
    cfg = make_cfg({"BASH_TIMEOUT": "soon"})
    assert cfg.BASH_TIMEOUT == config_mod.BASH_TIMEOUT
    assert "BASH_TIMEOUT" in capsys.readouterr().err


def test_bool_not_silently_used_as_int(make_cfg, capsys):
    """true 当整数用属于配置错误，应告警并沿用默认值。"""
    cfg = make_cfg({"BASH_TIMEOUT": True})
    assert cfg.BASH_TIMEOUT == config_mod.BASH_TIMEOUT
    assert "BASH_TIMEOUT" in capsys.readouterr().err


# ── 失败必须可见，不再静默丢弃 ───────────────────────────────────

def test_malformed_json_warns_and_keeps_defaults(make_cfg, capsys):
    cfg = make_cfg("{ 这不是 JSON ")
    assert cfg.WS_PORT == config_mod.WS_PORT
    err = capsys.readouterr().err
    assert "JSON" in err, "JSON 语法错误必须告警"


def test_non_object_top_level_warns(make_cfg, capsys):
    cfg = make_cfg("[1, 2, 3]")
    assert cfg.WS_PORT == config_mod.WS_PORT
    assert "顶层必须是对象" in capsys.readouterr().err


def test_typo_key_warns(make_cfg, capsys):
    """键名打错曾是完全无声的——用户以为配上了。"""
    make_cfg({"TOOL_GUARDRIAL_ENABLED": "false"})
    assert "TOOL_GUARDRIAL_ENABLED" in capsys.readouterr().err


def test_non_overridable_key_warns(make_cfg, capsys):
    """Path 类目录配置不在 _OVERRIDABLE 里，写了必须告知被忽略。"""
    make_cfg({"SCREENSHOTS_DIR": "/tmp/x"})
    assert "SCREENSHOTS_DIR" in capsys.readouterr().err


def test_valid_settings_produce_no_warnings(make_cfg, capsys):
    cfg = make_cfg({
        "BASH_TIMEOUT": 15,
        "TOOL_GUARDRAIL_ENABLED": False,
        "COMPRESS_THRESHOLD_RATIO": 0.6,
    })
    assert cfg.BASH_TIMEOUT == 15
    assert cfg.TOOL_GUARDRAIL_ENABLED is False
    assert pytest.approx(0.6) == cfg.COMPRESS_THRESHOLD_RATIO
    assert capsys.readouterr().err == "", "合法配置不应产生任何告警"


def test_missing_settings_file_is_silent(tmp_path, monkeypatch, capsys):
    """没有 settings.json 是正常情况，不该刷告警。"""
    monkeypatch.setattr(config_mod, "_SETTINGS_FILE", tmp_path / "nope.json")
    config_mod._Config()
    assert capsys.readouterr().err == ""


def test_jsonc_comment_lines_still_supported(make_cfg):
    """既有的 JSONC 支持（// 注释行）不能被本次改动破坏。"""
    cfg = make_cfg(
        "{\n"
        "  // BASH_TIMEOUT: 999,\n"
        '  "BASH_TIMEOUT": 42\n'
        "}\n"
    )
    assert cfg.BASH_TIMEOUT == 42
