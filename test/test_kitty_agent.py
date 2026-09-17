"""KittyAgent 内部键剥离契约（回归）。

`_strip_internal` 在每次 LLM 调用前执行，唯一职责是拦住会话内部的记账字段
（`_seq` / `_compressed_summary`）泄漏到 API 请求里：泄漏轻则被服务端以未知字段
报错，重则把压缩摘要这类内部状态暴露出去。

此前该函数没有任何测试覆盖，而它的实现刚做过一次等价改写（原来的
`for m in messages: m = {...}` 会被 PLW2901 判为循环变量被覆盖）。
这里把契约固定下来：剥离干净、干净消息不复制、输入不被就地修改。
"""

from kittymind.agent.kitty_agent import _INTERNAL_KEYS, KittyAgent


def test_strip_internal_removes_every_internal_key():
    msgs = [{"role": "user", "content": "hi", "_seq": 1, "_compressed_summary": True}]

    out = KittyAgent._strip_internal(msgs)

    assert out == [{"role": "user", "content": "hi"}]
    assert not (set(out[0]) & set(_INTERNAL_KEYS))


def test_strip_internal_passes_clean_messages_through_unmodified():
    """不含内部键的消息按原对象透传，不做无谓的 dict 复制（每次调用都跑的热路径）。"""
    msg = {"role": "user", "content": "hi"}

    out = KittyAgent._strip_internal([msg])

    assert out[0] is msg


def test_strip_internal_does_not_mutate_input():
    """剥离必须靠重建字典实现，不能就地删键——入参是调用方持有的会话消息。"""
    msg = {"role": "user", "content": "hi", "_seq": 7}

    KittyAgent._strip_internal([msg])

    assert msg == {"role": "user", "content": "hi", "_seq": 7}


def test_strip_internal_mixed_batch_keeps_order_and_content():
    msgs = [
        {"role": "user", "content": "a", "_seq": 0},
        {"role": "assistant", "content": "b"},
        {"role": "tool", "content": "c", "_seq": 1, "_compressed_summary": True},
    ]

    out = KittyAgent._strip_internal(msgs)

    assert [m["content"] for m in out] == ["a", "b", "c"]
    assert out[0] is not msgs[0]  # 含内部键 → 必须重建
    assert out[1] is msgs[1]  # 干净 → 原样透传
    assert out[2] is not msgs[2]


def test_strip_internal_returns_new_list_even_when_nothing_changes():
    """返回值始终是新列表：调用方会往结果里继续 append，不能污染入参列表。"""
    msgs = [{"role": "user", "content": "hi"}]

    out = KittyAgent._strip_internal(msgs)
    out.append({"role": "assistant", "content": "extra"})

    assert len(msgs) == 1
