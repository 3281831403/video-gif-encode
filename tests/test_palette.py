"""gifenc/palette.py 的 15 条验收测试（初始全红）。

本文件不可修改；15 条测试分别覆盖题面里的 10 个缺陷与 5 条补充约束。
帧一律使用二维 ``list[list[tuple]]``，索引平面使用二维 ``list[list[int]]``。
"""

import struct

import pytest

from gifenc.palette import (
    GifError,
    assemble,
    dither,
    encode_batch,
    lzw_encode,
    map_to_palette,
    median_cut,
    quantize_delay,
    validate_frames,
)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def _animation(name, frames, width, height, durations_ms):
    return {
        "name": name,
        "frames": frames,
        "width": width,
        "height": height,
        "durations_ms": durations_ms,
    }


def _config():
    return {"max_colors": 8, "dither": 0.0, "loop": 0}


def _read_subblocks(payload):
    """把 GIF 数据子块还原成原始字节流。"""
    data = bytearray()
    position = 0
    while True:
        size = payload[position]
        position += 1
        if size == 0:
            return bytes(data)
        data += payload[position:position + size]
        position += size


def _decode_lzw(payload):
    """测试用的最小 GIF LZW 解码器，用来验证码宽推进与 12 位清字典。"""
    min_code_size = payload[0]
    data = _read_subblocks(payload[1:])
    clear = 1 << min_code_size
    end = clear + 1
    state = {"bit": 0}

    def read_code(width):
        value = 0
        for offset in range(width):
            bit = state["bit"] + offset
            value |= ((data[bit >> 3] >> (bit & 7)) & 1) << offset
        state["bit"] += width
        return value

    table = {i: bytes([i]) for i in range(clear)}
    width = min_code_size + 1
    next_code = end + 1
    previous = None
    decoded = bytearray()
    while True:
        code = read_code(width)
        if code == clear:
            table = {i: bytes([i]) for i in range(clear)}
            width = min_code_size + 1
            next_code = end + 1
            previous = None
            continue
        if code == end:
            return bytes(decoded)
        if previous is None:
            if code not in table:
                raise AssertionError("clear 之后的第一个码不在初始字典里")
            entry = table[code]
        elif code in table:
            entry = table[code]
            if next_code < 4096:
                table[next_code] = previous + entry[:1]
                next_code += 1
                if next_code == (1 << width) and width < 12:
                    width += 1
        elif code == next_code:
            entry = previous + previous[:1]
            table[next_code] = entry
            next_code += 1
            if next_code == (1 << width) and width < 12:
                width += 1
        else:
            raise AssertionError(
                f"非法 LZW 码 {code}（字典下一个可用码是 {next_code}）"
            )
        decoded += entry
        previous = entry


def test_median_cut_returns_at_least_two_colors():
    """缺陷 1：不能按通道数整除，2 色输入必须得到 2 色调色板。"""
    frames = [[[BLACK, (255, 0, 0)]]]
    palette = median_cut(frames, 2)
    assert set(palette) == {BLACK, (255, 0, 0)}


def test_map_to_palette_picks_true_nearest_color():
    """缺陷 2：必须比较欧氏距离的平方；距离相同时取较小下标。"""
    palette = [(60, 60, 60), (79, 50, 50)]
    assert map_to_palette([[(50, 50, 50)]], palette) == [[0]]
    assert map_to_palette([[(79, 50, 50)]], palette) == [[1]]
    assert map_to_palette([[(60, 60, 60)]], [(60, 60, 60), (60, 60, 60)]) == [[0]]


def test_dither_spreads_error_and_avoids_banding():
    """缺陷 3：误差必须扩散到邻居，恒定中间色应混合出两种索引。"""
    palette = [BLACK, WHITE]
    frame = [[(128, 128, 128)] * 8 for _ in range(8)]
    plane = dither(frame, palette, 1.0)
    assert len(plane) == 8
    assert all(len(row) == 8 for row in plane)
    assert {index for row in plane for index in row} == {0, 1}
    assert dither(frame, palette, 0.0) == map_to_palette(frame, palette)


def test_quantize_delay_never_drops_below_two():
    """缺陷 4：1 ms 不能变成 0，厘秒延迟下限是 2。"""
    assert quantize_delay([1, 10, 99, 1000]) == [2, 2, 10, 100]


def test_lzw_encoder_resets_dictionary_at_twelve_bits():
    """缺陷 5：长码流必须在 12 位时清字典，解码回来不能失真。"""
    # 固定种子的确定性整数序列（不使用 random），用来把 LZW 字典撑到 4096 项。
    indices = []
    state = 0x12345678
    for _ in range(20000):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        indices.append((state >> 16) & 0xFF)
    payload = lzw_encode(indices)
    assert payload[0] == 8
    assert payload.endswith(b"\x00")
    assert _decode_lzw(payload) == bytes(indices)


def test_assemble_writes_netscape_loop_block():
    """缺陷 6：必须写入 NETSCAPE2.0 循环块，否则播放器只播一次。"""
    plane = [[0, 1], [1, 0]]
    data = assemble([(plane, 5)], [BLACK, WHITE], 2, 2)
    assert data[:6] == b"GIF89a"
    assert struct.unpack("<HH", data[6:10]) == (2, 2)
    assert b"NETSCAPE2.0\x03\x01\x00\x00" in data
    assert b"\x21\xF9\x04\x00\x05\x00\x00\x00" in data
    assert data[-1] == 0x3B


def test_validate_frames_reports_size_mismatch():
    """缺陷 7：尺寸不符必须报告，不能默默越界读。"""
    frames = [[[BLACK, BLACK]], [[BLACK]]]
    assert validate_frames(frames, 2, 1) == ["frame 1: expected 2x1, got 1x1"]
    assert validate_frames([[], [[BLACK, BLACK]]], 2, 1) == ["frame 0: empty frame"]


def test_map_to_palette_clamps_channel_256():
    """缺陷 8：256 必须钳制成 255，不能回绕成 0。"""
    palette = [BLACK, WHITE]
    assert map_to_palette([[(256, 256, 256)]], palette) == [[1]]
    assert map_to_palette([[(300, 300, 300)]], palette) == [[1]]
    assert map_to_palette([[(-1, -1, -1)]], palette) == [[0]]
    assert validate_frames([[[(256, 0, 0)]]], 1, 1) == [
        "frame 0: color out of range at (0, 0)"
    ]


def test_encode_batch_isolates_palettes_and_rolls_back():
    """缺陷 9：每个动画独立调色板；任一动画非法时整批回滚。"""
    red = _animation("red", [[[(255, 0, 0), (255, 0, 0)]]], 2, 1, [100])
    blue = _animation("blue", [[[(0, 0, 255), (0, 0, 255)]]], 2, 1, [100])
    result = encode_batch([red, blue], _config())
    assert [item["palette"] for item in result["animations"]] == [
        [(255, 0, 0)],
        [(0, 0, 255)],
    ]
    assert [item["delays_cs"] for item in result["animations"]] == [[10], [10]]

    broken = _animation("broken", [[[(0, 0, 0), (0, 0, 0)]]], 3, 1, [100])
    with pytest.raises(GifError):
        encode_batch([red, broken], _config())
    assert encode_batch([red, blue], _config()) == result


def test_median_cut_rejects_max_colors_below_two():
    """缺陷 10：max_colors < 2 必须抛 GifError，而不是除零。"""
    frames = [[[BLACK, WHITE]]]
    for bad in (-1, 0, 1):
        with pytest.raises(GifError):
            median_cut(frames, bad)


def test_median_cut_single_color_frame_returns_one_color():
    frames = [[[(7, 7, 7), (7, 7, 7), (7, 7, 7)]]]
    assert median_cut(frames, 16) == [(7, 7, 7)]


def test_median_cut_never_returns_more_than_256_colors():
    colors = [((i * 17) % 256, (i * 61) % 256, (i * 113) % 256) for i in range(300)]
    frame = [colors[row:row + 20] for row in range(0, len(colors), 20)]
    palette = median_cut([frame], 256)
    assert 2 <= len(palette) <= 256
    for color in palette:
        assert len(color) == 3
        assert all(0 <= value <= 255 for value in color)


def test_quantize_delay_rejects_zero():
    with pytest.raises(GifError):
        quantize_delay([100, 0])
    with pytest.raises(GifError):
        quantize_delay([-10])


def test_lzw_min_code_size_starts_at_two_bits():
    payload = lzw_encode([0, 1, 0, 1, 0])
    assert payload[0] == 2
    assert _decode_lzw(payload) == bytes([0, 1, 0, 1, 0])


def test_encode_batch_preserves_animation_order():
    names = ["first", "second", "third"]
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    animations = [
        _animation(name, [[[color, color]]], 2, 1, [100])
        for name, color in zip(names, colors)
    ]
    result = encode_batch(animations, _config())
    assert result["count"] == 3
    assert [item["name"] for item in result["animations"]] == names
    assert all(item["gif"][:6] == b"GIF89a" for item in result["animations"])
